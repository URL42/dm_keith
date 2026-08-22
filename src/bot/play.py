"""The play handler: ordinary messages become game actions."""

from __future__ import annotations

from telegram import Update
from telegram.constants import MessageEntityType
from telegram.ext import ContextTypes

from src.bot.commands import CHOOSING_GENRE_KEY
from src.bot.context import chat_state, get_repo, get_service, reply
from src.log import get_logger

log = get_logger(__name__)


def is_out_of_character(update: Update) -> bool:
    """True if this message is table talk rather than a game action.

    Players mention each other with @handle when they're talking to each other
    rather than to the DM, so Keith stays out of those. Mentioning Keith himself is
    the opposite -- that's addressing the DM directly, and it wins wherever it
    appears in the message, so "@hank look out, @keith I dodge" still gets played.
    """
    message = update.effective_message
    if message is None or not message.text:
        return False

    try:
        bot_username = update.get_bot().username
    except RuntimeError:  # no bot attached, e.g. outside a running application
        bot_username = None
    bot_handle = f"@{bot_username}".lower() if bot_username else None

    mentions_someone = False
    for entity in message.entities:
        # TEXT_MENTION covers players who have no @username.
        if entity.type not in {MessageEntityType.MENTION, MessageEntityType.TEXT_MENTION}:
            continue
        handle = message.text[entity.offset : entity.offset + entity.length].lower()
        if bot_handle and handle == bot_handle:
            return False
        mentions_someone = True

    return mentions_someone


async def handle_play(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a plain message into the campaign as that player's action."""
    chat, user = update.effective_chat, update.effective_user
    message = update.effective_message
    if chat is None or user is None or message is None or not message.text:
        return

    if is_out_of_character(update):
        log.debug("ignoring out-of-character message in chat %s", chat.id)
        return

    repo = get_repo(context)
    campaign = await repo.get_live_campaign(chat.id)
    if campaign is None:
        # Mid-/newgame the campaign doesn't exist yet, so a typed reply would
        # otherwise vanish into the same silence as chatter in a chat with no game.
        if chat_state(context).get(CHOOSING_GENRE_KEY):
            await message.reply_text("Pick a genre with the buttons above first.")
        return  # No campaign here; stay quiet rather than nagging.

    character = await repo.get_character(campaign.id, user.id)

    if campaign.status != "active":
        if character is None:
            await message.reply_text("Make a character first with /join.")
        else:
            await message.reply_text("The story hasn't started yet — /begin when you're ready.")
        return

    if character is None:
        await message.reply_text(
            "You're watching, not playing. /join to make a character and get involved."
        )
        return

    if character.status in {"dead", "retired"}:
        await message.reply_text(f"{character.name} is out of the story. /join won't help either.")
        return

    service = get_service(context)
    await reply(update, context, service, campaign, action=message.text, actor=character)
