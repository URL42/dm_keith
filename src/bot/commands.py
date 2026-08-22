"""Campaign commands: /newgame, /begin, /sheet, /party, /endgame."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.bot.context import get_repo, get_service, reply, send_preformatted
from src.game.genres import GENRES, get_genre
from src.game.memory import render_character
from src.log import get_logger

log = get_logger(__name__)

GENRE_PREFIX = "genre:"
CONFIRM_NEW = "newgame:confirm"
CANCEL_NEW = "newgame:cancel"


def genre_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(g.pitch, callback_data=f"{GENRE_PREFIX}{g.key}")]
            for g in GENRES.values()
        ]
    )


async def handle_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a campaign, confirming first if one is already running here."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    repo = get_repo(context)
    existing = await repo.get_live_campaign(chat.id)
    if existing is not None:
        await update.message.reply_text(
            "There's already a campaign running in here. Starting a new one retires it "
            "— the old story is kept, but you can't go back to it.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Retire it and start fresh", callback_data=CONFIRM_NEW)],
                    [InlineKeyboardButton("Never mind", callback_data=CANCEL_NEW)],
                ]
            ),
        )
        return

    await update.message.reply_text(
        "What kind of trouble are we getting into?", reply_markup=genre_keyboard()
    )


async def handle_newgame_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return
    await query.answer()

    if query.data == CANCEL_NEW:
        await query.edit_message_text("Wise. Carry on.")
        return

    # Retire it here, on the confirmation, rather than as a side effect of picking
    # a genre. That keeps "a campaign is live" a reliable signal that a stale genre
    # button shouldn't be honoured.
    repo = get_repo(context)
    existing = await repo.get_live_campaign(chat.id)
    if existing is not None:
        await repo.clear_pending_rolls(existing.id)
    await repo.end_campaign(chat.id)
    await query.edit_message_text(
        "Retired. What kind of trouble are we getting into instead?",
        reply_markup=genre_keyboard(),
    )


async def handle_genre_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create the campaign and let Keith set the scene."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return
    await query.answer()

    repo = get_repo(context)

    # These buttons live forever in the chat history. Tapping an old one must not
    # quietly retire the campaign people are currently playing.
    existing = await repo.get_live_campaign(chat.id)
    if existing is not None:
        await query.edit_message_text(
            "That's an old button — there's a campaign running in here already. "
            "Use /newgame if you really want to start over."
        )
        return

    genre_key = (query.data or "").removeprefix(GENRE_PREFIX)
    genre = get_genre(genre_key)
    campaign = await repo.create_campaign(chat.id, genre=genre.key)
    log.info("campaign %s created in chat %s (%s)", campaign.id, chat.id, genre.key)

    await query.edit_message_text(
        f"*{genre.label}* it is.\n\n"
        "Everyone who's playing: send /join to make a character. "
        "When the party's ready, /begin.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kick off play once at least one character exists."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    repo = get_repo(context)
    campaign = await repo.get_live_campaign(chat.id)
    if campaign is None:
        await update.message.reply_text("No campaign here yet. Start one with /newgame.")
        return

    party = await repo.list_party(campaign.id)
    if not party:
        await update.message.reply_text("Nobody has a character yet. Someone /join first.")
        return

    # Claim the start atomically: two /begin messages arriving together would
    # otherwise both pass the status check and narrate two opening scenes.
    if not await repo.try_activate(campaign.id):
        await update.message.reply_text("We're already playing. Just tell me what you do.")
        return

    campaign = await repo.get_live_campaign(chat.id)
    if campaign is None:
        return

    service = get_service(context)
    names = ", ".join(c.name for c in party)
    await reply(
        update,
        context,
        service,
        campaign,
        action=(
            f"The campaign begins. The party is: {names}. "
            "Open the story: set the scene, establish where they are and why, "
            "and give them something to react to."
        ),
        actor=None,
    )


async def handle_sheet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the caller's character sheet."""
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or update.message is None:
        return

    repo = get_repo(context)
    campaign = await repo.get_live_campaign(chat.id)
    if campaign is None:
        await update.message.reply_text("No campaign here. Start one with /newgame.")
        return

    character = await repo.get_character(campaign.id, user.id)
    if character is None:
        await update.message.reply_text("You don't have a character yet. Send /join.")
        return

    genre = get_genre(campaign.genre)
    await send_preformatted(update.message, render_character(character, genre))


async def handle_party(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show every character in the campaign."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    repo = get_repo(context)
    campaign = await repo.get_live_campaign(chat.id)
    if campaign is None:
        await update.message.reply_text("No campaign here. Start one with /newgame.")
        return

    party = await repo.list_party(campaign.id)
    if not party:
        await update.message.reply_text("The party is empty. Someone /join.")
        return

    genre = get_genre(campaign.genre)
    sheets = "\n\n".join(render_character(c, genre) for c in party)
    await send_preformatted(update.message, sheets)


async def handle_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retire the current campaign."""
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    repo = get_repo(context)
    campaign = await repo.get_live_campaign(chat.id)
    if campaign is None:
        await update.message.reply_text("Nothing to end.")
        return

    # Drop outstanding roll buttons too, so tapping a leftover one doesn't consume a
    # roll into a campaign that no longer exists.
    await repo.clear_pending_rolls(campaign.id)
    await repo.end_campaign(chat.id)
    await update.message.reply_text(
        "Campaign retired. The story is kept, but that's the end of it. /newgame when you're ready."
    )
