from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.config import Settings
from src.engine.modes import ModeRequest, ModeRouter
from src.engine.storage import SQLiteStore


@dataclass
class FakeAgent:
    reply: str

    def generate_reply(self, **_: object) -> str:
        return self.reply


class FailAgent:
    def generate_reply(self, **_: object) -> str:
        from src.agents import AgentNotConfiguredError

        raise AgentNotConfiguredError("offline")


def make_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "OPENAI_API_KEY": "",
            "TELEGRAM_BOT_TOKEN": "",
            "DMK_DB_PATH": str(tmp_path / "router.sqlite3"),
            "DMK_DEFAULT_MODE": "narrator",
            "DMK_PROFANITY_LEVEL": 2,
            "DMK_RATING": "PG-13",
            "DMK_MODEL": "gpt-4o-mini",
            "DMK_TANGENTS_LEVEL": 1,
            "DMK_ACHIEVEMENT_DENSITY": "normal",
        }
    )


def make_store(settings: Settings) -> SQLiteStore:
    store = SQLiteStore(db_path=settings.db_path_obj)
    store.migrate()
    return store


def test_mode_router_happy_path(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = make_store(settings)
    agent = FakeAgent("Paragraph one.\n\nParagraph two.")
    router = ModeRouter(store=store, agent=agent, settings=settings)
    request = ModeRequest(
        user_id="user-1",
        session_id="session-1",
        message="Sing me a ballad.",
        mode="narrator",
        triggers=("event.message",),
    )
    response = router.handle(request)
    assert response.text.startswith("🏆 ACHIEVEMENT UNLOCKED")
    assert "Paragraph one." in response.text
    assert response.mode in {"narrator", "achievements", "explain", "story"}


def test_mode_router_offline_fallback(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = make_store(settings)
    router = ModeRouter(store=store, agent=FailAgent(), settings=settings)
    request = ModeRequest(
        user_id="user-2",
        session_id="session-2",
        message="Are you there?",
        mode="narrator",
        triggers=("event.message",),
    )
    response = router.handle(request)
    assert "Keith" in response.text
    assert "uplink" in response.text or "oracle" in response.text
