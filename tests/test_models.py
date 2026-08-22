"""The provider factory: does swapping DMK_MODEL actually work?"""

from __future__ import annotations

import pytest

from src.llm.models import KNOWN_PROVIDERS, PROVIDER_CREDENTIALS, ModelConfigError, build_model


@pytest.mark.parametrize(
    ("spec", "credential"),
    [
        ("anthropic:claude-opus-5", "ANTHROPIC_API_KEY"),
        ("openai:gpt-5", "OPENAI_API_KEY"),
        ("deepseek:deepseek-chat", "DEEPSEEK_API_KEY"),
        ("ollama:qwen3:14b", "OLLAMA_BASE_URL"),
    ],
)
def test_every_supported_provider_builds(
    monkeypatch: pytest.MonkeyPatch, spec: str, credential: str
) -> None:
    """Each provider resolves once its credential is present."""
    for env in PROVIDER_CREDENTIALS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        credential, "http://localhost:11434/v1" if "URL" in credential else "sk-test"
    )

    model = build_model(spec)
    assert model is not None


def test_missing_credentials_name_the_variable_to_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in PROVIDER_CREDENTIALS.values():
        monkeypatch.delenv(env, raising=False)

    with pytest.raises(ModelConfigError, match="ANTHROPIC_API_KEY"):
        build_model("anthropic:claude-opus-5")


def test_an_unknown_provider_lists_the_known_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ModelConfigError) as excinfo:
        build_model("hal9000:hal")

    message = str(excinfo.value)
    assert all(provider in message for provider in KNOWN_PROVIDERS)
