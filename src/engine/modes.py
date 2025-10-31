"""Mode router for Dungeon Master Keith."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence, Tuple

from ..agents.dmk_agent import (
    AgentError,
    AgentHistoryMessage,
    AgentNotConfiguredError,
    DMKAgent,
)
from ..config import AllowedMode, Settings, get_settings
from ..engine.achievements import (
    Achievement,
    AchievementEvent,
    AwardContext,
    award_achievement,
    load_registry,
)
from ..engine.storage import SQLiteStore
from .character import profile_ready
from .story import StoryEngine
from ..utils.formatting import format_achievement_block


@dataclass(frozen=True)
class ModeRequest:
    user_id: str
    session_id: str
    message: str
    mode: AllowedMode
    triggers: Tuple[str, ...] = ("event.message",)
    metadata: dict[str, Any] = field(default_factory=dict)
    history: Sequence[AgentHistoryMessage] = ()
    attachments: Sequence[str] = ()
    display_name: Optional[str] = None


@dataclass(frozen=True)
class ModeResponse:
    text: str
    achievement_id: str
    mode: AllowedMode
    was_new: bool
    trigger: str


class ModeRouter:
    """Coordinate achievements, persona modes, and model calls."""

    def __init__(
        self,
        store: Optional[SQLiteStore] = None,
        agent: Optional[DMKAgent] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store or SQLiteStore()
        self.agent = agent or DMKAgent(self.settings)
        self._registry = load_registry()
        self._registry_index = {achievement.id: achievement for achievement in self._registry}
        self.story_engine = StoryEngine(self.store)

    def handle(self, request: ModeRequest) -> ModeResponse:
        if not request.triggers:
            triggers = ("event.message",)
        else:
            triggers = request.triggers

        attachments = list(request.attachments)
        metadata = dict(request.metadata)
        agent_message = request.message

        self.store.ensure_user(request.user_id, request.display_name)
        overrides = request.metadata.get("session_overrides", {})
        session_state = self.store.upsert_session(
            request.session_id,
            request.user_id,
            mode=overrides.get("mode", request.mode),
            profanity_level=overrides.get("profanity_level"),
            rating=overrides.get("rating"),
            tangents_level=overrides.get("tangents_level"),
            achievement_density=overrides.get("achievement_density"),
            story_mode_enabled=overrides.get("story_mode_enabled"),
        )

        if session_state.mode == "story":
            profile = self.store.get_story_profile(request.session_id)
            if not profile or not profile_ready(profile) or not session_state.story_mode_enabled:
                return self._story_setup_response(request, session_state.mode)
            story_turn = self.story_engine.process_turn(
                request.session_id,
                request.user_id,
                profile,
                request.message,
            )
            attachments.extend(story_turn.attachments)
            agent_message = story_turn.agent_message
            combined_triggers = list(triggers)
            for trig in story_turn.triggers:
                if trig not in combined_triggers:
                    combined_triggers.append(trig)
            triggers = tuple(combined_triggers)
            metadata.setdefault("story", {}).update(story_turn.metadata)

        achievement_event = AchievementEvent.from_trigger(
            user_id=request.user_id,
            session_id=request.session_id,
            trigger=triggers[0],
            extra_triggers=triggers[1:],
            payload={
                "metadata": metadata,
                "text": request.message,
                "agent_message": agent_message,
            },
        )
        award = award_achievement(achievement_event, AwardContext(store=self.store))

        was_new = True
        trigger_used = triggers[0]
        if award:
            achievement = award.achievement
            trigger_used = award.trigger
        else:
            was_new = False
            achievement = self._fallback_achievement(request.user_id)

        block = format_achievement_block(achievement)
        toggle_snapshot = {
            "profanity_level": session_state.profanity_level,
            "rating": session_state.rating,
            "tangents_level": session_state.tangents_level,
            "achievement_density": session_state.achievement_density,
        }

        body = self._generate_body(
            request=request,
            mode=session_state.mode,
            achievement=achievement,
            toggle_snapshot=toggle_snapshot,
            message=agent_message,
            attachments=tuple(attachments),
        )
        full_text = f"{block}\n\n{body}"
        return ModeResponse(
            text=full_text.strip(),
            achievement_id=achievement.id,
            mode=session_state.mode,  # reflects persisted state
            was_new=was_new,
            trigger=trigger_used,
        )

    def _generate_body(
        self,
        *,
        request: ModeRequest,
        mode: AllowedMode,
        achievement: Achievement,
        toggle_snapshot: dict[str, Any],
        message: str,
        attachments: Sequence[str],
    ) -> str:
        try:
            return self.agent.generate_reply(
                user_message=message,
                mode=mode,
                achievement=achievement,
                toggle_snapshot=toggle_snapshot,
                history=request.history,
                attachments=attachments,
            )
        except AgentNotConfiguredError:
            return self._offline_body(request, achievement, toggle_snapshot, message)
        except AgentError:
            return self._offline_body(
                request,
                achievement,
                toggle_snapshot,
                message=message,
                error_mode=True,
            )

    def _offline_body(
        self,
        request: ModeRequest,
        achievement: Achievement,
        toggle_snapshot: dict[str, Any],
        message: str,
        *,
        error_mode: bool = False,
    ) -> str:
        """Fallback when the OpenAI client is unavailable."""
        sanitized = message.strip()
        toggle_line = ", ".join(
            f"{key}={value}" for key, value in toggle_snapshot.items()
        )
        if error_mode:
            paragraph_one = (
                "Keith clears his throat, blames a gremlin for temporarily severing the "
                "oracular uplink, and vows to improvise anyway."
            )
        else:
            paragraph_one = (
                "Keith flexes his narrator cape and assures you the immersion field is "
                "still operational even without the grand oracle."
            )
        paragraph_two = (
            f"\"{sanitized}\" echoes off the dungeon walls while the control levers "
            f"flash ({toggle_line}). Keith scribbles a vow to revisit this moment "
            "once the mimicry crystal—also known as the API—behaves."
        )
        return f"{paragraph_one}\n\n{paragraph_two}"

    def _fallback_achievement(self, user_id: str) -> Achievement:
        """Return the most recent achievement for continuity, or a default."""
        latest = self.store.fetch_most_recent_for_user(user_id)
        if latest:
            achievement = self._registry_index.get(latest.achievement_id)
            if achievement:
                return achievement
        # Fallback to the first registry entry for deterministic behavior.
        return self._registry[0]

    def _story_setup_response(self, request: ModeRequest, mode: AllowedMode) -> ModeResponse:
        achievement = self._registry_index.get("session-zero-hero", self._registry[0])
        block = format_achievement_block(achievement)
        body = (
            "Keith taps the storybook closed. Before we dive into the saga, "
            "run `/character` to finish crafting your persona and then `/character finalize`."
            " Once you're ready, set `/mode story` and we'll start the orientation crawl."
        )
        text = f"{block}\n\n{body}"
        return ModeResponse(
            text=text,
            achievement_id=achievement.id,
            mode=mode,
            was_new=False,
            trigger="event.story.setup",
        )


__all__ = ["ModeRouter", "ModeRequest", "ModeResponse"]
