"""Campaign commands: /newgame, /begin, /sheet, /party, /endgame."""

from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from src.bot.context import chat_state, get_repo, get_service, reply, send_preformatted
from src.game.genres import GENRES, Genre, genre_for, genre_to_dict, get_genre
from src.game.memory import render_character
from src.llm.genre_builder import MAX_GENRE_NAME, GenreGenerationFailed, build_genre
from src.log import get_logger

log = get_logger(__name__)

GENRE_PREFIX = "genre:"
CONFIRM_NEW = "newgame:confirm"
CANCEL_NEW = "newgame:cancel"


#: Set while a genre keyboard is outstanding, so a typed reply builds that genre
#: instead of vanishing into the silence a chat with no campaign normally gets.
CHOOSING_GENRE_KEY = "choosing_genre"
#: Set while a generation is in flight, so two people typing at once don't both
#: build a setting and quietly retire each other's campaign.
BUILDING_GENRE_KEY = "building_genre"


def genre_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(g.button, callback_data=f"{GENRE_PREFIX}{g.key}")]
            for g in GENRES.values()
        ]
    )


def genre_menu_text() -> str:
    """The prompt plus a full description of each genre.

    The descriptions live here rather than on the buttons because Telegram cuts
    button text off at one line.
    """
    lines = ["*What kind of trouble are we getting into?*", ""]
    lines += [f"{g.emoji} *{g.label}* — {g.pitch}" for g in GENRES.values()]
    lines += ["", "_Or just type any genre you like — cyberpunk, pirates, cosy village mystery._"]
    return "\n".join(lines)


async def start_custom_genre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Build a genre from free text, then open the campaign in it.

    Hand-authoring every setting anyone might want doesn't scale and a fixed menu is
    a poor answer to "can we play cyberpunk", so anything off the menu is generated
    once, here, and stored on the campaign row.
    """
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None or not message.text:
        return

    request = message.text.strip()[:MAX_GENRE_NAME]
    if not request:
        return

    # One shot per /newgame. Left set, a chat that abandoned the menu would turn
    # the next stray "lol" into a generated campaign.
    state = chat_state(context)
    state.pop(CHOOSING_GENRE_KEY, None)

    repo = get_repo(context)
    if await repo.get_live_campaign(chat.id) is not None:
        await message.reply_text("There's already a campaign in here. /newgame to replace it.")
        return

    # Typing the name of a genre we already have should use the hand-written one
    # rather than spending a model call on a worse copy of it.
    built_in = next((g for g in GENRES.values() if g.label.lower() == request.lower()), None)
    if built_in is not None:
        await _open_campaign(update, context, built_in)
        return

    if state.get(BUILDING_GENRE_KEY):
        await message.reply_text("Still building the last one — give me a moment.")
        return
    state[BUILDING_GENRE_KEY] = True

    service = get_service(context)
    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    # Plain text: the request is whatever the player typed, and an underscore or
    # asterisk in it would make Telegram reject the message outright.
    notice = await message.reply_text(f"Building a {request} setting…")

    try:
        genre = await build_genre(request, service.model, service.settings)
    except GenreGenerationFailed as exc:
        log.warning("genre generation failed for %r: %s", request, exc)
        state[CHOOSING_GENRE_KEY] = True  # let them try again without /newgame
        await _safe_edit(notice, "I couldn't make that into a setting. Try naming it differently.")
        return
    finally:
        state.pop(BUILDING_GENRE_KEY, None)

    campaign = await repo.create_campaign(chat.id, genre=genre.key, genre_skin=genre_to_dict(genre))
    log.info("campaign %s created in chat %s (generated: %s)", campaign.id, chat.id, genre.label)

    archetypes = ", ".join(a.name for a in genre.archetypes)
    await _safe_edit(
        notice,
        f"{genre.emoji} <b>{html.escape(genre.label)}</b> — {html.escape(genre.pitch)}\n\n"
        f"You can be: {html.escape(archetypes)}.\n\n"
        "Everyone who's playing: send /join. When the party's ready, /begin.",
        parse_mode=ParseMode.HTML,
    )


async def _safe_edit(message: Message, text: str, parse_mode: str | None = None) -> None:
    """Edit a message, tolerating Telegram refusing it.

    A campaign has usually already been created by the time we get here, so a
    formatting error must not take the whole flow down with it.
    """
    try:
        await message.edit_text(text, parse_mode=parse_mode)
    except TelegramError:
        log.warning("couldn't edit message", exc_info=True)


async def _open_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE, genre: Genre) -> None:
    """Create a campaign in a built-in genre and say what happens next."""
    chat, message = update.effective_chat, update.effective_message
    if chat is None or message is None:
        return

    campaign = await get_repo(context).create_campaign(chat.id, genre=genre.key)
    log.info("campaign %s created in chat %s (%s)", campaign.id, chat.id, genre.key)
    await message.reply_text(
        f"{genre.emoji} <b>{html.escape(genre.label)}</b> it is.\n\n"
        "Everyone who's playing: send /join to make a character. "
        "When the party's ready, /begin.",
        parse_mode=ParseMode.HTML,
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

    chat_state(context)[CHOOSING_GENRE_KEY] = True
    await update.message.reply_text(
        genre_menu_text(), parse_mode=ParseMode.MARKDOWN, reply_markup=genre_keyboard()
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
    chat_state(context)[CHOOSING_GENRE_KEY] = True
    await query.edit_message_text(
        f"Retired.\n\n{genre_menu_text()}",
        parse_mode=ParseMode.MARKDOWN,
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

    chat_state(context).pop(CHOOSING_GENRE_KEY, None)
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

    genre = genre_for(campaign)
    await send_preformatted(update.message, render_character(character, genre, include_player=True))


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

    genre = genre_for(campaign)
    sheets = "\n\n".join(render_character(c, genre, include_player=True) for c in party)
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
