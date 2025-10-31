"""Utility to preview achievement registry contents."""

from __future__ import annotations

from pathlib import Path

from src.engine.achievements import load_registry


def main() -> None:
    registry = load_registry()
    print(f"Loaded {len(registry)} achievements from registry.json")  # noqa: T201
    for achievement in registry:
        triggers = ", ".join(achievement.triggers)
        print(  # noqa: T201
            f"- {achievement.id} [{achievement.rarity}] triggers: {triggers}"
        )


if __name__ == "__main__":
    main()
