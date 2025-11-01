"""Story engine runtime for DMK."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..storage import SQLiteStore, StoryProfile, StoryState
from ..character import ABILITY_KEYS, ability_modifier, level_from_xp

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
class StoryChoice:
    id: str
    label: str
    next_scene: str
    achievement_id: Optional[str] = None
    xp_reward: int = 0
    tags: Sequence[str] = field(default_factory=tuple)
    check: Optional["StoryCheck"] = None


@dataclass(frozen=True)
class StoryCheck:
    ability: str
    difficulty_class: int
    success_scene: Optional[str] = None
    failure_scene: Optional[str] = None
    success_xp: int = 0
    failure_xp: int = 0
    note: Optional[str] = None


@dataclass(frozen=True)
class StoryCheckOutcome:
    ability: str
    rolls: Sequence[int]
    kept: Sequence[int]
    modifier: int
    total: int
    difficulty_class: int
    success: bool
    manual: bool = False


@dataclass(frozen=True)
class StoryScene:
    id: str
    title: str
    narration: Sequence[str]
    choices: Sequence[StoryChoice]
    tags: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class StoryTurnResult:
    scene: StoryScene
    selected_choice: Optional[StoryChoice]
    agent_message: str
    attachments: Sequence[str]
    triggers: Sequence[str]
    metadata: dict
    check_outcome: Optional[StoryCheckOutcome] = None
    auto_generated_check: bool = False


class StoryEngine:
    """Simple scene graph story engine."""

    def __init__(self, store: SQLiteStore, campaign_path: Optional[Path] = None) -> None:
        self.store = store
        root = Path(__file__).resolve().parents[3] / "assets" / "story"
        self.campaign_path = campaign_path or (root / "campaign_intro.json")
        self._campaign = self._load_campaign(self.campaign_path)
        self.root_scene = self._campaign.get("root_scene")
        self.scenes = self._index_scenes(self._campaign["scenes"])

    def _load_campaign(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _index_scenes(self, raw_scenes: Sequence[dict]) -> dict[str, StoryScene]:
        indexed: dict[str, StoryScene] = {}
        for entry in raw_scenes:
            choices = []
            for choice_data in entry.get("choices", []):
                check = None
                if "check" in choice_data:
                    raw_check = choice_data["check"]
                    check = StoryCheck(
                        ability=raw_check["ability"],
                        difficulty_class=int(raw_check.get("dc", 10)),
                        success_scene=raw_check.get("success_scene"),
                        failure_scene=raw_check.get("failure_scene"),
                        success_xp=int(raw_check.get("success_xp", 0)),
                        failure_xp=int(raw_check.get("failure_xp", 0)),
                        note=raw_check.get("note"),
                    )
                choices.append(
                    StoryChoice(
                        id=choice_data["id"],
                        label=choice_data["label"],
                        next_scene=choice_data["next_scene"],
                        achievement_id=choice_data.get("achievement_id"),
                        xp_reward=int(choice_data.get("xp_reward", 0)),
                        tags=tuple(choice_data.get("tags", [])),
                        check=check,
                    )
                )
            scene = StoryScene(
                id=entry["id"],
                title=entry.get("title", entry["id"].title()),
                narration=tuple(entry.get("narration", [])),
                choices=tuple(choices),
                tags=tuple(entry.get("tags", [])),
            )
            indexed[scene.id] = scene
        return indexed

    def ensure_state(self, session_id: str, profile: StoryProfile) -> StoryState:
        state = self.store.get_story_state(session_id)
        if state is None:
            state = self.store.upsert_story_state(
                session_id,
                current_scene=self.root_scene,
                scene_history=[self.root_scene],
                flags={},
                stats={"xp": profile.experience, "level": profile.level},
            )
        elif state.current_scene is None:
            state = self.store.upsert_story_state(
                session_id,
                current_scene=self.root_scene,
                scene_history=list(state.scene_history) + [self.root_scene],
            )
        return state

    def current_scene(self, session_id: str, profile: StoryProfile) -> StoryScene:
        state = self.ensure_state(session_id, profile)
        scene_id = state.current_scene or self.root_scene
        return self.scenes.get(scene_id, self.scenes[self.root_scene])

    def process_turn(
        self,
        session_id: str,
        user_id: str,
        profile: StoryProfile,
        raw_input: str,
    ) -> Optional[StoryTurnResult]:
        state = self.ensure_state(session_id, profile)
        scene = self.scenes.get(state.current_scene or self.root_scene)
        if scene is None:
            return None

        choice = self._match_choice(raw_input, scene.choices)
        triggers = ["event.story.turn", "event.message"]
        attachments = []
        metadata: dict = {
            "scene_id": scene.id,
            "scene_title": scene.title,
        }
        check_outcome: Optional[StoryCheckOutcome] = None
        level_up: Optional[dict] = None

        auto_generated = False
        if choice:
            auto_generated = False
            triggers.insert(0, "event.story.choice")
            metadata["choice_id"] = choice.id
            metadata["choice_label"] = choice.label
            next_scene, check_outcome, level_up, xp_awarded, auto_generated, active_check = self._apply_choice(
                session_id, profile, state, choice
            )
            attachments.append(self._format_choice_log(choice))
            scene = next_scene
            state = self.store.get_story_state(session_id) or state
            if xp_awarded:
                metadata["xp_awarded"] = xp_awarded
            if level_up:
                triggers.insert(1, "event.story.level_up")
                metadata["level_up"] = level_up
            if check_outcome:
                metadata["check"] = {
                    "ability": check_outcome.ability,
                    "rolls": list(check_outcome.rolls),
                    "kept": list(check_outcome.kept),
                    "modifier": check_outcome.modifier,
                    "total": check_outcome.total,
                    "dc": check_outcome.difficulty_class,
                    "success": check_outcome.success,
                    "manual": check_outcome.manual,
                    "auto": auto_generated,
                    "note": getattr(active_check, "note", None),
                }
                attachments.append(self._format_check_attachment(check_outcome))

        attachments.append(self._format_scene_attachment(scene))
        agent_message = self._compose_agent_message(profile, scene, choice, raw_input)
        return StoryTurnResult(
            scene=scene,
            selected_choice=choice,
            agent_message=agent_message,
            attachments=attachments,
            triggers=tuple(dict.fromkeys(triggers)),  # preserve order, dedupe
            metadata=metadata,
            check_outcome=check_outcome,
            auto_generated_check=auto_generated,
        )

    def _apply_choice(
        self,
        session_id: str,
        profile: StoryProfile,
        state: StoryState,
        choice: StoryChoice,
    ) -> tuple[StoryScene, Optional[StoryCheckOutcome], Optional[dict], int, bool, Optional[StoryCheck]]:
        check_outcome: Optional[StoryCheckOutcome] = None
        level_up: Optional[dict] = None
        target_scene_id = choice.next_scene
        xp_award = choice.xp_reward

        flags = dict(state.flags)

        active_check = choice.check
        auto_generated = False
        if active_check is None:
            inferred = self._infer_auto_check(choice)
            if inferred:
                active_check = inferred
                auto_generated = True

        if active_check:
            check_outcome, flags = self._perform_check(
                session_id, profile, active_check, flags
            )
            if check_outcome.success:
                target_scene_id = active_check.success_scene or target_scene_id
                xp_award = active_check.success_xp or xp_award
            else:
                if active_check.failure_scene:
                    target_scene_id = active_check.failure_scene
                failure_xp = active_check.failure_xp
                if failure_xp is not None:
                    xp_award = failure_xp
                elif auto_generated:
                    xp_award = max(0, xp_award // 2)

        history = list(state.scene_history)
        history.append(target_scene_id)
        stats = dict(state.stats)

        new_xp = profile.experience
        new_level = profile.level
        if xp_award:
            new_xp = profile.experience + xp_award
            new_level = level_from_xp(new_xp)
            self.store.upsert_story_profile(
                session_id,
                profile.user_id,
                experience=new_xp,
                level=new_level,
            )
            stats["xp"] = new_xp
            stats["level"] = new_level
            if new_level > profile.level:
                level_up = {"from": profile.level, "to": new_level}

        self.store.upsert_story_state(
            session_id,
            current_scene=target_scene_id,
            scene_history=history[-50:],
            stats=stats,
            flags=flags,
        )
        next_scene = self.scenes.get(target_scene_id, self.scenes[self.root_scene])
        return next_scene, check_outcome, level_up, xp_award, auto_generated, active_check

    def _match_choice(
        self, user_input: str, choices: Sequence[StoryChoice]
    ) -> Optional[StoryChoice]:
        if not choices:
            return None
        text = user_input.strip().lower()
        if not text:
            return None
        if text.startswith("/choose"):
            parts = text.split()
            if len(parts) >= 2:
                text = parts[1]
        if text.isdigit():
            index = int(text) - 1
            if 0 <= index < len(choices):
                return choices[index]
        for choice in choices:
            if text == choice.id.lower():
                return choice
            if text in choice.label.lower():
                return choice
        return None

    def _infer_auto_check(self, choice: StoryChoice) -> Optional[StoryCheck]:
        for tag in choice.tags:
            if tag in AUTO_CHECK_TAGS:
                ability, dc = AUTO_CHECK_TAGS[tag]
                return StoryCheck(ability=ability, difficulty_class=dc, note=f"auto:{tag}")
        return None

    def _perform_check(
        self,
        session_id: str,
        profile: StoryProfile,
        check: StoryCheck,
        flags: dict[str, Any],
    ) -> tuple[StoryCheckOutcome, dict[str, Any]]:
        ability = check.ability.lower()
        score = profile.ability_scores.get(ability, 10)
        modifier = ability_modifier(score)

        flags = dict(flags)
        pending = flags.get("pending_roll")
        if pending and pending.get("ability") == ability:
            rolls = list(pending.get("rolls", [])) or [pending.get("total", 0) - modifier]
            kept = list(pending.get("kept", rolls))
            total = pending.get("total")
            if total is None:
                total = sum(kept) + modifier
            success = total >= check.difficulty_class
            outcome = StoryCheckOutcome(
                ability=ability,
                rolls=rolls,
                kept=kept,
                modifier=modifier,
                total=total,
                difficulty_class=check.difficulty_class,
                success=success,
                manual=True,
            )
            flags.pop("pending_roll", None)
            return outcome, flags

        roll_value = random.randint(1, 20)
        total = roll_value + modifier
        success = total >= check.difficulty_class
        rolls = [roll_value]
        kept = [roll_value]
        outcome = StoryCheckOutcome(
            ability=ability,
            rolls=rolls,
            kept=kept,
            modifier=modifier,
            total=total,
            difficulty_class=check.difficulty_class,
            success=success,
            manual=False,
        )
        self.store.log_story_roll(
            session_id=session_id,
            user_id=profile.user_id,
            expression=f"1d20+{modifier}",
            result_total=total,
            result_detail={
                "rolls": rolls,
                "kept": kept,
                "modifier": modifier,
                "ability": ability,
                "dc": check.difficulty_class,
                "success": success,
            },
        )
        return outcome, flags

    def _compose_agent_message(
        self,
        profile: StoryProfile,
        scene: StoryScene,
        choice: Optional[StoryChoice],
        raw_input: str,
    ) -> str:
        lines = [
            f"Character: {profile.character_name or 'Unnamed'} (Level {profile.level}, XP {profile.experience})",
            f"Race/Class: {profile.race or 'Unknown'} / {profile.character_class or 'Untrained'}",
            "Abilities: "
            + ", ".join(
                f"{ability.upper()} {profile.ability_scores.get(ability, 10)}"
                for ability in ABILITY_KEYS
            ),
            f"Current scene: {scene.id} — {scene.title}",
        ]
        if choice:
            lines.append(f"Player selected choice: {choice.id} ({choice.label})")
        lines.append(f"Player input: {raw_input.strip() or '[silence]'}")
        lines.append("Choices:")
        for idx, option in enumerate(scene.choices, start=1):
            lines.append(f"  {idx}. {option.label} (id={option.id})")
        return "\n".join(lines)

    def _format_scene_attachment(self, scene: StoryScene) -> str:
        narration = " ".join(scene.narration)
        choice_lines = [f"{idx}. {choice.label}" for idx, choice in enumerate(scene.choices, start=1)]
        if choice_lines:
            choice_text = "Choices: " + " | ".join(choice_lines)
        else:
            choice_text = "No explicit choices; free-form response allowed."
        return f"Scene[{scene.id}]: {scene.title}\n{narration}\n{choice_text}"

    def _format_choice_log(self, choice: StoryChoice) -> str:
        return f"Choice taken -> {choice.id}: {choice.label}"

    def _format_check_attachment(self, outcome: StoryCheckOutcome) -> str:
        status = "SUCCESS" if outcome.success else "FAIL"
        rolls = ", ".join(str(r) for r in outcome.rolls)
        kept = ", ".join(str(k) for k in outcome.kept)
        kept_segment = f" kept {kept}" if kept and kept != rolls else ""
        manual_tag = " (manual)" if outcome.manual else ""
        return (
            f"Check [{outcome.ability.upper()}]{manual_tag} {status}: rolls {rolls}{kept_segment}"
            f" + {outcome.modifier} = {outcome.total} vs DC {outcome.difficulty_class}"
        )


__all__ = [
    "StoryEngine",
    "StoryScene",
    "StoryChoice",
    "StoryCheck",
    "StoryCheckOutcome",
    "StoryTurnResult",
]
