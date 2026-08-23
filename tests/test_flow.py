"""End-to-end: /newgame → /join → /begin → play, driven through the real handlers.

Telegram objects are stand-ins and the model is scripted, but the handler bodies,
the conversation transitions, the repo and the agent are all the real thing.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
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


def genre_model() -> FunctionModel:
    """Answers the genre builder's structured-output call with a cyberpunk setting."""
    skin = {
        "label": "Cyberpunk",
        "emoji": "🌃",
        "pitch": "Neon, bad decisions and worse landlords.",
        "tone_note": "Rain-slick and sardonic.",
        "ability_names": {
            "str": "Muscle",
            "dex": "Reflex",
            "con": "Endurance",
            "int": "Tech",
            "wis": "Instinct",
            "cha": "Cool",
        },
        "archetypes": [
            {
                "name": name,
                "blurb": f"A {name}.",
                "priority": ["int", "dex", "cha", "con", "wis", "str"],
                "items": [
                    {"name": f"{name} deck", "kind": "tool", "description": "Warm to the touch."},
                    {"name": "Burner phone", "kind": "gear", "description": "Third this month."},
                    {"name": "Instant noodles", "kind": "supply", "description": "Dinner."},
                ],
            }
            for name in ("Netrunner", "Solo", "Fixer", "Techie", "Face")
        ],
        "origins": [
            {"name": name, "blurb": f"From the {name}."}
            for name in ("Sprawl", "Arcology", "Orbital", "Badlands", "Undercity")
        ],
    }

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # Structured output arrives as a call to the output tool.
        tool = info.output_tools[0]
        return ModelResponse(parts=[ToolCallPart(tool.name, skin)])

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


async def test_typing_a_genre_builds_it(repo: Repo, context: MagicMock) -> None:
    """Used to vanish silently. Now it generates the setting and opens the campaign."""
    from src.game.genres import genre_for
    from src.llm import genre_builder

    context.application.bot_data[SERVICE_KEY] = GameService(
        repo, "test:function", model=genre_model()
    )

    await handle_newgame(message_update(), context)
    typed = message_update("cyberpunk")
    await handle_play(typed, context)

    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None
    assert campaign.genre == genre_builder.genre_key_for("cyberpunk")

    # The generated setting is stored on the campaign, so it survives a restart.
    genre = genre_for(campaign)
    assert genre.label == "Cyberpunk"
    assert [a.name for a in genre.archetypes] == ["Netrunner", "Solo", "Fixer", "Techie", "Face"]
    assert genre.ability_label("int") == "Tech"


async def test_a_generated_genre_drives_character_creation(repo: Repo, context: MagicMock) -> None:
    """The archetypes offered at /join must come from the generated setting."""
    context.application.bot_data[SERVICE_KEY] = GameService(
        repo, "test:function", model=genre_model()
    )
    await handle_newgame(message_update(), context)
    await handle_play(message_update("cyberpunk"), context)

    joining = message_update()
    await start_join(joining, context)

    offered = joining.message.reply_text.await_args.args[0]
    assert "Netrunner" in offered
    assert "Fighter" not in offered  # not the fantasy fallback


async def test_a_failed_generation_says_so_rather_than_hanging(
    repo: Repo, context: MagicMock
) -> None:
    def exploding(messages: Any, info: Any) -> Any:
        raise RuntimeError("model is down")

    context.application.bot_data[SERVICE_KEY] = GameService(
        repo, "test:function", model=FunctionModel(exploding)
    )

    await handle_newgame(message_update(), context)
    typed = message_update("cyberpunk")
    await handle_play(typed, context)

    notice = typed.message.reply_text.return_value
    assert "couldn't make that into a setting" in notice.edit_text.await_args.args[0]
    assert await repo.get_live_campaign(CHAT_ID) is None


async def test_the_genre_menu_lists_full_descriptions(repo: Repo, context: MagicMock) -> None:
    """Button text gets cut off by Telegram, so the pitch goes in the message."""
    from src.bot.commands import genre_keyboard, genre_menu_text

    text = genre_menu_text()
    assert "structurally unsound dungeons" in text

    button = genre_keyboard().inline_keyboard[0][0]
    assert button.text == "🗡 Fantasy"
    assert len(button.text) < 24  # short enough not to be truncated


async def test_formatting_characters_in_a_typed_genre_dont_break_it(
    repo: Repo, context: MagicMock
) -> None:
    """An underscore in "80s_action" made Telegram reject the message outright,
    so the campaign was never created."""
    context.application.bot_data[SERVICE_KEY] = GameService(
        repo, "test:function", model=genre_model()
    )
    await handle_newgame(message_update(), context)

    typed = message_update("80s_action *movie* `weird`")
    await handle_play(typed, context)

    # Plain text, so nothing to misparse.
    notice_call = typed.message.reply_text.await_args
    assert notice_call.kwargs.get("parse_mode") is None
    assert await repo.get_live_campaign(CHAT_ID) is not None


async def test_a_broken_notice_edit_still_leaves_the_campaign_usable(
    repo: Repo, context: MagicMock
) -> None:
    """The edit is presentation; the campaign is already created by then."""
    from telegram.error import BadRequest

    context.application.bot_data[SERVICE_KEY] = GameService(
        repo, "test:function", model=genre_model()
    )
    await handle_newgame(message_update(), context)

    typed = message_update("cyberpunk")
    typed.message.reply_text.return_value.edit_text = AsyncMock(
        side_effect=BadRequest("can't parse entities")
    )
    await handle_play(typed, context)

    assert await repo.get_live_campaign(CHAT_ID) is not None


async def test_typing_a_built_in_genre_uses_the_hand_written_one(
    repo: Repo, context: MagicMock
) -> None:
    """No point spending a model call on a worse copy of Fantasy."""
    await handle_newgame(message_update(), context)
    await handle_play(message_update("Fantasy"), context)

    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None
    assert campaign.genre == "fantasy"
    assert campaign.genre_skin == {}  # not generated


async def test_an_abandoned_genre_menu_does_not_swallow_later_chatter(
    repo: Repo, context: MagicMock
) -> None:
    """Left armed, the flag turned the next stray "lol" into a generated campaign."""
    context.application.bot_data[SERVICE_KEY] = GameService(
        repo, "test:function", model=genre_model()
    )
    await handle_newgame(message_update(), context)
    await handle_play(message_update("cyberpunk"), context)

    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None

    # A second stray message must not build anything or replace the campaign.
    await handle_play(message_update("lol"), context)
    still = await repo.get_live_campaign(CHAT_ID)
    assert still is not None and still.id == campaign.id


# -- carrying a character between campaigns ---------------------------------


def upload(text: str, filename: str = "thorn.md", user_id: int = USER_ID) -> MagicMock:
    """An update carrying an uploaded .md sheet."""
    update = message_update()
    update.effective_user.id = user_id
    update.message.text = None
    update.message.document.file_size = len(text.encode())
    handle = AsyncMock()
    handle.download_as_bytearray = AsyncMock(return_value=bytearray(text.encode()))
    update.message.document.get_file = AsyncMock(return_value=handle)
    update.message.document.file_name = filename
    return update


async def _exported_sheet(repo: Repo, context: MagicMock) -> str:
    """Play a fantasy campaign, level up a bit, then /export."""
    from src.bot.sheets import handle_export

    await _play_through(context)
    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None
    character = await repo.get_character(campaign.id, USER_ID)
    assert character is not None
    await repo.grant_xp(character.id, 950)

    exporting = message_update()
    exporting.message.reply_document = AsyncMock()
    await handle_export(exporting, context)

    sent = exporting.message.reply_document.await_args.kwargs["document"]
    return sent.getvalue().decode()


async def test_a_character_survives_into_a_new_campaign(repo: Repo, context: MagicMock) -> None:
    from src.bot.creation import start_import

    sheet = await _exported_sheet(repo, context)
    assert "# Bramble" in sheet

    # A brand new campaign in a different genre.
    await handle_endgame(message_update(), context)
    await handle_newgame(message_update(), context)
    await handle_genre_choice(callback_update(f"{GENRE_PREFIX}fantasy"), context)

    context.user_data = {}
    await start_import(upload(sheet), context)
    await choose_archetype(callback_update(f"{ARCHETYPE_PREFIX}Wizard"), context)
    await choose_origin(callback_update(f"{ORIGIN_PREFIX}Elf"), context)

    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None
    carried = await repo.get_character(campaign.id, USER_ID)
    assert carried is not None

    # Everything that should have come with them:
    assert carried.name == "Bramble"
    assert carried.xp == 950
    assert carried.level == 3
    assert carried.abilities["str"] == 15
    assert {i.name for i in carried.items} >= {"Worn longsword", "Rations"}
    # ...and the new setting's answer to "what are you here".
    assert carried.archetype == "Wizard"
    assert carried.origin == "Elf"
    # A new adventure starts rested.
    assert carried.hp == carried.max_hp


async def test_an_edited_sheet_gets_publicly_shamed(repo: Repo, context: MagicMock) -> None:
    from src.bot.creation import start_import

    sheet = (await _exported_sheet(repo, context)).replace('"xp": 950', '"xp": 63000')

    await handle_endgame(message_update(), context)
    await handle_newgame(message_update(), context)
    await handle_genre_choice(callback_update(f"{GENRE_PREFIX}fantasy"), context)

    context.user_data = {}
    await start_import(upload(sheet), context)
    await choose_archetype(callback_update(f"{ARCHETYPE_PREFIX}Fighter"), context)

    # Creation finishes on the origin tap, so that's where the shaming lands.
    finishing = callback_update(f"{ORIGIN_PREFIX}Human")
    await choose_origin(finishing, context)

    said = [c.args[0] for c in finishing.message.reply_text.await_args_list if c.args]
    assert any("ACHIEVEMENT UNLOCKED" in s for s in said)

    # It still imported -- it's their game, they just don't get away with it quietly.
    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None
    carried = await repo.get_character(campaign.id, USER_ID)
    assert carried is not None and carried.xp == 63000

    earned = {aid for aid, _ in await repo.list_achievements(carried.id)}
    shame = {
        "creative-accounting",
        "notarised-by-nobody",
        "audited",
        "self-made-hero",
        "checksum-says-no",
        "welcome-back-allegedly",
    }
    assert earned & shame


async def test_rubbish_uploads_are_turned_away(repo: Repo, context: MagicMock) -> None:
    from src.bot.creation import start_import

    await _play_through(context)
    await handle_endgame(message_update(), context)
    await handle_newgame(message_update(), context)
    await handle_genre_choice(callback_update(f"{GENRE_PREFIX}fantasy"), context)

    context.user_data = {}
    bad = upload("this is just a shopping list")
    await start_import(bad, context)

    assert "can't find the character data" in bad.message.reply_text.await_args.args[0]
    campaign = await repo.get_live_campaign(CHAT_ID)
    assert campaign is not None
    assert await repo.get_character(campaign.id, USER_ID) is None


async def test_you_cannot_import_on_top_of_yourself(repo: Repo, context: MagicMock) -> None:
    from src.bot.creation import start_import

    sheet = await _exported_sheet(repo, context)  # leaves Bramble in a live campaign

    context.user_data = {}
    again = upload(sheet)
    await start_import(again, context)

    assert "already playing someone" in again.message.reply_text.await_args.args[0]
