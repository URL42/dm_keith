"""Carrying a character between campaigns.

`/export` sends a Markdown character sheet; uploading one back into a chat brings
that character into the current campaign, keeping their level, XP, items and
achievements. Abilities survive a change of setting untouched because they're stored
under canonical keys and only *displayed* under a genre's names -- Thorn's STR 16
becomes Muscle 16 in a cyberpunk game with no conversion at all.

What can't survive is archetype and origin: "Human Fighter" means nothing in a
cyberpunk campaign, so an import picks those again from the new setting.
"""

from __future__ import annotations

import io

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.achievements.runtime import load_registry
from src.bot.context import get_repo, user_state
from src.game.genres import genre_for
from src.game.sheets import (
    MAX_SHEET_BYTES,
    ImportedCharacter,
    SheetError,
    parse_sheet,
    render_sheet,
)
from src.log import get_logger

log = get_logger(__name__)

#: Where a parsed sheet waits while the player picks an archetype for it.
IMPORT_KEY = "imported_character"


def _filename(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-").lower()
    return f"{safe or 'character'}.md"


async def handle_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the caller their character sheet as a file."""
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
        await update.message.reply_text("You don't have a character to export. Send /join.")
        return

    registry = load_registry()
    earned = [aid for aid, _ in await repo.list_achievements(character.id)]
    titles = {aid: registry[aid].title for aid in earned if aid in registry}

    sheet = render_sheet(character, earned, genre_for(campaign), titles)
    await update.message.reply_document(
        document=io.BytesIO(sheet.encode()),
        filename=_filename(character.name),
        caption=(
            f"{character.name}, level {character.level}. Upload this file into any "
            "campaign to carry them over."
        ),
    )
    log.info(
        "exported %s (level %s) from campaign %s", character.name, character.level, campaign.id
    )


async def read_uploaded_sheet(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> ImportedCharacter | None:
    """Validate an uploaded sheet, replying with the reason if it isn't usable."""
    message = update.effective_message
    chat, user = update.effective_chat, update.effective_user
    if message is None or message.document is None or chat is None or user is None:
        return None

    if (message.document.file_size or 0) > MAX_SHEET_BYTES:
        await message.reply_text("That file is far too big to be a character sheet.")
        return None

    repo = get_repo(context)
    campaign = await repo.get_live_campaign(chat.id)
    if campaign is None:
        await message.reply_text("There's no campaign here to import into. /newgame first.")
        return None
    if await repo.get_character(campaign.id, user.id) is not None:
        await message.reply_text("You're already playing someone here. One character each.")
        return None

    handle = await message.document.get_file()
    raw = bytes(await handle.download_as_bytearray())
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        await message.reply_text("I can't read that file — character sheets are plain text.")
        return None

    try:
        imported = parse_sheet(text, known_achievements=set(load_registry()))
    except SheetError as exc:
        await message.reply_text(str(exc))
        return None

    if await repo.find_character_by_name(campaign.id, imported.name):
        await message.reply_text(
            f"There's already a {imported.name} in this party. Two of them would be confusing."
        )
        return None

    user_state(context)[IMPORT_KEY] = imported
    log.info(
        "importing %s (level %s, %s items%s) into campaign %s",
        imported.name,
        imported.level,
        len(imported.items),
        ", EDITED" if imported.edited else "",
        campaign.id,
    )

    await message.reply_text(
        f"*{imported.name}* — level {imported.level}, {imported.xp} XP"
        + (f", last seen in a {imported.from_campaign} campaign" if imported.from_campaign else "")
        + ".",
        parse_mode=ParseMode.MARKDOWN,
    )
    return imported
