"""Turning a `provider:model` string into something pydantic-ai can run.

pydantic-ai already understands every provider we care about, so this is thin --
it exists to give a clear error when a model spec is wrong, and to keep the
"which providers do we support" question answerable in one place.
"""

from __future__ import annotations

from pydantic_ai.models import Model, infer_model

#: Providers we've actually tried. Others may work; these are the supported set.
KNOWN_PROVIDERS = ("anthropic", "openai", "deepseek", "ollama")

#: The credential each provider expects. pydantic-ai reads these itself.
PROVIDER_CREDENTIALS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "ollama": "OLLAMA_BASE_URL",
}


class ModelConfigError(RuntimeError):
    """The configured model can't be built -- almost always a missing credential."""


def build_model(spec: str) -> Model:
    """Build the model named by `spec`, e.g. 'anthropic:claude-opus-5'.

    Raises ModelConfigError with an actionable message rather than letting a bare
    provider error surface halfway through someone's campaign.
    """
    provider = spec.split(":", 1)[0]
    try:
        return infer_model(spec)
    # Providers raise their own unrelated exception types, so this catch is broad
    # on purpose -- we re-raise as one thing the caller can actually handle.
    except Exception as exc:
        credential = PROVIDER_CREDENTIALS.get(provider)
        hint = f" Set {credential} in your .env." if credential else ""
        if provider not in KNOWN_PROVIDERS:
            hint = f" Known providers: {', '.join(KNOWN_PROVIDERS)}.{hint}"
        raise ModelConfigError(f"Could not load model {spec!r}: {exc}{hint}") from exc
