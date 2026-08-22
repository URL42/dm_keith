"""The player-facing dice roll.

Keith asks for a check and stops; we post a button; the player taps it and sees the
arithmetic; the result starts a fresh turn where Keith narrates the consequence.
Rolling for yourself is most of the fun of a dice game, and doing it inside his turn
made it invisible.

Everything here renders as HTML with escaped values: character names are free text
typed by players and roll reasons are written by the model, so neither can be
trusted not to contain formatting characters.
"""

from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from src.bot.context import get_repo, get_service, reply
from src.game.characters import xp_for_check
from src.game.dice import DiceInstruction, roll_instruction
from src.log import get_logger
from src.storage.repo import Character, PendingRoll

log = get_logger(__name__)

ROLL_PREFIX = "roll:"


def roll_keyboard(pending: PendingRoll) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎲 Roll", callback_data=f"{ROLL_PREFIX}{pending.id}")]]
    )


def describe_check(pending: PendingRoll, character: Character) -> str:
    line = (
        f"🎲 <b>{html.escape(character.name)}</b> — "
        f"{html.escape(pending.ability.upper())} check, DC {pending.dc}"
    )
    if pending.reason:
        line += f"\n<i>{html.escape(pending.reason)}</i>"
    return line


async def prompt_for_roll(message: Message, pending: PendingRoll, character: Character) -> None:
    """Post the button that hands the dice to the player."""
    await message.reply_text(
        describe_check(pending, character),
        parse_mode=ParseMode.HTML,
        reply_markup=roll_keyboard(pending),
    )


def verdict_for(total: int, natural: int, dc: int) -> str:
    """Whether the check passed, noting a natural 20 or 1 without hiding the result.

    A natural 20 that still misses a DC 20 is a failure; saying only "NATURAL 20"
    would have Keith narrate a triumph over a failed check.
    """
    outcome = "SUCCESS" if total >= dc else "FAILURE"
    if natural == 20:
        return f"{outcome} (natural 20)"
    if natural == 1:
        return f"{outcome} (natural 1)"
    return outcome


async def handle_roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Someone tapped the 🎲 button."""
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    roll_id = int((query.data or "").removeprefix(ROLL_PREFIX) or 0)
    repo = get_repo(context)

    # Ownership check and claim are one atomic step, so a bystander's mis-tap can't
    # consume someone else's roll and a double tap can't roll twice.
    pending, character = await repo.claim_pending_roll(roll_id, user.id)
    if pending is None or character is None:
        if character is not None:
            await query.answer(f"That's {character.name}'s roll, not yours.", show_alert=True)
        else:
            await query.answer("That roll has already been made.", show_alert=True)
        return

    await query.answer()

    result = roll_instruction(
        DiceInstruction(count=1, sides=20, ability=pending.ability),
        ability_modifier=character.modifier(pending.ability),
    )
    natural = result.kept[0] if result.kept else 0
    verdict = verdict_for(result.total, natural, pending.dc)

    modifier = result.ability_modifier
    arithmetic = (
        f"d20 [{natural}] {modifier:+d} {pending.ability.upper()} = {result.total} "
        f"vs DC {pending.dc}"
    )

    await repo.log_roll(
        pending.campaign_id,
        expression=pending.ability,
        detail=f"{arithmetic} — {verdict}",
        total=result.total,
        character_id=character.id,
        source="player",
        reason=pending.reason,
    )

    # Progression is the engine's job, not the DM's. Two models in a row narrated
    # entire sessions without ever awarding XP; a resolved check is a real obstacle
    # and a reliable place to pay for it.
    success = result.total >= pending.dc
    awarded = xp_for_check(pending.dc, success)
    levelled = False
    try:
        character, levelled = await repo.grant_xp(character.id, awarded)
        log.info(
            "xp %s +%s for DC %s check (%s) -> %s total%s",
            character.name,
            awarded,
            pending.dc,
            "success" if success else "failure",
            character.xp,
            ", LEVEL UP" if levelled else "",
        )
    except Exception:
        # The roll is already claimed and logged. Losing the XP is a shame; losing
        # the narration as well would leave the player with nothing at all.
        log.exception("couldn't award XP to %s", character.name)
        awarded = 0
    log.info(
        "player roll %s %s -> %s vs DC %s (%s)",
        character.name,
        pending.ability,
        result.total,
        pending.dc,
        verdict,
    )

    # Replace the button with the result so it can't be tapped again and the numbers
    # stay in the history. Presentation only -- if it fails, the turn must still run,
    # otherwise the roll is consumed and nobody ever hears what happened.
    reward = f"  <i>+{awarded} XP</i>" if awarded else ""
    if levelled:
        reward += f"  🎉 <b>LEVEL {character.level}</b>"
    try:
        await query.edit_message_text(
            f"{describe_check(pending, character)}\n\n"
            f"{html.escape(arithmetic)} — <b>{verdict}</b>{reward}",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        log.warning("couldn't update the roll message for %s", character.name, exc_info=True)

    campaign = await repo.get_campaign(pending.campaign_id)
    if campaign is None or campaign.status == "ended":
        if update.effective_message:
            await update.effective_message.reply_text(
                f"You rolled {result.total} — but that campaign is over."
            )
        return

    outcome = (
        f"{character.name} rolled {result.total} against DC {pending.dc} "
        f"({pending.ability.upper()} check: {pending.reason or 'no reason given'}) — {verdict}. "
        + (
            f"They earned {awarded} XP for the attempt (already awarded — don't call "
            "grant_xp for this roll). "
            if awarded
            else ""
        )
        + (
            f"They have just reached LEVEL {character.level}, max HP {character.max_hp} — "
            "announce it with appropriate drama. "
            if levelled
            else ""
        )
        + "Narrate what happens as a result."
    )
    service = get_service(context)
    await reply(update, context, service, campaign, action=outcome, actor=character)
