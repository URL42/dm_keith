"""Character creation: /join.

A short conversation -- archetype, origin, name -- driven by inline keyboards so
it's three taps and one typed name. Keyed per user, so several people in a group
can be building characters at the same time without colliding.
"""

from __future__ import annotations

import random
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.bot.context import get_repo, get_service, reply, send_preformatted, user_state
from src.game.characters import assign_standard_array, roll_abilities
from src.game.genres import DEFAULT_GENRE, Genre, genre_for
from src.game.memory import render_character
from src.log import get_logger

log = get_logger(__name__)

CHOOSING_ARCHETYPE, CHOOSING_ORIGIN, NAMING = range(3)

ARCHETYPE_PREFIX = "arch:"
ORIGIN_PREFIX = "orig:"
ROLL_STATS = "stats:roll"


def _keyboard(prefix: str, options: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Two-column keyboard of (label, value) pairs."""
    buttons = [
        InlineKeyboardButton(label, callback_data=f"{prefix}{value}") for label, value in options
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


async def start_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Begin creation, unless this player already has a character."""
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or update.message is None:
        return ConversationHandler.END

    repo = get_repo(context)
    campaign = await repo.get_live_campaign(chat.id)
    if campaign is None:
        await update.message.reply_text("No campaign here yet. Someone run /newgame first.")
        return ConversationHandler.END

    existing = await repo.get_character(campaign.id, user.id)
    if existing is not None:
        await update.message.reply_text(
            f"You're already playing {existing.name}. One character each — /sheet to see them."
        )
        return ConversationHandler.END

    genre = genre_for(campaign)
    user_state(context)["campaign_id"] = campaign.id

    blurbs = "\n".join(f"*{a.name}* — {a.blurb}" for a in genre.archetypes)
    await update.message.reply_text(
        f"Right. Who are you?\n\n{blurbs}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_keyboard(ARCHETYPE_PREFIX, [(a.name, a.name) for a in genre.archetypes]),
    )
    return CHOOSING_ARCHETYPE


async def choose_archetype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()

    archetype = (query.data or "").removeprefix(ARCHETYPE_PREFIX)
    user_state(context)["archetype"] = archetype

    genre = await _genre_from(context)
    blurbs = "\n".join(f"*{o.name}* — {o.blurb}" for o in genre.origins)
    await query.edit_message_text(
        f"A {archetype}. Predictable, but fine.\n\nWhere are you from?\n\n{blurbs}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_keyboard(ORIGIN_PREFIX, [(o.name, o.name) for o in genre.origins]),
    )
    return CHOOSING_ORIGIN


async def choose_origin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()

    origin = (query.data or "").removeprefix(ORIGIN_PREFIX)
    user_state(context)["origin"] = origin

    await query.edit_message_text(
        f"{origin} {user_state(context).get('archetype', '')}. Good.\n\n"
        "What's your name? Send it as a message — add a line about who you are if you like.",
    )
    return NAMING


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Create the character and have Keith introduce them."""
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or update.message is None or not update.message.text:
        return ConversationHandler.END

    text = update.message.text.strip()
    name, _, concept = text.partition("\n")
    name = name.strip()[:60]
    if not name:
        await update.message.reply_text("I need something to call you. Try again.")
        return NAMING

    repo = get_repo(context)
    campaign_id = user_state(context).get("campaign_id")
    campaign = await repo.get_campaign(campaign_id) if campaign_id else None
    if campaign is None or campaign.status == "ended":
        await update.message.reply_text("That campaign is gone. Start again with /newgame.")
        return ConversationHandler.END

    if await repo.find_character_by_name(campaign.id, name):
        await update.message.reply_text(
            f"There's already a {name} in this party. Pick something else — it gets confusing."
        )
        return NAMING

    genre = await _genre_from(context)
    archetype_name = user_state(context).get("archetype", "")
    archetype = genre.find_archetype(archetype_name)
    abilities = (
        assign_standard_array(archetype.priority) if archetype else roll_abilities(random.Random())
    )

    character = await repo.create_character(
        campaign.id,
        user_id=user.id,
        name=name,
        user_display=_display_name(user),
        archetype=archetype_name,
        origin=user_state(context).get("origin", ""),
        concept=concept.strip()[:400],
        abilities=abilities,
    )

    if archetype:
        for item in archetype.starting_items:
            await repo.add_item(
                character.id,
                item.name,
                kind=item.kind,
                description=item.description,
                equippable=item.equippable,
                stat_mods=item.stat_mods,
            )
            if item.equip_at_creation:
                await repo.set_equipped(character.id, item.name, True)

    log.info("character %s (%s) joined campaign %s", character.name, user.id, campaign.id)
    user_state(context).clear()

    # Re-read so the sheet shows the kit, and the bonus from what they're holding.
    character = await repo.get_character_by_id(character.id) or character

    await send_preformatted(update.message, render_character(character, genre, include_player=True))

    if campaign.status == "active":
        # Mid-campaign arrival: Keith writes them into the scene.
        service = get_service(context)
        await reply(
            update,
            context,
            service,
            campaign,
            action=(
                f"A new character joins the party mid-story: {character.name}, "
                f"a {character.origin} {character.archetype}. "
                "Introduce them into the current scene in a sentence or two."
            ),
            actor=None,
        )
    else:
        await update.message.reply_text("Ready. /begin when the party's assembled.")

    return ConversationHandler.END


async def cancel_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_state(context).clear()
    if update.message:
        await update.message.reply_text("Fine. /join again when you've made up your mind.")
    return ConversationHandler.END


async def _genre_from(context: ContextTypes.DEFAULT_TYPE) -> Genre:
    """The campaign's genre, re-read rather than rebuilt from a stored key.

    A generated genre only exists on the campaign row, so looking it up by key
    would silently hand back fantasy instead.
    """
    campaign_id = user_state(context).get("campaign_id")
    campaign = await get_repo(context).get_campaign(campaign_id) if campaign_id else None
    return genre_for(campaign) if campaign else DEFAULT_GENRE


def _display_name(user: Any) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    return getattr(user, "first_name", "") or "someone"


def build_join_handler() -> ConversationHandler:
    """The /join flow, scoped to one user in one chat."""
    return ConversationHandler(
        entry_points=[CommandHandler("join", start_join)],
        states={
            CHOOSING_ARCHETYPE: [
                CallbackQueryHandler(choose_archetype, pattern=f"^{ARCHETYPE_PREFIX}")
            ],
            CHOOSING_ORIGIN: [CallbackQueryHandler(choose_origin, pattern=f"^{ORIGIN_PREFIX}")],
            NAMING: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel_join)],
        # Keyed on (chat, user): several people can build characters in the same
        # group at once, and someone mid-creation in one chat doesn't have their
        # ordinary messages in *another* chat swallowed as a character name.
        per_chat=True,
        per_user=True,
        name="character_creation",
    )
