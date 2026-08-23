"""The chronicle: incremental chapters, watermarks, and the final pass."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.bot.chronicle import handle_chronicle, write_final_edition
from src.bot.context import CHRONICLER_KEY, REPO_KEY
from src.game.chronicle import MIN_CHAPTER_MESSAGES, catch_up, polish, render_book
from src.llm.chronicler import ChronicleFailed
from src.storage.repo import Campaign, Character, Repo


def chronicler(prefix: str = "Chapter", fail_after: int | None = None):
    """A model that returns one scripted chapter per call, optionally failing partway.

    One chapter per call is the real contract: the caller splits a long backlog into
    chapter-sized spans, so the model never decides how many to write.
    """
    calls = {"n": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if fail_after is not None and calls["n"] > fail_after:
            raise RuntimeError("the chronicler gave up")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"title": f"{prefix} {calls['n']}", "body": f"Body of {prefix} {calls['n']}."},
                )
            ]
        )

    return FunctionModel(respond), calls


async def _play(repo: Repo, campaign: Campaign, hero: Character, exchanges: int) -> None:
    for i in range(exchanges):
        await repo.add_message(campaign.id, "player", f"I do thing {i}", character_id=hero.id)
        await repo.add_message(campaign.id, "dm", f"Thing {i} happens, regrettably.")


# -- watermarks -------------------------------------------------------------


async def test_a_short_campaign_isnt_worth_a_chapter(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """Calling /chronicle twice in five minutes shouldn't produce a chapter."""
    await _play(repo, campaign, hero, exchanges=2)  # 4 messages
    model, calls = chronicler()

    assert await catch_up(repo, campaign, model=model, settings=None) == []
    assert calls["n"] == 0  # and it didn't spend a call finding that out


async def test_chapters_pick_up_where_the_last_one_stopped(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    await _play(repo, campaign, hero, exchanges=10)
    model, _ = chronicler()

    first = await catch_up(repo, campaign, model=model, settings=None)
    assert len(first) == 1

    # Nothing new: nothing written.
    assert await catch_up(repo, campaign, model=model, settings=None) == []

    # More play, one more chapter, and the watermark advanced.
    await _play(repo, campaign, hero, exchanges=10)
    second = await catch_up(repo, campaign, model=model, settings=None)
    assert len(second) == 1
    assert second[0].number == 2
    assert second[0].through_message_id > first[0].through_message_id


async def test_a_long_backlog_becomes_several_chapters(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """One /chronicle after a long absence shouldn't be one enormous chapter."""
    await _play(repo, campaign, hero, exchanges=100)  # 200 messages
    model, calls = chronicler()

    written = await catch_up(repo, campaign, model=model, settings=None)
    assert len(written) >= 3
    assert [c.number for c in written] == list(range(1, len(written) + 1))
    assert calls["n"] == len(written)

    # Every message is covered exactly once, in order.
    watermarks = [c.through_message_id for c in written]
    assert watermarks == sorted(watermarks)
    assert len(set(watermarks)) == len(watermarks)


async def test_a_short_remainder_is_folded_in(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """A trailing stub span shouldn't become a chapter about nothing."""
    from src.game.chronicle import MAX_CHAPTER_MESSAGES

    exchanges = (MAX_CHAPTER_MESSAGES + 4) // 2  # just over one span
    await _play(repo, campaign, hero, exchanges=exchanges)
    model, _ = chronicler()

    written = await catch_up(repo, campaign, model=model, settings=None)
    assert len(written) == 1


async def test_a_failure_partway_keeps_what_was_written(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """A long backlog is several paid calls; a failure must not throw the lot away."""
    await _play(repo, campaign, hero, exchanges=100)
    model, _ = chronicler(fail_after=2)

    with pytest.raises(ChronicleFailed):
        await catch_up(repo, campaign, model=model, settings=None)

    kept = await repo.list_chapters(campaign.id)
    assert len(kept) == 2

    # And a retry resumes rather than starting over.
    good, _ = chronicler(prefix="Recovered")
    more = await catch_up(repo, campaign, model=good, settings=None)
    assert more
    assert more[0].through_message_id > kept[-1].through_message_id


async def test_each_campaign_gets_its_own_book(repo: Repo, hero: Character) -> None:
    """/newgame starts a new story, so it starts a new book."""
    first = await repo.create_campaign(chat_id=-77, genre="fantasy")
    a = await repo.create_character(first.id, user_id=1, name="Thorn")
    await _play(repo, first, a, exchanges=10)
    model, _ = chronicler()
    await catch_up(repo, first, model=model, settings=None)

    second = await repo.create_campaign(chat_id=-77, genre="fantasy")
    b = await repo.create_character(second.id, user_id=1, name="Vex")
    await _play(repo, second, b, exchanges=10)
    await catch_up(repo, second, model=model, settings=None)

    assert len(await repo.list_chapters(first.id)) == 1
    assert len(await repo.list_chapters(second.id)) == 1
    assert (await repo.list_chapters(second.id))[0].number == 1  # numbering restarts


# -- the final pass ---------------------------------------------------------


async def test_the_final_pass_rewrites_every_chapter(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    await _play(repo, campaign, hero, exchanges=100)
    model, _ = chronicler(prefix="Draft")
    drafted = await catch_up(repo, campaign, model=model, settings=None)
    assert len(drafted) >= 3

    polished_model, calls = chronicler(prefix="Final")
    polished = await polish(repo, campaign, model=polished_model, settings=None)

    assert len(polished) == len(drafted)
    assert calls["n"] == len(drafted)  # one call per chapter, not one giant one
    assert all("Final" in c.title for c in await repo.list_chapters(campaign.id))
    # Numbering and coverage survive the rewrite.
    assert [c.number for c in polished] == list(range(1, len(polished) + 1))


async def test_a_failed_final_pass_leaves_the_book_alone(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """The rewrite is atomic: better the old book than half a new one."""
    await _play(repo, campaign, hero, exchanges=100)
    model, _ = chronicler(prefix="Draft")
    await catch_up(repo, campaign, model=model, settings=None)
    before = await repo.list_chapters(campaign.id)

    failing, _ = chronicler(prefix="Final", fail_after=1)
    with pytest.raises(ChronicleFailed):
        await polish(repo, campaign, model=failing, settings=None)

    after = await repo.list_chapters(campaign.id)
    assert [(c.number, c.title) for c in after] == [(c.number, c.title) for c in before]


# -- rendering --------------------------------------------------------------


async def test_the_book_has_a_title_page_cast_and_chapters(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    await repo.add_story_event(campaign.id, "npc", "Fizzwick, a one-booted gnome mapmaker.")
    await repo.add_story_event(campaign.id, "quest", "Find the Verdigris Vaults.")
    await _play(repo, campaign, hero, exchanges=10)

    model, _ = chronicler()
    await catch_up(repo, campaign, model=model, settings=None)
    book = await render_book(repo, campaign)

    assert book.startswith("# The Fantasy Campaign")
    assert "Thorn" in book
    assert "## Cast" in book
    assert "Fizzwick" in book
    assert "Find the Verdigris Vaults." not in book  # cast is NPCs only
    assert "## 1. Chapter 1" in book


async def test_achievements_land_in_the_chapter_they_were_earned_in(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """Grant ids rather than timestamps, so the range is exact."""
    await _play(repo, campaign, hero, exchanges=10)
    await repo.grant_achievement(campaign.id, hero.id, "door-tax", "common")

    model, _ = chronicler()
    await catch_up(repo, campaign, model=model, settings=None)

    await _play(repo, campaign, hero, exchanges=10)
    await repo.grant_achievement(campaign.id, hero.id, "poked-it", "common")
    await catch_up(repo, campaign, model=model, settings=None)

    book = await render_book(repo, campaign)
    first, second = book.split("## 2.")
    assert "The Door Tax" in first
    assert "You Poked It" not in first
    assert "You Poked It" in second


async def test_an_unwritten_book_still_renders(repo: Repo, campaign: Campaign) -> None:
    book = await render_book(repo, campaign)
    assert "Nothing has been written up yet" in book


# -- the command ------------------------------------------------------------


@pytest.fixture
def context(repo: Repo) -> MagicMock:
    model, _ = chronicler()
    ctx = MagicMock()
    ctx.application.bot_data = {REPO_KEY: repo, CHRONICLER_KEY: model}
    ctx.args = []
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


def command(text: str = "") -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = -100123
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.reply_document = AsyncMock()
    update.effective_message = update.message
    return update


async def test_chronicle_sends_the_book(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    await _play(repo, campaign, hero, exchanges=10)

    update = command()
    await handle_chronicle(update, context)

    update.message.reply_document.assert_awaited_once()
    sent = update.message.reply_document.await_args.kwargs
    assert sent["filename"].endswith(".md")
    assert "# The Fantasy Campaign" in sent["document"].getvalue().decode()


async def test_chronicle_declines_when_nothing_has_happened(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    await _play(repo, campaign, hero, exchanges=1)

    update = command()
    await handle_chronicle(update, context)

    assert "Play a bit more" in update.message.reply_text.await_args.args[0]
    update.message.reply_document.assert_not_awaited()


async def test_redo_replaces_only_the_last_chapter(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    await _play(repo, campaign, hero, exchanges=10)
    await handle_chronicle(command(), context)
    await _play(repo, campaign, hero, exchanges=10)
    await handle_chronicle(command(), context)

    before = await repo.list_chapters(campaign.id)
    assert len(before) == 2

    context.application.bot_data[CHRONICLER_KEY] = chronicler(prefix="Rewritten")[0]
    context.args = ["redo"]
    await handle_chronicle(command(), context)

    after = await repo.list_chapters(campaign.id)
    assert len(after) == 2
    assert after[0].title == before[0].title  # first chapter untouched
    assert "Rewritten" in after[1].title
    assert after[1].through_message_id == before[1].through_message_id


async def test_chronicle_says_so_when_no_model_is_configured(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """A broken DMK_SUMMARY_MODEL stops the book, not the game."""
    await _play(repo, campaign, hero, exchanges=10)
    context.application.bot_data[CHRONICLER_KEY] = None

    update = command()
    await handle_chronicle(update, context)

    assert "DMK_SUMMARY_MODEL" in update.message.reply_text.await_args.args[0]


async def test_the_final_edition_runs_at_endgame(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    await _play(repo, campaign, hero, exchanges=10)

    update = command()
    await write_final_edition(update, context, campaign)

    assert await repo.list_chapters(campaign.id)
    update.message.reply_document.assert_awaited_once()


async def test_endgame_survives_a_chronicler_failure(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """Retiring a campaign must work even if the book can't be written."""
    await _play(repo, campaign, hero, exchanges=10)
    context.application.bot_data[CHRONICLER_KEY] = chronicler(fail_after=0)[0]

    update = command()
    await write_final_edition(update, context, campaign)  # must not raise

    # The progress message is edited in place to explain what went wrong.
    notice = update.message.reply_text.return_value
    assert "couldn't finish" in notice.edit_text.await_args.args[0]


def test_the_minimum_is_a_real_conversation() -> None:
    """Sanity: the threshold is exchanges, not single messages."""
    assert MIN_CHAPTER_MESSAGES >= 6


# -- the bugs review found --------------------------------------------------


async def test_achievements_spread_across_chapters_written_in_one_go(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """Every chapter used to get the campaign-wide grant watermark, so a multi-chapter
    catch_up piled every achievement into chapter one."""
    await _play(repo, campaign, hero, exchanges=40)
    await repo.grant_achievement(campaign.id, hero.id, "door-tax", "common")
    await _play(repo, campaign, hero, exchanges=40)
    await repo.grant_achievement(campaign.id, hero.id, "poked-it", "common")

    # Second-resolution timestamps all collapse in a fast test, so space them out:
    # span one and its award, then span two and its award.
    await repo.conn.execute("UPDATE messages SET created_at = datetime('now') WHERE id <= 80")
    await repo.conn.execute(
        "UPDATE achievement_grants SET awarded_at = datetime('now', '+1 minute') "
        "WHERE achievement_id = 'door-tax'"
    )
    await repo.conn.execute(
        "UPDATE messages SET created_at = datetime('now', '+2 minutes') WHERE id > 80"
    )
    await repo.conn.execute(
        "UPDATE achievement_grants SET awarded_at = datetime('now', '+3 minutes') "
        "WHERE achievement_id = 'poked-it'"
    )
    await repo.conn.commit()

    model, _ = chronicler()
    written = await catch_up(repo, campaign, model=model, settings=None)
    assert len(written) == 2

    # Each award renders under the chapter whose span earned it.
    book = await render_book(repo, campaign)
    first, second = book.split("## 2.")
    assert "The Door Tax" in first and "You Poked It" not in first
    assert "You Poked It" in second


async def test_a_failure_inside_replace_chapters_keeps_the_old_book(
    repo: Repo, campaign: Campaign, hero: Character, monkeypatch
) -> None:
    """The dangerous one: sqlite leaves the transaction open on error, so without a
    rollback the next commit anywhere would persist half a book."""
    await _play(repo, campaign, hero, exchanges=20)
    model, _ = chronicler(prefix="Draft")
    await catch_up(repo, campaign, model=model, settings=None)
    before = await repo.list_chapters(campaign.id)
    assert before

    real_execute = repo.conn.execute
    calls = {"n": 0}

    async def flaky(sql: str, *args: object, **kwargs: object) -> object:
        if "INSERT INTO chronicle_chapters" in sql:
            calls["n"] += 1
            if calls["n"] > 0:
                raise RuntimeError("disk full")
        return await real_execute(sql, *args, **kwargs)

    monkeypatch.setattr(repo.conn, "execute", flaky)
    with pytest.raises(RuntimeError):
        await repo.replace_chapters(campaign.id, list(before))
    monkeypatch.undo()

    # The mechanism, not just the outcome: sqlite must not be left mid-transaction,
    # or the next commit anywhere in the app would persist the half-done delete.
    assert not repo.conn.in_transaction

    # An ordinary write elsewhere must not commit the half-done delete.
    await repo.add_message(campaign.id, "player", "meanwhile, play continues")

    after = await repo.list_chapters(campaign.id)
    assert [(c.number, c.title) for c in after] == [(c.number, c.title) for c in before]


async def test_endgame_writes_the_ending_even_when_it_is_short(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """A campaign usually ends on a few messages, and those are the climax."""
    await _play(repo, campaign, hero, exchanges=10)
    await handle_chronicle(command(), context)
    covered = (await repo.last_chapter(campaign.id)).through_message_id

    # Two more exchanges and the campaign is over -- well under the minimum.
    await _play(repo, campaign, hero, exchanges=2)

    await write_final_edition(command(), context, campaign)

    last = await repo.last_chapter(campaign.id)
    assert last is not None
    assert last.through_message_id > covered  # the ending made it into the book


async def test_endgame_still_retires_when_telegram_fails(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """Retiring is the job; the book is a bonus. A network blip must not block it."""
    from telegram.error import NetworkError

    await _play(repo, campaign, hero, exchanges=10)

    update = command()
    update.message.reply_document = AsyncMock(side_effect=NetworkError("flaky wifi"))

    await write_final_edition(update, context, campaign)  # must not raise


async def test_two_chronicles_at_once_write_each_span_once(
    repo: Repo, campaign: Campaign, hero: Character, context: MagicMock
) -> None:
    """Updates run concurrently; both calls would otherwise write the same span."""
    import asyncio as _asyncio

    await _play(repo, campaign, hero, exchanges=20)

    await _asyncio.gather(
        handle_chronicle(command(), context), handle_chronicle(command(), context)
    )

    chapters = await repo.list_chapters(campaign.id)
    watermarks = [c.through_message_id for c in chapters]
    assert len(set(watermarks)) == len(watermarks)  # no span written twice
