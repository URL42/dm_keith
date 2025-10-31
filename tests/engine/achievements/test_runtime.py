from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.engine.achievements import (
    AchievementEvent,
    AwardContext,
    award_achievement,
    load_registry,
)
from src.engine.storage import SQLiteStore


def make_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(db_path=tmp_path / "dmk_test.sqlite3")
    store.migrate()
    return store


def test_registry_contains_achievements() -> None:
    registry = load_registry()
    assert len(registry) >= 40
    sample = registry[0]
    assert sample.id
    assert sample.title
    assert sample.triggers


def test_cooldown_prevents_duplicate_award(tmp_path) -> None:
    store = make_store(tmp_path)
    now = datetime.now(timezone.utc)
    event = AchievementEvent.from_trigger(
        user_id="user-1",
        session_id="session-1",
        trigger="event.message",
    )
    first = award_achievement(event, AwardContext(store=store, now=now))
    assert first is not None

    # Second call happens too soon; ensure we do not grant the same achievement.
    second = award_achievement(
        event,
        AwardContext(store=store, now=now + timedelta(seconds=5)),
    )
    assert second is not None
    assert second.achievement.id != first.achievement.id


def test_once_per_user_respected(tmp_path) -> None:
    store = make_store(tmp_path)
    now = datetime.now(timezone.utc)
    event = AchievementEvent.from_trigger(
        user_id="user-2",
        session_id="session-2",
        trigger="cmd.set.profanity",
    )
    first = award_achievement(event, AwardContext(store=store, now=now))
    assert first is not None
    second = award_achievement(
        event, AwardContext(store=store, now=now + timedelta(seconds=10))
    )
    assert second is None
