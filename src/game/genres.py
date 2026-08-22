"""Genre skins.

Every genre runs on the same six-ability chassis (see game/characters.py). A genre
only changes what those abilities are *called* and what kind of characters exist,
so a campaign can be re-skinned without touching a single stored score.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.game.characters import ABILITY_KEYS


@dataclass(frozen=True)
class Archetype:
    """A genre's answer to "class"."""

    name: str
    blurb: str
    #: Abilities in priority order; the standard array is dealt out down this list.
    priority: tuple[str, ...]
    starting_items: tuple[str, ...] = ()


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
            ("Worn longsword", "Dented shield", "Rations"),
        ),
        Archetype(
            "Rogue",
            "Professionally sneaky. Allegedly reformed.",
            ("dex", "cha", "int", "con", "wis", "str"),
            ("Twin daggers", "Lockpicks", "Suspiciously heavy purse"),
        ),
        Archetype(
            "Wizard",
            "Has read every book in the tower. Has fought approximately nothing.",
            ("int", "wis", "con", "dex", "cha", "str"),
            ("Spellbook", "Gnarled staff", "Component pouch"),
        ),
        Archetype(
            "Cleric",
            "On speaking terms with something enormous.",
            ("wis", "con", "str", "cha", "int", "dex"),
            ("Holy symbol", "Mace", "Bandages"),
        ),
        Archetype(
            "Bard",
            "Talks first, thinks later, rhymes throughout.",
            ("cha", "dex", "int", "con", "wis", "str"),
            ("Lute", "Rapier", "Book of terrible poetry"),
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
