"""End-to-end: /newgame → /join → /begin → play, driven through the real handlers.

Telegram objects are stand-ins and the model is scripted, but the handler bodies,
the conversation transitions, the repo and the agent are all the real thing.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.bot.commands import (
    CANCEL_NEW,
    CONFIRM_NEW,
    GENRE_PREFIX,
    handle_begin,
    handle_endgame,
    handle_genre_choice,
    handle_newgame,
    handle_newgame_confirm,
    handle_sheet,
)
from src.bot.context import REPO_KEY, SERVICE_KEY
from src.bot.creation import (
    ARCHETYPE_PREFIX,
    ORIGIN_PREFIX,
    choose_archetype,
    choose_origin,
    receive_name,
    start_join,
)
from src.bot.play import handle_play
from src.game.session import GameService
from src.storage.repo import Repo

CHAT_ID = -100999
USER_ID = 7


def narrating_model(reply: str = "The tavern door bangs shut behind you.") -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(reply)])

    return FunctionModel(respond)


@pytest.fixture
def context(repo: Repo) -> MagicMock:
    """A handler context wired to the real repo and a scripted game service."""
    ctx = MagicMock()
    ctx.application.bot_data = {
        REPO_KEY: repo,
        SERVICE_KEY: GameService(repo, "test:function", model=narrating_model()),
    }
    ctx.user_data = {}
    ctx.chat_data = {}
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


def message_update(text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = CHAT_ID
    update.effective_user.id = USER_ID
    update.effective_user.username = "hank"
    update.effective_user.first_name = "Hank"
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_message = update.message
    update.effective_message.entities = []
    return update


def callback_update(data: str) -> MagicMock:
    update = message_update()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


async def _play_through(context: MagicMock) -> Any:
    """/newgame → fantasy → /join → Fighter → Human → name → /begin."""
    await handle_newgame(message_update(), context)
    await handle_genre_choice(callback_update(f"{GENRE_PREFIX}fantasy"), context)

    await start_join(message_update(), context)
    await choose_archetype(callback_update(f"{ARCHETYPE_PREFIX}Fighter"), context)
    await choose_origin(callback_update(f"{ORIGIN_PREFIX}Human"), context)
    await receive_name(message_update("Bramble\nAfraid of geese."), context)

    begin = message_update()
    await handle_begin(begin, context)
    return begin


async def test_a_full_solo_campaign_gets_off_the_ground(repo: Repo, context: MagicMock) -> None:
    await _play_through(context)

    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None
    assert campaign.genre == "fantasy"
    assert campaign.status == "active"

    character = await repo.get_character(campaign.id, USER_ID)
    assert character is not None
    assert character.name == "Bramble"
    assert character.archetype == "Fighter"
    assert character.origin == "Human"
    assert character.concept == "Afraid of geese."
    assert character.user_display == "@hank"

    # A Fighter's standard array puts its best score in strength.
    assert character.abilities["str"] == 15
    # And they start with their archetype's kit.
    assert {i.name for i in character.items} == {"Worn longsword", "Dented shield", "Rations"}

    # The opening scene was narrated and recorded.
    messages = await repo.recent_messages(campaign.id)
    assert messages[-1].role == "dm"
    assert "tavern door" in messages[-1].content


async def test_playing_a_turn_records_the_exchange(repo: Repo, context: MagicMock) -> None:
    await _play_through(context)

    turn = message_update("I order the strongest thing they have")
    await handle_play(turn, context)

    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None
    character = await repo.get_character(campaign.id, USER_ID)
    assert character is not None

    messages = await repo.recent_messages(campaign.id)
    player_turns = [m for m in messages if m.role == "player"]
    assert player_turns[-1].content == "I order the strongest thing they have"
    assert player_turns[-1].character_id == character.id
    assert messages[-1].role == "dm"


async def test_table_talk_is_ignored(repo: Repo, context: MagicMock) -> None:
    """An @mention of another player means they're talking to each other."""
    await _play_through(context)
    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None
    before = await repo.count_messages_after(campaign.id, 0)

    chatter = message_update("@dave you free saturday?")
    entity = MagicMock()
    entity.type = "mention"
    entity.offset = 0
    entity.length = len("@dave")
    chatter.effective_message.entities = [entity]
    chatter.get_bot.return_value.username = "dmkeith_bot"

    await handle_play(chatter, context)

    assert await repo.count_messages_after(campaign.id, 0) == before
    chatter.message.reply_text.assert_not_awaited()


async def test_you_cannot_join_twice(repo: Repo, context: MagicMock) -> None:
    await _play_through(context)

    second = message_update()
    await start_join(second, context)

    second.message.reply_text.assert_awaited_once()
    assert "already playing Bramble" in second.message.reply_text.await_args.args[0]


async def test_a_spectator_is_told_to_join(repo: Repo, context: MagicMock) -> None:
    await _play_through(context)

    outsider = message_update("I draw my sword")
    outsider.effective_user.id = 999  # never joined
    await handle_play(outsider, context)

    outsider.message.reply_text.assert_awaited_once()
    assert "/join" in outsider.message.reply_text.await_args.args[0]


async def test_play_is_silent_when_no_campaign_exists(repo: Repo, context: MagicMock) -> None:
    """Keith shouldn't nag in a chat that isn't running a game."""
    stray = message_update("just chatting")
    await handle_play(stray, context)
    stray.message.reply_text.assert_not_awaited()


async def test_begin_refuses_an_empty_party(repo: Repo, context: MagicMock) -> None:
    await handle_newgame(message_update(), context)
    await handle_genre_choice(callback_update(f"{GENRE_PREFIX}fantasy"), context)

    begin = message_update()
    await handle_begin(begin, context)

    assert "/join" in begin.message.reply_text.await_args.args[0]
    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None and campaign.status == "creating"


async def test_sheet_shows_the_character(repo: Repo, context: MagicMock) -> None:
    await _play_through(context)

    sheet = message_update()
    await handle_sheet(sheet, context)

    text = sheet.message.reply_text.await_args.args[0]
    assert "Bramble" in text
    assert "Worn longsword" in text


async def test_duplicate_character_names_are_rejected(repo: Repo, context: MagicMock) -> None:
    await _play_through(context)

    # A second player tries to reuse the name.
    context.user_data = {}
    second = message_update()
    second.effective_user.id = 8
    second.effective_user.username = "dave"
    await start_join(second, context)
    await choose_archetype(callback_update(f"{ARCHETYPE_PREFIX}Rogue"), context)
    await choose_origin(callback_update(f"{ORIGIN_PREFIX}Elf"), context)

    clash = message_update("Bramble")
    clash.effective_user.id = 8
    await receive_name(clash, context)

    assert "already a Bramble" in clash.message.reply_text.await_args.args[0]


async def test_a_stale_genre_button_cannot_destroy_a_live_campaign(
    repo: Repo, context: MagicMock
) -> None:
    """Those buttons live in the chat history forever. Tapping an old one must not
    silently retire the campaign people are playing."""
    await _play_through(context)
    original = await repo.get_live_campaign(CHAT_ID)
    assert original is not None

    stale = callback_update(f"{GENRE_PREFIX}fantasy")
    await handle_genre_choice(stale, context)

    still_live = await repo.get_live_campaign(CHAT_ID)
    assert still_live is not None
    assert still_live.id == original.id
    assert still_live.status == "active"
    assert "old button" in stale.callback_query.edit_message_text.await_args.args[0]


async def test_confirming_a_new_game_does_replace_the_campaign(
    repo: Repo, context: MagicMock
) -> None:
    """The deliberate path still works: confirm, then pick a genre."""
    await _play_through(context)
    original = await repo.get_live_campaign(CHAT_ID)
    assert original is not None

    await handle_newgame_confirm(callback_update(CONFIRM_NEW), context)
    await handle_genre_choice(callback_update(f"{GENRE_PREFIX}fantasy"), context)

    replacement = await repo.get_live_campaign(CHAT_ID)
    assert replacement is not None
    assert replacement.id != original.id

    retired = await repo.get_campaign(original.id)
    assert retired is not None and retired.status == "ended"


async def test_cancelling_a_new_game_leaves_the_campaign_alone(
    repo: Repo, context: MagicMock
) -> None:
    await _play_through(context)
    original = await repo.get_live_campaign(CHAT_ID)
    assert original is not None

    await handle_newgame_confirm(callback_update(CANCEL_NEW), context)

    still_live = await repo.get_live_campaign(CHAT_ID)
    assert still_live is not None and still_live.id == original.id


async def test_two_begins_only_open_the_story_once(repo: Repo, context: MagicMock) -> None:
    """Both would otherwise pass the status check and narrate two openings."""
    await handle_newgame(message_update(), context)
    await handle_genre_choice(callback_update(f"{GENRE_PREFIX}fantasy"), context)
    await start_join(message_update(), context)
    await choose_archetype(callback_update(f"{ARCHETYPE_PREFIX}Fighter"), context)
    await choose_origin(callback_update(f"{ORIGIN_PREFIX}Human"), context)
    await receive_name(message_update("Bramble"), context)

    first, second = message_update(), message_update()
    await asyncio.gather(handle_begin(first, context), handle_begin(second, context))

    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None and campaign.status == "active"

    messages = await repo.recent_messages(campaign.id, limit=50)
    openings = [m for m in messages if m.role == "system" and "campaign begins" in m.content]
    assert len(openings) == 1


async def test_endgame_retires_the_campaign(repo: Repo, context: MagicMock) -> None:
    await _play_through(context)

    ending = message_update()
    await handle_endgame(ending, context)

    assert await repo.get_live_campaign(CHAT_ID) is None
    assert "retired" in ending.message.reply_text.await_args.args[0].lower()


async def test_a_model_failure_is_reported_not_narrated(repo: Repo, context: MagicMock) -> None:
    """The old bot turned API failures into 'Keith being whimsical'."""
    await _play_through(context)

    def exploding(messages: Any, info: Any) -> Any:
        raise RuntimeError("provider is down")

    context.application.bot_data[SERVICE_KEY] = GameService(
        repo, "test:function", model=FunctionModel(exploding)
    )

    turn = message_update("I open the chest")
    await handle_play(turn, context)

    said = turn.message.reply_text.await_args.args[0]
    assert "RuntimeError" in said
    assert "Nothing happened" in said


async def test_typing_instead_of_tapping_a_genre_gets_an_answer(
    repo: Repo, context: MagicMock
) -> None:
    """Used to vanish silently: /newgame doesn't create the campaign, so a typed
    reply hit the 'no campaign here, stay quiet' path."""
    await handle_newgame(message_update(), context)

    typed = message_update("cyberpunk")
    await handle_play(typed, context)

    typed.message.reply_text.assert_awaited_once()
    assert "Pick a genre" in typed.message.reply_text.await_args.args[0]


async def test_the_genre_menu_lists_full_descriptions(repo: Repo, context: MagicMock) -> None:
    """Button text gets cut off by Telegram, so the pitch goes in the message."""
    from src.bot.commands import genre_keyboard, genre_menu_text

    text = genre_menu_text()
    assert "structurally unsound dungeons" in text

    button = genre_keyboard().inline_keyboard[0][0]
    assert button.text == "🗡 Fantasy"
    assert len(button.text) < 24  # short enough not to be truncated
