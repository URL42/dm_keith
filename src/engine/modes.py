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
from ..engine.storage import SQLiteStore, SessionState
from .character import profile_ready
from .story import StoryEngine, StoryTurnResult
from ..utils.formatting import format_achievement_block


AUTO_CHECK_TAGS = {
    "chaos": ("cha", 12),
    "risk": ("dex", 12),
    "puzzle": ("int", 13),
    "stealth": ("dex", 13),
    "social": ("cha", 12),
    "ally": ("cha", 11),
    "exploration": ("wis", 11),
    "combat": ("str", 14),
    "magic": ("int", 14),
    "inventory": ("wis", 10),
    "shortcut": ("dex", 12),
}


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
    achievement_id: Optional[str]
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
        story_context: Optional[str] = None
        story_turn = None

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
            profile = self.store.get_story_profile(request.session_id) or profile
            story_context = self._story_context_text(profile, story_turn)
            if story_context:
                attachments.append(story_context)
            attachments.extend(story_turn.attachments)
            agent_message = story_turn.agent_message
            combined_triggers = list(triggers)
            for trig in story_turn.triggers:
                if trig not in combined_triggers:
                    combined_triggers.append(trig)
            triggers = tuple(combined_triggers)
            metadata.setdefault("story", {}).update(story_turn.metadata)

        should_award = self._should_award(session_state, request, story_turn)

        achievement: Optional[Achievement]
        trigger_used = triggers[0]
        was_new = False
        if should_award:
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
            if award:
                achievement = award.achievement
                trigger_used = award.trigger
                was_new = True
            else:
                achievement = None
        else:
            achievement = None

        block = format_achievement_block(achievement) if achievement else ""
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
            agent_payload=agent_message,
            attachments=tuple(attachments),
            fallback_context=story_context,
        )
        full_text = f"{block}\n\n{body}" if block else body
        return ModeResponse(
            text=full_text.strip(),
            achievement_id=achievement.id if achievement else None,
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
        agent_payload: str,
        attachments: Sequence[str],
        fallback_context: Optional[str],
    ) -> str:
        try:
            return self.agent.generate_reply(
                user_message=agent_payload,
                mode=mode,
                achievement=achievement,
                toggle_snapshot=toggle_snapshot,
                history=request.history,
                attachments=attachments,
            )
        except AgentNotConfiguredError:
            return self._offline_body(
                request,
                achievement,
                toggle_snapshot,
                message=request.message,
                context=fallback_context,
            )
        except AgentError:
            return self._offline_body(
                request,
                achievement,
                toggle_snapshot,
                message=request.message,
                context=fallback_context,
                error_mode=True,
            )

    def _offline_body(
        self,
        request: ModeRequest,
        achievement: Achievement,
        toggle_snapshot: dict[str, Any],
        message: str,
        context: Optional[str] = None,
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
                "Keith whacks the oracle crystal—silence. The uplink is down, so you're getting handcrafted narration instead."
            )
        else:
            paragraph_one = (
                "Keith flexes his narrator cape and assures you the immersion field is still operational even without the grand oracle."
            )
        if context:
            paragraph_two = (
                "He recaps the moment manually so future historians don't miss a beat:\n"
                f"{context}"
            )
        else:
            paragraph_two = "He at least jots down your words for later: " + (sanitized or "[no input]")
        paragraph_three = (
            "Consider trying the command again after a short rest. "
            f"(toggles: {toggle_line})"
        )
        return f"{paragraph_one}\n\n{paragraph_two}\n\n{paragraph_three}"

    def _fallback_achievement(self, user_id: str) -> Achievement:
        """Return the most recent achievement for continuity, or a default."""
        latest = self.store.fetch_most_recent_for_user(user_id)
        if latest:
            achievement = self._registry_index.get(latest.achievement_id)
            if achievement:
                return achievement
        # Fallback to the first registry entry for deterministic behavior.
        return self._registry[0]

    def _story_context_text(self, profile, story_turn) -> Optional[str]:
        if not story_turn or not profile:
            return None
        order = ["str", "dex", "con", "int", "wis", "cha"]
        ability_summary = ", ".join(
            f"{ability.upper()} {profile.ability_scores.get(ability, 10)}"
            for ability in order
        )
        lines = [
            f"Character: {profile.character_name or 'Unnamed'} (Level {profile.level}, XP {profile.experience})",
            f"Race/Class: {profile.race or 'Unknown'} / {profile.character_class or 'Untrained'}",
            f"Abilities: {ability_summary}",
            f"Current scene: {story_turn.scene.id} — {story_turn.scene.title}",
        ]
        if story_turn.selected_choice:
            choice = story_turn.selected_choice
            lines.append(f"Selected choice: {choice.id} ({choice.label})")
        if story_turn.check_outcome:
            outcome = story_turn.check_outcome
            status = "success" if outcome.success else "failure"
            manual = " (manual)" if outcome.manual else ""
            auto = " (auto)" if story_turn.auto_generated_check and not outcome.manual else ""
            note = story_turn.metadata.get("check", {}).get("note") if getattr(story_turn, "metadata", None) else None
            base_line = (
                f"Check: {outcome.ability.upper()}{manual}{auto} {status} — rolls {list(outcome.kept)} total {outcome.total} vs DC {outcome.difficulty_class}"
            )
            if note:
                base_line += f" ({note})"
            lines.append(base_line)
        if story_turn.scene.choices:
            lines.append("Choices:")
            for idx, option in enumerate(story_turn.scene.choices, start=1):
                lines.append(f"  {idx}. {option.label} (id={option.id})")
        return "\n".join(lines)

    def _should_award(
        self,
        session_state: SessionState,
        request: ModeRequest,
        story_turn: Optional[StoryTurnResult],
    ) -> bool:
        message_text = request.message.strip()
        interesting = bool(message_text)

        if session_state.mode == "story":
            interesting = bool(
                story_turn
                and (
                    story_turn.selected_choice is not None
                    or story_turn.check_outcome is not None
                    or (story_turn.metadata.get("xp_awarded") or 0) > 0
                )
            )

        if not interesting:
            return False

        density = session_state.achievement_density or "normal"
        probabilities = {"low": 0.25, "normal": 0.55, "high": 0.85}
        probability = probabilities.get(density, 0.55)
        seed = hash(
            (
                request.user_id,
                request.session_id,
                session_state.mode,
                message_text,
            )
        ) & 0xFFFF
        roll = seed / 0xFFFF
        return roll < probability

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
