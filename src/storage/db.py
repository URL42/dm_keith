"""Async SQLite connection management.

One connection for the whole process. aiosqlite runs the real sqlite3 calls on a
background thread, so nothing here blocks the event loop -- which is the bug that
made the old bot freeze every chat whenever one player took a turn.
"""

from __future__ import annotations

import os
from pathlib import Path

import aiosqlite

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class DatabaseUnwritable(RuntimeError):
    """We can't write where the database is supposed to live."""


def _check_writable(db_path: Path) -> None:
    """Fail early and legibly if the database directory isn't writable.

    In Docker this is nearly always a mount whose owner doesn't match the user the
    container runs as. SQLite's own error for that ("unable to open database file")
    gives no hint of the cause, which makes it a miserable thing to debug.
    """
    directory = db_path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DatabaseUnwritable(
            f"Can't create {directory}: {exc}\n"
            "Under Docker, check that DMK_DB_DIR is bind-mounted in "
            "docker-compose.yml and exists on the host."
        ) from exc

    target = db_path if db_path.exists() else directory
    if not os.access(target, os.W_OK):
        raise DatabaseUnwritable(
            f"{target} is not writable by uid {os.getuid()}:{os.getgid()}.\n"
            "Under Docker, set DMK_UID and DMK_GID in .env to the host user that "
            "owns that directory (`id -u` / `id -g`), or chown it to match."
        )


async def connect(db_path: Path) -> aiosqlite.Connection:
    """Open the database, apply the schema, and hand back a ready connection."""
    _check_writable(db_path)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text())
    # executescript commits and resets pragmas set inside it, so re-assert the
    # per-connection one that matters.
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.commit()
    return conn
