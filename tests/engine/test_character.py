from __future__ import annotations

from pathlib import Path

from src.engine.character import (
    CharacterManager,
    ability_modifier,
    level_from_xp,
    profile_ready,
)
from src.engine.storage import SQLiteStore


def make_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(db_path=tmp_path / "character.sqlite3")
    store.migrate()
    return store


def test_character_manager_levels_and_scores(tmp_path) -> None:
    store = make_store(tmp_path)
    manager = CharacterManager(store)
    session_id = "session-test"
    user_id = "user-test"

    profile = manager.ensure_profile(session_id, user_id)
    assert profile.level == 1
    manager.update_basic_field(session_id, user_id, character_name="Test", race="Elf", character_class="Bard")
    profile = manager.set_ability_score(session_id, user_id, "int", 16)
    assert profile.ability_scores["int"] == 16
    assert ability_modifier(16) == 3

    profile = manager.adjust_experience(session_id, user_id, 350)
    assert profile.experience == 350
    assert level_from_xp(profile.experience) == profile.level

    profile = manager.assign_random_ability_scores(session_id, user_id)
    assert all(4 <= score <= 20 for score in profile.ability_scores.values())
    profile = manager.add_inventory_item(session_id, user_id, "torch", quantity=2)
    assert profile.inventory["torch"] == 2
    profile = manager.remove_inventory_item(session_id, user_id, "torch", quantity=1)
    assert profile.inventory["torch"] == 1
    manager.clear_inventory(session_id, user_id)
    assert not manager.get_inventory(session_id, user_id)


def test_finalize_profile_marks_story_ready(tmp_path) -> None:
    store = make_store(tmp_path)
    manager = CharacterManager(store)
    session_id = "session-ready"
    user_id = "user-ready"
    manager.ensure_profile(session_id, user_id)
    manager.update_basic_field(
        session_id,
        user_id,
        character_name="Rin",
        race="Human",
        character_class="Wizard",
    )
    profile = manager.finalize_profile(session_id, user_id)
    assert profile_ready(profile)
    session_state = store.get_session(session_id)
    assert session_state is not None and session_state.story_mode_enabled
