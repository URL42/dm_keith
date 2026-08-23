"""Keeping the campaign's book up to date, and rendering it.

The book is stored as chapters and grows by appending. It is never regenerated
during play: a campaign worth keeping runs to tens of thousands of words, which
can't be re-novelised in one request at any price. Each `/chronicle` writes up only
what has happened since the last chapter's watermark.

The cost of that is honest and worth knowing: an early chapter was written before
anyone knew how the campaign ended, so it can't foreshadow. The final pass at
`/endgame` fixes exactly that, once the story is finite.
"""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from src.achievements.runtime import format_block, load_registry
from src.game.genres import genre_for
from src.game.memory import render_character, render_transcript
from src.llm.chronicler import Chapter as WrittenChapter
from src.llm.chronicler import rewrite_chapter, write_chapter
from src.log import get_logger
from src.storage.repo import Campaign, Chapter, Message, Repo

log = get_logger(__name__)

#: Below this, there isn't a chapter's worth of story yet -- calling /chronicle
#: twice in five minutes shouldn't produce a chapter about opening a door.
MIN_CHAPTER_MESSAGES = 12
#: A backlog longer than this is split, so one /chronicle after a long absence
#: produces a readable sequence rather than a single enormous chapter.
MAX_CHAPTER_MESSAGES = 80


async def _context(repo: Repo, campaign: Campaign) -> tuple[str, str]:
    """The established facts and party sheets every chapter is written against."""
    genre = genre_for(campaign)
    party = await repo.list_party(campaign.id)
    events = await repo.recent_events(campaign.id, limit=200)

    established = "\n".join(f"- [{e.kind}] {e.summary}" for e in events)
    sheets = "\n\n".join(render_character(c, genre, detailed=False) for c in party)
    return established, sheets


async def _story_so_far(repo: Repo, campaign_id: int) -> str:
    """Enough of the book for the next chapter to follow on from.

    Titles for shape, then the last chapter in full so the prose can pick up mid-scene
    without restating everything.
    """
    chapters = await repo.list_chapters(campaign_id)
    if not chapters:
        return ""

    lines = [f"{c.number}. {c.title}" for c in chapters]
    tail = chapters[-1]
    return (
        "Chapters so far:\n"
        + "\n".join(lines)
        + f"\n\nChapter {tail.number} in full:\n\n{tail.body}"
    )


def split_into_spans(messages: list[Message]) -> list[list[Message]]:
    """Break a backlog into chapter-sized spans, covering everything exactly once.

    A trailing remainder shorter than a chapter is folded into the span before it,
    so a long absence doesn't end with a chapter about somebody opening a door.
    """
    spans: list[list[Message]] = []
    index = 0
    while index < len(messages):
        span = messages[index : index + MAX_CHAPTER_MESSAGES]
        if 0 < len(messages) - (index + len(span)) < MIN_CHAPTER_MESSAGES:
            span = messages[index:]
        index += len(span)
        spans.append(span)
    return spans


async def catch_up(
    repo: Repo,
    campaign: Campaign,
    *,
    model: Model | str,
    settings: ModelSettings | None,
    force: bool = False,
) -> list[Chapter]:
    """Write chapters for everything that has happened since the last one.

    Each chapter is committed as it is produced, so a failure halfway through a long
    backlog keeps the chapters already written and the next call resumes from there.

    `force` writes a final short chapter regardless of length. Used at /endgame,
    where a handful of trailing messages is usually the climax -- leaving it out
    because it was short would omit the most important part of the book.
    """
    last = await repo.last_chapter(campaign.id)
    watermark = last.through_message_id if last else 0
    outstanding = await repo.messages_after(campaign.id, watermark)

    if not outstanding or (len(outstanding) < MIN_CHAPTER_MESSAGES and not force):
        return []

    established, party = await _context(repo, campaign)
    party_rows = await repo.list_party(campaign.id)
    written: list[Chapter] = []

    # Work forward in chapter-sized spans, each written knowing the ones before it.
    spans = split_into_spans(outstanding)
    for position, span in enumerate(spans):
        # Bound the grant watermark by where the *next* span starts, not where this
        # one ends: stamping every chapter with the campaign-wide maximum piles all
        # the achievements into chapter one, and using this span's last message drops
        # any award whose timestamp ticked over a second later.
        following = spans[position + 1] if position + 1 < len(spans) else None
        grant_id = await repo.latest_grant_id(
            campaign.id, before=following[0].created_at if following else None
        )

        chapter = await write_chapter(
            model=model,
            settings=settings,
            story_so_far=await _story_so_far(repo, campaign.id),
            established=established,
            party=party,
            transcript=render_transcript(span, party_rows),
        )
        written.append(
            await repo.add_chapter(campaign.id, chapter.title, chapter.body, span[-1].id, grant_id)
        )

    log.info("chronicle: wrote %s chapter(s) for campaign %s", len(written), campaign.id)
    return written


async def polish(
    repo: Repo,
    campaign: Campaign,
    *,
    model: Model | str,
    settings: ModelSettings | None,
) -> list[Chapter]:
    """Rewrite every chapter now that the campaign is finished.

    Written incrementally, chapter two couldn't know chapter nine existed. This gives
    each chapter the whole outline so the early ones can point forward. Chapter by
    chapter rather than one enormous request, and swapped in atomically at the end so
    a failure can't leave a half-rewritten book.
    """
    chapters = await repo.list_chapters(campaign.id)
    if not chapters:
        return []

    established, party = await _context(repo, campaign)
    outline = "\n".join(f"{c.number}. {c.title}" for c in chapters)
    party_rows = await repo.list_party(campaign.id)

    polished: list[Chapter] = []
    previous_watermark = 0
    for chapter in chapters:
        span = await repo.messages_between(
            campaign.id, previous_watermark, chapter.through_message_id
        )
        rewritten: WrittenChapter = await rewrite_chapter(
            model=model,
            settings=settings,
            outline=outline,
            established=established,
            party=party,
            chapter_number=chapter.number,
            original=chapter.body,
            transcript=render_transcript(span, party_rows),
        )
        polished.append(replace(chapter, title=rewritten.title, body=rewritten.body))
        previous_watermark = chapter.through_message_id

    await repo.replace_chapters(campaign.id, polished)
    log.info("chronicle: polished %s chapter(s) for campaign %s", len(polished), campaign.id)
    return polished


async def render_book(repo: Repo, campaign: Campaign) -> str:
    """The whole book as Markdown: title page, cast, chapters, achievements."""
    genre = genre_for(campaign)
    chapters = await repo.list_chapters(campaign.id)
    party = await repo.list_party(campaign.id)
    registry = load_registry()

    title = f"# The {genre.label} Campaign"
    lines = [title, ""]

    for character in party:
        described = f"{character.origin} {character.archetype}".strip()
        lines.append(f"**{character.name}**" + (f" — {described}" if described else "") + "  ")
    if party:
        lines.append("")

    npcs = [e for e in await repo.recent_events(campaign.id, limit=200) if e.kind == "npc"]
    if npcs:
        lines += ["## Cast", ""]
        lines += [f"- {e.summary}" for e in npcs]
        lines += [""]

    if not chapters:
        lines += ["*Nothing has been written up yet.*", ""]
        return "\n".join(lines)

    previous_grant = 0
    for chapter in chapters:
        lines += [f"## {chapter.number}. {chapter.title}", "", chapter.body.strip(), ""]

        grants = await repo.grants_between(campaign.id, previous_grant, chapter.through_grant_id)
        for grant in grants:
            achievement = registry.get(grant.achievement_id)
            if achievement is None:
                continue
            block = format_block(achievement)
            # Fenced so the block keeps the shape it had in play.
            lines += [f"*{grant.character_name}:*", "", "```", block, "```", ""]
        previous_grant = chapter.through_grant_id

    return "\n".join(lines)
