"""Telegram application wiring."""

from __future__ import annotations

import html
from collections.abc import Callable, Coroutine
from typing import Any

import aiosqlite
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.bot.commands import (
    CANCEL_NEW,
    CONFIRM_NEW,
    GENRE_PREFIX,
    handle_begin,
    handle_endgame,
    handle_genre_choice,
    handle_newgame,
    handle_newgame_confirm,
    handle_party,
    handle_sheet,
)
from src.bot.context import CONN_KEY, REPO_KEY, SERVICE_KEY
from src.bot.creation import build_join_handler
from src.bot.play import handle_play
from src.bot.rolls import ROLL_PREFIX, handle_roll
from src.bot.sheets import handle_export
from src.config import Settings
from src.game.session import GameService
from src.log import get_logger
from src.storage.db import connect
from src.storage.repo import Repo

log = get_logger(__name__)

START_TEXT = (
    "🎲 *Dungeon Master Keith*, reporting for duty.\n\n"
    "I run campaigns. You make questionable decisions. I narrate the consequences "
    "with more enthusiasm than they deserve.\n\n"
    "Start with /newgame, then everyone playing sends /join.\n"
    "See /help for the rest."
)

#: The menu Telegram shows when someone types "/". Order is the order shown.
BOT_COMMANDS = [
    BotCommand("newgame", "Start a campaign — pick a genre"),
    BotCommand("join", "Make your character"),
    BotCommand("begin", "Start the story once the party's ready"),
    BotCommand("sheet", "Your character sheet"),
    BotCommand("party", "Everyone's character sheets"),
    BotCommand("export", "Save your character to a file"),
    BotCommand("endgame", "Retire the current campaign"),
    BotCommand("help", "What all of this does"),
]

HELP_TEXT = (
    "*Running a game*\n"
    "/newgame — pick a genre and start a campaign\n"
    "/join — make your character\n"
    "/begin — start the story once the party's ready\n"
    "/endgame — retire the current campaign\n\n"
    "*During play*\n"
    "Just type what you do. No command needed.\n"
    "/sheet — your character\n"
    "/party — everyone's characters\n\n"
    "_Talking to another player rather than to me? @mention them and I'll stay out of it._"
)


def build_application(settings: Settings) -> Application:
    """Construct the Telegram application with handlers and lifecycle hooks."""
    app = (
        Application.builder()
        .token(settings.telegram_token)
        # Without this, python-telegram-bot handles exactly one update at a time,
        # so a single slow model call would freeze every other chat -- the same
        # symptom as the old bot's blocking API call, one layer up. Turns within
        # a campaign are still serialised, by GameService's per-campaign lock.
        .concurrent_updates(True)
        .post_init(_startup(settings))
        .post_shutdown(_close_database)
        .build()
    )

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("ping", handle_ping))

    app.add_handler(CommandHandler("newgame", handle_newgame))
    app.add_handler(CommandHandler("begin", handle_begin))
    app.add_handler(CommandHandler("sheet", handle_sheet))
    app.add_handler(CommandHandler("party", handle_party))
    app.add_handler(CommandHandler("endgame", handle_endgame))
    app.add_handler(CommandHandler("export", handle_export))

    # Creation is a conversation, so it must see /join before anything else does.
    app.add_handler(build_join_handler())

    app.add_handler(CallbackQueryHandler(handle_roll, pattern=f"^{ROLL_PREFIX}"))
    app.add_handler(CallbackQueryHandler(handle_genre_choice, pattern=f"^{GENRE_PREFIX}"))
    app.add_handler(
        CallbackQueryHandler(handle_newgame_confirm, pattern=f"^({CONFIRM_NEW}|{CANCEL_NEW})$")
    )

    # Anything else that isn't a command is a move in the story.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_play))

    app.add_error_handler(handle_error)
    return app


def _startup(settings: Settings) -> Callable[[Application], Coroutine[Any, Any, None]]:
    async def post_init(app: Application) -> None:
        conn = await connect(settings.db_path)
        try:
            repo = Repo(conn)
            # Resolves the provider, so an unknown provider or a missing API key
            # fails here rather than mid-campaign. A typo'd *model name* still
            # gets through -- providers only reject those on the first real call.
            service = GameService(repo, settings.model, effort=settings.effort)
        except Exception:
            await conn.close()
            raise

        app.bot_data[CONN_KEY] = conn
        app.bot_data[REPO_KEY] = repo
        app.bot_data[SERVICE_KEY] = service
        log.info("database ready at %s", settings.db_path)
        log.info(
            "dm model=%s effort=%s summary model=%s",
            settings.model,
            settings.effort,
            settings.summary_model,
        )
        # Populates the menu Telegram shows when someone types "/".
        await app.bot.set_my_commands(BOT_COMMANDS)

        # Say so explicitly: an idle bot produces no further output, which otherwise
        # looks indistinguishable from a hang.
        log.info("listening for messages — send /newgame in Telegram to start")

    return post_init


async def _close_database(app: Application) -> None:
    conn: aiosqlite.Connection | None = app.bot_data.get(CONN_KEY)
    if conn is not None:
        await conn.close()
        log.info("database closed")


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(START_TEXT, parse_mode=ParseMode.MARKDOWN)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def handle_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("Still here. Still judging you.")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the traceback and tell the player something actually went wrong.

    The old bot swallowed errors into an 'offline' narration, so a broken API key
    looked like Keith being whimsical. It doesn't do that any more.
    """
    log.exception("unhandled error while processing update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        name = type(context.error).__name__
        await update.effective_message.reply_text(
            f"Keith trips over his own dice: <code>{html.escape(name)}</code>. Try again.",
            parse_mode=ParseMode.HTML,
        )
