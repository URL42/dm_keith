from __future__ import annotations

from pathlib import Path


def test_system_prompt_contains_directives() -> None:
    prompt_path = Path("prompts/system/dmk_system.md")
    content = prompt_path.read_text(encoding="utf-8")
    assert "ACHIEVEMENT UNLOCKED" in content
    assert "Mode guidelines" in content
    assert "PG-13" in content
