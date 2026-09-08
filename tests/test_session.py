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

    instructions, prompt = seen[0], seen[1]

    # Instructions hold only the stable half -- persona and setting. Keeping them
    # byte-identical turn to turn is what makes the prefix cacheable.
    assert "Dungeon Master Keith" in instructions
    assert "The party entered the crypt." not in instructions
    assert "The corridor flickers into view." not in instructions
    # The achievement catalogue is fetched by tool now, not shipped every request.
    assert "door-tax" not in instructions

    # Everything that changes rides in the prompt instead.
    assert "The party entered the crypt." in prompt
    assert "Met Vex the fence" in prompt
    assert "The corridor flickers into view." in prompt
    assert "Thorn" in prompt
    assert "I keep walking" in prompt


async def test_the_instructions_are_identical_across_turns(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """The point of moving state out of instructions: an unchanging cacheable prefix."""
    seen: list[str] = []
    service = service_with(repo, capturing_model(seen))

    await service.take_turn(campaign, "first thing", hero)
    await service.take_turn(campaign, "second thing", hero)

    # seen is [instructions, prompt, instructions, prompt]
    assert seen[0] == seen[2]
    assert seen[1] != seen[3]


async def test_the_pacing_nudge_appears_only_once_the_dice_have_gone_quiet(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """Keith requested three rolls across an entire deployment. The nudge tracks how
    long it's been since anyone rolled, and stands down as soon as they do."""
    seen: list[str] = []
    service = service_with(repo, capturing_model(seen))

    # Early on, no rolls yet is just being early.
    await service.take_turn(campaign, "we set off", hero)
    assert "## Pacing" not in seen[1]

    # After a while with nothing rolled, say so.
    for _ in range(6):
        await repo.add_message(campaign.id, "player", "more adventuring", character_id=hero.id)
    seen.clear()
    await service.take_turn(campaign, "still going", hero)
    assert "## Pacing" in seen[1]
    assert "request_roll" in seen[1]
    # It's about rolling, not bookkeeping: XP is awarded by the engine now.
    assert "grant_xp" not in seen[1]

    # A roll stands it down -- it must be able to switch off, or it becomes
    # standing pressure to roll on every single turn.
    await repo.log_roll(
        campaign.id,
        expression="dex",
        detail="d20 [12] +2 DEX = 14",
        total=14,
        character_id=hero.id,
        source="player",
        reason="the ledge",
    )
    seen.clear()
    await service.take_turn(campaign, "onwards", hero)
    assert "## Pacing" not in seen[1]

    # ...and comes back when the dice go quiet again. The nudge this replaced keyed
    # off a character having 0 XP, so a single check disabled it for good.
    await repo.grant_xp(hero.id, 50)
    for _ in range(6):
        await repo.add_message(campaign.id, "player", "and on", character_id=hero.id)
    seen.clear()
    await service.take_turn(campaign, "and on again", hero)
    assert "## Pacing" in seen[1]


async def test_context_survives_a_restart(repo: Repo, campaign: Campaign, hero: Character) -> None:
    """A new GameService over the same database still knows what happened."""
    first = service_with(repo, echo_model("Round one."))
    await first.take_turn(campaign, "I shout into the dark", hero)

    seen: list[str] = []
    second = service_with(repo, capturing_model(seen))
    await second.take_turn(campaign, "I shout again", hero)

    assert "I shout into the dark" in seen[1]
    assert "Round one." in seen[1]


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
    # By character name only -- a handle here ends up in the narration.
    assert "@hank" not in prompt
    assert "I kick the door" in prompt
    # No actor means no attribution wrapper.
    assert build_user_prompt(None, "The world turns") == "The world turns"


def test_character_sheet_uses_genre_ability_names(hero: Character) -> None:
    sheet = render_character(hero, FANTASY)
    assert "Thorn" in sheet
    assert "STR 14" in sheet
    assert f"HP {hero.hp}/{hero.max_hp}" in sheet
    assert "carrying: nothing" in sheet


def test_provider_specific_settings_do_not_leak() -> None:
    """Anthropic-only keys must not reach OpenAI, DeepSeek or a local model."""
    from src.game.session import MAX_REPLY_TOKENS, model_settings_for

    anthropic = model_settings_for("anthropic:claude-opus-5", "medium")
    assert anthropic["anthropic_cache_instructions"] is True
    assert anthropic["anthropic_effort"] == "medium"

    for spec in ("openai:gpt-5", "deepseek:deepseek-chat", "ollama:qwen3:14b"):
        settings = model_settings_for(spec, "medium")
        assert not any(k.startswith("anthropic_") for k in settings)
        # But the reply-length ceiling applies everywhere -- a 1,200-word
        # monologue on DeepSeek is what prompted it.
        assert settings["max_tokens"] == MAX_REPLY_TOKENS

    assert anthropic["max_tokens"] == MAX_REPLY_TOKENS


async def test_the_dm_never_sees_a_players_handle(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """@handles leaked into the fiction because they were in the prompt twice."""
    seen: list[str] = []
    service = service_with(repo, capturing_model(seen))
    await service.take_turn(campaign, "I say hello", hero)

    assert hero.user_display == "@hank"
    assert "@hank" not in seen[0]  # instructions
    assert "@hank" not in seen[1]  # prompt: sheets, transcript and the action
    # The character name is still there -- that's how the DM addresses them.
    assert "Thorn" in seen[1]


def test_the_sheet_players_see_still_names_them(hero: Character) -> None:
    """The handle is useful to humans, just not to the narrator."""
    assert "@hank" in render_character(hero, FANTASY, include_player=True)
    assert "@hank" not in render_character(hero, FANTASY)


def test_a_stored_genre_survives_a_new_field_on_starting_item() -> None:
    """Skins are read for the life of a campaign, so a row written by an older
    version has to keep working -- otherwise adding a field would re-skin every
    live custom campaign back to fantasy mid-play."""
    import json

    from src.game.genres import FANTASY, genre_from_dict, genre_to_dict

    raw = json.loads(json.dumps(genre_to_dict(FANTASY)))
    assert genre_from_dict(raw) == FANTASY

    for archetype in raw["archetypes"]:
        for item in archetype["starting_items"]:
            item["weight_in_stones"] = 3  # a field this version doesn't know

    restored = genre_from_dict(raw)
    assert restored.archetypes[0].starting_items[0].name == "Worn longsword"


def test_a_stored_genre_with_a_duplicated_priority_is_repaired() -> None:
    """assign_standard_array indexes positionally and would run off the end."""
    import json

    from src.game.characters import ABILITY_KEYS, assign_standard_array
    from src.game.genres import FANTASY, genre_from_dict, genre_to_dict

    raw = json.loads(json.dumps(genre_to_dict(FANTASY)))
    raw["archetypes"][0]["priority"] = ["dex", "dex", "dex"]

    priority = genre_from_dict(raw).archetypes[0].priority
    assert sorted(priority) == sorted(ABILITY_KEYS)
    assert assign_standard_array(priority)["dex"] == 15
