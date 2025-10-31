"""Configuration and feature toggle utilities for Dungeon Master Keith."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

# Load environment variables from a local .env if present.
load_dotenv()


AllowedProfanityLevel = Literal[0, 1, 2, 3]
AllowedRating = Literal["PG", "PG-13", "R"]
AllowedTangentsLevel = Literal[0, 1, 2]
AllowedAchievementDensity = Literal["low", "normal", "high"]
AllowedMode = Literal["narrator", "achievements", "explain", "story"]


class Settings(BaseModel):
    """Runtime configuration derived from environment variables."""

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    db_path: str = Field(default="./local/dev.sqlite3", alias="DMK_DB_PATH")
    default_mode: AllowedMode = Field(default="narrator", alias="DMK_DEFAULT_MODE")
    profanity_level: AllowedProfanityLevel = Field(
        default=3, alias="DMK_PROFANITY_LEVEL"
    )
    rating: AllowedRating = Field(default="PG-13", alias="DMK_RATING")
    model: str = Field(default="gpt-4o", alias="DMK_MODEL")
    tangents_level: AllowedTangentsLevel = Field(
        default=1, alias="DMK_TANGENTS_LEVEL"
    )
    achievement_density: AllowedAchievementDensity = Field(
        default="normal", alias="DMK_ACHIEVEMENT_DENSITY"
    )

    @field_validator("db_path", mode="before")
    @classmethod
    def _expand_db_path(cls, value: str) -> str:
        path = Path(value).expanduser()
        return str(path)

    @field_validator("profanity_level")
    @classmethod
    def _validate_profanity(cls, value: int) -> int:
        if value not in (0, 1, 2, 3):
            raise ValueError("DMK_PROFANITY_LEVEL must be between 0 and 3 inclusive.")
        return value

    @field_validator("rating")
    @classmethod
    def _normalize_rating(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"PG", "PG-13", "R"}:
            raise ValueError("DMK_RATING must be 'PG', 'PG-13', or 'R'.")
        return normalized

    @field_validator("default_mode")
    @classmethod
    def _ensure_mode(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"narrator", "achievements", "explain", "story"}:
            raise ValueError(
                "DMK_DEFAULT_MODE must be one of narrator|achievements|explain|story."
            )
        return normalized

    @property
    def db_path_obj(self) -> Path:
        """Return the database path as a Path instance."""
        return Path(self.db_path)

    def has_openai_credentials(self) -> bool:
        """True when an OpenAI API key is configured."""
        return bool(self.openai_api_key.strip())

    def has_telegram_credentials(self) -> bool:
        """True when a Telegram bot token is configured."""
        return bool(self.telegram_bot_token.strip())


def _raw_environment() -> dict[str, Optional[str]]:
    """Snapshot environment variables relevant to the settings."""
    keys = [
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "DMK_DB_PATH",
        "DMK_DEFAULT_MODE",
        "DMK_PROFANITY_LEVEL",
        "DMK_RATING",
        "DMK_MODEL",
        "DMK_TANGENTS_LEVEL",
        "DMK_ACHIEVEMENT_DENSITY",
    ]
    return {key: os.getenv(key) for key in keys}


def ensure_database_path(path: Path) -> Path:
    """
    Ensure the SQLite file parent directory exists.

    Returns the resolved path for downstream usage.
    """
    if path.suffix != ".sqlite3":
        path = path.with_suffix(".sqlite3")
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    return path.resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and memoize Settings from the environment."""
    try:
        return Settings(**_raw_environment())
    except ValidationError as exc:
        raise RuntimeError(f"Invalid DMK configuration: {exc}") from exc


__all__ = [
    "Settings",
    "AllowedMode",
    "AllowedProfanityLevel",
    "AllowedRating",
    "AllowedTangentsLevel",
    "AllowedAchievementDensity",
    "ensure_database_path",
    "get_settings",
]
