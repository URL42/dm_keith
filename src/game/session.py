"""Running one turn of a campaign.

The bot layer hands us a player's action; we assemble context, run the DM agent,
persist both sides of the exchange, and hand back what to say. Everything that talks
to the model goes through here.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from src.game.genres import genre_for
from src.game.memory import build_instructions, build_turn_context, build_user_prompt
from src.llm.dm_agent import GameDeps, dm_agent
from src.llm.models import build_model
from src.log import get_logger
from src.storage.repo import Campaign, Character, PendingRoll, Repo

log = get_logger(__name__)

#: A turn may call tools several times (roll, damage, loot, record) before answering.
#: This is a runaway guard, not a target.
TURN_LIMITS = UsageLimits(request_limit=12, tool_calls_limit=20)


#: How long we'll wait on the model before giving up. Without this a hung provider
#: connection would hold the campaign lock forever.
TURN_TIMEOUT_SECONDS = 180


#: A ceiling on one reply. The prompt asks for under 200 words; in real play a
#: turn ran to 1,200, narrating three scenes and taking the player's decisions for
#: them. This is a backstop against that, not the target -- it leaves room for a
#: long-but-reasonable turn while making a runaway monologue impossible.
MAX_REPLY_TOKENS = 900


def model_settings_for(model_spec: str, effort: str) -> ModelSettings:
    """Per-turn model settings.

    `max_tokens` applies everywhere. The `anthropic_*` keys don't: sending them to
    OpenAI, DeepSeek or Ollama would at best be ignored and at worst rejected, so
    the provider prefix gates them.

    Caching matters more here than it looks: a turn makes 2-4 model calls and each
    one resends the whole prompt, so most of a turn's input tokens are repeats of
    the previous call within the same turn.
    """
    if not model_spec.startswith("anthropic:"):
        return ModelSettings(max_tokens=MAX_REPLY_TOKENS)
    return AnthropicModelSettings(
        max_tokens=MAX_REPLY_TOKENS,
        anthropic_cache_instructions=True,
        anthropic_cache_tool_definitions=True,
        anthropic_cache_messages=True,
        anthropic_effort=effort,  # type: ignore[typeddict-item]
    )


@dataclass
class TurnResult:
    """What the bot should do with a completed turn."""

    reply: str
    cues: list[str] = field(default_factory=list)
    #: Set when Keith handed a roll to a player; the bot posts a button for it.
    pending_roll: PendingRoll | None = None


class TurnFailed(RuntimeError):
    """A turn didn't complete.

    `partial` is True when tools had already run and committed before the failure,
    so the player needs to be told the world may have moved anyway.
    """

    def __init__(self, cause: BaseException, *, partial: bool) -> None:
        super().__init__(str(cause))
        self.cause_name = type(cause).__name__
        self.partial = partial


class CampaignEnded(TurnFailed):
    """The campaign was retired while this turn was queued."""


class GameService:
    """Orchestrates turns. One instance per process."""

    def __init__(
        self,
        repo: Repo,
        model_spec: str,
        model: Model | None = None,
        effort: str = "medium",
    ) -> None:
        """Build the service. `model` overrides the spec -- tests pass a fake here."""
        self.repo = repo
        self.model_spec = model_spec
        self._model = model if model is not None else build_model(model_spec)
        self._settings = model_settings_for(model_spec, effort)
        #: One lock per campaign, so a chat's turns resolve in order while different
        #: chats still run concurrently.
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def model(self) -> Model:
        """The configured model, for one-off calls outside the turn loop."""
        return self._model

    @property
    def settings(self) -> ModelSettings:
        return self._settings

    def lock_for(self, campaign_id: int) -> asyncio.Lock:
        return self._locks.setdefault(campaign_id, asyncio.Lock())

    async def take_turn(
        self,
        campaign: Campaign,
        action: str,
        actor: Character | None = None,
        *,
        persist_action: bool = True,
    ) -> TurnResult:
        """Run one exchange: the player acts, Keith responds, both are recorded."""
        async with self.lock_for(campaign.id):
            genre = genre_for(campaign)

            # Re-read the campaign: this turn may have been queued behind another
            # one, or behind /endgame.
            current = await self.repo.get_campaign(campaign.id) or campaign
            if current.status == "ended":
                raise CampaignEnded(RuntimeError("campaign retired"), partial=False)

            if persist_action:
                await self.repo.add_message(
                    campaign.id,
                    "player" if actor else "system",
                    action,
                    character_id=actor.id if actor else None,
                )

            # Instructions stay byte-identical across turns so they can be cached;
            # everything that changes rides in the user prompt.
            instructions = build_instructions(current, genre)
            context = "## Campaign state\n\n" + await build_turn_context(self.repo, current)

            deps = GameDeps(
                repo=self.repo,
                campaign=current,
                genre=genre,
                actor=actor,
                rng=random.Random(),
            )

            started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    dm_agent.run(
                        build_user_prompt(actor, action, context),
                        model=self._model,
                        deps=deps,
                        instructions=instructions,
                        usage_limits=TURN_LIMITS,
                        model_settings=self._settings,
                    ),
                    timeout=TURN_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                # Tools commit as they run, so anything they already did has stuck.
                # A requested roll is the one thing we can't leave behind: its button
                # is posted by the bot layer from the result we're about to not
                # return, so the row would sit there with nothing ever offering it.
                await self._drop_pending(deps)
                raise TurnFailed(exc, partial=deps.mutations > 0) from exc
            elapsed = time.monotonic() - started

            reply = (result.output or "").strip()
            usage = result.usage
            log.info(
                "turn campaign=%s model=%s %.1fs in=%s out=%s cache_read=%s cache_write=%s "
                "requests=%s",
                campaign.id,
                self.model_spec,
                elapsed,
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                usage.requests,
            )

            if reply:
                await self.repo.add_message(campaign.id, "dm", reply)
            else:
                # An empty turn is surfaced to the player as "Keith says nothing" and
                # the button never gets posted, so a roll asked for here is orphaned
                # just as surely as one lost to an exception.
                await self._drop_pending(deps)

            return TurnResult(
                reply=reply,
                cues=list(dict.fromkeys(deps.cues)),
                pending_roll=deps.pending_roll,
            )

    async def _drop_pending(self, deps: GameDeps) -> None:
        """Undo a roll request whose button will never reach the chat.

        Best-effort: this runs on the failure path, and a cleanup that raised would
        replace the real error with a less useful one.
        """
        if deps.pending_roll is None:
            return
        try:
            await self.repo.delete_pending_roll(deps.pending_roll.id)
            # Undone, so it no longer counts as the world having moved. Without this
            # a turn whose only tool call was request_roll tells the player "some of
            # it may already have happened" when nothing did.
            deps.mutations -= 1
        except Exception:
            log.warning("couldn't clear the orphaned pending roll", exc_info=True)
        deps.pending_roll = None
