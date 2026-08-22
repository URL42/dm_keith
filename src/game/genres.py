"""Genre skins.

Every genre runs on the same six-ability chassis (see game/characters.py). A genre
only changes what those abilities are *called* and what kind of characters exist,
so a campaign can be re-skinned without touching a single stored score.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.game.characters import ABILITY_KEYS


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
    #: Shown on the /newgame keyboard.
    pitch: str
    #: Canonical ability key -> what this genre calls it.
    ability_names: dict[str, str]
    archetypes: tuple[Archetype, ...]
    origins: tuple[Origin, ...]
    tone_note: str = ""

    def ability_label(self, key: str) -> str:
        return self.ability_names.get(key, key.upper())

    def find_archetype(self, name: str) -> Archetype | None:
        return next((a for a in self.archetypes if a.name.lower() == name.lower()), None)


FANTASY = Genre(
    key="fantasy",
    label="Fantasy",
    pitch="🗡 Swords, spellbooks and structurally unsound dungeons.",
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
    """Look up a genre, falling back to fantasy for anything unrecognised."""
    return GENRES.get(key.lower(), DEFAULT_GENRE)


def render_ability_block(genre: Genre, scores: dict[str, int], modifiers: dict[str, int]) -> str:
    """One line per ability, using this genre's names."""
    return "\n".join(
        f"  {genre.ability_label(key)}: {scores[key]} ({modifiers[key]:+d})" for key in ABILITY_KEYS
    )
