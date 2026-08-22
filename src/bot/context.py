"""Shared helpers: reaching the repo/service, and the one path that sends a DM turn.

Every handler that makes Keith speak goes through `reply`, so the typing indicator,
message splitting, sound cues and error surfacing behave the same everywhere.
"""

from __future__ import annotations

import html
from typing import Any

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from src.game.session import GameService, TurnFailed
from src.log import get_logger
from src.storage.repo import Campaign, Character, Repo

log = get_logger(__name__)

REPO_KEY = "repo"
CONN_KEY = "conn"
SERVICE_KEY = "service"

#: Telegram rejects messages over 4096 characters.
MAX_MESSAGE_LEN = 4000


def get_repo(context: ContextTypes.DEFAULT_TYPE) -> Repo:
    repo = context.application.bot_data.get(REPO_KEY)
    if not isinstance(repo, Repo):
        raise RuntimeError("database not initialised")
    return repo


def get_service(context: ContextTypes.DEFAULT_TYPE) -> GameService:
    service = context.application.bot_data.get(SERVICE_KEY)
    if not isinstance(service, GameService):
        raise RuntimeError("game service not initialised")
    return service


def user_state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Per-user scratch space for the creation conversation.

    python-telegram-bot types `user_data` as optional because it's absent outside a
    user context; inside a conversation handler it always exists.
    """
    state = context.user_data
    if state is None:
        raise RuntimeError("no user context available")
    return state


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Break a long reply into sendable chunks, preferring paragraph breaks.

    Each chunk is as full as possible: a break point is only used if it's past the
    halfway mark, otherwise we fill to the limit. Splitting early would emit a
    stub message before the real content.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = limit
        for separator in ("\n\n", "\n", " "):
            candidate = window.rfind(separator)
            if candidate >= limit // 2:
                split_at = candidate
                break

        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)
    return chunks


async def send_preformatted(message: Any, body: str) -> None:
    """Send text in a code block, split if long and escaped so it can't break out.

    Character sheets contain player-written names and concepts; a stray backtick
    would otherwise break Telegram's Markdown parser and surface as an error.
    """
    safe = body.replace("`", "'")
    # Leave room for the fences on each chunk.
    for chunk in split_message(safe, MAX_MESSAGE_LEN - 10):
        await message.reply_text(f"```\n{chunk}\n```", parse_mode=ParseMode.MARKDOWN)


async def reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: GameService,
    campaign: Campaign,
    action: str,
    actor: Character | None,
    *,
    persist_action: bool = True,
) -> None:
    """Run a turn and send Keith's answer, or say plainly that it failed."""
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return

    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)

    try:
        result = await service.take_turn(campaign, action, actor, persist_action=persist_action)
    except TurnFailed as exc:
        # No silent fallback: the old bot dressed API failures up as whimsy, which
        # made a dead API key look like a personality quirk.
        log.exception("turn failed for campaign %s", campaign.id)

        # Tools commit as they run, so a turn that died partway may already have
        # applied damage or loot. Say so -- telling the player "nothing happened"
        # invites them to repeat an action that partly landed.
        warning = (
            " Some of it may already have happened — check /sheet before repeating that."
            if exc.partial
            else " Nothing happened — try again."
        )
        await message.reply_text(
            f"Keith trips over his own dice: <code>{html.escape(exc.cause_name)}</code>.{warning}",
            parse_mode=ParseMode.HTML,
        )
        return

    if not result.reply:
        await message.reply_text(
            "Keith stares into the middle distance and says nothing. Try again."
        )
        return

    sent = message
    for chunk in split_message(result.reply):
        sent = await message.reply_text(chunk)

    # If Keith asked for a check, the dice go to the player -- hang the button off
    # the end of his narration, where the cliffhanger is.
    if result.pending_roll is not None:
        from src.bot.rolls import prompt_for_roll

        character = await get_repo(context).get_character_by_id(result.pending_roll.character_id)
        if character is not None:
            await prompt_for_roll(sent, result.pending_roll, character)

    await send_cues(context, chat.id, result.cues)


async def send_cues(context: ContextTypes.DEFAULT_TYPE, chat_id: int, cues: list[str]) -> None:
    """Sound effects. Wired up properly in a later milestone."""
    if cues:
        log.debug("cues raised but not yet wired: %s", cues)
