"""The DM's tools, driven against a real database with a scripted model.

FunctionModel lets us say "call this tool with these arguments, then answer", so we
exercise the real tool bodies and the real repo without touching a provider.
"""

from __future__ import annotations

import random
from collections.abc import Iterable

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.game.genres import FANTASY
from src.llm.dm_agent import GameDeps, dm_agent
from src.storage.repo import Campaign, Character, Repo


def scripted(
    *calls: tuple[str, dict[str, object]], final: str = "And so it goes."
) -> FunctionModel:
    """A model that makes the given tool calls one per turn, then answers."""
    queue = list(calls)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if queue:
            name, args = queue.pop(0)
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart(final)])

    return FunctionModel(respond)


def deps_for(repo: Repo, campaign: Campaign, actor: Character | None = None) -> GameDeps:
    return GameDeps(
        repo=repo,
        campaign=campaign,
        genre=FANTASY,
        actor=actor,
        rng=random.Random(11),
    )


def tool_outputs(messages: Iterable[ModelMessage]) -> list[str]:
    """Everything the tools said back to the model, in order.

    Successful calls arrive as tool-return parts; a ModelRetry arrives as a
    retry-prompt. Both are "what the model was told", so both belong here.
    """
    out = []
    for message in messages:
        for part in getattr(message, "parts", []):
            if part.part_kind in {"tool-return", "retry-prompt"}:
                out.append(str(part.content))
    return out


async def test_roll_dice_applies_the_ability_modifier_and_logs_it(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    model = scripted(
        ("roll_dice", {"expression": "str", "character_name": "Thorn", "reason": "door"})
    )
    result = await dm_agent.run(
        "I shoulder the door", model=model, deps=deps_for(repo, campaign, hero)
    )

    assert result.output == "And so it goes."
    rendered = tool_outputs(result.all_messages())[0]
    assert "STR" in rendered

    cur = await repo.conn.execute("SELECT expression, reason, character_id FROM dice_rolls")
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["reason"] == "door"
    assert rows[0]["character_id"] == hero.id


async def test_roll_dice_flags_a_natural_twenty(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """A seeded RNG lets us assert on the crit annotation deterministically."""
    deps = deps_for(repo, campaign, hero)
    # Find a seed whose first d20 is a 20 so the test isn't a coin flip.
    for seed in range(500):
        if random.Random(seed).randint(1, 20) == 20:
            deps.rng = random.Random(seed)
            break

    model = scripted(("roll_dice", {"expression": "1d20", "reason": "luck"}))
    result = await dm_agent.run("I try my luck", model=model, deps=deps)
    assert "NATURAL 20" in tool_outputs(result.all_messages())[0]


async def test_unknown_character_makes_the_model_retry(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """An invented name must come back as guidance, not blow up the turn."""
    model = scripted(
        ("update_hp", {"character_name": "Gandalf", "delta": -5, "reason": "rocks"}),
        ("update_hp", {"character_name": "Thorn", "delta": -5, "reason": "rocks"}),
    )
    result = await dm_agent.run("rocks fall", model=model, deps=deps_for(repo, campaign, hero))

    outputs = tool_outputs(result.all_messages())
    assert any("no character called" in o.lower() for o in outputs)

    character = await repo.get_character_by_id(hero.id)
    assert character is not None
    assert character.hp == hero.max_hp - 5


async def test_update_hp_warns_when_a_character_goes_down(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    model = scripted(("update_hp", {"character_name": "Thorn", "delta": -999, "reason": "dragon"}))
    result = await dm_agent.run(
        "the dragon exhales", model=model, deps=deps_for(repo, campaign, hero)
    )

    note = tool_outputs(result.all_messages())[0]
    assert "DOWN and dying" in note
    assert "don't kill them outright" in note


async def test_grant_xp_reports_a_level_up_and_raises_a_cue(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    deps = deps_for(repo, campaign, hero)
    model = scripted(
        ("grant_xp", {"character_name": "Thorn", "amount": 400, "reason": "the bridge"})
    )
    result = await dm_agent.run("we cross", model=model, deps=deps)

    assert "LEVEL UP" in tool_outputs(result.all_messages())[0]
    assert "reward" in deps.cues


async def test_grant_xp_rejects_nonpositive_awards(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    model = scripted(
        ("grant_xp", {"character_name": "Thorn", "amount": 0, "reason": "nothing"}),
        ("grant_xp", {"character_name": "Thorn", "amount": 50, "reason": "something"}),
    )
    result = await dm_agent.run("hmm", model=model, deps=deps_for(repo, campaign, hero))
    assert any("must be positive" in o for o in tool_outputs(result.all_messages()))

    character = await repo.get_character_by_id(hero.id)
    assert character is not None and character.xp == 50


async def test_items_round_trip_through_the_tools(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    model = scripted(
        (
            "add_item",
            {
                "character_name": "Thorn",
                "name": "Ember Blade",
                "description": "It smoulders.",
                "kind": "weapon",
                "equippable": True,
                "stat_mods": {"str": 1},
            },
        ),
        ("remove_item", {"character_name": "Thorn", "item_name": "Ember Blade"}),
    )
    await dm_agent.run("I loot the corpse", model=model, deps=deps_for(repo, campaign, hero))

    assert await repo.list_items(hero.id) == []


async def test_removing_an_item_they_lack_makes_the_model_retry(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    model = scripted(("remove_item", {"character_name": "Thorn", "item_name": "Excalibur"}))
    result = await dm_agent.run("I drop it", model=model, deps=deps_for(repo, campaign, hero))
    assert any("isn't carrying" in o for o in tool_outputs(result.all_messages()))


async def test_record_event_persists_and_validates_kind(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    deps = deps_for(repo, campaign, hero)
    model = scripted(
        ("record_event", {"kind": "gossip", "summary": "wrong kind"}),
        ("record_event", {"kind": "quest", "summary": "Retrieve the lens", "entities": ["Vex"]}),
    )
    result = await dm_agent.run("we agree", model=model, deps=deps)

    assert any("kind must be one of" in o for o in tool_outputs(result.all_messages()))

    events = await repo.recent_events(campaign.id)
    assert len(events) == 1
    assert events[0].kind == "quest"
    assert events[0].entities == ("Vex",)
    assert "new_quest" in deps.cues


async def test_award_achievement_returns_a_block_once(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    deps = deps_for(repo, campaign, hero)
    model = scripted(
        ("award_achievement", {"character_name": "Thorn", "achievement_id": "door-tax"}),
        ("award_achievement", {"character_name": "Thorn", "achievement_id": "door-tax"}),
    )
    result = await dm_agent.run("I open the fridge", model=model, deps=deps)

    outputs = tool_outputs(result.all_messages())
    assert "🏆 ACHIEVEMENT UNLOCKED" in outputs[0]
    assert "already has" in outputs[1]
    assert "new_achievement" in deps.cues


async def test_unknown_achievement_id_makes_the_model_retry(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    model = scripted(
        ("award_achievement", {"character_name": "Thorn", "achievement_id": "not-a-real-one"})
    )
    result = await dm_agent.run(
        "something happens", model=model, deps=deps_for(repo, campaign, hero)
    )
    assert any("No achievement with id" in o for o in tool_outputs(result.all_messages()))


async def test_get_party_reports_live_numbers(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    await repo.apply_damage(hero.id, -4)
    model = scripted(("get_party", {}))
    result = await dm_agent.run("who's hurt?", model=model, deps=deps_for(repo, campaign, hero))

    sheet = tool_outputs(result.all_messages())[0]
    assert "Thorn" in sheet
    assert f"HP {hero.max_hp - 4}/{hero.max_hp}" in sheet


@pytest.mark.parametrize("expression", ["banana", ""])
async def test_bad_dice_expressions_make_the_model_retry(
    repo: Repo, campaign: Campaign, hero: Character, expression: str
) -> None:
    model = scripted(("roll_dice", {"expression": expression, "character_name": "Thorn"}))
    result = await dm_agent.run("I do a thing", model=model, deps=deps_for(repo, campaign, hero))
    assert any("notation" in o for o in tool_outputs(result.all_messages()))
