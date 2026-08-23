"""Exporting and importing character sheets.

A sheet is a Markdown file you can read, print or paste into a wiki, with a fenced
JSON block at the end carrying the exact data. Prose is lovely for humans and awful
to parse -- "HP 14/18" is unambiguous right up until a concept line contains a
slash -- so the JSON is the source of truth and the prose is for you.

Everything arriving from a file is untrusted: it has been round the outside world
and may have been edited on the way. `parse_sheet` validates and clamps every field
rather than trusting any of it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from src.game.characters import (
    ABILITY_KEYS,
    MAX_LEVEL,
    XP_THRESHOLDS,
    ability_modifier,
    clamp_ability,
    level_from_xp,
    normalise_abilities,
)
from src.game.genres import Genre

#: Bumped if the payload shape ever changes incompatibly.
SHEET_VERSION = 1

MAX_NAME = 60
MAX_CONCEPT = 400
MAX_ITEMS = 50
MAX_ITEM_QUANTITY = 999
MAX_ITEM_STAT_MOD = 3
#: A character sheet is a couple of kilobytes. Anything larger isn't one.
MAX_SHEET_BYTES = 256 * 1024

JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


class SheetError(ValueError):
    """The uploaded file isn't a character sheet we can read."""


@dataclass
class ImportedItem:
    name: str
    kind: str = "gear"
    description: str = ""
    quantity: int = 1
    equippable: bool = False
    equipped: bool = False
    consumable: bool = False
    stat_mods: dict[str, int] | None = None


@dataclass
class ImportedCharacter:
    """A character read back off a sheet, already validated and clamped."""

    name: str
    concept: str
    level: int
    xp: int
    abilities: dict[str, int]
    items: list[ImportedItem] = field(default_factory=list)
    achievements: list[str] = field(default_factory=list)
    from_campaign: str = ""
    #: True when the file's contents no longer match its own checksum.
    edited: bool = False


def _checksum(payload: dict[str, Any]) -> str:
    """A short digest of everything except the digest itself.

    Deliberately not a secret: anyone reading this file could recompute it. It
    exists to notice a hand-edited sheet, not to prevent one.
    """
    body = {k: v for k, v in payload.items() if k != "checksum"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def build_payload(character: Any, achievements: list[str], genre: Genre) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": SHEET_VERSION,
        "name": character.name,
        "concept": character.concept,
        "level": character.level,
        "xp": character.xp,
        "abilities": dict(character.abilities),
        "items": [
            {
                "name": i.name,
                "kind": i.kind,
                "description": i.description,
                "quantity": i.quantity,
                "equippable": i.equippable,
                "equipped": i.equipped,
                "consumable": i.consumable,
                "stat_mods": i.stat_mods or None,
            }
            for i in character.items
        ],
        "achievements": achievements,
        "from_campaign": genre.label,
    }
    payload["checksum"] = _checksum(payload)
    return payload


def render_sheet(
    character: Any, achievements: list[str], genre: Genre, titles: dict[str, str]
) -> str:
    """The whole file: a readable sheet, then the data that actually gets imported."""
    payload = build_payload(character, achievements, genre)
    scores = character.effective_abilities()

    abilities = " · ".join(
        f"{genre.ability_label(k)} {scores[k]} ({ability_modifier(scores[k]):+d})"
        for k in ABILITY_KEYS
    )

    if character.items:
        carried = ", ".join(
            f"{i.name}"
            + (f" x{i.quantity}" if i.quantity > 1 else "")
            + (" *(equipped)*" if i.equipped else "")
            for i in character.items
        )
    else:
        carried = "nothing"

    earned = " · ".join(f"🏆 {titles.get(a, a)}" for a in achievements) or "none yet"

    return "\n".join(
        [
            f"# {character.name}",
            "",
            f"*Level {character.level} {character.origin} {character.archetype}"
            f"{' — ' + character.concept if character.concept else ''}*",
            "",
            f"**HP** {character.hp}/{character.max_hp}  **XP** {character.xp}",
            "",
            abilities,
            "",
            f"**Carrying:** {carried}",
            "",
            f"**Earned:** {earned}",
            "",
            f"_Last played in a {genre.label} campaign._",
            "",
            "---",
            "",
            "<!-- Everything below is what gets imported. Edit it if you like; the -->",
            "<!-- bot will notice, and will have something to say about it.        -->",
            "",
            "```json",
            json.dumps(payload, indent=2),
            "```",
            "",
        ]
    )


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _parse_items(raw: Any) -> list[ImportedItem]:
    items: list[ImportedItem] = []
    for entry in (raw or [])[:MAX_ITEMS]:
        if not isinstance(entry, dict):
            continue
        name = _clean_text(entry.get("name"), MAX_NAME)
        if not name:
            continue

        mods: dict[str, int] = {}
        for key, value in (entry.get("stat_mods") or {}).items():
            if key in ABILITY_KEYS:
                try:
                    mods[key] = max(-MAX_ITEM_STAT_MOD, min(MAX_ITEM_STAT_MOD, int(value)))
                except (TypeError, ValueError):
                    continue

        try:
            quantity = max(1, min(MAX_ITEM_QUANTITY, int(entry.get("quantity", 1))))
        except (TypeError, ValueError):
            quantity = 1

        items.append(
            ImportedItem(
                name=name,
                kind=_clean_text(entry.get("kind", "gear"), 30) or "gear",
                description=_clean_text(entry.get("description"), 200),
                quantity=quantity,
                equippable=bool(entry.get("equippable")),
                equipped=bool(entry.get("equipped")) and bool(entry.get("equippable")),
                consumable=bool(entry.get("consumable")),
                stat_mods=mods or None,
            )
        )
    return items


def parse_sheet(text: str, known_achievements: set[str] | None = None) -> ImportedCharacter:
    """Read a sheet back. Raises SheetError on anything that isn't one.

    Every value is clamped to a legal range: a sheet is a file that has been outside
    our control, and no amount of editing it should be able to produce a character
    the game can't represent.
    """
    if len(text.encode()) > MAX_SHEET_BYTES:
        raise SheetError("That file is far too big to be a character sheet.")

    match = JSON_BLOCK.search(text)
    if match is None:
        raise SheetError("I can't find the character data in that file.")

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SheetError("The character data in that file is malformed.") from exc

    if not isinstance(payload, dict):
        raise SheetError("The character data in that file is malformed.")
    if payload.get("version") != SHEET_VERSION:
        raise SheetError(
            f"That sheet is version {payload.get('version')!r}; I read version {SHEET_VERSION}."
        )

    name = _clean_text(payload.get("name"), MAX_NAME)
    if not name:
        raise SheetError("That sheet has no name on it.")

    try:
        xp = max(0, min(XP_THRESHOLDS[-1] * 2, int(payload.get("xp", 0))))
    except (TypeError, ValueError):
        xp = 0

    # Level is derived rather than trusted, so a sheet can't claim level 9 on 40 XP.
    level = min(MAX_LEVEL, level_from_xp(xp))

    abilities = normalise_abilities(
        {
            k: clamp_ability(int(v))
            for k, v in (payload.get("abilities") or {}).items()
            if k in ABILITY_KEYS and isinstance(v, (int, float))
        }
    )

    achievements = [
        a
        for a in (payload.get("achievements") or [])
        if isinstance(a, str) and (known_achievements is None or a in known_achievements)
    ]

    return ImportedCharacter(
        name=name,
        concept=_clean_text(payload.get("concept"), MAX_CONCEPT),
        level=level,
        xp=xp,
        abilities=abilities,
        items=_parse_items(payload.get("items")),
        achievements=achievements,
        from_campaign=_clean_text(payload.get("from_campaign"), 60),
        edited=payload.get("checksum") != _checksum(payload),
    )
