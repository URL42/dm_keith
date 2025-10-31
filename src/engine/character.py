"""Character creation and management helpers for DMK story mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .storage import SQLiteStore, StoryProfile, StoryState

ABILITY_KEYS = ("str", "dex", "con", "int", "wis", "cha")
XP_THRESHOLDS = [
    0,
    300,
    900,
    2700,
    6500,
    14000,
    23000,
    34000,
    48000,
    64000,
]
SUPPORTED_RACES = {
    "human",
    "elf",
    "dwarf",
    "halfling",
    "gnome",
    "tiefling",
    "dragonborn",
    "orc",
    "goblin",
    "half-elf",
    "half-orc",
}
SUPPORTED_CLASSES = {
    "barbarian",
    "bard",
    "cleric",
    "druid",
    "fighter",
    "monk",
    "paladin",
    "ranger",
    "rogue",
    "sorcerer",
    "warlock",
    "wizard",
    "artificer",
}


def default_ability_scores() -> Dict[str, int]:
    return {ability: 10 for ability in ABILITY_KEYS}


class CharacterManager:
    """High-level helper for managing story characters."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    # Profile helpers -------------------------------------------------
    def ensure_profile(self, session_id: str, user_id: str) -> StoryProfile:
        profile = self.store.get_story_profile(session_id)
        if profile:
            return profile
        return self.store.upsert_story_profile(
            session_id,
            user_id,
            ability_scores=default_ability_scores(),
            inventory={},
            metadata={},
        )

    def reset_profile(self, session_id: str, user_id: str) -> StoryProfile:
        return self.store.upsert_story_profile(
            session_id,
            user_id,
            character_name=None,
            pronouns=None,
            race=None,
            character_class=None,
            backstory=None,
            level=1,
            experience=0,
            ability_scores=default_ability_scores(),
            inventory={},
            metadata={"reset_at": "auto"},
        )

    def update_basic_field(
        self,
        session_id: str,
        user_id: str,
        *,
        character_name: Optional[str] = None,
        pronouns: Optional[str] = None,
        race: Optional[str] = None,
        character_class: Optional[str] = None,
        backstory: Optional[str] = None,
    ) -> StoryProfile:
        return self.store.upsert_story_profile(
            session_id,
            user_id,
            character_name=character_name,
            pronouns=pronouns,
            race=race,
            character_class=character_class,
            backstory=backstory,
        )

    def set_ability_score(
        self, session_id: str, user_id: str, ability: str, value: int
    ) -> StoryProfile:
        ability_key = ability.lower()
        if ability_key not in ABILITY_KEYS:
            raise ValueError(f"Ability must be one of {', '.join(ABILITY_KEYS)}")
        if value < 1 or value > 20:
            raise ValueError("Ability scores must be between 1 and 20.")
        profile = self.ensure_profile(session_id, user_id)
        ability_scores = dict(profile.ability_scores)
        ability_scores[ability_key] = value
        return self.store.upsert_story_profile(
            session_id,
            user_id,
            ability_scores=ability_scores,
        )

    def adjust_experience(
        self, session_id: str, user_id: str, delta: int
    ) -> StoryProfile:
        profile = self.ensure_profile(session_id, user_id)
        total = max(0, profile.experience + delta)
        level = level_from_xp(total)
        updated = self.store.upsert_story_profile(
            session_id,
            user_id,
            experience=total,
            level=level,
        )
        state = self.store.get_story_state(session_id)
        if state is not None:
            stats = dict(state.stats)
            stats["xp"] = total
            stats["level"] = level
            self.store.upsert_story_state(session_id, stats=stats)
        return updated

    def finalize_profile(self, session_id: str, user_id: str) -> StoryProfile:
        profile = self.ensure_profile(session_id, user_id)
        if not profile_ready(profile):
            missing = required_fields_missing(profile)
            raise ValueError(
                "Character needs more info: " + ", ".join(missing)
            )
        # Ensure story state exists
        state = self.store.get_story_state(session_id)
        if state is None:
            self.store.upsert_story_state(
                session_id,
                current_scene=None,
                scene_history=[],
                flags={},
                stats={"xp": profile.experience, "level": profile.level},
            )
        # Mark session ready for story mode
        self.store.upsert_session(
            session_id,
            user_id,
            story_mode_enabled=True,
        )
        return self.ensure_profile(session_id, user_id)

    # Inventory helpers ------------------------------------------------
    def get_inventory(self, session_id: str, user_id: str) -> dict[str, int]:
        profile = self.ensure_profile(session_id, user_id)
        inventory = profile.inventory or {}
        return {key: int(value) for key, value in inventory.items()}

    def add_inventory_item(
        self, session_id: str, user_id: str, item: str, quantity: int = 1
    ) -> StoryProfile:
        item = item.strip()
        if not item:
            raise ValueError("Item name cannot be empty.")
        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
        inventory = self.get_inventory(session_id, user_id)
        inventory[item] = inventory.get(item, 0) + quantity
        return self.store.upsert_story_profile(
            session_id,
            user_id,
            inventory=inventory,
        )

    def remove_inventory_item(
        self, session_id: str, user_id: str, item: str, quantity: int = 1
    ) -> StoryProfile:
        item = item.strip()
        if not item:
            raise ValueError("Item name cannot be empty.")
        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
        inventory = self.get_inventory(session_id, user_id)
        if item not in inventory:
            raise ValueError(f"{item} is not in the inventory.")
        remaining = inventory[item] - quantity
        if remaining > 0:
            inventory[item] = remaining
        else:
            inventory.pop(item)
        return self.store.upsert_story_profile(
            session_id,
            user_id,
            inventory=inventory,
        )

    def clear_inventory(self, session_id: str, user_id: str) -> StoryProfile:
        return self.store.upsert_story_profile(
            session_id,
            user_id,
            inventory={},
        )

    # Formatting ------------------------------------------------------
    def render_profile(self, profile: StoryProfile) -> str:
        ability_lines = []
        for ability in ABILITY_KEYS:
            score = profile.ability_scores.get(ability, 10)
            modifier = ability_modifier(score)
            ability_lines.append(f"{ability.upper()}: {score} ({modifier:+})")

        lines = ["Character Sheet"]
        lines.append(f"Name: {profile.character_name or 'Unset'}")
        lines.append(f"Pronouns: {profile.pronouns or 'Unset'}")
        lines.append(f"Race: {profile.race or 'Unset'}")
        lines.append(f"Class: {profile.character_class or 'Unset'}")
        lines.append(f"Level: {profile.level}  XP: {profile.experience}")
        lines.append("Abilities:")
        lines.extend(f"  - {line}" for line in ability_lines)
        if profile.backstory:
            lines.append(f"Backstory: {profile.backstory[:240]}")
        return "\n".join(lines)

    def render_inventory(self, profile: StoryProfile) -> str:
        inventory = profile.inventory or {}
        if not inventory:
            return "Inventory is currently empty. Time to loot something shiny."
        lines = ["Inventory:"]
        for item, qty in sorted(inventory.items()):
            lines.append(f"  - {item}: {qty}")
        return "\n".join(lines)


def ability_modifier(score: int) -> int:
    return (score - 10) // 2


def level_from_xp(xp: int) -> int:
    level = 1
    for index, threshold in enumerate(XP_THRESHOLDS, start=1):
        if xp >= threshold:
            level = index
        else:
            break
    return max(1, level)


def profile_ready(profile: StoryProfile) -> bool:
    return not required_fields_missing(profile)


def required_fields_missing(profile: StoryProfile) -> list[str]:
    missing: list[str] = []
    if not profile.character_name:
        missing.append("name")
    if not profile.race:
        missing.append("race")
    if not profile.character_class:
        missing.append("class")
    return missing


__all__ = [
    "CharacterManager",
    "ability_modifier",
    "level_from_xp",
    "profile_ready",
    "required_fields_missing",
    "ABILITY_KEYS",
    "SUPPORTED_RACES",
    "SUPPORTED_CLASSES",
    "XP_THRESHOLDS",
]
