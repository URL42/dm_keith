"""Character sheet maths: abilities, modifiers, HP, XP and levels.

Pure functions only -- no database, no I/O. Everything that mutates a character
goes through storage/repo.py; this module just answers "what should the numbers be".
"""

from __future__ import annotations

import random

#: Canonical ability keys. Genres rename them for display (see game/genres.py) but
#: the stored keys never change, so a campaign can be re-skinned without migration.
ABILITY_KEYS: tuple[str, ...] = ("str", "dex", "con", "int", "wis", "cha")

#: XP required to *reach* each level. Index 0 is level 1. Ported from the old engine.
XP_THRESHOLDS: tuple[int, ...] = (0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000)

MAX_LEVEL = len(XP_THRESHOLDS)
#: 3 is the floor 4d6-drop-lowest can produce, so nothing -- curse, cursed item or
#: critical failure -- should ever push a character below what the dice could roll.
MIN_ABILITY = 3
MAX_ABILITY = 20

#: The classic point-buy-ish spread, assigned to a character's priority order.
STANDARD_ARRAY: tuple[int, ...] = (15, 14, 13, 12, 10, 8)


def ability_modifier(score: int) -> int:
    """D&D's modifier curve: 10-11 is +0, every 2 points is +/-1."""
    return (score - 10) // 2


def level_from_xp(xp: int) -> int:
    """Highest level whose threshold the XP total has reached."""
    level = 1
    for index, threshold in enumerate(XP_THRESHOLDS, start=1):
        if xp >= threshold:
            level = index
        else:
            break
    return min(level, MAX_LEVEL)


def xp_to_next_level(xp: int) -> int | None:
    """XP still needed for the next level, or None at max level."""
    level = level_from_xp(xp)
    if level >= MAX_LEVEL:
        return None
    return XP_THRESHOLDS[level] - xp


def max_hp_for(level: int, con_score: int) -> int:
    """HP grows with level and constitution. Rules-light on purpose."""
    con_mod = ability_modifier(con_score)
    return max(1, 10 + con_mod * 2 + (level - 1) * (5 + con_mod))


#: XP per point of DC when a check succeeds, and when it fails. Failure still pays
#: something -- a snapped lockpick teaches you as much as an opened door, and a run
#: of bad luck shouldn't stall progression entirely.
XP_PER_DC_SUCCESS = 4
XP_PER_DC_FAILURE = 2


def xp_for_check(dc: int, success: bool) -> int:
    """XP for attempting a check of difficulty `dc`.

    The engine awards this, rather than leaving it to the DM. Two different models
    both narrated whole sessions -- quests, fights, failures -- without ever calling
    grant_xp, so nobody could level. Tying the award to a resolved check keeps the
    pacing tied to real obstacles instead of a timer, and it can't be forgotten.

    A DC 15 check pays 60 on success, 30 on failure; 300 XP reaches level 2.
    """
    rate = XP_PER_DC_SUCCESS if success else XP_PER_DC_FAILURE
    return max(1, dc * rate)


def clamp_ability(score: int) -> int:
    return max(MIN_ABILITY, min(MAX_ABILITY, score))


def default_abilities() -> dict[str, int]:
    return dict.fromkeys(ABILITY_KEYS, 10)


def assign_standard_array(priority: list[str] | tuple[str, ...]) -> dict[str, int]:
    """Hand out STANDARD_ARRAY best-first down a priority order.

    Abilities missing from `priority` get whatever is left over, so a partial or
    empty priority list is still valid.
    """
    ordered = [a for a in priority if a in ABILITY_KEYS]
    ordered += [a for a in ABILITY_KEYS if a not in ordered]
    return {ability: STANDARD_ARRAY[i] for i, ability in enumerate(ordered)}


def roll_abilities(rng: random.Random | None = None) -> dict[str, int]:
    """4d6-drop-lowest for each ability -- the reroll button behind /join."""
    rng = rng or random.Random()
    scores = {}
    for ability in ABILITY_KEYS:
        dice = sorted(rng.randint(1, 6) for _ in range(4))
        scores[ability] = sum(dice[1:])
    return scores


def normalise_abilities(raw: dict[str, int] | None) -> dict[str, int]:
    """Coerce whatever is in the database into a complete, clamped ability dict."""
    scores = default_abilities()
    for key, value in (raw or {}).items():
        if key in ABILITY_KEYS:
            scores[key] = clamp_ability(int(value))
    return scores
