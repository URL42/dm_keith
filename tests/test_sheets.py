"""Character sheets: export, import, and what happens if you edit one."""

from __future__ import annotations

import json

import pytest

from src.game.genres import FANTASY
from src.game.sheets import (
    MAX_ITEM_STAT_MOD,
    MAX_ITEMS,
    SHEET_VERSION,
    SheetError,
    parse_sheet,
    render_sheet,
)
from src.storage.repo import Character, Repo


async def _kitted_out(repo: Repo, hero: Character) -> Character:
    await repo.grant_xp(hero.id, 950)
    await repo.add_item(hero.id, "Worn longsword", equippable=True, stat_mods={"str": 1})
    await repo.set_equipped(hero.id, "Worn longsword", True)
    await repo.add_item(hero.id, "Rations", quantity=3)
    character = await repo.get_character_by_id(hero.id)
    assert character is not None
    return character


def _sheet(character: Character, achievements: list[str] | None = None) -> str:
    earned = achievements or []
    return render_sheet(character, earned, FANTASY, {a: a.title() for a in earned})


async def test_a_sheet_round_trips(repo: Repo, hero: Character) -> None:
    character = await _kitted_out(repo, hero)
    imported = parse_sheet(_sheet(character, ["door-tax"]))

    assert imported.name == "Thorn"
    assert imported.xp == character.xp
    assert imported.level == character.level
    assert imported.abilities == character.abilities
    assert imported.achievements == ["door-tax"]
    assert not imported.edited

    carried = {i.name: i for i in imported.items}
    assert carried["Rations"].quantity == 3
    assert carried["Worn longsword"].equipped
    assert carried["Worn longsword"].stat_mods == {"str": 1}


async def test_the_readable_half_is_actually_readable(repo: Repo, hero: Character) -> None:
    """The point of Markdown over JSON: you can open it and see your character."""
    character = await _kitted_out(repo, hero)
    text = _sheet(character)

    assert text.startswith("# Thorn")
    assert "Worn longsword" in text
    assert "```json" in text

    # The prose shows what you're actually rolling with -- 14 base plus the +1 from
    # the sword in your hand. The JSON stores the base, so re-importing doesn't bake
    # the bonus in and then apply it again.
    assert "Strength 15 (+2)" in text
    assert '"str": 14' in text


async def test_editing_the_sheet_is_noticed(repo: Repo, hero: Character) -> None:
    character = await _kitted_out(repo, hero)
    text = _sheet(character)

    assert not parse_sheet(text).edited

    tampered = text.replace('"xp": 950', '"xp": 60000')
    imported = parse_sheet(tampered)
    assert imported.edited
    assert imported.xp == 60000  # it still imports -- it's their game


async def test_an_unedited_sheet_is_never_flagged(repo: Repo, hero: Character) -> None:
    """A false accusation would be worse than missing a real one."""
    character = await _kitted_out(repo, hero)
    for achievements in ([], ["door-tax"], ["door-tax", "poked-it"]):
        assert not parse_sheet(_sheet(character, achievements)).edited


async def test_level_is_derived_not_trusted(repo: Repo, hero: Character) -> None:
    """A sheet can't claim level 9 on 40 XP."""
    character = await _kitted_out(repo, hero)
    text = _sheet(character).replace('"level": 3', '"level": 9')

    imported = parse_sheet(text)
    assert imported.level == 3  # what 950 XP actually buys


@pytest.mark.parametrize(
    ("edit", "check"),
    [
        ('"str": 14', '"str": 9999'),
        ('"str": 14', '"str": -50'),
    ],
)
async def test_abilities_are_clamped(repo: Repo, hero: Character, edit: str, check: str) -> None:
    character = await _kitted_out(repo, hero)
    imported = parse_sheet(_sheet(character).replace(edit, check))
    assert 3 <= imported.abilities["str"] <= 20


async def test_item_bonuses_are_clamped(repo: Repo, hero: Character) -> None:
    character = await _kitted_out(repo, hero)
    text = _sheet(character).replace('"str": 1\n', '"str": 99\n')
    imported = parse_sheet(text)

    blade = next(i for i in imported.items if i.name == "Worn longsword")
    assert blade.stat_mods is not None
    assert blade.stat_mods["str"] <= MAX_ITEM_STAT_MOD


async def test_a_hoard_of_items_is_bounded(repo: Repo, hero: Character) -> None:
    character = await _kitted_out(repo, hero)
    payload = json.loads(_sheet(character).split("```json\n")[1].split("\n```")[0])
    payload["items"] = [{"name": f"Rock {i}"} for i in range(500)]

    text = f"# x\n\n```json\n{json.dumps(payload)}\n```\n"
    assert len(parse_sheet(text).items) <= MAX_ITEMS


async def test_unknown_achievements_are_dropped(repo: Repo, hero: Character) -> None:
    """A sheet shouldn't be able to invent achievements that don't exist."""
    from src.achievements.runtime import load_registry

    character = await _kitted_out(repo, hero)
    text = _sheet(character, ["door-tax"]).replace(
        '"door-tax"', '"door-tax", "legendary-god-slayer"'
    )
    imported = parse_sheet(text, known_achievements=set(load_registry()))
    assert imported.achievements == ["door-tax"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("just some prose, no data at all", "can't find the character data"),
        ("```json\n{not json}\n```", "malformed"),
        ('```json\n{"version": 99}\n```', "version"),
        ('```json\n{"version": 1, "name": "  "}\n```', "no name"),
    ],
)
def test_rubbish_files_are_rejected_with_a_reason(text: str, expected: str) -> None:
    with pytest.raises(SheetError, match=expected):
        parse_sheet(text)


def test_an_enormous_file_is_rejected() -> None:
    with pytest.raises(SheetError, match="far too big"):
        parse_sheet("x" * (300 * 1024))


async def test_abilities_survive_a_change_of_genre(repo: Repo, hero: Character) -> None:
    """The reason abilities are stored under canonical keys.

    A Fantasy character's Strength becomes a cyberpunk character's Muscle with no
    conversion at all -- only the label the genre puts on it changes.
    """
    from dataclasses import replace

    character = await _kitted_out(repo, hero)
    cyberpunk = replace(
        FANTASY,
        key="custom:cyberpunk",
        label="Cyberpunk",
        ability_names={**FANTASY.ability_names, "str": "Muscle"},
    )

    imported = parse_sheet(_sheet(character))
    assert imported.abilities["str"] == 14
    assert cyberpunk.ability_label("str") == "Muscle"


def test_the_version_is_recorded(repo: Repo) -> None:
    """Old sheets should fail loudly rather than being half-read."""
    with pytest.raises(SheetError, match="version"):
        parse_sheet(f'```json\n{{"version": {SHEET_VERSION + 1}, "name": "x"}}\n```')
