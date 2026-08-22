"""Runtime configuration, loaded once from the environment.

Everything the bot needs to boot lives here. Provider API keys are deliberately
*not* read into this object -- pydantic-ai reads ANTHROPIC_API_KEY / OPENAI_API_KEY /
DEEPSEEK_API_KEY / OLLAMA_BASE_URL straight from the environment itself, so the keys
never pass through our code.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

load_dotenv()

DEFAULT_MODEL = "anthropic:claude-opus-5"
DEFAULT_SUMMARY_MODEL = "anthropic:claude-haiku-4-5"


class Settings(BaseModel):
    """Immutable view of the environment."""

    telegram_token: str = Field(min_length=1)
    model: str = DEFAULT_MODEL
    summary_model: str = DEFAULT_SUMMARY_MODEL
    db_path: Path = Path("local/dmk.sqlite3")
    log_level: str = "INFO"
    #: How hard the model thinks per turn. Narration doesn't need the default 'high',
    #: and lower settings are markedly faster and cheaper. Anthropic models only.
    effort: str = "medium"

    model_config = {"frozen": True}

    @field_validator("effort")
    @classmethod
    def _known_effort(cls, value: str) -> str:
        level = value.lower()
        valid = {"low", "medium", "high", "xhigh", "max"}
        if level not in valid:
            raise ValueError(f"DMK_EFFORT must be one of {sorted(valid)}, got {value!r}")
        return level

    @field_validator("log_level")
    @classmethod
    def _upper(cls, value: str) -> str:
        level = value.upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in valid:
            raise ValueError(f"DMK_LOG_LEVEL must be one of {sorted(valid)}, got {value!r}")
        return level

    @field_validator("model", "summary_model")
    @classmethod
    def _has_provider_prefix(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError(
                f"Model {value!r} needs a provider prefix, e.g. 'anthropic:claude-opus-5', "
                "'openai:gpt-5', 'deepseek:deepseek-chat', 'ollama:qwen3:14b'."
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read settings from the environment. Cached -- call it freely."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    return Settings(
        telegram_token=token,
        model=os.getenv("DMK_MODEL", DEFAULT_MODEL),
        summary_model=os.getenv("DMK_SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL),
        db_path=Path(os.getenv("DMK_DB_PATH", "local/dmk.sqlite3")),
        log_level=os.getenv("DMK_LOG_LEVEL", "INFO"),
        effort=os.getenv("DMK_EFFORT", "medium"),
    )
