"""The /chronicle command.

Writes up whatever has happened since the last chapter and sends the book so far.
`/chronicle redo` rewrites the most recent chapter, which is the recourse when one
comes out badly -- it's why there's no raw transcript export.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from src.bot.context import get_chronicler, get_repo
from src.game.chronicle import MIN_CHAPTER_MESSAGES, catch_up, polish, render_book
from src.game.genres import genre_for
from src.llm.chronicler import ChronicleFailed
from src.log import get_logger
from src.storage.repo import Campaign, Repo

log = get_logger(__name__)

#: One writer per campaign. Updates run concurrently, so two /chronicle calls (or an
#: impatient second /endgame) would otherwise both read the same watermark, write the
#: same span twice, and pay for it twice.
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(campaign_id: int) -> asyncio.Lock:
    return _locks.setdefault(campaign_id, asyncio.Lock())


def _filename(campaign: Campaign) -> str:
    label = genre_for(campaign).label.lower().replace(" ", "-")
    return f"the-{label}-campaign.md"


async def _watermark(repo: Repo, campaign_id: int) -> int:
    """The last message covered by a chapter, or 0 if nothing is written yet."""
    last = await repo.last_chapter(campaign_id)
    return last.through_message_id if last else 0


async def _say(message: Any, text: str) -> None:
    """Edit a progress message, tolerating Telegram refusing it.

    Progress reporting must never be the thing that fails a command.
    """
    try:
        await message.edit_text(text)
    except TelegramError:
        log.warning("couldn't update the progress message", exc_info=True)


async def send_book(update: Update, repo: Repo, campaign: Campaign) -> None:
    message = update.effective_message
    if message is None:
        return

    book = await render_book(repo, campaign)
    chapters = await repo.list_chapters(campaign.id)
    words = len(book.split())

    await message.reply_document(
        document=io.BytesIO(book.encode()),
        filename=_filename(campaign),
        caption=f"{len(chapters)} chapter{'s' if len(chapters) != 1 else ''}, {words:,} words.",
    )


async def handle_chronicle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch the book up and send it. `/chronicle redo` rewrites the last chapter."""
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return

    repo = get_repo(context)
    campaign = await repo.get_live_campaign(chat.id)
    if campaign is None:
        await message.reply_text("No campaign here to write up. /newgame to start one.")
        return

    args = context.args or []
    redo = bool(args) and args[0].lower() in {"redo", "again", "rewrite"}

    model = get_chronicler(context)
    if model is None:
        await message.reply_text(
            "I can't reach the model that writes these. Check DMK_SUMMARY_MODEL and its API key."
        )
        return

    if redo:
        last = await repo.last_chapter(campaign.id)
        if last is None:
            await message.reply_text("There's nothing written yet to rewrite.")
            return
        # Dropping it puts its span back below the watermark, so catch_up rewrites
        # it. Only safe because a chapter's span is always at least the minimum --
        # otherwise the deleted text would be unrecoverable.
        await repo.delete_chapter(last.id)
        await message.reply_text(f"Rewriting chapter {last.number}…")

    outstanding = await repo.count_messages_after(campaign.id, await _watermark(repo, campaign.id))
    if outstanding < MIN_CHAPTER_MESSAGES:
        if redo:
            # Shouldn't happen, but deleting a chapter we then decline to rewrite
            # would be silent data loss, so say so rather than shrugging.
            log.error("redo left campaign %s with too little to rewrite", campaign.id)
            await message.reply_text(
                "Something went wrong rewriting that — the chapter is gone and I can't "
                "reconstruct it. Sorry. Keep playing and /chronicle will pick up from here."
            )
            return
        if await repo.list_chapters(campaign.id):
            await message.reply_text("Nothing much has happened since the last chapter.")
            await send_book(update, repo, campaign)
        else:
            await message.reply_text("Play a bit more first — there's no chapter in this yet.")
        return

    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    notice = await message.reply_text("Writing it up…")

    try:
        async with _lock_for(campaign.id):
            written = await catch_up(repo, campaign, model=model, settings=None)
    except ChronicleFailed as exc:
        log.warning("chronicle failed for campaign %s: %s", campaign.id, exc)
        await _say(
            notice,
            "I couldn't get the words out. That chapter is gone — run /chronicle again "
            "to rewrite it."
            if redo
            else "I couldn't get the words out. Anything already written is safe — try again.",
        )
        return

    titles = "\n".join(f"{c.number}. {c.title}" for c in written)
    await _say(notice, f"Written:\n{titles}" if titles else "Nothing new to write.")
    await send_book(update, repo, campaign)


async def write_final_edition(
    update: Update, context: ContextTypes.DEFAULT_TYPE, campaign: Campaign
) -> None:
    """Catch up and then polish the whole book. Called when a campaign is retired.

    Best-effort: retiring a campaign must succeed even if the book doesn't, so
    everything here is caught and reported rather than raised.
    """
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return

    repo = get_repo(context)
    model = get_chronicler(context)
    if model is None:
        log.warning("no chronicler configured; skipping the final edition")
        return

    notice = None
    try:
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
        notice = await message.reply_text(
            "Closing the book on this one. Writing the final edition — this takes a few "
            "minutes and a few model calls, so give it a moment."
        )

        async with _lock_for(campaign.id):
            # force: a campaign usually ends on a handful of messages, and that
            # handful is the climax. Skipping it for being short would leave the
            # most important part of the book out.
            await catch_up(repo, campaign, model=model, settings=None, force=True)
            chapters = await polish(repo, campaign, model=model, settings=None)

        if not chapters:
            await _say(notice, "Nothing was ever written up for this one.")
            return

        await _say(notice, f"Final edition: {len(chapters)} chapters, revised end to end.")
        await send_book(update, repo, campaign)
    except Exception:
        # Everything, not just ChronicleFailed, and including Telegram errors:
        # retiring the campaign is the caller's job and must not fail because the
        # book didn't work out.
        log.exception("final edition failed for campaign %s", campaign.id)
        if notice is not None:
            await _say(notice, "I couldn't finish the final edit. The chapters are still here.")
