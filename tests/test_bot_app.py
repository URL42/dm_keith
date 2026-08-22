"""Bot wiring: handlers registered, database lifecycle, error surfacing.

No Telegram network is involved -- we build the Application and drive the handler
coroutines with stand-in update objects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Update

from src.bot.app import (
    _close_database,
    _startup,
    build_application,
    handle_error,
    handle_help,
    handle_ping,
    handle_start,
)
from src.bot.context import CONN_KEY, REPO_KEY, SERVICE_KEY
from src.config import Settings
from src.game.session import GameService
from src.storage.repo import Repo


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(telegram_token="123:FAKE", db_path=tmp_path / "bot.sqlite3")


def _fake_update() -> tuple[Any, AsyncMock]:
    """An update whose reply_text we can inspect."""
    reply = AsyncMock()
    update = MagicMock()
    update.message.reply_text = reply
    return update, reply


def test_all_commands_are_registered(settings: Settings) -> None:
    app = build_application(settings)
    registered = {
        cmd
        for group in app.handlers.values()
        for handler in group
        for cmd in getattr(handler, "commands", set()) or set()
    }
    assert {"start", "help", "ping", "newgame", "begin", "sheet", "party", "endgame"} <= registered
    assert app.error_handlers, "an error handler must be attached"


def test_join_is_handled_by_the_creation_conversation(settings: Settings) -> None:
    """/join must reach the ConversationHandler, not a bare command handler."""
    from telegram.ext import ConversationHandler

    app = build_application(settings)
    conversations = [
        h for group in app.handlers.values() for h in group if isinstance(h, ConversationHandler)
    ]
    assert conversations, "character creation conversation is not registered"
    entry_commands = {
        cmd for h in conversations for e in h.entry_points for cmd in getattr(e, "commands", set())
    }
    assert "join" in entry_commands


async def test_post_init_attaches_a_repo_and_shutdown_closes_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app = MagicMock()
    app.bot_data = {}
    app.bot.set_my_commands = AsyncMock()

    await _startup(settings)(app)

    # The "/" menu is populated so players can see their options.
    app.bot.set_my_commands.assert_awaited_once()
    registered = {c.command for c in app.bot.set_my_commands.await_args.args[0]}
    assert {"newgame", "join", "begin", "sheet", "party", "endgame", "help"} == registered
    assert isinstance(app.bot_data[REPO_KEY], Repo)
    assert isinstance(app.bot_data[SERVICE_KEY], GameService)
    assert settings.db_path.exists()

    # The repo is usable, not just present.
    campaign = await app.bot_data[REPO_KEY].create_campaign(chat_id=-1, genre="fantasy")
    assert campaign.id > 0

    await _close_database(app)
    with pytest.raises(ValueError, match="no active connection"):
        await app.bot_data[CONN_KEY].execute("SELECT 1")


async def test_a_bad_model_fails_at_startup_not_mid_campaign(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better a loud crash on boot than Keith dying halfway through a session."""
    from src.llm.models import PROVIDER_CREDENTIALS, ModelConfigError

    for env in PROVIDER_CREDENTIALS.values():
        monkeypatch.delenv(env, raising=False)

    app = MagicMock()
    app.bot_data = {}
    with pytest.raises(ModelConfigError):
        await _startup(settings)(app)


async def test_shutdown_without_a_connection_is_harmless() -> None:
    app = MagicMock()
    app.bot_data = {}
    await _close_database(app)  # must not raise


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (handle_start, "Dungeon Master Keith"),
        (handle_help, "/newgame"),
        (handle_ping, "Still here"),
    ],
)
async def test_simple_handlers_reply(handler: Any, expected: str) -> None:
    update, reply = _fake_update()
    await handler(update, MagicMock())
    reply.assert_awaited_once()
    assert expected in reply.await_args.args[0]


async def test_error_handler_reports_the_failure_instead_of_hiding_it() -> None:
    """The old bot turned API errors into 'Keith being whimsical'. Never again."""
    reply = AsyncMock()
    update = MagicMock(spec=Update)
    update.effective_message.reply_text = reply

    context = MagicMock()
    context.error = RuntimeError("the API is on fire")

    await handle_error(update, context)

    reply.assert_awaited_once()
    text = reply.await_args.args[0]
    assert "RuntimeError" in text
