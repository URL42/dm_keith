"""Dice parsing and rolling helpers."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Optional

DICE_PATTERN = re.compile(r"^(?P<count>\d*)d(?P<sides>\d+)(?P<adv>adv|dis)?(?P<rest>.*)$")
MODIFIER_PATTERN = re.compile(r"([+\-]\s*\d+)")
ABILITY_PATTERN = re.compile(r"\b(str|dex|con|int|wis|cha)\b")


@dataclass(frozen=True)
class DiceInstruction:
    count: int
    sides: int
    modifier: int = 0
    advantage: int = 0  # 1 advantage, -1 disadvantage, 0 normal
    ability: Optional[str] = None


@dataclass(frozen=True)
class RollResult:
    instruction: DiceInstruction
    rolls: list[int]
    kept: list[int]
    ability_modifier: int
    total: int


class DiceParseError(ValueError):
    pass


def parse_dice_expression(expression: str) -> DiceInstruction:
    text = expression.strip().lower()
    if not text:
        raise DiceParseError("Provide a dice expression like '1d20+3' or 'str'.")

    # Pure ability check -> assume 1d20
    ability_match = ABILITY_PATTERN.search(text)
    if ability_match and "d" not in text:
        ability = ability_match.group(1)
        modifier = _extract_modifier(text.replace(ability, ""))
        return DiceInstruction(count=1, sides=20, modifier=modifier, advantage=0, ability=ability)

    match = DICE_PATTERN.match(text)
    if not match:
        # fallback ability pattern even if contains extra tokens
        if ability_match:
            ability = ability_match.group(1)
            modifier = _extract_modifier(text.replace(ability, ""))
            return DiceInstruction(count=1, sides=20, modifier=modifier, advantage=0, ability=ability)
        raise DiceParseError(f"Could not parse dice expression: {expression!r}")

    count = int(match.group("count") or 1)
    sides = int(match.group("sides"))
    adv_token = match.group("adv")
    rest = match.group("rest") or ""
    advantage = 0
    if adv_token == "adv":
        advantage = 1
    elif adv_token == "dis":
        advantage = -1

    ability = None
    ability_match = ABILITY_PATTERN.search(rest)
    if ability_match:
        ability = ability_match.group(1)
        rest = rest.replace(ability, "")

    modifier = _extract_modifier(rest)
    return DiceInstruction(count=count, sides=sides, modifier=modifier, advantage=advantage, ability=ability)


def roll_instruction(
    instruction: DiceInstruction,
    *,
    ability_modifier: int = 0,
    rng: Optional[random.Random] = None,
) -> RollResult:
    rng = rng or random.Random()
    rolls: list[int] = []
    kept: list[int] = []

    if instruction.advantage != 0 and instruction.count == 1 and instruction.sides == 20:
        first = rng.randint(1, instruction.sides)
        second = rng.randint(1, instruction.sides)
        rolls.extend([first, second])
        kept_value = max(first, second) if instruction.advantage == 1 else min(first, second)
        kept.append(kept_value)
    else:
        for _ in range(instruction.count):
            value = rng.randint(1, instruction.sides)
            rolls.append(value)
            kept.append(value)

    total = sum(kept) + instruction.modifier + ability_modifier
    return RollResult(
        instruction=instruction,
        rolls=rolls,
        kept=kept,
        ability_modifier=ability_modifier,
        total=total,
    )


def _extract_modifier(text: str) -> int:
    modifier = 0
    for match in MODIFIER_PATTERN.findall(text.replace(" ", "")):
        modifier += int(match)
    return modifier


__all__ = [
    "DiceInstruction",
    "RollResult",
    "DiceParseError",
    "parse_dice_expression",
    "roll_instruction",
]
