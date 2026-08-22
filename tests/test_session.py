"""The turn loop: context assembly, persistence, and continuity across restarts."""

from __future__ import annotations

import asyncio

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.game.genres import FANTASY
from src.game.memory import build_turn_context, build_user_prompt, render_character
from src.game.session import GameService
from src.storage.repo import Campaign, Character, Repo


def echo_model(reply: str = "The door creaks open.") -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(reply)])

    return FunctionModel(respond)


def capturing_model(sink: list[str]) -> FunctionModel:
    """Records the instructions and prompt the model actually received."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sink.append(info.instructions or "")
        for message in messages:
            for part in getattr(message, "parts", []):
                if part.part_kind == "user-prompt":
                    sink.append(str(part.content))
        return ModelResponse(parts=[TextPart("Something happens.")])

    return FunctionModel(respond)


def service_with(repo: Repo, model: FunctionModel) -> GameService:
    return GameService(repo, model_spec="test:function", model=model)


async def test_a_turn_persists_both_sides(repo: Repo, campaign: Campaign, hero: Character) -> None:
    service = service_with(repo, echo_model())
    result = await service.take_turn(campaign, "I open the door", hero)

    assert result.reply == "The door creaks open."

    messages = await repo.recent_messages(campaign.id)
    assert [(m.role, m.content) for m in messages] == [
        ("player", "I open the door"),
        ("dm", "The door creaks open."),
    ]
    assert messages[0].character_id == hero.id


async def test_the_model_sees_the_story_so_far(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """This is the whole point of the rewrite -- the old bot sent no history."""
    await repo.add_message(campaign.id, "player", "I light a torch", character_id=hero.id)
    await repo.add_message(campaign.id, "dm", "The corridor flickers into view.")
    await repo.add_story_event(campaign.id, "npc", "Met Vex the fence", ["Vex"])
    await repo.update_campaign(campaign.id, summary="The party entered the crypt.")

    seen: list[str] = []
    service = service_with(repo, capturing_model(seen))
    fresh = await repo.get_campaign(campaign.id)
    assert fresh is not None
    await service.take_turn(fresh, "I keep walking", hero)

    instructions = seen[0]
    assert "The party entered the crypt." in instructions
    assert "Met Vex the fence" in instructions
    assert "The corridor flickers into view." in instructions
    assert "Thorn" in instructions
    # The persona and the achievement catalogue ride along too.
    assert "Dungeon Master Keith" in instructions
    assert "icebox-raider" in instructions

    prompt = seen[1]
    assert "Thorn" in prompt
    assert "I keep walking" in prompt


async def test_context_survives_a_restart(repo: Repo, campaign: Campaign, hero: Character) -> None:
    """A new GameService over the same database still knows what happened."""
    first = service_with(repo, echo_model("Round one."))
    await first.take_turn(campaign, "I shout into the dark", hero)

    seen: list[str] = []
    second = service_with(repo, capturing_model(seen))
    await second.take_turn(campaign, "I shout again", hero)

    assert "I shout into the dark" in seen[0]
    assert "Round one." in seen[0]


async def test_turns_in_one_campaign_are_serialised(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """Concurrent messages must not interleave into a scrambled transcript."""
    order: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        order.append("start")
        order.append("end")
        return ModelResponse(parts=[TextPart("ok")])

    service = service_with(repo, FunctionModel(respond))
    await asyncio.gather(*(service.take_turn(campaign, f"action {i}", hero) for i in range(4)))

    # Every turn completed before the next began.
    assert order == ["start", "end"] * 4
    assert await repo.count_messages_after(campaign.id, 0) == 8


async def test_system_actions_are_not_attributed_to_a_character(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    service = service_with(repo, echo_model())
    await service.take_turn(campaign, "The campaign begins.", actor=None)

    messages = await repo.recent_messages(campaign.id)
    assert messages[0].role == "system"
    assert messages[0].character_id is None


async def test_an_empty_reply_is_not_recorded(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    service = service_with(repo, echo_model("   "))
    result = await service.take_turn(campaign, "I mumble", hero)

    assert result.reply == ""
    messages = await repo.recent_messages(campaign.id)
    assert [m.role for m in messages] == ["player"]


async def test_transcript_renders_speakers_by_character_name(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    other = await repo.create_character(campaign.id, user_id=2, name="Vex")
    await repo.add_message(campaign.id, "player", "I hide", character_id=other.id)
    await repo.add_message(campaign.id, "dm", "Poorly.")

    fresh = await repo.get_campaign(campaign.id)
    assert fresh is not None
    context = await build_turn_context(repo, fresh)

    assert "Vex: I hide" in context
    assert "DM: Poorly." in context


def test_user_prompt_names_the_actor(hero: Character) -> None:
    prompt = build_user_prompt(hero, "I kick the door")
    assert "Thorn" in prompt
    assert "@hank" in prompt
    assert "I kick the door" in prompt
    # No actor means no attribution wrapper.
    assert build_user_prompt(None, "The world turns") == "The world turns"


def test_character_sheet_uses_genre_ability_names(hero: Character) -> None:
    sheet = render_character(hero, FANTASY)
    assert "Thorn" in sheet
    assert "STR 14" in sheet
    assert f"HP {hero.hp}/{hero.max_hp}" in sheet
    assert "carrying: nothing" in sheet
