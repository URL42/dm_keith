"""Telegram bot bridge for Dungeon Master Keith."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Sequence

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from ..config import get_settings
from ..engine.character import (
    SUPPORTED_CLASSES,
    SUPPORTED_RACES,
    CharacterManager,
    ability_modifier,
    profile_ready,
    required_fields_missing,
)
from ..utils.dice import DiceParseError, parse_dice_expression, roll_instruction
from ..engine.modes import ModeRequest, ModeRouter
from ..engine.storage import SessionState, SQLiteStore


SOUND_MAP = {
    "new_achievement": Path("assets/sounds/new_achievement.mp3"),
    "new_quest": Path("assets/sounds/new_quest.mp3"),
}


class TelegramBot:
    """High-level coordinator for Telegram interactions."""

    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.has_telegram_credentials():
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Set it in your .env file.")

        self.store = SQLiteStore()
        self.store.migrate()
        self.character_manager = CharacterManager(self.store)
        self.router = ModeRouter(store=self.store)
        self._sound_cache: dict[str, str] = {}

    def build_application(self) -> Application:
        application = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .post_init(self._post_init)
            .build()
        )

        application.add_handler(CommandHandler("start", self.handle_start))
        application.add_handler(CommandHandler("mode", self.handle_mode))
        application.add_handler(CommandHandler("character", self.handle_character))
        application.add_handler(CommandHandler("profile", self.handle_profile))
        application.add_handler(CommandHandler("inventory", self.handle_inventory))
        application.add_handler(CommandHandler("roll", self.handle_roll))
        application.add_handler(CommandHandler("story", self.handle_story_status))
        application.add_handler(CommandHandler("choose", self.handle_choose))
        application.add_handler(CommandHandler("history", self.handle_history))
        application.add_handler(CommandHandler("restart", self.handle_restart))
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

    async def handle_character(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message:
            return
        args = context.args or []
        user_id = f"telegram:{update.effective_user.id if update.effective_user else 'anonymous'}"
        session_id = f"telegram:{update.effective_chat.id if update.effective_chat else 'unknown'}"
        display_name = update.effective_user.full_name if update.effective_user else None

        self.store.ensure_user(user_id, display_name)
        session_state = self.store.upsert_session(session_id, user_id)
        profile = self.character_manager.ensure_profile(session_id, user_id)

        if not args:
            await message.reply_text(self._character_summary(profile, session_state.story_mode_enabled))
            return

        command = args[0].lower()
        remainder = args[1:]
        try:
            if command in {"new", "reset"}:
                profile = self.character_manager.reset_profile(session_id, user_id)
                profile = self.character_manager.assign_random_ability_scores(session_id, user_id)
                profile = self.character_manager.clear_inventory(session_id, user_id)
                self.store.upsert_session(session_id, user_id, story_mode_enabled=False)
                response = (
                    "Fresh adventurer coming right up! I rolled 4–20 for each stat and emptied your pockets.\n\n"
                    f"{self.character_manager.render_profile(profile)}\n\n"
                    "Next steps:\n"
                    "  • /character name <name>\n"
                    "  • /character race <race>\n"
                    "  • /character class <class>\n"
                    "  • /character finalize (when you're ready for story mode)\n"
                    "Need a mulligan later? /restart will reset the saga."
                )
            elif command == "name":
                value = " ".join(remainder).strip()
                if not value:
                    raise ValueError("Provide a name after `/character name`.")
                profile = self.character_manager.update_basic_field(
                    session_id, user_id, character_name=value
                )
                response = f"Name set to {value}."
            elif command == "race":
                value = " ".join(remainder).strip()
                if not value:
                    raise ValueError("Provide a race after `/character race`.")
                profile = self.character_manager.update_basic_field(
                    session_id, user_id, race=value
                )
                if value.lower() not in SUPPORTED_RACES:
                    response = (
                        f"Race set to {value}. Keith hasn't seen that in the handbook, "
                        "but he'll roll with it."
                    )
                else:
                    response = f"Race set to {value.title()}."
            elif command in {"class", "role"}:
                value = " ".join(remainder).strip()
                if not value:
                    raise ValueError("Provide a class after `/character class`.")
                profile = self.character_manager.update_basic_field(
                    session_id, user_id, character_class=value
                )
                if value.lower() not in SUPPORTED_CLASSES:
                    response = (
                        f"Class set to {value}. Keith will improvise the spell list as needed."
                    )
                else:
                    response = f"Class set to {value.title()}."
            elif command == "backstory":
                value = " ".join(remainder).strip()
                if not value:
                    raise ValueError("Share a few words of backstory after `/character backstory`.")
                profile = self.character_manager.update_basic_field(
                    session_id, user_id, backstory=value[:1024]
                )
                response = "Backstory stored. Keith promises only mild embellishments."
            elif command in {"ability", "stat"}:
                if len(remainder) < 2:
                    raise ValueError("Usage: `/character ability <stat> <value>`.")
                ability = remainder[0]
                try:
                    value = int(remainder[1])
                except ValueError as exc:  # pragma: no cover - defensive
                    raise ValueError("Ability scores must be integers.") from exc
                profile = self.character_manager.set_ability_score(session_id, user_id, ability, value)
                response = f"{ability.upper()} set to {value}."
            elif command in {"finalize", "ready"}:
                profile = self.character_manager.finalize_profile(session_id, user_id)
                response = (
                    "Character locked in! Story mode is now enabled. "
                    "Use `/mode story` to embark on the campaign."
                )
            elif command in {"show", "status", "sheet"}:
                response = self._character_summary(profile, session_state.story_mode_enabled)
            elif command == "help":
                response = self._character_help()
            else:
                response = (
                    "Unknown subcommand. Try `/character help` for available options."
                )
        except ValueError as exc:
            response = f"⚠️ {exc}"
        await message.reply_text(response)

    async def handle_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message:
            return
        user_id = f"telegram:{update.effective_user.id if update.effective_user else 'anonymous'}"
        session_id = f"telegram:{update.effective_chat.id if update.effective_chat else 'unknown'}"
        display_name = update.effective_user.full_name if update.effective_user else None

        self.store.ensure_user(user_id, display_name)
        session_state = self.store.upsert_session(session_id, user_id)
        profile = self.character_manager.ensure_profile(session_id, user_id)
        await message.reply_text(self._character_summary(profile, session_state.story_mode_enabled))

    async def handle_inventory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message:
            return
        args = context.args or []
        user_id = f"telegram:{update.effective_user.id if update.effective_user else 'anonymous'}"
        session_id = f"telegram:{update.effective_chat.id if update.effective_chat else 'unknown'}"
        display_name = update.effective_user.full_name if update.effective_user else None

        self.store.ensure_user(user_id, display_name)
        self.store.upsert_session(session_id, user_id)
        profile = self.character_manager.ensure_profile(session_id, user_id)

        try:
            if not args or args[0].lower() in {"show", "list"}:
                response = self.character_manager.render_inventory(profile)
            elif args[0].lower() == "add":
                item, quantity = self._parse_inventory_args(args[1:])
                profile = self.character_manager.add_inventory_item(
                    session_id, user_id, item, quantity
                )
                response = f"Added {quantity} x {item}."
            elif args[0].lower() in {"remove", "drop"}:
                item, quantity = self._parse_inventory_args(args[1:])
                profile = self.character_manager.remove_inventory_item(
                    session_id, user_id, item, quantity
                )
                response = f"Removed {quantity} x {item}."
            elif args[0].lower() == "clear":
                profile = self.character_manager.clear_inventory(session_id, user_id)
                response = "Inventory cleared. Keith sweeps the bag dramatically."
            else:
                response = (
                    "Usage: /inventory [show|add|remove|clear] — e.g. `/inventory add torch 2`"
                )
        except ValueError as exc:
            response = f"⚠️ {exc}"

        await message.reply_text(response)

    async def handle_story_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message:
            return
        user_id = f"telegram:{update.effective_user.id if update.effective_user else 'anonymous'}"
        session_id = f"telegram:{update.effective_chat.id if update.effective_chat else 'unknown'}"
        display_name = update.effective_user.full_name if update.effective_user else None

        self.store.ensure_user(user_id, display_name)
        session_state = self.store.upsert_session(session_id, user_id)
        profile = self.character_manager.ensure_profile(session_id, user_id)
        if not profile_ready(profile) or not session_state.story_mode_enabled:
            await message.reply_text(
                "Finish character creation (`/character finalize`) before checking the story state."
            )
            return
        scene = self.router.story_engine.current_scene(session_id, profile)
        state = self.store.get_story_state(session_id)
        await message.reply_text(self._format_story_scene(scene, state))

    async def handle_choose(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message:
            return
        args = context.args or []
        if not args:
            await message.reply_text("Usage: /choose <option number or id>")
            return
        choice_text = " ".join(args)
        await self._dispatch(
            update,
            choice_text,
            triggers=("event.story.choice_cmd", "event.message"),
            metadata={"command": "choose"},
        )

    async def handle_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message:
            return
        limit = 5
        if context.args and context.args[0].isdigit():
            limit = max(1, min(20, int(context.args[0])))

        user_id = f"telegram:{update.effective_user.id if update.effective_user else 'anonymous'}"
        session_id = f"telegram:{update.effective_chat.id if update.effective_chat else 'unknown'}"
        rolls = self.store.fetch_recent_story_rolls(session_id, limit=limit)
        if not rolls:
            await message.reply_text("No rolls logged yet. Try `/roll 1d20` to christen the dice.")
            return
        lines = ["Recent Rolls:"]
        for roll in rolls:
            timestamp = roll.created_at.astimezone().strftime("%H:%M:%S")
            detail = roll.result_detail or {}
            ability = detail.get("ability")
            ability_part = f" [{ability.upper()}]" if ability else ""
            lines.append(
                f"{timestamp}{ability_part} {roll.expression} → {roll.result_total}"
            )
        await message.reply_text("\n".join(lines))

    async def handle_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message:
            return
        user_id = f"telegram:{update.effective_user.id if update.effective_user else 'anonymous'}"
        session_id = f"telegram:{update.effective_chat.id if update.effective_chat else 'unknown'}"
        display_name = update.effective_user.full_name if update.effective_user else None

        self.store.ensure_user(user_id, display_name)
        profile = self.character_manager.reset_profile(session_id, user_id)
        profile = self.character_manager.assign_random_ability_scores(session_id, user_id)
        profile = self.character_manager.clear_inventory(session_id, user_id)
        self.store.upsert_session(session_id, user_id, mode="narrator", story_mode_enabled=False)
        self.store.upsert_story_state(
            session_id,
            current_scene=None,
            scene_history=[],
            flags={},
            stats={"xp": profile.experience, "level": profile.level},
        )
        summary = self.character_manager.render_profile(profile)
        response = (
            "Story reset! Keith tears up the previous script and hands you a fresh character sheet.\n\n"
            f"{summary}\n\n"
            "Rename and class-up with `/character name`, `/character race`, `/character class`, then `/character finalize` to rejoin the campaign."
        )
        await message.reply_text(response)

    async def handle_roll(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not message:
            return
        expr_text = " ".join(context.args or []).strip()
        if not expr_text:
            await message.reply_text(
                "Usage: /roll <expression> (e.g., /roll 1d20+3 or /roll str)"
            )
            return

        user_id = f"telegram:{update.effective_user.id if update.effective_user else 'anonymous'}"
        session_id = f"telegram:{update.effective_chat.id if update.effective_chat else 'unknown'}"
        display_name = update.effective_user.full_name if update.effective_user else None

        self.store.ensure_user(user_id, display_name)
        session_state = self.store.upsert_session(session_id, user_id)
        profile = self.character_manager.ensure_profile(session_id, user_id)

        try:
            instruction = parse_dice_expression(expr_text)
        except DiceParseError as exc:
            await message.reply_text(f"⚠️ {exc}")
            return

        ability_mod = 0
        if instruction.ability:
            ability_mod = ability_modifier(profile.ability_scores.get(instruction.ability, 10))

        result = roll_instruction(instruction, ability_modifier=ability_mod)
        self.store.log_story_roll(
            session_id=session_id,
            user_id=user_id,
            expression=expr_text,
            result_total=result.total,
            result_detail={
                "rolls": result.rolls,
                "kept": result.kept,
                "modifier": instruction.modifier,
                "ability_modifier": ability_mod,
                "advantage": instruction.advantage,
                "ability": instruction.ability,
            },
        )

        state = self.store.get_story_state(session_id)
        if state is not None and instruction.ability:
            flags = dict(state.flags)
            flags["pending_roll"] = {
                "ability": instruction.ability,
                "rolls": result.rolls,
                "kept": result.kept,
                "modifier": instruction.modifier,
                "ability_modifier": ability_mod,
                "advantage": instruction.advantage,
                "total": result.total,
            }
            self.store.upsert_story_state(session_id, flags=flags)

        advantage_note = ""
        if instruction.advantage == 1:
            advantage_note = " (advantage)"
        elif instruction.advantage == -1:
            advantage_note = " (disadvantage)"

        roll_line = ", ".join(str(r) for r in result.rolls)
        kept_line = ", ".join(str(k) for k in result.kept)
        lines = [f"🎲 Roll{advantage_note}: {expr_text}"]
        if instruction.ability:
            lines.append(
                f"Ability modifier {instruction.ability.upper()}: {ability_mod:+}"
            )
        lines.append(f"Rolls: {roll_line}")
        if kept_line != roll_line:
            lines.append(f"Kept: {kept_line}")
        modifier_total = instruction.modifier + ability_mod
        if modifier_total:
            lines.append(f"Modifiers applied: {modifier_total:+}")
        lines.append(f"Total: {result.total}")

        if instruction.ability:
            lines.append("Stored for the next matching ability check.")

        await message.reply_text("\n".join(lines))

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
        await self._maybe_send_sound(effective_message, response, normalized_triggers)

    def _ensure_session(self, session_id: str, user_id: str) -> SessionState:
        state = self.store.get_session(session_id)
        if state:
            return state
        # Ensure a user row exists before creating a session to satisfy FK constraints.
        self.store.ensure_user(user_id)
        return self.store.upsert_session(session_id, user_id)

    def _character_summary(self, profile, story_ready: bool) -> str:
        summary = self.character_manager.render_profile(profile)
        missing = required_fields_missing(profile)
        lines = [summary, ""]
        if missing:
            lines.append("Missing fields: " + ", ".join(missing))
        else:
            if story_ready:
                lines.append("Story mode active. Use `/mode story` to jump in.")
            else:
                lines.append("All core fields set. Run `/character finalize` to enable story mode.")
        lines.append(self._character_help(short=True))
        return "\n".join(lines)

    def _character_help(self, short: bool = False) -> str:
        lines = [
            "Commands:",
            "  /character show — display current sheet",
            "  /character name <value>",
            "  /character race <value>",
            "  /character class <value>",
            "  /character ability <stat> <value>",
            "  /character backstory <text>",
            "  /character finalize — lock in and enable story mode",
            "  /character reset — start over",
            "  /inventory [show|add|remove|clear]",
            "  /restart — reset story, stats, and inventory",
        ]
        if short:
            return "\n".join(lines[:5])
        return "\n".join(lines)

    async def _maybe_send_sound(
        self,
        message,
        response,
        triggers: Sequence[str],
    ) -> None:
        """Send a short audio cue for key events."""
        sound_key = None
        if response.was_new and response.achievement_id:
            sound_key = "new_achievement"
        elif "event.story.choice" in triggers:
            sound_key = "new_quest"

        if sound_key is None:
            return
        path = SOUND_MAP.get(sound_key)
        if not path or not path.exists():
            return

        cached_id = self._sound_cache.get(sound_key)
        if cached_id:
            await message.reply_audio(audio=cached_id)
            return

        with path.open("rb") as handle:
            sent = await message.reply_audio(audio=handle)
        audio = getattr(sent, "audio", None)
        if audio and getattr(audio, "file_id", None):
            self._sound_cache[sound_key] = audio.file_id

    def _format_story_scene(self, scene, state) -> str:
        narration = "\n".join(scene.narration)
        lines = [f"Scene: {scene.title} ({scene.id})", narration]
        if scene.choices:
            lines.append("Choices:")
            for idx, choice in enumerate(scene.choices, start=1):
                lines.append(f"  {idx}. {choice.label} (id={choice.id})")
        else:
            lines.append("No scripted choices here—improvise!")

        pending_roll = None
        if state and state.flags:
            pending_roll = state.flags.get("pending_roll")
        if pending_roll:
            ability = pending_roll.get("ability", "?").upper()
            total = pending_roll.get("total")
            lines.append(f"Pending roll stored for {ability}: total {total}")
        lines.append("Use `/choose <id>` or reply with the option text to proceed.")
        return "\n".join([line for line in lines if line])

    def _parse_inventory_args(self, parts: list[str]) -> tuple[str, int]:
        if not parts:
            raise ValueError("Specify an item, e.g. `/inventory add torch 2`.")
        quantity = 1
        if parts[-1].isdigit():
            quantity = int(parts[-1])
            item_parts = parts[:-1]
        else:
            item_parts = parts
        item = " ".join(item_parts).strip()
        if not item:
            raise ValueError("Item name cannot be empty.")
        if quantity <= 0:
            raise ValueError("Quantity must be positive.")
        return item, quantity


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
