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
    #: Set on achievements the engine awards itself when a game event happens.
    #: These are kept out of the catalogue Keith reads -- they're not his to give.
    trigger: str | None = None


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
            trigger=entry.get("trigger"),
        )
        for entry in entries
    }


@lru_cache(maxsize=1)
def _by_trigger() -> dict[str, Achievement]:
    return {a.trigger: a for a in load_registry().values() if a.trigger}


def for_trigger(name: str) -> Achievement | None:
    """The achievement the engine awards for a given game event, if any."""
    return _by_trigger().get(name)


def format_block(achievement: Achievement) -> str:
    """Keith's signature 🏆 block."""
    return (
        "🏆 ACHIEVEMENT UNLOCKED:\n"
        f'"{achievement.title}"\n'
        f"Description: {achievement.description}\n"
        f"Reward: {achievement.reward} Rarity: {achievement.rarity}"
    )


def render_catalogue(limit: int | None = None, exclude: set[str] | None = None) -> str:
    """The menu Keith picks from.

    Fetched via a tool rather than shipped in every prompt -- it's ~1200 tokens, and
    most turns don't award anything.
    """
    entries = sorted(
        (
            a
            for a in load_registry().values()
            # Engine-awarded ones aren't Keith's to give, so he never sees them.
            if a.trigger is None and a.id not in (exclude or set())
        ),
        key=lambda a: (RARITY_ORDER.index(a.rarity) if a.rarity in RARITY_ORDER else 99, a.id),
    )
    if limit is not None:
        entries = entries[:limit]
    if not entries:
        return "Nothing left in the catalogue that they haven't already earned."
    return "\n".join(f"- {a.id} ({a.rarity}): {a.title} — {a.description}" for a in entries)


async def award_trigger(
    repo: Repo, campaign: Campaign, character: Character, trigger: str
) -> str | None:
    """Award the achievement for a game event. Returns the block, or None.

    None covers both "no achievement for that event" and "they already have it",
    which are the same thing to the caller.
    """
    achievement = for_trigger(trigger)
    if achievement is None:
        return None
    return await award(repo, campaign, character, achievement.id)


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
