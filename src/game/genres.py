"""Genre skins.

Every genre runs on the same six-ability chassis (see game/characters.py). A genre
only changes what those abilities are *called* and what kind of characters exist,
so a campaign can be re-skinned without touching a single stored score.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from src.game.characters import ABILITY_KEYS
from src.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class StartingItem:
    """A piece of kit a character begins with.

    D&D hands out equipment by class and background; this is the same idea, kept
    light. `equip_at_creation` picks the one thing they're holding when play starts.
    """

    name: str
    kind: str = "gear"
    description: str = ""
    equippable: bool = False
    equip_at_creation: bool = False
    stat_mods: dict[str, int] | None = None


@dataclass(frozen=True)
class Archetype:
    """A genre's answer to "class"."""

    name: str
    blurb: str
    #: Abilities in priority order; the standard array is dealt out down this list.
    priority: tuple[str, ...]
    starting_items: tuple[StartingItem, ...] = ()


@dataclass(frozen=True)
class Origin:
    """A genre's answer to "race" or "background"."""

    name: str
    blurb: str


@dataclass(frozen=True)
class Genre:
    key: str
    label: str
    #: Telegram truncates inline button text to whatever fits on one line, so the
    #: button gets the emoji and label only; the pitch goes in the message body.
    emoji: str
    pitch: str
    #: Canonical ability key -> what this genre calls it.
    ability_names: dict[str, str]
    archetypes: tuple[Archetype, ...]
    origins: tuple[Origin, ...]
    tone_note: str = ""

    @property
    def button(self) -> str:
        return f"{self.emoji} {self.label}"

    def ability_label(self, key: str) -> str:
        return self.ability_names.get(key, key.upper())

    def find_archetype(self, name: str) -> Archetype | None:
        return next((a for a in self.archetypes if a.name.lower() == name.lower()), None)


FANTASY = Genre(
    key="fantasy",
    label="Fantasy",
    emoji="🗡",
    pitch="Swords, spellbooks and structurally unsound dungeons.",
    ability_names={
        "str": "Strength",
        "dex": "Dexterity",
        "con": "Constitution",
        "int": "Intelligence",
        "wis": "Wisdom",
        "cha": "Charisma",
    },
    archetypes=(
        Archetype(
            "Fighter",
            "Solves problems with a sword. Solves other problems with a bigger sword.",
            ("str", "con", "dex", "wis", "cha", "int"),
            (
                StartingItem(
                    "Worn longsword",
                    "weapon",
                    "Notched, reliable, and heavier than it looks.",
                    equippable=True,
                    equip_at_creation=True,
                    stat_mods={"str": 1},
                ),
                StartingItem(
                    "Dented shield", "armour", "It has stopped things before.", equippable=True
                ),
                StartingItem("Rations", "supply", "Three days of aggressively beige food."),
            ),
        ),
        Archetype(
            "Rogue",
            "Professionally sneaky. Allegedly reformed.",
            ("dex", "cha", "int", "con", "wis", "str"),
            (
                StartingItem(
                    "Twin daggers",
                    "weapon",
                    "Quick, quiet, and easy to explain away.",
                    equippable=True,
                    equip_at_creation=True,
                    stat_mods={"dex": 1},
                ),
                StartingItem(
                    "Lockpicks", "tool", "For doors that were being unreasonable.", equippable=True
                ),
                StartingItem("Suspiciously heavy purse", "treasure", "Best not to ask."),
            ),
        ),
        Archetype(
            "Wizard",
            "Has read every book in the tower. Has fought approximately nothing.",
            ("int", "wis", "con", "dex", "cha", "str"),
            (
                StartingItem(
                    "Gnarled staff",
                    "implement",
                    "A focus for spellwork, and a serviceable walking stick.",
                    equippable=True,
                    equip_at_creation=True,
                    stat_mods={"int": 1},
                ),
                StartingItem(
                    "Spellbook", "implement", "Everything you know, and several things you don't."
                ),
                StartingItem("Component pouch", "supply", "Bat guano, chalk, and worse."),
            ),
        ),
        Archetype(
            "Cleric",
            "On speaking terms with something enormous.",
            ("wis", "con", "str", "cha", "int", "dex"),
            (
                StartingItem(
                    "Holy symbol",
                    "implement",
                    "A direct line, allegedly.",
                    equippable=True,
                    equip_at_creation=True,
                    stat_mods={"wis": 1},
                ),
                StartingItem("Mace", "weapon", "Blunt theology.", equippable=True),
                StartingItem("Bandages", "supply", "For when faith runs late."),
            ),
        ),
        Archetype(
            "Bard",
            "Talks first, thinks later, rhymes throughout.",
            ("cha", "dex", "int", "con", "wis", "str"),
            (
                StartingItem(
                    "Lute",
                    "implement",
                    "Slightly out of tune, permanently.",
                    equippable=True,
                    equip_at_creation=True,
                    stat_mods={"cha": 1},
                ),
                StartingItem("Rapier", "weapon", "Mostly for punctuation.", equippable=True),
                StartingItem(
                    "Book of terrible poetry", "treasure", "Your own. Unpublished, mercifully."
                ),
            ),
        ),
    ),
    origins=(
        Origin("Human", "Ambitious, adaptable, alarmingly short-lived."),
        Origin("Elf", "Elegant, ancient, insufferable about it."),
        Origin("Dwarf", "Stubborn as bedrock and twice as hard to move."),
        Origin("Halfling", "Small, lucky, and consistently underestimated."),
        Origin("Orc", "Built like a door and just as direct."),
        Origin("Tiefling", "Infernal heritage, mundane problems."),
    ),
    tone_note="Classic sword-and-sorcery, played for adventure and comedy.",
)

#: Registered genres. M3 adds sci-fi, horror, western and custom generation.
GENRES: dict[str, Genre] = {FANTASY.key: FANTASY}

DEFAULT_GENRE = FANTASY


def get_genre(key: str) -> Genre:
    """Look up a built-in genre, falling back to fantasy for anything unrecognised."""
    return GENRES.get(key.lower(), DEFAULT_GENRE)


def genre_to_dict(genre: Genre) -> dict[str, Any]:
    """Serialise a genre for storage on the campaign row."""
    return asdict(genre) | {"key": genre.key}


def _priority(raw: Any) -> tuple[str, ...]:
    """A complete, de-duplicated ability order.

    `assign_standard_array` walks this positionally, so a stored list with a
    repeat would run off the end of the array and crash /join.
    """
    ordered: list[str] = []
    for ability in raw or ():
        key = str(ability).lower()
        if key in ABILITY_KEYS and key not in ordered:
            ordered.append(key)
    ordered += [a for a in ABILITY_KEYS if a not in ordered]
    return tuple(ordered)


def _starting_item(raw: dict[str, Any]) -> StartingItem:
    """Build an item from stored JSON, ignoring fields we no longer know about.

    Skins are written once and read for the life of a campaign, so a row written by
    an older version has to keep working -- otherwise adding a field here would
    re-skin every live custom campaign back to fantasy mid-play.
    """
    known = {f.name for f in fields(StartingItem)}
    return StartingItem(**{k: v for k, v in raw.items() if k in known})


def genre_from_dict(raw: dict[str, Any]) -> Genre:
    """Rebuild a stored genre. Raises KeyError/TypeError on anything malformed."""
    return Genre(
        key=raw["key"],
        label=raw["label"],
        emoji=raw.get("emoji", "🎲"),
        pitch=raw.get("pitch", ""),
        ability_names=dict(raw["ability_names"]),
        archetypes=tuple(
            Archetype(
                name=a["name"],
                blurb=a.get("blurb", ""),
                priority=_priority(a.get("priority", ABILITY_KEYS)),
                starting_items=tuple(_starting_item(i) for i in a.get("starting_items", ())),
            )
            for a in raw["archetypes"]
        ),
        origins=tuple(Origin(name=o["name"], blurb=o.get("blurb", "")) for o in raw["origins"]),
        tone_note=raw.get("tone_note", ""),
    )


def genre_for(campaign: Any) -> Genre:
    """The genre a campaign is actually being played in.

    Generated genres live on the campaign row, so a campaign keeps the setting it
    was created with even though it isn't in the built-in table.
    """
    skin = getattr(campaign, "genre_skin", None)
    if skin:
        try:
            return genre_from_dict(skin)
        except (KeyError, TypeError, ValueError):
            log.warning("campaign %s has an unreadable genre skin; falling back", campaign.id)
    return get_genre(campaign.genre)


def render_ability_block(genre: Genre, scores: dict[str, int], modifiers: dict[str, int]) -> str:
    """One line per ability, using this genre's names."""
    return "\n".join(
        f"  {genre.ability_label(key)}: {scores[key]} ({modifiers[key]:+d})" for key in ABILITY_KEYS
    )
