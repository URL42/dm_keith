"""SQLite storage helpers for Dungeon Master Keith."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

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
    achievement_density: str
    story_mode_enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class StoryProfile:
    session_id: str
    user_id: str
    character_name: Optional[str]
    pronouns: Optional[str]
    race: Optional[str]
    character_class: Optional[str]
    backstory: Optional[str]
    level: int
    experience: int
    ability_scores: dict[str, Any]
    inventory: dict[str, Any]
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class StoryState:
    session_id: str
    current_scene: Optional[str]
    scene_history: Sequence[str]
    flags: dict[str, Any]
    stats: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class StoryRoll:
    id: int
    session_id: str
    user_id: str
    expression: str
    result_total: int
    result_detail: dict[str, Any]
    created_at: datetime


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
            self._ensure_session_columns(conn)
            conn.commit()

    def close(self) -> None:
        """Close the connection if open."""
        if self._connection is not None:
            with self._lock:
                self._connection.close()
                self._connection = None

    def _ensure_session_columns(self, conn: sqlite3.Connection) -> None:
        """Add newly introduced columns to sessions when missing."""
        alterations = [
            (
                "ALTER TABLE sessions ADD COLUMN achievement_density TEXT NOT NULL DEFAULT 'normal'",
                "achievement_density",
            ),
            (
                "ALTER TABLE sessions ADD COLUMN story_mode_enabled INTEGER NOT NULL DEFAULT 0",
                "story_mode_enabled",
            ),
        ]
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        for statement, column in alterations:
            if column in existing:
                continue
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" in str(exc).lower():
                    continue
                raise

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
                       achievement_density,
                       story_mode_enabled,
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
            achievement_density=row["achievement_density"],
            story_mode_enabled=bool(row["story_mode_enabled"]),
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
        achievement_density: Optional[str] = None,
        story_mode_enabled: Optional[bool] = None,
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
        next_density = (
            achievement_density
            if achievement_density is not None
            else (current.achievement_density if current else "normal")
        )
        next_story_mode = (
            int(story_mode_enabled)
            if story_mode_enabled is not None
            else (1 if (current and current.story_mode_enabled) else 0)
        )

        conn = self.connect()
        with self._lock:
            conn.execute(
                """
                INSERT INTO sessions (id, user_id, mode, profanity_level, rating, tangents_level, achievement_density, story_mode_enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    mode=excluded.mode,
                    profanity_level=excluded.profanity_level,
                    rating=excluded.rating,
                    tangents_level=excluded.tangents_level,
                    achievement_density=excluded.achievement_density,
                    story_mode_enabled=excluded.story_mode_enabled,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    session_id,
                    user_id,
                    next_mode,
                    next_profanity,
                    next_rating,
                    next_tangents,
                    next_density,
                    next_story_mode,
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
        awarded_at_str = awarded_at.strftime("%Y-%m-%d %H:%M:%S")
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
                    awarded_at_str,
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

    def get_story_profile(self, session_id: str) -> Optional[StoryProfile]:
        conn = self.connect()
        with self._lock:
            row = conn.execute(
                """
                SELECT session_id,
                       user_id,
                       character_name,
                       pronouns,
                       race,
                       character_class,
                       backstory,
                       level,
                       experience,
                       ability_scores,
                       inventory,
                       metadata,
                       created_at,
                       updated_at
                FROM story_profiles
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return StoryProfile(
            session_id=row["session_id"],
            user_id=row["user_id"],
            character_name=row["character_name"],
            pronouns=row["pronouns"],
            race=row["race"],
            character_class=row["character_class"],
            backstory=row["backstory"],
            level=row["level"],
            experience=row["experience"],
            ability_scores=json.loads(row["ability_scores"] or "{}"),
            inventory=json.loads(row["inventory"] or "{}"),
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    def upsert_story_profile(
        self,
        session_id: str,
        user_id: str,
        *,
        character_name: Optional[str] = None,
        pronouns: Optional[str] = None,
        race: Optional[str] = None,
        character_class: Optional[str] = None,
        backstory: Optional[str] = None,
        level: Optional[int] = None,
        experience: Optional[int] = None,
        ability_scores: Optional[dict[str, Any]] = None,
        inventory: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> StoryProfile:
        current = self.get_story_profile(session_id)
        next_level = level if level is not None else (current.level if current else 1)
        next_experience = (
            experience if experience is not None else (current.experience if current else 0)
        )
        next_ability = ability_scores if ability_scores is not None else (
            current.ability_scores if current else {}
        )
        next_inventory = inventory if inventory is not None else (current.inventory if current else {})
        next_metadata = metadata if metadata is not None else (current.metadata if current else {})
        conn = self.connect()
        with self._lock:
            conn.execute(
                """
                INSERT INTO story_profiles (
                    session_id,
                    user_id,
                    character_name,
                    pronouns,
                    race,
                    character_class,
                    backstory,
                    level,
                    experience,
                    ability_scores,
                    inventory,
                    metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    character_name=excluded.character_name,
                    pronouns=excluded.pronouns,
                    race=excluded.race,
                    character_class=excluded.character_class,
                    backstory=excluded.backstory,
                    level=excluded.level,
                    experience=excluded.experience,
                    ability_scores=excluded.ability_scores,
                    inventory=excluded.inventory,
                    metadata=excluded.metadata,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    session_id,
                    user_id,
                    character_name
                    if character_name is not None
                    else (current.character_name if current else None),
                    pronouns if pronouns is not None else (current.pronouns if current else None),
                    race if race is not None else (current.race if current else None),
                    character_class
                    if character_class is not None
                    else (current.character_class if current else None),
                    backstory if backstory is not None else (current.backstory if current else None),
                    next_level,
                    next_experience,
                    json.dumps(next_ability, separators=(",", ":")),
                    json.dumps(next_inventory, separators=(",", ":")),
                    json.dumps(next_metadata, separators=(",", ":")),
                ),
            )
            conn.commit()
        return self.get_story_profile(session_id)  # type: ignore[return-value]

    def get_story_state(self, session_id: str) -> Optional[StoryState]:
        conn = self.connect()
        with self._lock:
            row = conn.execute(
                """
                SELECT session_id,
                       current_scene,
                       scene_history,
                       flags,
                       stats,
                       created_at,
                       updated_at
                FROM story_state
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return StoryState(
            session_id=row["session_id"],
            current_scene=row["current_scene"],
            scene_history=tuple(json.loads(row["scene_history"] or "[]")),
            flags=json.loads(row["flags"] or "{}"),
            stats=json.loads(row["stats"] or "{}"),
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    def upsert_story_state(
        self,
        session_id: str,
        *,
        current_scene: Optional[str] = None,
        scene_history: Optional[Sequence[str]] = None,
        flags: Optional[dict[str, Any]] = None,
        stats: Optional[dict[str, Any]] = None,
    ) -> StoryState:
        current = self.get_story_state(session_id)
        next_scene = current_scene if current_scene is not None else (current.current_scene if current else None)
        next_history = (
            list(scene_history)
            if scene_history is not None
            else (list(current.scene_history) if current else [])
        )
        next_flags = flags if flags is not None else (current.flags if current else {})
        next_stats = stats if stats is not None else (current.stats if current else {})

        conn = self.connect()
        with self._lock:
            conn.execute(
                """
                INSERT INTO story_state (
                    session_id,
                    current_scene,
                    scene_history,
                    flags,
                    stats
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    current_scene=excluded.current_scene,
                    scene_history=excluded.scene_history,
                    flags=excluded.flags,
                    stats=excluded.stats,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    session_id,
                    next_scene,
                    json.dumps(list(next_history), separators=(",", ":")),
                    json.dumps(next_flags, separators=(",", ":")),
                    json.dumps(next_stats, separators=(",", ":")),
                ),
            )
            conn.commit()
        return self.get_story_state(session_id)  # type: ignore[return-value]

    def log_story_roll(
        self,
        session_id: str,
        user_id: str,
        expression: str,
        result_total: int,
        result_detail: dict[str, Any],
    ) -> StoryRoll:
        conn = self.connect()
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cursor = conn.execute(
                """
                INSERT INTO story_rolls (
                    session_id,
                    user_id,
                    expression,
                    result_total,
                    result_detail,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    expression,
                    result_total,
                    json.dumps(result_detail, separators=(",", ":")),
                    created_at,
                ),
            )
            conn.commit()
            roll_id = cursor.lastrowid
        return StoryRoll(
            id=roll_id,
            session_id=session_id,
            user_id=user_id,
            expression=expression,
            result_total=result_total,
            result_detail=result_detail,
            created_at=datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
        )

    def fetch_recent_story_rolls(
        self,
        session_id: str,
        limit: int = 10,
    ) -> list[StoryRoll]:
        conn = self.connect()
        with self._lock:
            rows = conn.execute(
                """
                SELECT id,
                       session_id,
                       user_id,
                       expression,
                       result_total,
                       result_detail,
                       created_at
                FROM story_rolls
                WHERE session_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        rolls: list[StoryRoll] = []
        for row in rows:
            rolls.append(
                StoryRoll(
                    id=row["id"],
                    session_id=row["session_id"],
                    user_id=row["user_id"],
                    expression=row["expression"],
                    result_total=row["result_total"],
                    result_detail=json.loads(row["result_detail"] or "{}"),
                    created_at=_parse_datetime(row["created_at"]),
                )
            )
        return rolls


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    raise TypeError(f"Unsupported datetime value: {value!r}")


__all__ = [
    "SQLiteStore",
    "AchievementGrant",
    "SessionState",
    "StoryProfile",
    "StoryState",
    "StoryRoll",
]
