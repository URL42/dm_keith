"""Turning a played campaign into something readable.

The chronicler is Keith again, but writing rather than running: past tense, third
person, telling you about people he spent months trying to kill. Keeping his voice
rather than switching to a neutral narrator is deliberate — a book told *by the
dungeon* is the thing worth keeping, and it's consistent with the achievement blocks
that close each chapter.

Runs on DMK_SUMMARY_MODEL, so the game can stay on a cheap fast model while the book
gets a good one. Nothing is waiting on it, so it can afford to be slow.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from src.log import get_logger

log = get_logger(__name__)

#: Nobody is watching this, but a hung provider shouldn't wedge the command either.
CHAPTER_TIMEOUT_SECONDS = 180
CHAPTER_LIMITS = UsageLimits(request_limit=4)

VOICE = """
You are Dungeon Master Keith, writing up a campaign you ran as a book someone will
actually read — most likely one of the players, years later.

Write it as a story, not a transcript:

- Past tense, third person. Use the characters' names throughout.
- Keep your voice. You are wry, theatrical, fond of these people and entirely
  unwilling to admit it. You narrated their worst decisions live and you have not
  softened on any of them. A narrator with a personality is the point.
- **Strip the machinery out of the prose.** No dice, no target numbers, no hit
  points, no experience. "The pick snapped off in the lock and something on the
  other side stopped moving" — never "she rolled a 5 against DC 16". The numbers are
  recorded elsewhere; they are not the story.
- Keep what actually happened. Don't invent events, NPCs or outcomes that aren't in
  the material, and don't quietly improve anyone's decisions. The bad ideas are the
  good bits.
- Dialogue is welcome where the transcript supports it.
- PG-13, exactly as it was played. Parody-safe: no characters, quotes or lore from
  existing books, films or games.

Give every chapter a title with some character to it — not "Chapter Four" and not a
flat summary of the contents.
"""


class Chapter(BaseModel):
    title: str = Field(description="A few words with some flavour. No chapter number.")
    body: str = Field(description="The chapter itself, in Markdown paragraphs.")


class ChronicleFailed(RuntimeError):
    """The chronicler couldn't produce a chapter."""


#: Exactly one chapter per call, deliberately. The caller already splits a long
#: backlog into chapter-sized spans, so the model never needs to decide how many to
#: write -- and one chapter per span keeps a chapter's watermark unambiguous, which
#: everything downstream (grant ranges, the polish pass, redo) relies on.
chapter_agent = Agent(output_type=Chapter, retries=2, instructions=VOICE)


async def write_chapter(
    *,
    model: Model | str,
    settings: ModelSettings | None,
    story_so_far: str,
    established: str,
    party: str,
    transcript: str,
) -> Chapter:
    """Write up one span of play as one chapter."""
    prompt = "\n\n".join(
        part
        for part in [
            f"## The story so far\n\n{story_so_far}" if story_so_far else "",
            f"## Everything established to date\n\n{established}" if established else "",
            f"## The party\n\n{party}",
            f"## What happened next\n\n{transcript}",
            "Write one chapter covering *What happened next*. Do not retell the story "
            "so far — it is there for continuity, and the reader has already read it.",
        ]
        if part
    )

    try:
        result = await asyncio.wait_for(
            chapter_agent.run(
                prompt, model=model, model_settings=settings, usage_limits=CHAPTER_LIMITS
            ),
            timeout=CHAPTER_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise ChronicleFailed(str(exc)) from exc

    chapter = result.output
    if not chapter.body.strip():
        raise ChronicleFailed("The chronicler came back with nothing.")

    log.info("wrote %r, %s words", chapter.title, len(chapter.body.split()))
    return chapter


async def rewrite_chapter(
    *,
    model: Model | str,
    settings: ModelSettings | None,
    outline: str,
    established: str,
    party: str,
    chapter_number: int,
    original: str,
    transcript: str,
) -> Chapter:
    """Rewrite one chapter knowing how the whole campaign turned out.

    The incremental version can't foreshadow: chapter two was written before anyone
    knew chapter nine existed. This pass gives every chapter the full outline so the
    early ones can finally point forward.
    """
    prompt = "\n\n".join(
        [
            "## The whole campaign, in order\n\n" + outline,
            f"## Everything established\n\n{established}" if established else "",
            f"## The party\n\n{party}",
            f"## Chapter {chapter_number} as first written\n\n{original}",
            f"## The play this chapter covers\n\n{transcript}",
            f"Rewrite chapter {chapter_number}. It was written before you knew how any "
            "of this ended; now you do. Keep the same events and the same span of the "
            "story, but you may foreshadow what is coming and echo what came before. "
            "Return exactly one chapter.",
        ]
    )

    try:
        result = await asyncio.wait_for(
            chapter_agent.run(
                prompt, model=model, model_settings=settings, usage_limits=CHAPTER_LIMITS
            ),
            timeout=CHAPTER_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise ChronicleFailed(str(exc)) from exc

    chapter = result.output
    if not chapter.body.strip():
        raise ChronicleFailed(
            f"The chronicler came back with nothing for chapter {chapter_number}."
        )
    return chapter
