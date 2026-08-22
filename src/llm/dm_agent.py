"""The DM agent and the tools it uses to change the world.

Every tool here mutates real database state and returns a short string describing
what happened -- that string is what the model sees, so it doubles as the model's
confirmation that the change stuck. Tools raise ModelRetry (rather than blowing up
the turn) when the model invents a character or item name, so it can correct itself.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from pydantic_ai import Agent, ModelRetry, RunContext

from src.achievements.runtime import AchievementError, award, render_catalogue
from src.game.characters import ABILITY_KEYS
from src.game.dice import DiceParseError, format_roll, parse_dice_expression, roll_instruction
from src.game.genres import Genre
from src.game.memory import render_character
from src.log import get_logger
from src.storage.repo import Campaign, Character, PendingRoll, Repo

log = get_logger(__name__)

#: Structured memory categories. Kept short on purpose -- a long enum invites the
#: model to file everything under the wrong one.
EVENT_KINDS = ("npc", "quest", "decision", "location", "lore")

# Sanity bounds. The model is a narrator, not a trusted caller: a confused one (or
# one talked into it by player text in the transcript) will otherwise happily award
# a billion XP or a +100 sword. Everything out of range comes back as a ModelRetry.
MAX_XP_AWARD = 5_000
MAX_STAT_MOD = 3
MAX_ITEM_QUANTITY = 999
MAX_DICE_COUNT = 100
MAX_DICE_SIDES = 1_000


@dataclass
class GameDeps:
    """Everything a tool needs to act on the current campaign."""

    repo: Repo
    campaign: Campaign
    genre: Genre
    #: The character whose action triggered this turn, if any.
    actor: Character | None = None
    rng: random.Random = field(default_factory=random.Random)
    #: Sound cues raised by tools this turn, drained by the bot layer afterwards.
    cues: list[str] = field(default_factory=list)
    #: Count of tools that changed game state. Tools commit as they go, so if a turn
    #: fails partway this tells us whether the world already moved.
    mutations: int = 0
    #: Set when Keith hands a roll to a player; the bot layer turns it into a button.
    pending_roll: PendingRoll | None = None


dm_agent = Agent(
    deps_type=GameDeps,
    output_type=str,
    retries=2,
)


async def _require_character(ctx: RunContext[GameDeps], name: str) -> Character:
    """Look up a character by name, or tell the model to try again."""
    character = await ctx.deps.repo.find_character_by_name(ctx.deps.campaign.id, name)
    if character is None:
        party = await ctx.deps.repo.list_party(ctx.deps.campaign.id)
        known = ", ".join(c.name for c in party) or "nobody yet"
        raise ModelRetry(
            f"There is no character called {name!r} in this campaign. The party is: {known}."
        )
    return character


@dm_agent.tool
async def get_party(ctx: RunContext[GameDeps]) -> str:
    """Read the current character sheets: HP, abilities, level, XP and inventory."""
    party = await ctx.deps.repo.list_party(ctx.deps.campaign.id)
    if not party:
        return "Nobody has joined this campaign yet."
    return "\n\n".join(render_character(c, ctx.deps.genre) for c in party)


@dm_agent.tool
async def roll_dice(
    ctx: RunContext[GameDeps],
    expression: str,
    character_name: str | None = None,
    reason: str = "",
) -> str:
    """Roll dice. Call this BEFORE narrating an uncertain outcome.

    Args:
        expression: Standard dice notation -- "1d20", "2d6+3", "1d20adv" for
            advantage, "1d20dis" for disadvantage. You may also pass a bare ability
            ("str", "dex", "con", "int", "wis", "cha") to roll 1d20 plus that
            character's modifier.
        character_name: Whose roll this is. Required for ability modifiers to apply.
        reason: What the roll is for, e.g. "climbing the wall, DC 15".
    """
    try:
        instruction = parse_dice_expression(expression)
    except DiceParseError as exc:
        raise ModelRetry(
            f"{exc} Use notation like '1d20', '2d6+3', '1d20adv', or an ability name."
        ) from exc

    if not 1 <= instruction.count <= MAX_DICE_COUNT:
        raise ModelRetry(f"Roll between 1 and {MAX_DICE_COUNT} dice, not {instruction.count}.")
    if not 2 <= instruction.sides <= MAX_DICE_SIDES:
        raise ModelRetry(
            f"Dice need between 2 and {MAX_DICE_SIDES} sides, not {instruction.sides}."
        )

    character = await _require_character(ctx, character_name) if character_name else None

    modifier = 0
    if instruction.ability and character is not None:
        modifier = character.modifier(instruction.ability)
    elif instruction.ability and character is None:
        raise ModelRetry(
            f"Rolling {instruction.ability.upper()} needs a character_name so I know "
            "whose modifier to apply."
        )

    result = roll_instruction(instruction, ability_modifier=modifier, rng=ctx.deps.rng)
    rendered = format_roll(result)

    await ctx.deps.repo.log_roll(
        ctx.deps.campaign.id,
        expression=expression,
        detail=rendered,
        total=result.total,
        character_id=character.id if character else None,
        source="dm",
        reason=reason,
    )
    log.info("roll %s -> %s (%s)", expression, result.total, reason or "no reason given")

    natural = result.kept[0] if result.kept else 0
    if instruction.sides == 20 and len(result.kept) == 1:
        if natural == 20:
            rendered += "  ← NATURAL 20"
        elif natural == 1:
            rendered += "  ← NATURAL 1"
    return rendered


@dm_agent.tool
async def request_roll(
    ctx: RunContext[GameDeps], character_name: str, ability: str, dc: int, reason: str
) -> str:
    """Ask a PLAYER to roll their own d20 check. Use this for their decisions.

    This hands the dice to the player: they get a button, they see the number, and
    you find out the result on your next turn. Use it whenever the outcome depends
    on something their character is choosing to attempt -- sneaking, persuading,
    climbing, disarming, resisting.

    Call it at most once per turn, and END YOUR TURN immediately after. Narrate up
    to the moment of tension and stop; do not guess the outcome or describe what
    happens next. You'll be told the result and can narrate the consequence then.

    For anything that isn't the player's own attempt -- damage dice, monster
    attacks, NPC checks, random chance -- use roll_dice instead and keep going.

    Args:
        character_name: Whose check it is.
        ability: One of str, dex, con, int, wis, cha.
        dc: Target number. 10 routine, 15 hard, 20 heroic.
        reason: What they're attempting, shown on the button.
    """
    ability = ability.strip().lower()
    if ability not in ABILITY_KEYS:
        raise ModelRetry(f"{ability!r} isn't an ability. Use one of: {', '.join(ABILITY_KEYS)}.")
    if not 1 <= dc <= 30:
        raise ModelRetry(f"A DC of {dc} is off the scale. Use 5-30, usually 10-20.")
    if ctx.deps.pending_roll is not None:
        # Only one button gets posted per turn, so a second request would leave a
        # character owing a roll nothing ever offers them.
        raise ModelRetry(
            "You've already asked for a roll this turn. Finish your reply and wait "
            "for it; ask anyone else next turn."
        )

    character = await _require_character(ctx, character_name)
    pending = await ctx.deps.repo.create_pending_roll(
        ctx.deps.campaign.id, character.id, ability, dc, reason
    )
    ctx.deps.pending_roll = pending
    ctx.deps.mutations += 1
    log.info("roll requested from %s: %s DC %s (%s)", character.name, ability, dc, reason)

    return (
        f"Asked {character.name} to roll {ability.upper()} against DC {dc}. "
        "Finish your reply at the moment of tension and stop -- do not narrate the "
        "outcome. You'll be told what they rolled."
    )


@dm_agent.tool
async def update_hp(ctx: RunContext[GameDeps], character_name: str, delta: int, reason: str) -> str:
    """Apply damage or healing. Negative delta damages, positive heals.

    Args:
        character_name: Who is being hurt or healed.
        delta: HP change. -6 for six damage, +4 for four healing.
        reason: What did it, e.g. "goblin spear".
    """
    character = await _require_character(ctx, character_name)
    updated = await ctx.deps.repo.apply_damage(character.id, delta)
    ctx.deps.mutations += 1

    verb = "takes" if delta < 0 else "recovers"
    log.info(
        "hp %s %s %+d -> %s/%s (%s)", updated.name, verb, delta, updated.hp, updated.max_hp, reason
    )
    note = f"{updated.name} {verb} {abs(delta)} HP ({reason}) → {updated.hp}/{updated.max_hp}"
    if updated.status == "dying":
        note += ". They are DOWN and dying -- narrate that, don't kill them outright."
    return note


@dm_agent.tool
async def grant_xp(ctx: RunContext[GameDeps], character_name: str, amount: int, reason: str) -> str:
    """Award XP after a real obstacle. Roughly 25-100 for a scene.

    Args:
        character_name: Who earned it.
        amount: XP to award. Must be positive, and at most 5000.
        reason: What they did to earn it.
    """
    if amount <= 0:
        raise ModelRetry("XP awards must be positive.")
    if amount > MAX_XP_AWARD:
        raise ModelRetry(
            f"{amount} XP is far too much for one turn -- the cap is {MAX_XP_AWARD}. "
            "A whole scene is worth 25-100."
        )

    character = await _require_character(ctx, character_name)
    updated, levelled = await ctx.deps.repo.grant_xp(character.id, amount)
    ctx.deps.mutations += 1

    log.info(
        "xp %s +%s -> %s total, level %s (%s)",
        updated.name,
        amount,
        updated.xp,
        updated.level,
        reason,
    )
    note = f"{updated.name} gains {amount} XP ({reason}) → {updated.xp} total"
    if levelled:
        ctx.deps.cues.append("reward")
        note += (
            f". LEVEL UP: now level {updated.level}, max HP {updated.max_hp}. "
            "Announce this with appropriate drama."
        )
    return note


@dm_agent.tool
async def add_item(
    ctx: RunContext[GameDeps],
    character_name: str,
    name: str,
    description: str = "",
    kind: str = "misc",
    quantity: int = 1,
    equippable: bool = False,
    consumable: bool = False,
    stat_mods: dict[str, int] | None = None,
) -> str:
    """Give a character an item -- loot, a gift, a purchase.

    Args:
        character_name: Who receives it.
        name: The item's name. Reuse the exact name to stack or upgrade an existing one.
        description: What it is and what it does.
        kind: Loose category -- weapon, armour, tool, treasure, misc.
        quantity: How many.
        equippable: True if it can be worn or wielded.
        consumable: True if using it uses it up.
        stat_mods: Ability bonuses while equipped, e.g. {"str": 1}. Between -3 and 3.
    """
    if not 1 <= quantity <= MAX_ITEM_QUANTITY:
        raise ModelRetry(f"Quantity must be between 1 and {MAX_ITEM_QUANTITY}.")

    for ability, mod in (stat_mods or {}).items():
        if ability not in ABILITY_KEYS:
            raise ModelRetry(f"{ability!r} isn't an ability. Use: {', '.join(ABILITY_KEYS)}.")
        if abs(mod) > MAX_STAT_MOD:
            raise ModelRetry(
                f"A {mod:+d} bonus to {ability.upper()} is too strong -- "
                f"keep item modifiers between -{MAX_STAT_MOD} and +{MAX_STAT_MOD}."
            )

    character = await _require_character(ctx, character_name)
    item = await ctx.deps.repo.add_item(
        character.id,
        name,
        kind=kind,
        description=description,
        quantity=quantity,
        equippable=equippable,
        consumable=consumable,
        stat_mods=stat_mods,
    )
    ctx.deps.mutations += 1
    ctx.deps.cues.append("reward")
    log.info("item +%s x%s to %s", item.name, item.quantity, character.name)
    return f"{character.name} now carries {item.name} x{item.quantity}."


@dm_agent.tool
async def remove_item(
    ctx: RunContext[GameDeps], character_name: str, item_name: str, quantity: int = 1
) -> str:
    """Remove an item -- consumed, dropped, stolen or destroyed.

    Args:
        character_name: Who loses it.
        item_name: The item's name as it appears on their sheet.
        quantity: How many to remove.
    """
    character = await _require_character(ctx, character_name)
    removed = await ctx.deps.repo.remove_item(character.id, item_name, quantity=quantity)
    if not removed:
        carried = ", ".join(i.name for i in character.items) or "nothing"
        raise ModelRetry(f"{character.name} isn't carrying {item_name!r}. They have: {carried}.")
    ctx.deps.mutations += 1
    log.info("item -%s x%s from %s", item_name, quantity, character.name)
    return f"{character.name} no longer has {item_name} (x{quantity})."


@dm_agent.tool
async def record_event(
    ctx: RunContext[GameDeps], kind: str, summary: str, entities: list[str] | None = None
) -> str:
    """Remember something that must survive past the recent-turns window.

    Use this for named NPCs, accepted quests, promises made, places discovered, and
    decisions with consequences. This is your long-term memory -- if it isn't
    recorded here, you will forget it.

    Args:
        kind: One of npc, quest, decision, location, lore.
        summary: One sentence, written so it still makes sense fifty turns later.
        entities: Names involved, for later lookup.
    """
    if kind not in EVENT_KINDS:
        raise ModelRetry(f"kind must be one of: {', '.join(EVENT_KINDS)}.")

    await ctx.deps.repo.add_story_event(ctx.deps.campaign.id, kind, summary, entities or [])
    ctx.deps.mutations += 1
    if kind == "quest":
        ctx.deps.cues.append("new_quest")
    log.info("event [%s] %s", kind, summary)
    return f"Recorded [{kind}]: {summary}"


@dm_agent.tool
async def list_achievements(ctx: RunContext[GameDeps], character_name: str | None = None) -> str:
    """The catalogue of achievements you can award, with their ids.

    Call this only when something has happened that deserves an award -- the list is
    long, and there's no reason to read it on an ordinary turn. Pass a character name
    to hide the ones they already have.
    """
    earned: set[str] = set()
    if character_name:
        character = await _require_character(ctx, character_name)
        earned = {aid for aid, _ in await ctx.deps.repo.list_achievements(character.id)}
    return render_catalogue(exclude=earned)


@dm_agent.tool
async def award_achievement(
    ctx: RunContext[GameDeps], character_name: str, achievement_id: str
) -> str:
    """Award an achievement for a standout moment. Returns the 🏆 block to include.

    Only award these for genuinely notable beats -- a spectacular success or failure,
    a first, a disaster. Not every turn. Get valid ids from list_achievements.

    Args:
        character_name: Who earned it.
        achievement_id: An id from list_achievements.
    """
    character = await _require_character(ctx, character_name)
    try:
        block = await award(ctx.deps.repo, ctx.deps.campaign, character, achievement_id)
    except AchievementError as exc:
        raise ModelRetry(str(exc)) from exc

    if block is None:
        return (
            f"{character.name} already has {achievement_id!r}. Acknowledge it playfully "
            "instead, or pick a different one."
        )
    ctx.deps.mutations += 1
    ctx.deps.cues.append("new_achievement")
    log.info("achievement %s -> %s", achievement_id, character.name)
    return f"Award granted. Include this block verbatim at the top of your reply:\n\n{block}"
