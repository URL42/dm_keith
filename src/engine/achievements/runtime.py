"""Achievement runtime helper for Dungeon Master Keith."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..storage.sqlite import AchievementGrant, SQLiteStore

REGISTRY_PATH = Path(__file__).resolve().with_name("registry.json")

RARITY_PRIORITY = {"common": 0, "uncommon": 1, "rare": 2, "epic": 3, "mythic": 4}


@dataclass(frozen=True)
class Achievement:
    id: str
    title: str
    description: str
    reward: str
    rarity: str
    tags: tuple[str, ...]
    triggers: tuple[str, ...]
    cooldown_sec: int
    once_per_user: bool

    @property
    def cooldown(self) -> timedelta:
        return timedelta(seconds=self.cooldown_sec)


@dataclass(frozen=True)
class AchievementEvent:
    """Event payload describing why an achievement might be awarded."""

    user_id: str
    trigger_keys: tuple[str, ...]
    session_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_trigger(
        cls,
        user_id: str,
        trigger: str,
        *,
        session_id: Optional[str] = None,
        extra_triggers: Optional[Iterable[str]] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> "AchievementEvent":
        triggers = [trigger]
        if extra_triggers:
            triggers.extend(extra_triggers)
        return cls(
            user_id=user_id,
            trigger_keys=tuple(dict.fromkeys(triggers)),  # preserve order, dedupe
            session_id=session_id,
            payload=payload or {},
        )


@dataclass(frozen=True)
class AwardContext:
    """Context required to award an achievement."""

    store: SQLiteStore
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class AwardedAchievement:
    """Structured response containing the selected achievement and the grant row."""

    achievement: Achievement
    grant: AchievementGrant
    trigger: str


def award_achievement(
    event: AchievementEvent,
    context: AwardContext,
) -> Optional[AwardedAchievement]:
    """
    Attempt to award a single achievement for the provided event.

    The function enforces once-per-user and cooldown rules by consulting the SQLite store.
    Returns None when no eligible achievement exists.
    """
    registry = list(load_registry())
    trigger_keys = event.trigger_keys or ()
    if not trigger_keys:
        return None

    candidates = _matching_achievements(registry, trigger_keys)
    if not candidates:
        return None

    rng = random.Random(_stable_seed(event))
    rng.shuffle(candidates)

    for achievement, trigger in candidates:
        if _eligible_for_award(achievement, event, context, trigger):
            grant = context.store.log_achievement(
                achievement_id=achievement.id,
                user_id=event.user_id,
                session_id=event.session_id,
                rarity=achievement.rarity,
                detail={
                    "trigger": trigger,
                    "payload": event.payload,
                },
            )
            return AwardedAchievement(
                achievement=achievement,
                grant=grant,
                trigger=trigger,
            )
    return None


def _eligible_for_award(
    achievement: Achievement,
    event: AchievementEvent,
    context: AwardContext,
    trigger: str,
) -> bool:
    store = context.store

    latest_session: Optional[AchievementGrant] = None
    if event.session_id:
        latest_session = store.fetch_latest_grant(
            achievement.id, event.user_id, event.session_id
        )

    if latest_session:
        delta = context.now - latest_session.awarded_at
        if achievement.cooldown_sec <= 0 and delta.total_seconds() < 1:
            # Prevent immediate duplicates inside the same session even when cooldown is zero.
            return False
        if achievement.cooldown_sec > 0 and delta < achievement.cooldown:
            return False

    latest = store.fetch_latest_grant_any_session(achievement.id, event.user_id)
    if latest is None:
        return True

    if achievement.once_per_user:
        return False

    if achievement.cooldown_sec <= 0:
        # Allow duplicates when no cooldown and not once-per-user.
        return True

    delta = context.now - latest.awarded_at
    return delta >= achievement.cooldown


def _matching_achievements(
    registry: Sequence[Achievement],
    trigger_keys: Sequence[str],
) -> list[tuple[Achievement, str]]:
    pairs: list[tuple[Achievement, str]] = []
    seen_ids: set[tuple[str, str]] = set()
    for trigger in trigger_keys:
        for achievement in registry:
            if trigger in achievement.triggers:
                pair = (achievement.id, trigger)
                if pair not in seen_ids:
                    seen_ids.add(pair)
                    pairs.append((achievement, trigger))
    # Sort deterministically before shuffle for reproducibility.
    pairs.sort(
        key=lambda item: (
            trigger_keys.index(item[1]),
            -RARITY_PRIORITY.get(item[0].rarity, 0),
            item[0].id,
        )
    )
    return pairs


def _stable_seed(event: AchievementEvent) -> int:
    basis = "|".join(
        [
            event.user_id,
            event.session_id or "",
            ",".join(event.trigger_keys),
        ]
    )
    return hash(basis)


@lru_cache(maxsize=1)
def load_registry() -> tuple[Achievement, ...]:
    """Load and cache the achievement registry."""
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    achievements = []
    for entry in raw:
        achievements.append(
            Achievement(
                id=entry["id"],
                title=entry["title"],
                description=entry["description"],
                reward=entry["reward"],
                rarity=entry["rarity"],
                tags=tuple(entry.get("tags", [])),
                triggers=tuple(entry.get("triggers", [])),
                cooldown_sec=int(entry.get("cooldown_sec", 0)),
                once_per_user=bool(entry.get("once_per_user", False)),
            )
        )
    return tuple(achievements)


__all__ = [
    "Achievement",
    "AchievementEvent",
    "AwardContext",
    "AwardedAchievement",
    "award_achievement",
    "load_registry",
]
