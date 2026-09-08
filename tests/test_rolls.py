"""The player-facing roll: two-phase turns, ownership, and no double rolls."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.bot.context import REPO_KEY, SERVICE_KEY
from src.bot.rolls import ROLL_PREFIX, handle_roll
from src.game.memory import (
    ROLL_GAP_MESSAGES,
    ROLL_GRACE_MESSAGES,
    build_turn_context,
    render_pacing,
)
from src.game.session import GameService, TurnFailed
from src.llm.dm_agent import dm_agent
from src.storage.repo import Campaign, Character, Repo
from tests.test_dm_tools import deps_for, scripted, tool_outputs

# -- the repo layer ---------------------------------------------------------


async def test_a_pending_roll_can_only_be_claimed_once(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")

    taken, character = await repo.claim_pending_roll(pending.id, hero.user_id)
    assert taken is not None and taken.dc == 12
    assert character is not None and character.id == hero.id

    assert await repo.claim_pending_roll(pending.id, hero.user_id) == (None, None)


async def test_simultaneous_taps_only_yield_one_roll(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """Two fast taps must not both come back with a roll to make."""
    pending = await repo.create_pending_roll(campaign.id, hero.id, "str", 15, "the door")

    results = await asyncio.gather(
        *(repo.claim_pending_roll(pending.id, hero.user_id) for _ in range(5))
    )
    assert sum(1 for roll, _ in results if roll is not None) == 1


async def test_claiming_someone_elses_roll_leaves_it_alone(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")

    roll, character = await repo.claim_pending_roll(pending.id, user_id=999)
    assert roll is None
    # The owner comes back so the caller can say whose roll it is...
    assert character is not None and character.name == "Thorn"
    # ...and it's still there for them.
    claimed, _ = await repo.claim_pending_roll(pending.id, hero.user_id)
    assert claimed is not None


async def test_a_second_request_replaces_the_first_and_retires_its_id(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """The superseded button must not claim the new roll.

    Ids are AUTOINCREMENT and the replace is delete-then-insert, so the old id is
    never handed out again -- otherwise tapping a stale button would roll the wrong
    ability against the wrong DC.
    """
    first = await repo.create_pending_roll(campaign.id, hero.id, "dex", 10, "the ledge")
    second = await repo.create_pending_roll(campaign.id, hero.id, "wis", 18, "the whisper")

    assert second.id != first.id
    assert (second.ability, second.dc) == ("wis", 18)

    # Tapping the old button finds nothing.
    assert await repo.claim_pending_roll(first.id, hero.user_id) == (None, None)

    # The new one is intact and unchanged.
    claimed, _ = await repo.claim_pending_roll(second.id, hero.user_id)
    assert claimed is not None
    assert (claimed.ability, claimed.dc) == ("wis", 18)


async def test_two_characters_can_each_owe_a_roll(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    other = await repo.create_character(campaign.id, user_id=2, name="Vex")
    a = await repo.create_pending_roll(campaign.id, hero.id, "str", 12, "shove")
    b = await repo.create_pending_roll(campaign.id, other.id, "dex", 14, "dodge")

    assert a.id != b.id
    assert (await repo.claim_pending_roll(a.id, hero.user_id))[0] is not None
    assert (await repo.claim_pending_roll(b.id, other.user_id))[0] is not None


async def test_clearing_drops_every_outstanding_roll(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """/endgame must not leave buttons that roll into a retired campaign."""
    other = await repo.create_character(campaign.id, user_id=2, name="Vex")
    a = await repo.create_pending_roll(campaign.id, hero.id, "str", 12, "shove")
    b = await repo.create_pending_roll(campaign.id, other.id, "dex", 14, "dodge")

    await repo.clear_pending_rolls(campaign.id)

    assert await repo.claim_pending_roll(a.id, hero.user_id) == (None, None)
    assert await repo.claim_pending_roll(b.id, other.user_id) == (None, None)


# -- the tool ---------------------------------------------------------------


async def test_request_roll_records_and_reports_back(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    deps = deps_for(repo, campaign, hero)
    model = scripted(
        (
            "request_roll",
            {"character_name": "Thorn", "ability": "dex", "dc": 14, "reason": "the ledge"},
        )
    )
    result = await dm_agent.run("I edge along the ledge", model=model, deps=deps)

    note = tool_outputs(result.all_messages())[0]
    assert "DEX" in note and "14" in note
    assert "do not narrate the outcome" in note

    assert deps.pending_roll is not None
    assert deps.pending_roll.ability == "dex"


@pytest.mark.parametrize(
    ("ability", "dc", "expected"),
    [("luck", 12, "isn't an ability"), ("dex", 99, "off the scale")],
)
async def test_request_roll_validates_its_arguments(
    repo: Repo, campaign: Campaign, hero: Character, ability: str, dc: int, expected: str
) -> None:
    model = scripted(
        ("request_roll", {"character_name": "Thorn", "ability": ability, "dc": dc, "reason": "x"})
    )
    result = await dm_agent.run("I try", model=model, deps=deps_for(repo, campaign, hero))
    assert any(expected in o for o in tool_outputs(result.all_messages()))


async def test_the_turn_reports_the_pending_roll_to_the_bot(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """The bot layer needs to know to post a button."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for message in messages:
            for part in getattr(message, "parts", []):
                if part.part_kind == "tool-return":
                    return ModelResponse(parts=[TextPart("You reach for the ledge…")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "request_roll",
                    {"character_name": "Thorn", "ability": "dex", "dc": 14, "reason": "ledge"},
                )
            ]
        )

    service = GameService(repo, "test:function", model=FunctionModel(respond))
    result = await service.take_turn(campaign, "I edge along", hero)

    assert result.pending_roll is not None
    assert result.pending_roll.dc == 14
    assert result.reply == "You reach for the ledge…"


# -- the button -------------------------------------------------------------


@pytest.fixture
def context(repo: Repo) -> MagicMock:
    ctx = MagicMock()
    ctx.application.bot_data = {
        REPO_KEY: repo,
        SERVICE_KEY: GameService(
            repo,
            "test:function",
            model=FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("You make it.")])),
        ),
    }
    ctx.bot.send_chat_action = AsyncMock()
    ctx.chat_data = {}
    return ctx


def tap(roll_id: int, user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = -1
    update.callback_query.data = f"{ROLL_PREFIX}{roll_id}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.effective_message.reply_text = AsyncMock()
    return update


async def test_tapping_your_own_roll_rolls_it(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    await repo.update_campaign(campaign.id, status="active")
    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")

    update = tap(pending.id, hero.user_id)
    await handle_roll(update, context)

    shown = update.callback_query.edit_message_text.await_args.args[0]
    assert "DEX" in shown
    assert "DC 12" in shown
    # The arithmetic is shown, not just a verdict: d20 [n] +2 DEX = total.
    assert "d20 [" in shown and "+2 DEX" in shown

    # The roll is recorded as the player's, and Keith narrated the consequence.
    cur = await repo.conn.execute("SELECT source, character_id FROM dice_rolls")
    rows = await cur.fetchall()
    assert rows[0]["source"] == "player"
    assert rows[0]["character_id"] == hero.id
    update.effective_message.reply_text.assert_awaited()


async def test_someone_elses_roll_is_refused(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")

    update = tap(pending.id, user_id=999)
    await handle_roll(update, context)

    update.callback_query.answer.assert_awaited_once()
    assert "Thorn's roll" in update.callback_query.answer.await_args.args[0]

    # And it's still there for the rightful owner.
    claimed, _ = await repo.claim_pending_roll(pending.id, hero.user_id)
    assert claimed is not None


async def test_a_stale_button_says_so(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    update = tap(4242, hero.user_id)
    await handle_roll(update, context)

    assert "already been made" in update.callback_query.answer.await_args.args[0]
    update.effective_message.reply_text.assert_not_awaited()


async def test_tapping_twice_only_rolls_once(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    await repo.update_campaign(campaign.id, status="active")
    pending = await repo.create_pending_roll(campaign.id, hero.id, "str", 10, "the door")

    await handle_roll(tap(pending.id, hero.user_id), context)
    second = tap(pending.id, hero.user_id)
    await handle_roll(second, context)

    cur = await repo.conn.execute("SELECT count(*) AS n FROM dice_rolls")
    row = await cur.fetchone()
    assert row is not None and row["n"] == 1
    assert "already been made" in second.callback_query.answer.await_args.args[0]


async def test_the_roll_uses_the_characters_modifier(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """Thorn has DEX 15, so every roll carries a visible +2."""
    await repo.update_campaign(campaign.id, status="active")
    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")

    await handle_roll(tap(pending.id, hero.user_id), context)

    cur = await repo.conn.execute("SELECT total, detail FROM dice_rolls")
    row = await cur.fetchone()
    assert row is not None
    assert "+2 DEX" in row["detail"]

    # And the recorded total really is the die plus that modifier.
    die = int(row["detail"].split("[")[1].split("]")[0])
    assert row["total"] == die + 2


async def test_an_equipped_item_changes_the_roll(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """Starting gear grants +1, and it has to reach the dice."""
    await repo.update_campaign(campaign.id, status="active")
    await repo.add_item(hero.id, "Twin daggers", equippable=True, stat_mods={"dex": 1})
    await repo.set_equipped(hero.id, "Twin daggers", True)

    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")
    await handle_roll(tap(pending.id, hero.user_id), context)

    cur = await repo.conn.execute("SELECT detail FROM dice_rolls")
    row = await cur.fetchone()
    assert row is not None
    assert "+3 DEX" in row["detail"]  # DEX 15 + 1 from the daggers -> 16 -> +3


@pytest.mark.parametrize(
    ("total", "natural", "dc", "expected"),
    [
        (19, 20, 20, "FAILURE (natural 20)"),
        (21, 20, 20, "SUCCESS (natural 20)"),
        (12, 1, 10, "SUCCESS (natural 1)"),
        (5, 1, 10, "FAILURE (natural 1)"),
        (15, 12, 12, "SUCCESS"),
        (8, 7, 12, "FAILURE"),
    ],
)
def test_a_natural_twenty_does_not_hide_a_failed_check(
    total: int, natural: int, dc: int, expected: str
) -> None:
    """A natural 20 that still misses the DC is a failure, and must read as one."""
    from src.bot.rolls import verdict_for

    assert verdict_for(total, natural, dc) == expected


async def test_the_turn_still_runs_if_the_message_edit_fails(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """The edit is cosmetic. Losing it must not swallow the consequence.

    The roll is already claimed and logged by then, so an exception here would eat
    the player's turn with no way to get it back.
    """
    from telegram.error import BadRequest

    await repo.update_campaign(campaign.id, status="active")
    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")

    update = tap(pending.id, hero.user_id)
    update.callback_query.edit_message_text = AsyncMock(
        side_effect=BadRequest("message to edit not found")
    )

    await handle_roll(update, context)

    update.effective_message.reply_text.assert_awaited()


async def test_tapping_after_the_campaign_ended_says_so(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    await repo.update_campaign(campaign.id, status="active")
    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")
    await repo.end_campaign(campaign.chat_id)

    update = tap(pending.id, hero.user_id)
    await handle_roll(update, context)

    said = update.effective_message.reply_text.await_args.args[0]
    assert "campaign is over" in said


async def test_formatting_characters_in_names_and_reasons_survive(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """Names are player text and reasons are model text; neither can be trusted.

    An unescaped '<' or '&' would make Telegram reject the message, and the button
    would never appear for a roll Keith just asked for.
    """
    from src.bot.rolls import describe_check

    await repo.update_character(hero.id, name="Za<rok> & Sons")
    character = await repo.get_character_by_id(hero.id)
    assert character is not None

    pending = await repo.create_pending_roll(
        campaign.id, hero.id, "dex", 12, "slip past the <very> alert dog & its friend"
    )
    rendered = describe_check(pending, character)

    assert "&lt;rok&gt;" in rendered
    assert "&amp;" in rendered
    # The only angle brackets left are our own tags.
    assert "<very>" not in rendered


async def test_the_button_carries_the_roll_id_back(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """The callback_data round-trip is the whole mechanism; nothing else checks it."""
    from src.bot.rolls import prompt_for_roll, roll_keyboard

    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")

    button = roll_keyboard(pending).inline_keyboard[0][0]
    assert button.callback_data == f"{ROLL_PREFIX}{pending.id}"
    assert int(button.callback_data.removeprefix(ROLL_PREFIX)) == pending.id

    message = MagicMock()
    message.reply_text = AsyncMock()
    await prompt_for_roll(message, pending, hero)

    message.reply_text.assert_awaited_once()
    text = message.reply_text.await_args.args[0]
    assert "Thorn" in text and "DEX" in text and "DC 12" in text and "the ledge" in text
    assert message.reply_text.await_args.kwargs["reply_markup"] is not None


# -- XP from checks ---------------------------------------------------------


async def test_resolving_a_check_awards_xp(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """The engine pays for checks, because two different models never did."""
    from src.game.characters import xp_for_check

    await repo.update_campaign(campaign.id, status="active")
    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 15, "the ledge")

    update = tap(pending.id, hero.user_id)
    await handle_roll(update, context)

    character = await repo.get_character_by_id(hero.id)
    assert character is not None
    total = await repo.conn.execute("SELECT total FROM dice_rolls")
    row = await total.fetchone()
    assert row is not None

    expected = xp_for_check(15, row["total"] >= 15)
    assert character.xp == expected
    assert expected > 0

    # The player is told, and so is Keith.
    assert f"+{expected} XP" in update.callback_query.edit_message_text.await_args.args[0]


async def test_failing_a_check_still_pays_something(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """A run of bad luck shouldn't stall progression completely."""
    from src.game.characters import XP_PER_DC_FAILURE, XP_PER_DC_SUCCESS, xp_for_check

    assert xp_for_check(15, success=False) == 15 * XP_PER_DC_FAILURE
    assert xp_for_check(15, success=True) == 15 * XP_PER_DC_SUCCESS
    assert xp_for_check(15, success=False) < xp_for_check(15, success=True)
    # Harder checks pay more.
    assert xp_for_check(20, success=True) > xp_for_check(10, success=True)


async def test_enough_checks_produce_a_level_up(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """The whole point: progression that actually happens.

    Ten DC 15 checks pay at least 300 XP even if every one of them fails, so this
    doesn't depend on how the dice land.
    """
    from src.game.characters import XP_THRESHOLDS, xp_for_check

    checks = 10
    assert checks * xp_for_check(15, success=False) >= XP_THRESHOLDS[1]

    await repo.update_campaign(campaign.id, status="active")
    for _ in range(checks):
        pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 15, "another ledge")
        await handle_roll(tap(pending.id, hero.user_id), context)

    character = await repo.get_character_by_id(hero.id)
    assert character is not None
    assert character.level >= 2
    assert character.max_hp > hero.max_hp


async def test_a_failed_xp_award_still_narrates_the_roll(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock, monkeypatch
) -> None:
    """The roll is claimed and logged before XP; losing the award mustn't also lose
    the narration and leave the player with nothing."""
    await repo.update_campaign(campaign.id, status="active")
    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")

    async def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("database went away")

    monkeypatch.setattr(repo, "grant_xp", boom)

    update = tap(pending.id, hero.user_id)
    await handle_roll(update, context)

    update.effective_message.reply_text.assert_awaited()
    shown = update.callback_query.edit_message_text.await_args.args[0]
    assert "XP" not in shown  # no award to report


# -- engine-awarded achievements --------------------------------------------


async def test_a_first_check_unlocks_something(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """Leaving achievements to the DM meant they stopped happening entirely."""
    await repo.update_campaign(campaign.id, status="active")
    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")

    update = tap(pending.id, hero.user_id)
    await handle_roll(update, context)

    earned = await repo.list_achievements(hero.id)
    assert "first-of-many" in {aid for aid, _ in earned}

    # And the 🏆 block is actually posted to the chat.
    posted = [c.args[0] for c in update.effective_message.reply_text.await_args_list]
    assert any("ACHIEVEMENT UNLOCKED" in p for p in posted)


async def test_a_crit_and_a_fumble_each_unlock_once(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock, monkeypatch
) -> None:
    from src.game import dice

    await repo.update_campaign(campaign.id, status="active")

    def always(value: int) -> object:
        return lambda self, a, b: value

    for natural, expected in ((20, "natural-twenty"), (1, "natural-one")):
        monkeypatch.setattr(dice.random.Random, "randint", always(natural))
        for _ in range(2):  # twice: it must only ever be awarded once
            pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "again")
            await handle_roll(tap(pending.id, hero.user_id), context)

        earned = [aid for aid, _ in await repo.list_achievements(hero.id)]
        assert earned.count(expected) == 1


async def test_levelling_unlocks_the_progression_achievement(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    await repo.update_campaign(campaign.id, status="active")
    for _ in range(10):
        pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 15, "a ledge")
        await handle_roll(tap(pending.id, hero.user_id), context)

    earned = {aid for aid, _ in await repo.list_achievements(hero.id)}
    assert "ascending" in earned


def test_engine_achievements_are_hidden_from_the_dms_catalogue() -> None:
    """Keith shouldn't be able to hand out ones the engine owns."""
    from src.achievements.runtime import load_registry, render_catalogue

    catalogue = render_catalogue()
    triggered = [a for a in load_registry().values() if a.trigger]
    assert triggered, "expected some engine-triggered achievements"

    for achievement in triggered:
        assert achievement.id not in catalogue

    # But there are still plenty for him to choose from.
    assert catalogue.count("\n") > 20


def test_the_registry_is_written_for_a_dungeon() -> None:
    """It used to be the old chatbot's: fridges, CSVs and mode switches."""
    from src.achievements.runtime import load_registry

    registry = load_registry()
    assert len(registry) >= 30

    tags = {t for a in registry.values() for t in a.tags}
    assert {"combat", "loot", "exploration"} <= tags
    assert not {"analysis", "mode"} & tags  # the old bot's vocabulary

    rarities = {a.rarity for a in registry.values()}
    assert rarities == {"common", "uncommon", "rare", "epic", "mythic"}


# -- pacing: keeping the dice from going quiet -------------------------------


def test_pacing_says_nothing_in_the_opening_messages() -> None:
    """Early on, no rolls yet is just early -- not a problem to point at."""
    assert render_pacing(messages_so_far=4, messages_since_roll=None) == ""


def test_pacing_notices_a_campaign_that_has_never_rolled() -> None:
    nudge = render_pacing(messages_so_far=20, messages_since_roll=None)
    assert "request_roll" in nudge
    assert "20 messages" in nudge


def test_pacing_notices_the_dice_going_quiet() -> None:
    """The bug this replaces: one early roll silenced the nudge forever."""
    nudge = render_pacing(messages_so_far=61, messages_since_roll=7)
    assert "request_roll" in nudge
    # The gap, not the campaign length: 61 contains "7" nowhere, so this can only
    # pass if the right number was interpolated.
    assert "7 messages" in nudge


def test_pacing_is_quiet_while_a_button_is_waiting_to_be_pressed() -> None:
    """Players can type instead of tapping, so this state lasts. Nudging through it
    would make Keith ask again every turn -- and each new request replaces the
    pending row, killing the button already sitting in the chat."""
    assert render_pacing(60, 40, roll_outstanding=True) == ""
    assert render_pacing(60, None, roll_outstanding=True) == ""
    # ...but it is genuinely quiet once that check has been taken.
    assert render_pacing(60, 40, roll_outstanding=False) != ""


async def test_an_unpressed_button_silences_the_nudge_end_to_end(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    for _ in range(ROLL_GRACE_MESSAGES):
        await repo.add_message(campaign.id, "player", "we press on", character_id=hero.id)
    assert "## Pacing" in await build_turn_context(repo, campaign)

    pending = await repo.create_pending_roll(campaign.id, hero.id, "dex", 12, "the ledge")
    assert await repo.has_pending_roll(campaign.id) is True
    assert "## Pacing" not in await build_turn_context(repo, campaign)

    await repo.claim_pending_roll(pending.id, hero.user_id)
    assert await repo.has_pending_roll(campaign.id) is False


async def test_keiths_own_rolls_do_not_count_as_the_party_rolling(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """The whole point is players touching dice. If a DM roll moved the watermark,
    Keith could silence the nudge by rolling for himself -- and the original bug,
    a campaign where nobody is ever asked to roll, would come back invisibly."""
    for _ in range(ROLL_GRACE_MESSAGES):
        await repo.add_message(campaign.id, "player", "we press on", character_id=hero.id)

    await repo.log_roll(
        campaign.id,
        expression="2d6+3",
        detail="goblin spear",
        total=9,
        character_id=hero.id,
        source="dm",
    )

    assert await repo.messages_since_last_player_roll(campaign.id) is None
    assert "## Pacing" in await build_turn_context(repo, campaign)


def test_pacing_is_quiet_just_after_a_roll() -> None:
    """It has to be able to switch off, or it becomes standing pressure."""
    assert render_pacing(messages_so_far=60, messages_since_roll=1) == ""


async def test_pacing_recovers_after_a_roll_and_fires_again_later(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """End to end against the database: quiet, then rolled, then quiet again.

    The old nudge keyed off a character having 0 XP, so a single resolved check
    disabled it permanently. This asserts it comes back.
    """
    for _ in range(ROLL_GRACE_MESSAGES):
        await repo.add_message(campaign.id, "player", "we press on", character_id=hero.id)

    assert await repo.messages_since_last_player_roll(campaign.id) is None
    context = await build_turn_context(repo, campaign)
    assert "## Pacing" in context

    await repo.log_roll(
        campaign.id,
        expression="dex",
        detail="d20 [12] +2 DEX = 14",
        total=14,
        character_id=hero.id,
        source="player",
        reason="the ledge",
    )
    # XP has been earned by now, which is precisely what used to silence the nudge.
    await repo.grant_xp(hero.id, 50)
    assert await repo.messages_since_last_player_roll(campaign.id) == 0
    assert "## Pacing" not in await build_turn_context(repo, campaign)

    for _ in range(ROLL_GAP_MESSAGES + 1):
        await repo.add_message(campaign.id, "player", "and on", character_id=hero.id)

    assert await repo.messages_since_last_player_roll(campaign.id) >= ROLL_GAP_MESSAGES
    assert "## Pacing" in await build_turn_context(repo, campaign)


# -- a roll whose button never arrives ---------------------------------------


async def _outstanding(repo: Repo, campaign_id: int, character_id: int) -> int:
    """How many rolls this character currently owes. Read directly: there is no
    repo method for it, and adding one purely for a test would be scope creep."""
    cur = await repo.conn.execute(
        "SELECT count(*) AS n FROM pending_rolls WHERE campaign_id = ? AND character_id = ?",
        (campaign_id, character_id),
    )
    row = await cur.fetchone()
    return int(row["n"])


def _roll_then(text: str | None) -> FunctionModel:
    """Requests a roll, then answers with `text` -- or raises if None."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for message in messages:
            for part in getattr(message, "parts", []):
                if part.part_kind == "tool-return":
                    if text is None:
                        raise RuntimeError("the provider fell over")
                    return ModelResponse(parts=[TextPart(text)])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "request_roll",
                    {"character_name": "Thorn", "ability": "dex", "dc": 14, "reason": "ledge"},
                )
            ]
        )

    return FunctionModel(respond)


async def test_a_failed_turn_does_not_leave_a_roll_nobody_can_make(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """The button is posted from the result, so a turn that never returns one
    would strand the row -- and the character would silently owe a check."""
    service = GameService(repo, "test:function", model=_roll_then(None))

    with pytest.raises(TurnFailed):
        await service.take_turn(campaign, "I edge along", hero)

    assert await _outstanding(repo, campaign.id, hero.id) == 0


async def test_an_empty_turn_does_not_leave_a_roll_nobody_can_make(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """A blank reply is surfaced as "Keith says nothing" and posts no button, so it
    strands the row exactly as an exception does.

    Whitespace rather than "": pydantic-ai treats a truly empty output as no output
    and retries into UnexpectedModelBehavior, which is the exception path above.
    Whitespace is accepted as an answer and only becomes empty when we strip it,
    which is precisely the case the session layer has to catch for itself."""
    service = GameService(repo, "test:function", model=_roll_then("   \n  "))
    result = await service.take_turn(campaign, "I edge along", hero)

    assert result.reply == ""
    assert result.pending_roll is None
    assert await _outstanding(repo, campaign.id, hero.id) == 0


async def test_a_roll_survives_a_turn_that_actually_worked(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """The cleanup must not fire on the happy path."""
    service = GameService(repo, "test:function", model=_roll_then("You reach out…"))
    result = await service.take_turn(campaign, "I edge along", hero)

    assert result.pending_roll is not None
    assert await _outstanding(repo, campaign.id, hero.id) == 1
