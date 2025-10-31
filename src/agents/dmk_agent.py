"""OpenAI Agents SDK bridge for Dungeon Master Keith."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

try:
    from openai import OpenAI  # type: ignore
except ImportError:  # pragma: no cover - handled in runtime
    OpenAI = None  # type: ignore[assignment]

from ..config import AllowedMode, Settings, get_settings
from ..engine.achievements import Achievement

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system" / "dmk_system.md"


class AgentError(RuntimeError):
    """Base exception for agent failures."""


class AgentNotConfiguredError(AgentError):
    """Raised when the OpenAI client cannot be initialized (missing API key)."""


@dataclass(frozen=True)
class AgentHistoryMessage:
    role: str
    content: str


class DMKAgent:
    """Wrapper around the OpenAI Responses API tailored for DMK."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        client: Optional[Any] = None,
        system_prompt_path: Optional[Path] = None,
    ) -> None:
        self.settings = settings or get_settings()
        prompt_path = system_prompt_path or SYSTEM_PROMPT_PATH
        self.system_prompt = prompt_path.read_text(encoding="utf-8")
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is not None:
            return self._client
        if OpenAI is None:
            raise AgentNotConfiguredError(
                "openai package is not installed. Install dependencies via `uv sync`."
            )
        if not self.settings.has_openai_credentials():
            raise AgentNotConfiguredError(
                "OPENAI_API_KEY is not configured. Set it in the environment or .env file."
            )
        self._client = OpenAI(api_key=self.settings.openai_api_key)
        return self._client

    def generate_reply(
        self,
        *,
        user_message: str,
        mode: AllowedMode,
        achievement: Achievement,
        toggle_snapshot: dict[str, Any],
        history: Sequence[AgentHistoryMessage] = (),
        attachments: Sequence[str] = (),
    ) -> str:
        """
        Produce a reply body (without the achievement block) from the OpenAI API.

        The returned text is expected to contain one or two paragraphs, as the
        caller will prepend the achievement block.
        """
        conversation_input = [
            {
                "role": "system",
                "content": [{"type": "text", "text": self.system_prompt}],
            }
        ]
        conversation_input.extend(_as_input_messages(history))
        attachment_note = ""
        if attachments:
            joined = "\n".join(f"- {item}" for item in attachments)
            attachment_note = f"\nAttachments:\n{joined}"

        toggle_lines = "\n".join(
            f"- {key}: {value}" for key, value in toggle_snapshot.items()
        )
        achievement_context = (
            f"Achievement id: {achievement.id}\n"
            f"Title: {achievement.title}\n"
            f"Description: {achievement.description}\n"
            f"Reward: {achievement.reward}\n"
            f"Rarity: {achievement.rarity}"
        )
        task_prompt = (
            "You will receive a user message and optional context.\n"
            "An achievement block referencing this moment has ALREADY been prepared.\n"
            "Respond with the paragraphs only—do not repeat or recreate the achievement block.\n"
            "Blend theatrical narration with comedy while staying factual.\n"
        )
        user_payload = (
            f"[Mode]\n{mode}\n\n"
            f"[Toggles]\n{toggle_lines}\n\n"
            f"[Achievement]\n{achievement_context}\n\n"
            f"{task_prompt}\n"
            f"User message:\n{user_message.strip()}{attachment_note}"
        )

        conversation_input.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": user_payload}],
            }
        )

        try:
            response = self.client.responses.create(  # type: ignore[call-arg]
                model=self.settings.model,
                input=conversation_input,
                temperature=0.6,
            )
        except Exception as exc:  # pragma: no cover - network failures
            raise AgentError(f"Failed to call OpenAI Responses API: {exc}") from exc

        body = _extract_text(response)
        if not body.strip():
            raise AgentError("Agent response was empty.")
        return body.strip()


def _as_input_messages(
    history: Iterable[AgentHistoryMessage],
) -> Iterable[dict[str, Any]]:
    for message in history:
        yield {
            "role": message.role,
            "content": [{"type": "text", "text": message.content}],
        }


def _extract_text(response: Any) -> str:
    """Normalize Responses API output into plain text."""
    if hasattr(response, "output_text"):
        text = getattr(response, "output_text", "") or ""
        if text:
            return text
    output = getattr(response, "output", None)
    if not output:
        return ""
    chunks: list[str] = []
    for item in output:
        message = getattr(item, "message", None)
        if not message:
            continue
        content = getattr(message, "content", [])
        for piece in content:
            if getattr(piece, "type", None) == "text":
                text_value = getattr(piece, "text", "")
                if hasattr(text_value, "value"):
                    text_value = text_value.value
                if isinstance(text_value, str):
                    chunks.append(text_value)
    return "".join(chunks)


__all__ = [
    "DMKAgent",
    "AgentError",
    "AgentNotConfiguredError",
    "AgentHistoryMessage",
]
