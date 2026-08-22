"""Async SQLite connection management.

One connection for the whole process. aiosqlite runs the real sqlite3 calls on a
background thread, so nothing here blocks the event loop -- which is the bug that
made the old bot freeze every chat whenever one player took a turn.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def connect(db_path: Path) -> aiosqlite.Connection:
    """Open the database, apply the schema, and hand back a ready connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text())
    # executescript commits and resets pragmas set inside it, so re-assert the
    # per-connection one that matters.
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.commit()
    return conn
