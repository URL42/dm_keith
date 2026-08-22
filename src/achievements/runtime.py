"""Achievement catalogue and awarding.

The old engine rolled dice behind the scenes to decide whether an achievement should
appear, using a trigger vocabulary that had drifted out of sync with what the bot
actually emitted -- so story achievements almost never fired. Keith now picks them
himself from a catalogue we hand him, and this module just enforces the rules and
formats the block.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.storage.repo import Campaign, Character, Repo

REGISTRY_PATH = Path(__file__).parent / "registry.json"

RARITY_ORDER = ("common", "uncommon", "rare", "epic", "mythic")


class AchievementError(ValueError):
    """The requested achievement doesn't exist."""


@dataclass(frozen=True)
class Achievement:
    id: str
    title: str
    description: str
    reward: str
    rarity: str
    tags: tuple[str, ...] = ()


@lru_cache(maxsize=1)
def load_registry() -> dict[str, Achievement]:
    raw = json.loads(REGISTRY_PATH.read_text())
    entries = raw if isinstance(raw, list) else raw.get("achievements", [])
    return {
        entry["id"]: Achievement(
            id=entry["id"],
            title=entry["title"],
            description=entry.get("description", ""),
            reward=entry.get("reward", ""),
            rarity=entry.get("rarity", "common"),
            tags=tuple(entry.get("tags", ())),
        )
        for entry in entries
    }


def format_block(achievement: Achievement) -> str:
    """Keith's signature 🏆 block."""
    return (
        "🏆 ACHIEVEMENT UNLOCKED:\n"
        f'"{achievement.title}"\n'
        f"Description: {achievement.description}\n"
        f"Reward: {achievement.reward} Rarity: {achievement.rarity}"
    )


def render_catalogue(limit: int | None = None) -> str:
    """The menu Keith picks from, compact enough to sit in the instructions."""
    entries = sorted(
        load_registry().values(),
        key=lambda a: (RARITY_ORDER.index(a.rarity) if a.rarity in RARITY_ORDER else 99, a.id),
    )
    if limit is not None:
        entries = entries[:limit]
    return "\n".join(f"- {a.id} ({a.rarity}): {a.title} — {a.description}" for a in entries)


async def award(
    repo: Repo, campaign: Campaign, character: Character, achievement_id: str
) -> str | None:
    """Grant an achievement. Returns the block, or None if already earned.

    Achievements are per character, so two players in one campaign can each earn the
    same one, but neither earns it twice.
    """
    achievement = load_registry().get(achievement_id)
    if achievement is None:
        raise AchievementError(
            f"No achievement with id {achievement_id!r}. Pick one from the catalogue."
        )

    if await repo.has_achievement(character.id, achievement.id):
        return None

    await repo.grant_achievement(campaign.id, character.id, achievement.id, achievement.rarity)
    return format_block(achievement)
