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

#: Where docker-compose mounts the host's database directory. Inside a container,
#: this is the only path whose contents outlive the container.
CONTAINER_DATA_DIR = Path("/data")


class DatabaseUnwritable(RuntimeError):
    """We can't write where the database is supposed to live."""


class DatabaseNotPersistent(RuntimeError):
    """In a container, but pointed somewhere that isn't the mounted volume."""


def in_container() -> bool:
    return Path("/.dockerenv").exists()


def _check_persistent(db_path: Path) -> None:
    """In a container, refuse to write anywhere the host can't see.

    Writing to an unmounted path inside a container looks like it works and then
    silently loses the entire campaign on the next `docker compose up --build`.
    Better to refuse to start.
    """
    if not in_container():
        return
    # Resolve both sides: either can be a symlink, and comparing a resolved path
    # against an unresolved one silently reports a false mismatch.
    data_dir = CONTAINER_DATA_DIR.resolve()
    resolved = db_path.resolve()
    if data_dir == resolved or data_dir in resolved.parents:
        return

    raise DatabaseNotPersistent(
        f"DMK_DB_PATH is {db_path}, which is inside the container and would be "
        f"lost on the next rebuild.\n"
        f"Under Docker the database must live under {CONTAINER_DATA_DIR}. Remove "
        f"DMK_DB_PATH from your .env (the image defaults to "
        f"{CONTAINER_DATA_DIR / 'main.sqlite3'}) and set DMK_DB_DIR to the host "
        f"directory you want it stored in."
    )


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
    _check_persistent(db_path)
    _check_writable(db_path)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text())
    # executescript commits and resets pragmas set inside it, so re-assert the
    # per-connection one that matters.
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.commit()
    return conn
