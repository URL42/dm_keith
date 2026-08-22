"""Dice parsing and rolling. Ported from the old suite, plus advantage coverage."""

from __future__ import annotations

import random

import pytest

from src.game.dice import (
    DiceParseError,
    format_roll,
    parse_dice_expression,
    roll_instruction,
)


def test_parse_basic() -> None:
    instruction = parse_dice_expression("2d6+3")
    assert (instruction.count, instruction.sides, instruction.modifier) == (2, 6, 3)
    assert instruction.advantage == 0


def test_parse_bare_ability_means_d20() -> None:
    instruction = parse_dice_expression("str")
    assert (instruction.count, instruction.sides) == (1, 20)
    assert instruction.ability == "str"


def test_parse_advantage() -> None:
    assert parse_dice_expression("1d20adv+2").advantage == 1
    assert parse_dice_expression("1d20dis").advantage == -1


def test_parse_rejects_nonsense() -> None:
    with pytest.raises(DiceParseError):
        parse_dice_expression("banana")
    with pytest.raises(DiceParseError):
        parse_dice_expression("   ")


def test_roll_is_deterministic_with_seeded_rng() -> None:
    instruction = parse_dice_expression("3d6")
    first = roll_instruction(instruction, rng=random.Random(1))
    second = roll_instruction(instruction, rng=random.Random(1))
    assert first.total == second.total
    assert len(first.rolls) == 3


def test_advantage_keeps_the_higher_die() -> None:
    instruction = parse_dice_expression("1d20adv")
    result = roll_instruction(instruction, rng=random.Random(3))
    assert len(result.rolls) == 2
    assert result.kept == [max(result.rolls)]


def test_disadvantage_keeps_the_lower_die() -> None:
    instruction = parse_dice_expression("1d20dis")
    result = roll_instruction(instruction, rng=random.Random(3))
    assert result.kept == [min(result.rolls)]


def test_ability_modifier_is_added_to_total() -> None:
    instruction = parse_dice_expression("str")
    result = roll_instruction(instruction, ability_modifier=3, rng=random.Random(5))
    assert result.total == result.kept[0] + 3


def test_format_roll_mentions_ability_and_total() -> None:
    instruction = parse_dice_expression("str+1")
    result = roll_instruction(instruction, ability_modifier=2, rng=random.Random(5))
    rendered = format_roll(result)
    assert "STR" in rendered
    assert str(result.total) in rendered
