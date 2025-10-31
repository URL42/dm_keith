"""Telegram bot bridge for Dungeon Master Keith."""

from __future__ import annotations

import csv
import io
from typing import Sequence

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from ..config import get_settings
from ..engine.modes import ModeRequest, ModeRouter
from ..engine.storage import SessionState, SQLiteStore


class TelegramBot:
    """High-level coordinator for Telegram interactions."""

    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.has_telegram_credentials():
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Set it in your .env file.")

        self.store = SQLiteStore()
        self.store.migrate()
        self.router = ModeRouter(store=self.store)

    def build_application(self) -> Application:
        application = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .post_init(self._post_init)
            .build()
        )

        application.add_handler(CommandHandler("start", self.handle_start))
        application.add_handler(CommandHandler("mode", self.handle_mode))
        application.add_handler(CommandHandler("set", self.handle_set))
        attachment_filter = filters.Document.ALL | filters.PHOTO
        application.add_handler(MessageHandler(attachment_filter, self.handle_attachment))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        application.add_error_handler(self.handle_error)
        return application

    async def _post_init(self, application: Application) -> None:
        # Ensure migrations are applied before first update.
        self.store.migrate()

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = "The narrator peers down and greets the new challenger."
        triggers = ("event.message.first_contact", "event.message")
        await self._dispatch(update, message, triggers=triggers)

    async def handle_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Usage: /mode <narrator|achievements|explain|story>"
            )
            return
        desired_mode = args[0].strip().lower()
        if desired_mode not in {"narrator", "achievements", "explain", "story"}:
            await update.message.reply_text(
                "Mode must be one of narrator, achievements, explain, or story."
            )
            return
        message = f"Switching mode to {desired_mode}."
        overrides = {"mode": desired_mode}
        triggers = (f"cmd.mode.{desired_mode}", "event.message")
        await self._dispatch(
            update,
            message,
            triggers=triggers,
            session_overrides=overrides,
        )

    async def handle_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /set <profanity|rating|tangents> <value>"
            )
            return
        field, value = args[0].lower(), args[1]
        overrides = {}
        trigger = "event.message"
        if field == "profanity":
            try:
                level = int(value)
            except ValueError:
                await update.message.reply_text("Profanity level must be an integer between 0 and 3.")
                return
            if level not in (0, 1, 2, 3):
                await update.message.reply_text("Profanity level must be between 0 and 3.")
                return
            overrides["profanity_level"] = level
            trigger = "cmd.set.profanity"
        elif field == "rating":
            normalized = value.upper()
            if normalized not in {"PG", "PG-13", "R"}:
                await update.message.reply_text("Rating must be PG, PG-13, or R.")
                return
            overrides["rating"] = normalized
            trigger = "cmd.set.rating"
        elif field == "tangents":
            try:
                tangents = int(value)
            except ValueError:
                await update.message.reply_text("Tangents level must be 0, 1, or 2.")
                return
            if tangents not in (0, 1, 2):
                await update.message.reply_text("Tangents level must be 0, 1, or 2.")
                return
            overrides["tangents_level"] = tangents
            trigger = "cmd.set.tangents"
        else:
            await update.message.reply_text(
                "Unsupported setting. Use profanity, rating, or tangents."
            )
            return

        message = f"Setting {field} to {value}."
        await self._dispatch(
            update,
            message,
            triggers=(trigger, "event.message"),
            session_overrides=overrides,
        )

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        text = update.message.text or ""
        triggers = ("event.message",)
        await self._dispatch(update, text, triggers=triggers)

    async def handle_attachment(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message:
            return

        attachment = None
        filename = "upload"
        mime_type = "application/octet-stream"

        if message.document:
            attachment = message.document
            filename = attachment.file_name or filename
            mime_type = attachment.mime_type or mime_type
        elif message.photo:
            attachment = message.photo[-1]
            filename = f"photo_{attachment.file_unique_id}.jpg"
            mime_type = "image/jpeg"

        if attachment is None:
            return

        file = await attachment.get_file()
        buffer = io.BytesIO()
        await file.download_to_memory(out=buffer)
        buffer.seek(0)

        triggers = ("event.upload.file", "event.message")
        attachments: list[str] = []
        session_overrides = {"mode": "explain"}
        text = f"User uploaded {filename}. Provide an analysis."

        if filename.lower().endswith(".csv") or mime_type in {
            "text/csv",
            "application/vnd.ms-excel",
        }:
            summary = summarize_csv(buffer)
            attachments.append(summary)
            triggers = ("event.upload.csv", "event.message")
            text = f"CSV uploaded: {filename}. Analyze the data."
        elif mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
            attachments.append(f"PDF uploaded: {filename} ({mime_type})")
            triggers = ("event.upload.pdf", "event.message")
        elif mime_type.startswith("image/"):
            attachments.append(f"Image uploaded: {filename} ({mime_type})")
            triggers = ("event.upload.image", "event.message")
        else:
            attachments.append(f"File uploaded: {filename} ({mime_type})")

        await self._dispatch(
            update,
            text,
            triggers=triggers,
            attachments=attachments,
            session_overrides=session_overrides,
            metadata={"filename": filename, "mime_type": mime_type},
        )

    async def handle_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.error:
            print(f"Telegram error: {context.error}")  # noqa: T201

    async def _dispatch(
        self,
        update: Update,
        message: str,
        *,
        triggers: Sequence[str],
        session_overrides: dict | None = None,
        attachments: Sequence[str] | None = None,
        metadata: dict | None = None,
    ) -> None:
        effective_message = update.effective_message
        if not effective_message:
            return

        user_id = f"telegram:{update.effective_user.id if update.effective_user else 'anonymous'}"
        session_id = f"telegram:{update.effective_chat.id if update.effective_chat else 'unknown'}"
        display_name = update.effective_user.full_name if update.effective_user else None

        session_state = self._ensure_session(session_id, user_id)
        mode = session_state.mode
        overrides = session_overrides or {}
        if "mode" in overrides:
            mode = overrides["mode"]

        base_metadata = {"source": "telegram"}
        if metadata:
            base_metadata.update(metadata)
        if overrides:
            base_metadata["session_overrides"] = overrides

        normalized_triggers = tuple(triggers)
        if session_state.created_at == session_state.updated_at and "event.message.first_contact" not in normalized_triggers:
            normalized_triggers = ("event.message.first_contact",) + normalized_triggers

        request = ModeRequest(
            user_id=user_id,
            session_id=session_id,
            message=message,
            mode=mode,  # type: ignore[arg-type]
            triggers=normalized_triggers,
            metadata=base_metadata,
            attachments=tuple(attachments or ()),
            display_name=display_name,
        )
        response = self.router.handle(request)
        await effective_message.reply_text(response.text)

    def _ensure_session(self, session_id: str, user_id: str) -> SessionState:
        state = self.store.get_session(session_id)
        if state:
            return state
        # Ensure a user row exists before creating a session to satisfy FK constraints.
        self.store.ensure_user(user_id)
        return self.store.upsert_session(session_id, user_id)


def summarize_csv(buffer: io.BytesIO) -> str:
    """Return a concise summary of a CSV file."""
    buffer.seek(0)
    text = buffer.read().decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return "CSV Summary: File is empty."
    headers = rows[0]
    body = rows[1:]
    row_count = len(body)
    column_count = len(headers)
    preview_rows = body[:3]
    preview = "; ".join(", ".join(row) for row in preview_rows) if preview_rows else "No data rows."
    header_preview = ", ".join(headers[:6]) + ("..." if len(headers) > 6 else "")
    return (
        f"CSV Summary: {row_count} rows x {column_count} columns. "
        f"Headers: {header_preview}. Sample: {preview}"
    )


def main() -> None:
    bot = TelegramBot()
    application = bot.build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
