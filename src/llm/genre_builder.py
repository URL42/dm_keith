"""Building a genre on demand.

Hand-authoring archetypes and origins for every setting anyone might want doesn't
scale, and a fixed menu is a poor answer to "can we play cyberpunk". So anything
that isn't a built-in genre gets generated once, at /newgame, and stored on the
campaign row -- the six-ability chassis underneath never changes, only its names.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from src.game.characters import ABILITY_KEYS
from src.game.genres import Archetype, Genre, Origin, StartingItem
from src.log import get_logger

log = get_logger(__name__)

MAX_GENRE_NAME = 60
#: Fewer than this and the setting isn't worth playing -- better to say so than to
#: open a campaign with two archetypes in it.
MIN_OPTIONS = 3
#: Someone is watching a "Building a … setting" message while this runs.
GENRE_TIMEOUT_SECONDS = 90
GENRE_LIMITS = UsageLimits(request_limit=4)


class GeneratedItem(BaseModel):
    name: str
    kind: str = "gear"
    description: str = ""


class GeneratedArchetype(BaseModel):
    """A "class" for this setting."""

    name: str
    blurb: str = Field(description="One witty sentence. Punch up, never down.")
    priority: list[str] = Field(
        description=(
            "All six ability keys (str, dex, con, int, wis, cha) in order, best "
            "first, for this archetype."
        )
    )
    items: list[GeneratedItem] = Field(
        description="Exactly three pieces of starting kit. The first is their signature tool."
    )

    @field_validator("priority")
    @classmethod
    def _valid_priority(cls, value: list[str]) -> list[str]:
        seen = [a.lower() for a in value if a.lower() in ABILITY_KEYS]
        # Tolerate a short or duplicated list rather than failing the whole campaign;
        # anything missing goes on the end in canonical order. Duplicates matter --
        # assign_standard_array indexes positionally and would run off the end.
        ordered: list[str] = []
        for ability in seen:
            if ability not in ordered:
                ordered.append(ability)
        ordered += [a for a in ABILITY_KEYS if a not in ordered]
        if ordered != [a.lower() for a in value]:
            # Silently repairing means a "Netrunner" can end up with 15 Muscle and
            # nothing in the logs to say why.
            log.warning("repaired an archetype's ability priority: %r -> %r", value, ordered)
        return ordered


class GeneratedOrigin(BaseModel):
    """A "race" or background for this setting."""

    name: str
    blurb: str = Field(description="One witty sentence.")


class GenreSkin(BaseModel):
    """Everything that makes a setting feel like itself."""

    label: str = Field(description="The genre's proper name, e.g. 'Cyberpunk'.")
    emoji: str = Field(description="A single emoji for the genre.")
    pitch: str = Field(description="One sentence selling the setting, with a joke in it.")
    tone_note: str = Field(description="One sentence on how this setting should feel to play.")
    ability_names: dict[str, str] = Field(
        description=(
            "What this setting calls each of str, dex, con, int, wis, cha. Keys must "
            "be exactly those six. Rename them to fit -- a cyberpunk game might use "
            "Muscle, Reflex, Endurance, Tech, Instinct, Cool."
        )
    )
    archetypes: list[GeneratedArchetype] = Field(description="Exactly five, all distinct.")
    origins: list[GeneratedOrigin] = Field(description="Exactly five, all distinct.")

    @field_validator("ability_names")
    @classmethod
    def _complete_ability_names(cls, value: dict[str, str]) -> dict[str, str]:
        names = {k.lower(): v for k, v in value.items() if k.lower() in ABILITY_KEYS}
        missing = [key for key in ABILITY_KEYS if key not in names]
        if missing:
            # A partial answer still produces a playable genre, but a cyberpunk
            # setting quietly keeping "WIS" is worth seeing in the logs.
            log.warning("genre skin left these abilities unnamed: %s", ", ".join(missing))
        return {key: names.get(key, key.upper()) for key in ABILITY_KEYS}

    def to_genre(self, key: str) -> Genre:
        return Genre(
            key=key,
            label=self.label,
            emoji=self.emoji[:2] or "🎲",
            pitch=self.pitch,
            ability_names=self.ability_names,
            archetypes=tuple(
                Archetype(
                    name=a.name,
                    blurb=a.blurb,
                    priority=tuple(a.priority),
                    starting_items=tuple(
                        StartingItem(
                            name=item.name,
                            kind=item.kind,
                            description=item.description,
                            equippable=index == 0,
                            equip_at_creation=index == 0,
                            stat_mods={a.priority[0]: 1} if index == 0 else None,
                        )
                        for index, item in enumerate(a.items[:3])
                    ),
                )
                for a in self.archetypes[:5]
            ),
            origins=tuple(Origin(name=o.name, blurb=o.blurb) for o in self.origins[:5]),
            tone_note=self.tone_note,
        )


genre_agent = Agent(
    output_type=GenreSkin,
    retries=2,
    instructions=(
        "You build settings for a comedic tabletop roleplaying game.\n\n"
        "Given a genre, invent five archetypes (its answer to character classes) and "
        "five origins (its answer to species or backgrounds), and rename the six "
        "abilities to suit. Everything should be recognisable to someone who knows "
        "the genre, and funny in the same dry, affectionate way the rest of the game "
        "is funny -- roast the tropes, never the player.\n\n"
        "Invent your own material: no names, characters or proper nouns from existing "
        "books, films or games. Keep it PG-13."
    ),
)


class GenreGenerationFailed(RuntimeError):
    """The model couldn't produce a usable setting."""


def genre_key_for(request: str) -> str:
    """A stable key for a requested genre, e.g. 'Deep Space Horror' -> 'custom:deep-space-horror'."""
    slug = "-".join(request.lower().split())[:40]
    return f"custom:{slug}"


async def build_genre(
    request: str, model: Model | str, settings: ModelSettings | None = None
) -> Genre:
    """Generate a playable genre from a free-text request."""
    request = request.strip()[:MAX_GENRE_NAME]
    if not request:
        raise GenreGenerationFailed("No genre given.")

    try:
        result = await asyncio.wait_for(
            genre_agent.run(
                f"Build the setting for a {request} campaign.",
                model=model,
                model_settings=settings,
                usage_limits=GENRE_LIMITS,
            ),
            timeout=GENRE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # Without the timeout a hung provider leaves "Building a … setting" on
        # screen forever, with no way back to the menu.
        raise GenreGenerationFailed(str(exc)) from exc

    genre = result.output.to_genre(genre_key_for(request))
    if len(genre.archetypes) < MIN_OPTIONS or len(genre.origins) < MIN_OPTIONS:
        raise GenreGenerationFailed(
            f"Only got {len(genre.archetypes)} archetypes and {len(genre.origins)} origins."
        )

    log.info(
        "generated genre %r: %s archetypes, %s origins",
        genre.label,
        len(genre.archetypes),
        len(genre.origins),
    )
    return genre
