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
from src.storage import db
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


async def test_a_container_refuses_to_write_outside_the_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover absolute DMK_DB_PATH would write inside the container.

    That looks like it works and then loses the whole campaign on the next
    `docker compose up --build`, so it has to be a startup failure instead.
    """
    monkeypatch.setattr(db, "in_container", lambda: True)

    with pytest.raises(db.DatabaseNotPersistent) as excinfo:
        await connect(tmp_path / "main.sqlite3")

    message = str(excinfo.value)
    assert "lost on the next rebuild" in message
    assert "DMK_DB_DIR" in message


async def test_a_container_accepts_the_mounted_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "in_container", lambda: True)
    monkeypatch.setattr(db, "CONTAINER_DATA_DIR", Path("/tmp"))

    conn = await connect(Path("/tmp/dmk-persistence-check/main.sqlite3"))
    await conn.close()


async def test_outside_a_container_any_path_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "in_container", lambda: False)
    conn = await connect(tmp_path / "anywhere.sqlite3")
    await conn.close()


LEGACY_SCHEMA = Path(__file__).parent / "fixtures" / "legacy_schema.sql"


async def test_a_database_from_the_old_bot_is_refused(tmp_path: Path) -> None:
    """Reproduces the server failure: both schemas have achievement_grants, with
    different columns, so the new index blew up with "no such column: character_id".
    """
    import aiosqlite

    legacy_path = tmp_path / "main.sqlite3"
    async with aiosqlite.connect(legacy_path) as legacy:
        await legacy.executescript(LEGACY_SCHEMA.read_text())
        await legacy.commit()

    with pytest.raises(db.LegacyDatabase) as excinfo:
        await connect(legacy_path)

    message = str(excinfo.value)
    assert "previous version of DM Keith" in message
    assert "no migration" in message
    # And it says what to do about it.
    assert "DMK_DB_PATH" in message


async def test_the_legacy_file_is_left_untouched(tmp_path: Path) -> None:
    """Refusing must not modify the old database on the way out."""
    import aiosqlite

    legacy_path = tmp_path / "main.sqlite3"
    async with aiosqlite.connect(legacy_path) as legacy:
        await legacy.executescript(LEGACY_SCHEMA.read_text())
        await legacy.commit()

    async with aiosqlite.connect(legacy_path) as check:
        cur = await check.execute("SELECT name FROM sqlite_master WHERE type='table'")
        before = {r[0] for r in await cur.fetchall()}

    with pytest.raises(db.LegacyDatabase):
        await connect(legacy_path)

    async with aiosqlite.connect(legacy_path) as check:
        cur = await check.execute("SELECT name FROM sqlite_master WHERE type='table'")
        after = {r[0] for r in await cur.fetchall()}

    assert before == after
    assert "campaigns" not in after


async def test_a_fresh_database_beside_a_legacy_one_is_fine(tmp_path: Path) -> None:
    """The documented fix: point at a new filename in the same directory."""
    import aiosqlite

    async with aiosqlite.connect(tmp_path / "main.sqlite3") as legacy:
        await legacy.executescript(LEGACY_SCHEMA.read_text())
        await legacy.commit()

    conn = await connect(tmp_path / "dmk.sqlite3")
    try:
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
        assert {"campaigns", "characters", "achievement_grants"} <= tables
    finally:
        await conn.close()
