"""Utility helpers for formatting DMK responses."""

from __future__ import annotations

from textwrap import dedent

from ..engine.achievements import Achievement


def format_achievement_block(achievement: Achievement) -> str:
    """Return the canonical achievement header block."""
    return dedent(
        f"""\
        🏆 ACHIEVEMENT UNLOCKED:
        "{achievement.title}"
        Description: {achievement.description}
        Reward: {achievement.reward}. Rarity: {achievement.rarity}
        """
    ).strip()


__all__ = ["format_achievement_block"]
