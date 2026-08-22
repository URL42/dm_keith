"""Startup failures must be legible.

These are the paths a deployment actually hits: a stale .env from the old bot, or
a database directory the container user can't write to. Both used to surface as
either a raw traceback or SQLite's uninformative "unable to open database file".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config import Settings, get_settings
from src.main import main
from src.storage.db import DatabaseUnwritable, connect


def test_an_old_style_model_name_is_rejected_with_advice() -> None:
    """The pre-revamp .env had DMK_MODEL=gpt-4o, which must not silently 'work'."""
    with pytest.raises(Exception) as excinfo:
        Settings(telegram_token="t", model="gpt-4o")

    message = str(excinfo.value)
    assert "provider prefix" in message
    assert "anthropic:" in message


def test_main_exits_nonzero_with_a_readable_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:FAKE")
    monkeypatch.setenv("DMK_MODEL", "gpt-4o")

    assert main() == 1

    stderr = capsys.readouterr().err
    assert "can't start" in stderr
    assert "provider prefix" in stderr
    assert ".env.example" in stderr
    assert "Traceback" not in stderr
    get_settings.cache_clear()


def test_main_reports_a_missing_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    get_settings.cache_clear()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    # get_settings() falls back to .env via dotenv, so blank the value explicitly.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")

    assert main() == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err
    get_settings.cache_clear()


async def test_an_unwritable_database_directory_names_the_fix(tmp_path: Path) -> None:
    """The Docker case: the mount belongs to a different uid than the container."""
    if os.getuid() == 0:
        pytest.skip("root can write anywhere")

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(DatabaseUnwritable) as excinfo:
            await connect(locked / "main.sqlite3")

        message = str(excinfo.value)
        assert "not writable" in message
        assert "DMK_UID" in message
    finally:
        locked.chmod(0o700)


async def test_a_writable_directory_is_created_on_demand(tmp_path: Path) -> None:
    """The server path (/mnt/sata/dmk/db) may not exist on first run."""
    nested = tmp_path / "mnt" / "sata" / "dmk" / "db"
    conn = await connect(nested / "main.sqlite3")
    try:
        assert nested.is_dir()
        assert (nested / "main.sqlite3").exists()
    finally:
        await conn.close()
