"""Character sheet maths."""

from __future__ import annotations

import random

import pytest

from src.game.characters import (
    ABILITY_KEYS,
    MAX_LEVEL,
    XP_THRESHOLDS,
    ability_modifier,
    assign_standard_array,
    clamp_ability,
    level_from_xp,
    max_hp_for,
    normalise_abilities,
    roll_abilities,
    xp_to_next_level,
)


@pytest.mark.parametrize(
    ("score", "expected"),
    [(1, -5), (8, -1), (10, 0), (11, 0), (12, 1), (15, 2), (20, 5)],
)
def test_ability_modifier(score: int, expected: int) -> None:
    assert ability_modifier(score) == expected


@pytest.mark.parametrize(
    ("xp", "level"),
    [(0, 1), (299, 1), (300, 2), (899, 2), (900, 3), (64000, 10), (999_999, 10)],
)
def test_level_from_xp(xp: int, level: int) -> None:
    assert level_from_xp(xp) == level


def test_xp_to_next_level() -> None:
    assert xp_to_next_level(0) == 300
    assert xp_to_next_level(300) == 600
    assert xp_to_next_level(XP_THRESHOLDS[-1]) is None


def test_max_hp_grows_with_level_and_con() -> None:
    frail = max_hp_for(1, con_score=8)
    sturdy = max_hp_for(1, con_score=16)
    assert sturdy > frail
    assert max_hp_for(5, 14) > max_hp_for(1, 14)
    # Even a terrible constitution leaves you alive at level 1.
    assert max_hp_for(1, con_score=1) >= 1


def test_clamp_and_normalise() -> None:
    assert clamp_ability(25) == 20
    assert clamp_ability(-3) == 1
    scores = normalise_abilities({"str": 99, "bogus": 5})
    assert scores["str"] == 20
    assert "bogus" not in scores
    assert set(scores) == set(ABILITY_KEYS)
    assert scores["cha"] == 10  # unspecified abilities default to 10


def test_standard_array_follows_priority() -> None:
    scores = assign_standard_array(["dex", "con"])
    assert scores["dex"] == 15
    assert scores["con"] == 14
    assert sorted(scores.values(), reverse=True) == [15, 14, 13, 12, 10, 8]


def test_standard_array_handles_empty_priority() -> None:
    scores = assign_standard_array([])
    assert set(scores) == set(ABILITY_KEYS)
    assert sorted(scores.values(), reverse=True) == [15, 14, 13, 12, 10, 8]


def test_roll_abilities_in_range() -> None:
    scores = roll_abilities(random.Random(7))
    assert set(scores) == set(ABILITY_KEYS)
    # 4d6-drop-lowest can only ever land between 3 and 18.
    assert all(3 <= v <= 18 for v in scores.values())


def test_progression_tops_out_at_level_ten() -> None:
    assert MAX_LEVEL == 10
    assert level_from_xp(XP_THRESHOLDS[-1] * 100) == 10
