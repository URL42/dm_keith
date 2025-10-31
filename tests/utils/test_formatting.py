from __future__ import annotations

from src.engine.achievements import Achievement
from src.utils import format_achievement_block


def test_format_achievement_block() -> None:
    achievement = Achievement(
        id="test-achievement",
        title="Test Title",
        description="You did a test thing.",
        reward="+1 Test",
        rarity="common",
        tags=("test",),
        triggers=("event.message",),
        cooldown_sec=0,
        once_per_user=False,
    )
    block = format_achievement_block(achievement)
    assert block.startswith("🏆 ACHIEVEMENT UNLOCKED")
    assert '"Test Title"' in block
    assert "Reward: +1 Test" in block
