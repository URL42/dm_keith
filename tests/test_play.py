"""The out-of-character rule: Keith stays out of player-to-player table talk."""

from __future__ import annotations

from unittest.mock import MagicMock

from telegram.constants import MessageEntityType

from src.bot.play import is_out_of_character


def _update(text: str, mentions: list[str], bot_username: str = "dmkeith_bot") -> MagicMock:
    entities = []
    for handle in mentions:
        offset = text.index(handle)
        entity = MagicMock()
        entity.type = MessageEntityType.MENTION
        entity.offset = offset
        entity.length = len(handle)
        entities.append(entity)

    message = MagicMock()
    message.text = text
    message.entities = entities

    update = MagicMock()
    update.effective_message = message
    update.get_bot.return_value.username = bot_username
    return update


def test_a_plain_action_is_in_character() -> None:
    assert not is_out_of_character(_update("I kick the door down", []))


def test_mentioning_another_player_is_table_talk() -> None:
    assert is_out_of_character(_update("@hank are you free tonight?", ["@hank"]))


def test_mentioning_the_bot_is_addressed_to_the_dm() -> None:
    assert not is_out_of_character(_update("@dmkeith_bot what do I see?", ["@dmkeith_bot"]))


def test_the_bot_mention_wins_wherever_it_appears() -> None:
    """Order must not matter -- addressing Keith at all means play the turn."""
    assert not is_out_of_character(
        _update("@dmkeith_bot tell @hank what happened", ["@dmkeith_bot", "@hank"])
    )
    assert not is_out_of_character(
        _update("@hank look out, @dmkeith_bot I dodge", ["@hank", "@dmkeith_bot"])
    )


def test_a_message_with_no_text_is_in_character() -> None:
    update = MagicMock()
    update.effective_message.text = None
    assert not is_out_of_character(update)


def test_mentions_are_matched_case_insensitively() -> None:
    assert not is_out_of_character(_update("@DMKeith_Bot hello", ["@DMKeith_Bot"]))


def test_a_missing_bot_does_not_crash_the_check() -> None:
    """`get_bot()` raises outside a running application; that must not kill a turn."""
    update = _update("I swing wildly", [])
    update.get_bot.side_effect = RuntimeError("no bot attached")
    assert not is_out_of_character(update)


def test_a_text_mention_also_counts_as_table_talk() -> None:
    """Players without an @username are mentioned via TEXT_MENTION entities."""
    text = "Dave you free saturday?"
    entity = MagicMock()
    entity.type = MessageEntityType.TEXT_MENTION
    entity.offset = 0
    entity.length = len("Dave")

    message = MagicMock()
    message.text = text
    message.entities = [entity]

    update = MagicMock()
    update.effective_message = message
    update.get_bot.return_value.username = "dmkeith_bot"

    assert is_out_of_character(update)
