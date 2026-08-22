"""Environment configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import DEFAULT_MODEL, Settings, get_settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"telegram_token": "t"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_defaults() -> None:
    settings = _settings()
    assert settings.model == DEFAULT_MODEL
    assert settings.log_level == "INFO"


def test_log_level_is_normalised() -> None:
    assert _settings(log_level="debug").log_level == "DEBUG"


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(log_level="chatty")


def test_model_needs_a_provider_prefix() -> None:
    with pytest.raises(ValidationError, match="provider prefix"):
        _settings(model="gpt-5")
    # Every provider we support is accepted.
    for spec in (
        "anthropic:claude-opus-5",
        "openai:gpt-5",
        "deepseek:deepseek-chat",
        "ollama:qwen3:14b",
    ):
        assert _settings(model=spec).model == spec


def test_missing_token_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        get_settings()
    get_settings.cache_clear()


def test_environment_variables_map_onto_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:FAKE")
    monkeypatch.setenv("DMK_MODEL", "deepseek:deepseek-chat")
    monkeypatch.setenv("DMK_SUMMARY_MODEL", "ollama:qwen3:14b")
    monkeypatch.setenv("DMK_DB_PATH", "/tmp/somewhere.sqlite3")
    monkeypatch.setenv("DMK_LOG_LEVEL", "debug")

    settings = get_settings()
    assert settings.telegram_token == "123:FAKE"
    assert settings.model == "deepseek:deepseek-chat"
    assert settings.summary_model == "ollama:qwen3:14b"
    assert settings.db_path == Path("/tmp/somewhere.sqlite3")
    assert settings.log_level == "DEBUG"

    # Cached: a later change to the environment does not leak into a live process.
    monkeypatch.setenv("DMK_MODEL", "openai:gpt-5")
    assert get_settings().model == "deepseek:deepseek-chat"
    get_settings.cache_clear()
