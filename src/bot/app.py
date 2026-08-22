"""Telegram application wiring.

M1 scope: the bot boots, opens the database, and answers /start, /help and /ping.
The DM agent, campaign commands and play handler arrive in later milestones.
"""

from __future__ import annotations

import html
from typing import Any

import aiosqlite
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from src.config import Settings
from src.log import get_logger
from src.storage.db import connect
from src.storage.repo import Repo

log = get_logger(__name__)

#: Key under which the Repo lives in Application.bot_data.
REPO_KEY = "repo"
CONN_KEY = "conn"

START_TEXT = (
    "🎲 *Dungeon Master Keith*, reporting for duty.\n\n"
    "I run campaigns. You make questionable decisions. I narrate the consequences "
    "with more enthusiasm than they deserve.\n\n"
    "Use /help to see what I can do."
)

HELP_TEXT = (
    "*Commands*\n"
    "/start — wake me up\n"
    "/help — this list\n"
    "/ping — check I'm still breathing\n\n"
    "_Campaign commands land in the next milestone._"
)


def build_application(settings: Settings) -> Application:
    """Construct the Telegram application with handlers and lifecycle hooks."""
    app = (
        Application.builder()
        .token(settings.telegram_token)
        .post_init(_open_database(settings))
        .post_shutdown(_close_database)
        .build()
    )

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CommandHandler("ping", handle_ping))
    app.add_error_handler(handle_error)
    return app


def _open_database(settings: Settings) -> Any:
    async def post_init(app: Application) -> None:
        conn = await connect(settings.db_path)
        app.bot_data[CONN_KEY] = conn
        app.bot_data[REPO_KEY] = Repo(conn)
        log.info("database ready at %s", settings.db_path)
        log.info("dm model=%s summary model=%s", settings.model, settings.summary_model)

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
