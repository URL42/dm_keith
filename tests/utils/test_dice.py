from __future__ import annotations

from src.utils.dice import DiceParseError, parse_dice_expression, roll_instruction


def test_parse_basic_dice() -> None:
    instruction = parse_dice_expression("2d6+3")
    assert instruction.count == 2
    assert instruction.sides == 6
    assert instruction.modifier == 3


def test_parse_ability_defaults_to_d20() -> None:
    instruction = parse_dice_expression("str")
    assert instruction.count == 1
    assert instruction.sides == 20
    assert instruction.ability == "str"


def test_roll_instruction_advantage() -> None:
    instruction = parse_dice_expression("1d20adv+2")
    result = roll_instruction(instruction, ability_modifier=1, rng=_FixedRng())
    assert result.total == max(5, 12) + 2 + 1
    assert len(result.rolls) == 2


def test_invalid_expression_raises() -> None:
    try:
        parse_dice_expression("not-a-die")
    except DiceParseError:
        return
    raise AssertionError("Expected DiceParseError")


class _FixedRng:
    """Deterministic RNG returning preset d20 rolls."""

    def __init__(self) -> None:
        self.values = [5, 12]

    def randint(self, _a: int, _b: int) -> int:
        return self.values.pop(0)
