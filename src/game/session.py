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
from pydantic_ai.usage import UsageLimits

from src.achievements.runtime import render_catalogue
from src.game.genres import get_genre
from src.game.memory import build_instructions, build_turn_context, build_user_prompt
from src.llm.dm_agent import GameDeps, dm_agent
from src.llm.models import build_model
from src.log import get_logger
from src.storage.repo import Campaign, Character, Repo

log = get_logger(__name__)

#: A turn may call tools several times (roll, damage, loot, record) before answering.
#: This is a runaway guard, not a target.
TURN_LIMITS = UsageLimits(request_limit=12, tool_calls_limit=20)


#: How long we'll wait on the model before giving up. Without this a hung provider
#: connection would hold the campaign lock forever.
TURN_TIMEOUT_SECONDS = 180


@dataclass
class TurnResult:
    """What the bot should do with a completed turn."""

    reply: str
    cues: list[str] = field(default_factory=list)


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

    def __init__(self, repo: Repo, model_spec: str, model: Model | None = None) -> None:
        """Build the service. `model` overrides the spec -- tests pass a fake here."""
        self.repo = repo
        self.model_spec = model_spec
        self._model = model if model is not None else build_model(model_spec)
        #: One lock per campaign, so a chat's turns resolve in order while different
        #: chats still run concurrently.
        self._locks: dict[int, asyncio.Lock] = {}

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
            genre = get_genre(campaign.genre)

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

            context = await build_turn_context(self.repo, current)
            instructions = "\n\n".join(
                [
                    build_instructions(current, genre),
                    "## Achievement catalogue\n\n"
                    "Award these with the award_achievement tool, by id, and sparingly.\n\n"
                    + render_catalogue(),
                    "## Campaign state\n\n" + context,
                ]
            )

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
                        build_user_prompt(actor, action),
                        model=self._model,
                        deps=deps,
                        instructions=instructions,
                        usage_limits=TURN_LIMITS,
                    ),
                    timeout=TURN_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                # Tools commit as they run, so anything they already did has stuck.
                raise TurnFailed(exc, partial=deps.mutations > 0) from exc
            elapsed = time.monotonic() - started

            reply = (result.output or "").strip()
            usage = result.usage
            log.info(
                "turn campaign=%s model=%s %.1fs in=%s out=%s requests=%s",
                campaign.id,
                self.model_spec,
                elapsed,
                usage.input_tokens,
                usage.output_tokens,
                usage.requests,
            )

            if reply:
                await self.repo.add_message(campaign.id, "dm", reply)

            return TurnResult(reply=reply, cues=list(dict.fromkeys(deps.cues)))
