from __future__ import annotations

import pytest

from src.config import get_settings
from src.config import toggles as toggle_module


def reset_cache() -> None:
    toggle_module.get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    reset_cache()
    yield
    reset_cache()


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("DMK_DB_PATH", str(tmp_path / "dmk.sqlite3"))
    settings = get_settings()
    assert settings.db_path_obj.name == "dmk.sqlite3"
    assert settings.default_mode == "narrator"
    assert settings.profanity_level == 3
    assert settings.rating == "PG-13"


def test_invalid_profanity_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DMK_PROFANITY_LEVEL", "7")
    with pytest.raises(RuntimeError):
        get_settings()
