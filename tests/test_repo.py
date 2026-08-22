"""Storage layer: campaigns, multiplayer characters, items, transcript."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from src.game.characters import max_hp_for
from src.storage.repo import Campaign, Character, MissingRow, Repo


async def test_create_and_fetch_campaign(repo: Repo) -> None:
    campaign = await repo.create_campaign(chat_id=-1, genre="scifi", genre_skin={"str": "Brawn"})
    live = await repo.get_live_campaign(-1)
    assert live is not None
    assert live.id == campaign.id
    assert live.genre == "scifi"
    assert live.genre_skin["str"] == "Brawn"
    assert live.status == "creating"


async def test_new_campaign_ends_the_previous_one(repo: Repo) -> None:
    first = await repo.create_campaign(chat_id=-1, genre="fantasy")
    second = await repo.create_campaign(chat_id=-1, genre="horror")

    live = await repo.get_live_campaign(-1)
    assert live is not None and live.id == second.id

    old = await repo.get_campaign(first.id)
    assert old is not None and old.status == "ended"


async def test_two_players_get_separate_characters(repo: Repo, campaign: Campaign) -> None:
    """The old schema keyed the sheet on the chat, so a group shared one character."""
    a = await repo.create_character(campaign.id, user_id=1, name="Thorn")
    b = await repo.create_character(campaign.id, user_id=2, name="Vex")

    assert a.id != b.id
    party = await repo.list_party(campaign.id)
    assert {c.name for c in party} == {"Thorn", "Vex"}

    await repo.apply_damage(a.id, -3)
    refreshed_b = await repo.get_character(campaign.id, user_id=2)
    assert refreshed_b is not None and refreshed_b.hp == refreshed_b.max_hp


async def test_find_character_by_name_is_case_insensitive(repo: Repo, hero: Character) -> None:
    found = await repo.find_character_by_name(hero.campaign_id, "thORn")
    assert found is not None and found.id == hero.id
    assert await repo.find_character_by_name(hero.campaign_id, "Nobody") is None


async def test_damage_clamps_and_flags_dying(repo: Repo, hero: Character) -> None:
    hurt = await repo.apply_damage(hero.id, -9999)
    assert hurt.hp == 0
    assert hurt.status == "dying"

    healed = await repo.apply_damage(hero.id, 5)
    assert healed.hp == 5
    assert healed.status == "active"

    overhealed = await repo.apply_damage(hero.id, 9999)
    assert overhealed.hp == overhealed.max_hp


async def test_grant_xp_levels_up_and_raises_max_hp(repo: Repo, hero: Character) -> None:
    character, levelled = await repo.grant_xp(hero.id, 100)
    assert not levelled
    assert character.level == 1

    character, levelled = await repo.grant_xp(hero.id, 250)  # crosses 300
    assert levelled
    assert character.level == 2
    assert character.max_hp == max_hp_for(2, hero.abilities["con"])
    assert character.max_hp > hero.max_hp


async def test_levelling_up_picks_a_dying_character_off_the_floor(
    repo: Repo, hero: Character
) -> None:
    downed = await repo.apply_damage(hero.id, -9999)
    assert downed.status == "dying"

    character, levelled = await repo.grant_xp(hero.id, 300)
    assert levelled
    assert character.hp > 0
    assert character.status == "active"


async def test_xp_cannot_be_negative(repo: Repo, hero: Character) -> None:
    with pytest.raises(ValueError, match="negative"):
        await repo.grant_xp(hero.id, -500)


async def test_simultaneous_damage_is_not_lost(repo: Repo, hero: Character) -> None:
    """Two players' turns land at once; both hits have to count.

    This is the shape of bug the whole async rewrite exists to prevent -- a
    read-modify-write that interleaves and silently drops one of the writes.
    """
    start = hero.hp
    await asyncio.gather(*(repo.apply_damage(hero.id, -1) for _ in range(5)))

    character = await repo.get_character_by_id(hero.id)
    assert character is not None
    assert character.hp == start - 5


async def test_simultaneous_xp_awards_are_not_lost(repo: Repo, hero: Character) -> None:
    await asyncio.gather(*(repo.grant_xp(hero.id, 10) for _ in range(5)))

    character = await repo.get_character_by_id(hero.id)
    assert character is not None
    assert character.xp == 50


async def test_adding_the_same_item_twice_stacks(repo: Repo, hero: Character) -> None:
    await repo.add_item(hero.id, "Torch", quantity=2)
    item = await repo.add_item(hero.id, "Torch", quantity=3)
    assert item.quantity == 5

    assert await repo.remove_item(hero.id, "torch", quantity=4)
    remaining = await repo.list_items(hero.id)
    assert remaining[0].quantity == 1

    assert await repo.remove_item(hero.id, "Torch")
    assert await repo.list_items(hero.id) == []
    assert not await repo.remove_item(hero.id, "Torch")


async def test_item_names_stack_regardless_of_case(repo: Repo, hero: Character) -> None:
    """The DM writes item names freehand, so casing must not split the stack."""
    await repo.add_item(hero.id, "Health Potion", quantity=1)
    await repo.add_item(hero.id, "health potion", quantity=2)

    items = await repo.list_items(hero.id)
    assert len(items) == 1
    assert items[0].quantity == 3


async def test_re_adding_an_item_refreshes_its_properties(repo: Repo, hero: Character) -> None:
    """Keith upgrades a plain blade into a magic one by adding it again."""
    await repo.add_item(hero.id, "Blade", description="A blade.")
    await repo.add_item(
        hero.id,
        "Blade",
        description="It hums.",
        equippable=True,
        stat_mods={"str": 2},
    )

    items = await repo.list_items(hero.id)
    assert len(items) == 1
    assert items[0].quantity == 2
    assert items[0].description == "It hums."
    assert items[0].equippable
    assert items[0].stat_mods == {"str": 2}
    assert await repo.set_equipped(hero.id, "Blade", True)


async def test_equipped_items_modify_effective_abilities(repo: Repo, hero: Character) -> None:
    await repo.add_item(hero.id, "Belt of Heft", equippable=True, stat_mods={"str": 2})
    assert await repo.set_equipped(hero.id, "belt of heft", True)

    equipped = await repo.get_character_by_id(hero.id)
    assert equipped is not None
    assert equipped.abilities["str"] == 14  # base is untouched
    assert equipped.effective_abilities()["str"] == 16
    assert equipped.modifier("str") == 3


async def test_cannot_equip_a_non_equippable_item(repo: Repo, hero: Character) -> None:
    await repo.add_item(hero.id, "Rock", equippable=False)
    assert not await repo.set_equipped(hero.id, "Rock", True)


async def test_transcript_round_trip(repo: Repo, campaign: Campaign, hero: Character) -> None:
    for i in range(5):
        await repo.add_message(campaign.id, "player", f"turn {i}", character_id=hero.id)
        await repo.add_message(campaign.id, "dm", f"reply {i}")

    recent = await repo.recent_messages(campaign.id, limit=4)
    assert [m.content for m in recent] == ["turn 3", "reply 3", "turn 4", "reply 4"]
    assert recent[0].role == "player"

    assert await repo.count_messages_after(campaign.id, after_id=0) == 10
    window = await repo.messages_between(campaign.id, after_id=2, before_id=5)
    assert len(window) == 3


async def test_story_events_and_watermark(repo: Repo, campaign: Campaign) -> None:
    await repo.add_story_event(campaign.id, "npc", "Met Vex the fence", ["Vex"])
    await repo.add_story_event(campaign.id, "quest", "Retrieve the lens")

    events = await repo.recent_events(campaign.id)
    assert [e.kind for e in events] == ["npc", "quest"]
    assert events[0].entities == ("Vex",)

    await repo.update_campaign(campaign.id, summary="Things happened.", summary_through_id=7)
    refreshed = await repo.get_campaign(campaign.id)
    assert refreshed is not None
    assert refreshed.summary == "Things happened."
    assert refreshed.summary_through_id == 7


async def test_achievements_are_per_character(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    other = await repo.create_character(campaign.id, user_id=2, name="Vex")

    await repo.grant_achievement(campaign.id, hero.id, "icebox-raider", "common")
    assert await repo.has_achievement(hero.id, "icebox-raider")
    assert not await repo.has_achievement(other.id, "icebox-raider")
    assert await repo.list_achievements(hero.id) == [("icebox-raider", "common")]


async def test_asset_cache_upserts(repo: Repo) -> None:
    assert await repo.get_asset("reward") is None
    await repo.set_asset("reward", "file-abc")
    await repo.set_asset("reward", "file-def")
    assert await repo.get_asset("reward") == "file-def"


async def test_one_character_per_player_per_campaign(repo: Repo, hero: Character) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await repo.create_character(hero.campaign_id, user_id=hero.user_id, name="Impostor")


async def test_only_one_live_campaign_per_chat(repo: Repo, campaign: Campaign) -> None:
    """Enforced by the partial unique index, not just by application logic."""
    with pytest.raises(sqlite3.IntegrityError):
        await repo.conn.execute(
            "INSERT INTO campaigns (chat_id, genre) VALUES (?, ?)",
            (campaign.chat_id, "horror"),
        )


async def test_deleting_a_campaign_cascades(
    repo: Repo, campaign: Campaign, hero: Character
) -> None:
    """Foreign keys are a per-connection PRAGMA that fails silently if not set."""
    await repo.add_item(hero.id, "Torch")
    await repo.conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign.id,))
    await repo.conn.commit()

    assert await repo.list_party(campaign.id) == []
    assert await repo.list_items(hero.id) == []


async def test_orphan_rows_are_rejected(repo: Repo) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await repo.conn.execute(
            "INSERT INTO characters (campaign_id, user_id, name) VALUES (?, ?, ?)",
            (9999, 1, "Ghost"),
        )


async def test_operations_on_a_missing_character_raise_clearly(repo: Repo) -> None:
    with pytest.raises(MissingRow):
        await repo.apply_damage(9999, -1)
    with pytest.raises(MissingRow):
        await repo.grant_xp(9999, 10)
