"""SQLite storage helpers for Dungeon Master Keith."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ...config.toggles import ensure_database_path, get_settings

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "docs" / "DB_SCHEMA.sql"


@dataclass(frozen=True)
class AchievementGrant:
    """Representation of a stored achievement grant."""

    id: int
    achievement_id: str
    user_id: str
    session_id: Optional[str]
    rarity: str
    awarded_at: datetime
    detail: dict[str, Any]


@dataclass(frozen=True)
class SessionState:
    id: str
    user_id: str
    mode: str
    profanity_level: int
    rating: str
    tangents_level: int
    created_at: datetime
    updated_at: datetime


class SQLiteStore:
    """Thread-safe helper around sqlite3 for DMK."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        settings = get_settings()
        if db_path is None:
            db_path = settings.db_path_obj
        self.path = ensure_database_path(db_path)
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        """Return (and lazily initialize) the sqlite3 connection."""
        if self._connection is None:
            self._connection = sqlite3.connect(
                self.path,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
        return self._connection

    def migrate(self) -> None:
        """Apply schema migrations from docs/DB_SCHEMA.sql."""
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn = self.connect()
        with self._lock:
            conn.executescript(sql)
            conn.commit()

    def close(self) -> None:
        """Close the connection if open."""
        if self._connection is not None:
            with self._lock:
                self._connection.close()
                self._connection = None

    def ensure_user(self, user_id: str, display_name: Optional[str] = None) -> None:
        """Insert or update a user record."""
        conn = self.connect()
        with self._lock:
            conn.execute(
                """
                INSERT INTO users (id, display_name)
                VALUES (?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name=excluded.display_name,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, display_name),
            )
            conn.commit()

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """Fetch a session record if it exists."""
        conn = self.connect()
        with self._lock:
            row = conn.execute(
                """
                SELECT id,
                       user_id,
                       mode,
                       profanity_level,
                       rating,
                       tangents_level,
                       created_at,
                       updated_at
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return SessionState(
            id=row["id"],
            user_id=row["user_id"],
            mode=row["mode"],
            profanity_level=row["profanity_level"],
            rating=row["rating"],
            tangents_level=row["tangents_level"],
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    def upsert_session(
        self,
        session_id: str,
        user_id: str,
        *,
        mode: Optional[str] = None,
        profanity_level: Optional[int] = None,
        rating: Optional[str] = None,
        tangents_level: Optional[int] = None,
    ) -> SessionState:
        """Create or update a session row, returning the latest view."""
        current = self.get_session(session_id)
        next_mode = mode if mode is not None else (current.mode if current else "narrator")
        next_profanity = (
            profanity_level
            if profanity_level is not None
            else (current.profanity_level if current else 3)
        )
        next_rating = rating if rating is not None else (current.rating if current else "PG-13")
        next_tangents = (
            tangents_level
            if tangents_level is not None
            else (current.tangents_level if current else 1)
        )

        conn = self.connect()
        with self._lock:
            conn.execute(
                """
                INSERT INTO sessions (id, user_id, mode, profanity_level, rating, tangents_level)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    mode=excluded.mode,
                    profanity_level=excluded.profanity_level,
                    rating=excluded.rating,
                    tangents_level=excluded.tangents_level,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    session_id,
                    user_id,
                    next_mode,
                    next_profanity,
                    next_rating,
                    next_tangents,
                ),
            )
            conn.commit()
        return self.get_session(session_id)  # type: ignore[return-value]

    def log_achievement(
        self,
        achievement_id: str,
        user_id: str,
        session_id: Optional[str],
        rarity: str,
        detail: Optional[dict[str, Any]] = None,
    ) -> AchievementGrant:
        """Insert an achievement grant row and return the created record."""
        payload = json.dumps(detail or {}, separators=(",", ":"))
        awarded_at = datetime.now(timezone.utc)
        conn = self.connect()
        with self._lock:
            cursor = conn.execute(
                """
                INSERT INTO achievement_grants
                    (achievement_id, user_id, session_id, rarity, awarded_at, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    achievement_id,
                    user_id,
                    session_id,
                    rarity,
                    awarded_at.isoformat(),
                    payload,
                ),
            )
            conn.commit()
            new_id = cursor.lastrowid
        return AchievementGrant(
            id=new_id,
            achievement_id=achievement_id,
            user_id=user_id,
            session_id=session_id,
            rarity=rarity,
            awarded_at=awarded_at,
            detail=detail or {},
        )

    def fetch_latest_grant(
        self,
        achievement_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Optional[AchievementGrant]:
        """Fetch the latest grant for a user (optionally scoped to a session)."""
        conn = self.connect()
        query = """
            SELECT id,
                   achievement_id,
                   user_id,
                   session_id,
                   rarity,
                   awarded_at,
                   detail
            FROM achievement_grants
            WHERE achievement_id = ?
              AND user_id = ?
        """
        params: list[Any] = [achievement_id, user_id]
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY awarded_at DESC LIMIT 1"

        with self._lock:
            row = conn.execute(query, params).fetchone()

        if not row:
            return None

        return AchievementGrant(
            id=row["id"],
            achievement_id=row["achievement_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            rarity=row["rarity"],
            awarded_at=_parse_datetime(row["awarded_at"]),
            detail=json.loads(row["detail"] or "{}"),
        )

    def fetch_latest_grant_any_session(
        self, achievement_id: str, user_id: str
    ) -> Optional[AchievementGrant]:
        """Convenience wrapper to fetch across sessions."""
        return self.fetch_latest_grant(achievement_id, user_id, session_id=None)

    def fetch_most_recent_for_user(self, user_id: str) -> Optional[AchievementGrant]:
        """Return the most recent achievement grant for a user."""
        conn = self.connect()
        with self._lock:
            row = conn.execute(
                """
                SELECT id,
                       achievement_id,
                       user_id,
                       session_id,
                       rarity,
                       awarded_at,
                       detail
                FROM achievement_grants
                WHERE user_id = ?
                ORDER BY awarded_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()

        if not row:
            return None

        return AchievementGrant(
            id=row["id"],
            achievement_id=row["achievement_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            rarity=row["rarity"],
            awarded_at=_parse_datetime(row["awarded_at"]),
            detail=json.loads(row["detail"] or "{}"),
        )


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(f"Unsupported datetime value: {value!r}")


__all__ = ["SQLiteStore", "AchievementGrant", "SessionState"]
