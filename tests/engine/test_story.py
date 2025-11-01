from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.engine.character import CharacterManager
from src.engine.story import StoryEngine
from src.engine.storage import SQLiteStore


def make_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(db_path=tmp_path / "story.sqlite3")
    store.migrate()
    return store


def build_campaign(tmp_path: Path) -> Path:
    data = {
        "campaign": "Unit Test Campaign",
        "root_scene": "intro",
        "scenes": [
            {
                "id": "intro",
                "title": "Chaotic Choice",
                "narration": ["A lever glows ominously."],
                "choices": [
                    {
                        "id": "pull_lever",
                        "label": "Yank the lever dramatically",
                        "next_scene": "after",
                        "tags": ["chaos"],
                        "xp_reward": 0,
                    }
                ],
            },
            {
                "id": "after",
                "title": "Aftermath",
                "narration": ["The dungeon groans in response."],
                "choices": [],
            },
        ],
    }
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(data))
    return path


def test_auto_check_inferred_from_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_store(tmp_path)
    campaign_path = build_campaign(tmp_path)
    engine = StoryEngine(store, campaign_path=campaign_path)

    manager = CharacterManager(store)
    session_id = "session-auto"
    user_id = "user-auto"
    profile = manager.ensure_profile(session_id, user_id)
    manager.update_basic_field(session_id, user_id, character_name="Carl", race="Human", character_class="Crawler")
    manager.set_ability_score(session_id, user_id, "cha", 18)
    profile = manager.ensure_profile(session_id, user_id)

    monkeypatch.setattr("src.engine.story.runtime.random.randint", lambda _a, _b: 15)

    result = engine.process_turn(session_id, user_id, profile, "1")
    assert result is not None
    assert result.auto_generated_check is True
    assert result.check_outcome is not None
    assert result.check_outcome.ability == "cha"
    assert result.metadata["check"]["auto"] is True
