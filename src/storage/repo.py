"""Typed CRUD over the SQLite schema.

Every read returns a frozen dataclass; every write goes through a method here.
Nothing else in the codebase writes SQL, so the schema has exactly one consumer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from src.game.characters import (
    ability_modifier,
    clamp_ability,
    level_from_xp,
    max_hp_for,
    normalise_abilities,
)


@dataclass(frozen=True)
class Campaign:
    id: int
    chat_id: int
    genre: str
    genre_skin: dict[str, Any]
    tone: str
    status: str
    summary: str
    summary_through_id: int


@dataclass(frozen=True)
class Item:
    id: int
    character_id: int
    name: str
    kind: str
    description: str
    quantity: int
    equippable: bool
    equipped: bool
    consumable: bool
    stat_mods: dict[str, int]


@dataclass(frozen=True)
class Character:
    id: int
    campaign_id: int
    user_id: int
    user_display: str
    name: str
    archetype: str
    origin: str
    concept: str
    level: int
    xp: int
    hp: int
    max_hp: int
    abilities: dict[str, int]
    status: str
    items: tuple[Item, ...] = ()

    def effective_abilities(self) -> dict[str, int]:
        """Base scores plus the mods from everything currently equipped.

        Clamped to the normal 1-20 range: gear should tilt the odds, not break the
        maths, however enthusiastic the DM got when it invented the item.
        """
        scores = dict(self.abilities)
        for item in self.items:
            if not item.equipped:
                continue
            for key, mod in item.stat_mods.items():
                if key in scores:
                    scores[key] = clamp_ability(scores[key] + mod)
        return scores

    def modifier(self, ability: str) -> int:
        return ability_modifier(self.effective_abilities().get(ability, 10))


@dataclass(frozen=True)
class PendingRoll:
    """A check Keith has handed to a player to roll for themselves."""

    id: int
    campaign_id: int
    character_id: int
    ability: str
    dc: int
    reason: str


@dataclass(frozen=True)
class Message:
    id: int
    role: str
    character_id: int | None
    content: str
    created_at: str = ""


@dataclass(frozen=True)
class Chapter:
    """One chapter of the campaign's written-up story."""

    id: int
    number: int
    title: str
    body: str
    through_message_id: int
    through_grant_id: int


@dataclass(frozen=True)
class Grant:
    """An achievement someone earned, with enough context to print it in a book."""

    id: int
    achievement_id: str
    rarity: str
    character_name: str


@dataclass(frozen=True)
class StoryEvent:
    id: int
    kind: str
    summary: str
    entities: tuple[str, ...] = field(default=())


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


class MissingRow(LookupError):
    """A row we just wrote, or were handed the id of, isn't there."""


class Repo:
    """All database access, on one aiosqlite connection.

    Anything that reads a row, does arithmetic in Python, and writes the result back
    has to hold `_write_lock`. We run on a single shared connection, so every `await`
    inside such a sequence is a point where another player's turn can interleave and
    read the pre-update value -- which silently loses damage, XP and consumed items.
    Plain single-statement reads and writes don't need the lock.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self._write_lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Hold the write lock, and roll back if the block doesn't finish.

        Without the rollback, sqlite leaves the implicit transaction open after an
        exception -- and because the connection is shared, the *next* commit anywhere
        in the app (a player taking a turn, say) would quietly persist the half-done
        work. That's how a failed book rewrite could delete most of a book.
        """
        async with self._write_lock:
            try:
                yield
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    # -- campaigns ---------------------------------------------------------

    async def get_live_campaign(self, chat_id: int) -> Campaign | None:
        cur = await self.conn.execute(
            "SELECT * FROM campaigns WHERE chat_id = ? AND status != 'ended'", (chat_id,)
        )
        row = await cur.fetchone()
        return self._campaign(row) if row else None

    async def get_campaign(self, campaign_id: int) -> Campaign | None:
        cur = await self.conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        row = await cur.fetchone()
        return self._campaign(row) if row else None

    async def create_campaign(
        self,
        chat_id: int,
        genre: str,
        genre_skin: dict[str, Any] | None = None,
        tone: str = "pg13",
    ) -> Campaign:
        """Start a campaign, ending any campaign already live in this chat.

        Both statements commit together: a crash between them would otherwise leave
        the chat with no live campaign at all.
        """
        async with self.transaction():
            await self.conn.execute(
                "UPDATE campaigns SET status = 'ended', updated_at = datetime('now') "
                "WHERE chat_id = ? AND status != 'ended'",
                (chat_id,),
            )
            cur = await self.conn.execute(
                "INSERT INTO campaigns (chat_id, genre, genre_skin, tone) VALUES (?, ?, ?, ?)",
                (chat_id, genre, json.dumps(genre_skin or {}), tone),
            )
            campaign_id = int(cur.lastrowid or 0)

        campaign = await self.get_campaign(campaign_id)
        if campaign is None:
            raise MissingRow(f"campaign {campaign_id} vanished immediately after insert")
        return campaign

    async def end_campaign(self, chat_id: int) -> None:
        await self.conn.execute(
            "UPDATE campaigns SET status = 'ended', updated_at = datetime('now') "
            "WHERE chat_id = ? AND status != 'ended'",
            (chat_id,),
        )
        await self.conn.commit()

    async def try_activate(self, campaign_id: int) -> bool:
        """Move a campaign from 'creating' to 'active'. True only for the winner.

        The status test is inside the UPDATE, so two simultaneous /begin commands
        can't both open the story.
        """
        cur = await self.conn.execute(
            "UPDATE campaigns SET status = 'active', updated_at = datetime('now') "
            "WHERE id = ? AND status = 'creating'",
            (campaign_id,),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def update_campaign(self, campaign_id: int, **fields: Any) -> None:
        allowed = {"genre", "genre_skin", "tone", "status", "summary", "summary_through_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "genre_skin" in updates and not isinstance(updates["genre_skin"], str):
            updates["genre_skin"] = json.dumps(updates["genre_skin"])
        clause = ", ".join(f"{k} = ?" for k in updates)
        await self.conn.execute(
            f"UPDATE campaigns SET {clause}, updated_at = datetime('now') WHERE id = ?",
            (*updates.values(), campaign_id),
        )
        await self.conn.commit()

    # -- characters --------------------------------------------------------

    async def get_character(self, campaign_id: int, user_id: int) -> Character | None:
        cur = await self.conn.execute(
            "SELECT * FROM characters WHERE campaign_id = ? AND user_id = ?",
            (campaign_id, user_id),
        )
        row = await cur.fetchone()
        return await self._character(row) if row else None

    async def get_character_by_id(self, character_id: int) -> Character | None:
        cur = await self.conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,))
        row = await cur.fetchone()
        return await self._character(row) if row else None

    async def find_character_by_name(self, campaign_id: int, name: str) -> Character | None:
        """Case-insensitive lookup -- the DM refers to characters by name, not id."""
        cur = await self.conn.execute(
            "SELECT * FROM characters WHERE campaign_id = ? AND lower(name) = lower(?)",
            (campaign_id, name.strip()),
        )
        row = await cur.fetchone()
        return await self._character(row) if row else None

    async def list_party(self, campaign_id: int) -> list[Character]:
        cur = await self.conn.execute(
            "SELECT * FROM characters WHERE campaign_id = ? ORDER BY id", (campaign_id,)
        )
        return [await self._character(row) for row in await cur.fetchall()]

    async def create_character(
        self,
        campaign_id: int,
        user_id: int,
        name: str,
        *,
        user_display: str = "",
        archetype: str = "",
        origin: str = "",
        concept: str = "",
        abilities: dict[str, int] | None = None,
    ) -> Character:
        scores = normalise_abilities(abilities)
        hp = max_hp_for(1, scores["con"])
        cur = await self.conn.execute(
            "INSERT INTO characters (campaign_id, user_id, user_display, name, archetype, "
            "origin, concept, hp, max_hp, abilities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                campaign_id,
                user_id,
                user_display,
                name,
                archetype,
                origin,
                concept,
                hp,
                hp,
                json.dumps(scores),
            ),
        )
        await self.conn.commit()
        character_id = int(cur.lastrowid or 0)
        character = await self.get_character_by_id(character_id)
        if character is None:
            raise MissingRow(f"character {character_id} vanished immediately after insert")
        return character

    async def update_character(self, character_id: int, **fields: Any) -> None:
        allowed = {
            "user_display",
            "name",
            "archetype",
            "origin",
            "concept",
            "level",
            "xp",
            "hp",
            "max_hp",
            "abilities",
            "status",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "abilities" in updates and not isinstance(updates["abilities"], str):
            updates["abilities"] = json.dumps(updates["abilities"])
        clause = ", ".join(f"{k} = ?" for k in updates)
        await self.conn.execute(
            f"UPDATE characters SET {clause} WHERE id = ?", (*updates.values(), character_id)
        )
        await self.conn.commit()

    async def apply_damage(self, character_id: int, delta: int) -> Character:
        """Add `delta` to HP (negative for damage), clamped to 0..max_hp.

        The clamp and the dying/active flip happen inside one UPDATE so two
        simultaneous hits can't read the same starting HP and lose one of them.
        """
        cur = await self.conn.execute(
            """
            UPDATE characters SET
                hp = max(0, min(max_hp, hp + :delta)),
                status = CASE
                    WHEN max(0, min(max_hp, hp + :delta)) = 0 AND status = 'active' THEN 'dying'
                    WHEN max(0, min(max_hp, hp + :delta)) > 0 AND status = 'dying' THEN 'active'
                    ELSE status
                END
            WHERE id = :id
            """,
            {"delta": delta, "id": character_id},
        )
        await self.conn.commit()
        if cur.rowcount == 0:
            raise MissingRow(f"no character with id {character_id}")

        refreshed = await self.get_character_by_id(character_id)
        if refreshed is None:
            raise MissingRow(f"no character with id {character_id}")
        return refreshed

    async def grant_xp(self, character_id: int, amount: int) -> tuple[Character, bool]:
        """Award XP. Returns the refreshed character and whether it levelled up.

        XP only ever goes up -- de-levelling would need max_hp and ability picks
        unwound too, which isn't a thing the game does.
        """
        if amount < 0:
            raise ValueError("XP awards cannot be negative")

        async with self._write_lock:
            character = await self.get_character_by_id(character_id)
            if character is None:
                raise MissingRow(f"no character with id {character_id}")

            new_xp = character.xp + amount
            new_level = level_from_xp(new_xp)
            levelled = new_level > character.level

            fields: dict[str, Any] = {"xp": new_xp, "level": new_level}
            if levelled:
                new_max = max_hp_for(new_level, character.abilities["con"])
                fields["max_hp"] = new_max
                # Levelling heals the difference, so a level-up is always a relief --
                # and it picks a downed character back up off the floor.
                fields["hp"] = min(new_max, character.hp + (new_max - character.max_hp))
                if fields["hp"] > 0 and character.status == "dying":
                    fields["status"] = "active"
            await self.update_character(character_id, **fields)

        refreshed = await self.get_character_by_id(character_id)
        if refreshed is None:
            raise MissingRow(f"no character with id {character_id}")
        return refreshed, levelled

    # -- items -------------------------------------------------------------

    async def list_items(self, character_id: int) -> list[Item]:
        cur = await self.conn.execute(
            "SELECT * FROM items WHERE character_id = ? ORDER BY id", (character_id,)
        )
        return [self._item(row) for row in await cur.fetchall()]

    async def add_item(
        self,
        character_id: int,
        name: str,
        *,
        kind: str = "misc",
        description: str = "",
        quantity: int = 1,
        equippable: bool = False,
        consumable: bool = False,
        stat_mods: dict[str, int] | None = None,
    ) -> Item:
        """Add an item, or bump the quantity if the character already has one.

        Re-adding an existing item also refreshes its properties, so the DM can
        upgrade a plain sword into a magic one by adding it again with stat_mods.
        Name matching is case-insensitive (the index collates NOCASE), because the
        DM generates item names from free text and will not be consistent about it.
        """
        clean = name.strip()
        await self.conn.execute(
            """
            INSERT INTO items (character_id, name, kind, description, quantity,
                               equippable, consumable, stat_mods)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (character_id, name) DO UPDATE SET
                quantity    = quantity + excluded.quantity,
                kind        = excluded.kind,
                description = excluded.description,
                equippable  = excluded.equippable,
                consumable  = excluded.consumable,
                stat_mods   = excluded.stat_mods
            """,
            (
                character_id,
                clean,
                kind,
                description,
                max(1, quantity),
                int(equippable),
                int(consumable),
                json.dumps(stat_mods) if stat_mods else None,
            ),
        )
        await self.conn.commit()
        cur = await self.conn.execute(
            "SELECT * FROM items WHERE character_id = ? AND name = ?", (character_id, clean)
        )
        row = await cur.fetchone()
        if row is None:
            raise MissingRow(f"item {clean!r} vanished immediately after insert")
        return self._item(row)

    async def remove_item(self, character_id: int, name: str, quantity: int = 1) -> bool:
        """Drop `quantity` of an item. Returns False if they never had it."""
        async with self._write_lock:
            cur = await self.conn.execute(
                "SELECT * FROM items WHERE character_id = ? AND name = ?",
                (character_id, name.strip()),
            )
            row = await cur.fetchone()
            if row is None:
                return False
            remaining = row["quantity"] - max(1, quantity)
            if remaining > 0:
                await self.conn.execute(
                    "UPDATE items SET quantity = ? WHERE id = ?", (remaining, row["id"])
                )
            else:
                await self.conn.execute("DELETE FROM items WHERE id = ?", (row["id"],))
            await self.conn.commit()
            return True

    async def set_equipped(self, character_id: int, name: str, equipped: bool) -> bool:
        cur = await self.conn.execute(
            "UPDATE items SET equipped = ? WHERE character_id = ? AND name = ? AND equippable = 1",
            (int(equipped), character_id, name.strip()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # -- transcript & memory -----------------------------------------------

    async def add_message(
        self, campaign_id: int, role: str, content: str, character_id: int | None = None
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO messages (campaign_id, role, character_id, content) VALUES (?, ?, ?, ?)",
            (campaign_id, role, character_id, content),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def recent_messages(self, campaign_id: int, limit: int = 30) -> list[Message]:
        """The last `limit` messages, oldest first."""
        cur = await self.conn.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE campaign_id = ? ORDER BY id DESC LIMIT ?) "
            "ORDER BY id ASC",
            (campaign_id, limit),
        )
        return [self._message(row) for row in await cur.fetchall()]

    async def messages_between(
        self, campaign_id: int, after_id: int, before_id: int
    ) -> list[Message]:
        cur = await self.conn.execute(
            "SELECT * FROM messages WHERE campaign_id = ? AND id > ? AND id <= ? ORDER BY id",
            (campaign_id, after_id, before_id),
        )
        return [self._message(row) for row in await cur.fetchall()]

    async def messages_after(self, campaign_id: int, after_id: int) -> list[Message]:
        """Everything since a watermark, oldest first."""
        cur = await self.conn.execute(
            "SELECT * FROM messages WHERE campaign_id = ? AND id > ? ORDER BY id",
            (campaign_id, after_id),
        )
        return [self._message(row) for row in await cur.fetchall()]

    async def count_messages_after(self, campaign_id: int, after_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT count(*) AS n FROM messages WHERE campaign_id = ? AND id > ?",
            (campaign_id, after_id),
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def messages_since_last_player_roll(self, campaign_id: int) -> int | None:
        """How many messages have passed since a player last touched the dice.

        None means nobody has ever rolled here -- a different situation from "rolled
        a while ago", and the nudge phrases the two differently.

        Reads the `roll_marks` watermark rather than comparing timestamps: rolls and
        messages are separate id sequences, and `created_at` is only accurate to the
        second, so it can't order a roll against messages written alongside it.

        A campaign that was mid-flight when this shipped has no watermark and reads
        as never having rolled, so the nudge fires once and then settles as soon as
        anyone rolls. That's the right way round: it errs toward more dice.
        """
        cur = await self.conn.execute(
            "SELECT message_id FROM roll_marks WHERE campaign_id = ?", (campaign_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return await self.count_messages_after(campaign_id, int(row["message_id"]))

    async def add_story_event(
        self, campaign_id: int, kind: str, summary: str, entities: list[str] | None = None
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO story_events (campaign_id, kind, summary, entities) VALUES (?, ?, ?, ?)",
            (campaign_id, kind, summary, json.dumps(entities or [])),
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def recent_events(self, campaign_id: int, limit: int = 20) -> list[StoryEvent]:
        cur = await self.conn.execute(
            "SELECT * FROM (SELECT * FROM story_events WHERE campaign_id = ? ORDER BY id DESC "
            "LIMIT ?) ORDER BY id ASC",
            (campaign_id, limit),
        )
        return [
            StoryEvent(
                id=row["id"],
                kind=row["kind"],
                summary=row["summary"],
                entities=tuple(_json_load(row["entities"], [])),
            )
            for row in await cur.fetchall()
        ]

    # -- rolls & achievements ----------------------------------------------

    async def log_roll(
        self,
        campaign_id: int,
        expression: str,
        detail: str,
        total: int,
        *,
        character_id: int | None = None,
        source: str = "dm",
        reason: str = "",
    ) -> None:
        # A player's roll also moves the pacing watermark, so the two writes go in
        # one transaction: a half-applied pair would leave the nudge reading a
        # watermark for a roll that isn't there.
        async with self.transaction():
            await self.conn.execute(
                "INSERT INTO dice_rolls (campaign_id, character_id, expression, detail, total, "
                "source, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (campaign_id, character_id, expression, detail, total, source, reason),
            )
            if source == "player":
                await self.conn.execute(
                    "INSERT INTO roll_marks (campaign_id, message_id) "
                    "VALUES (?, (SELECT coalesce(max(id), 0) FROM messages WHERE campaign_id = ?)) "
                    "ON CONFLICT (campaign_id) DO UPDATE SET message_id = excluded.message_id",
                    (campaign_id, campaign_id),
                )

    # -- pending player rolls ----------------------------------------------

    async def create_pending_roll(
        self, campaign_id: int, character_id: int, ability: str, dc: int, reason: str
    ) -> PendingRoll:
        """Ask a character for a roll, replacing any roll they already owe.

        Delete-then-insert rather than an upsert, so the replacement gets a fresh id
        and the superseded button in the chat can never be tapped into the new roll.
        """
        async with self.transaction():
            await self.conn.execute(
                "DELETE FROM pending_rolls WHERE campaign_id = ? AND character_id = ?",
                (campaign_id, character_id),
            )
            cur = await self.conn.execute(
                "INSERT INTO pending_rolls (campaign_id, character_id, ability, dc, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (campaign_id, character_id, ability, dc, reason),
            )
            roll_id = int(cur.lastrowid or 0)
            cur = await self.conn.execute("SELECT * FROM pending_rolls WHERE id = ?", (roll_id,))
            row = await cur.fetchone()

        if row is None:
            raise MissingRow("pending roll vanished immediately after insert")
        return self._pending_roll(row)

    async def claim_pending_roll(
        self, roll_id: int, user_id: int
    ) -> tuple[PendingRoll | None, Character | None]:
        """Claim a roll on behalf of `user_id`.

        Returns (roll, character) when claimed; (None, character) when it belongs to
        somebody else, so the caller can say whose it is; (None, None) when it has
        already been made or never existed.

        The ownership test and the delete happen together under the write lock, so
        two taps can't both come back with a roll and nobody can take another
        player's.
        """
        async with self._write_lock:
            cur = await self.conn.execute("SELECT * FROM pending_rolls WHERE id = ?", (roll_id,))
            row = await cur.fetchone()
            if row is None:
                return None, None

            character = await self.get_character_by_id(row["character_id"])
            if character is None:
                return None, None
            if character.user_id != user_id:
                return None, character

            await self.conn.execute("DELETE FROM pending_rolls WHERE id = ?", (roll_id,))
            await self.conn.commit()

        return self._pending_roll(row), character

    async def has_pending_roll(self, campaign_id: int) -> bool:
        """Whether anyone in this campaign currently owes a roll.

        The dice aren't quiet while a button is sitting in the chat unpressed --
        nothing has been rolled, but asking again is the wrong thing to do.
        """
        cur = await self.conn.execute(
            "SELECT 1 FROM pending_rolls WHERE campaign_id = ? LIMIT 1", (campaign_id,)
        )
        return await cur.fetchone() is not None

    async def delete_pending_roll(self, roll_id: int) -> None:
        """Drop one outstanding roll.

        Used when a turn asked for a check but then failed before its button could
        be posted: the row would otherwise leave a character owing a roll that
        nothing in the chat ever offers them.
        """
        async with self._write_lock:
            await self.conn.execute("DELETE FROM pending_rolls WHERE id = ?", (roll_id,))
            await self.conn.commit()

    async def clear_pending_rolls(self, campaign_id: int) -> None:
        """Drop every outstanding roll. Called when a campaign is retired."""
        async with self._write_lock:
            await self.conn.execute(
                "DELETE FROM pending_rolls WHERE campaign_id = ?", (campaign_id,)
            )
            await self.conn.commit()

    async def has_achievement(self, character_id: int, achievement_id: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM achievement_grants WHERE character_id = ? AND achievement_id = ?",
            (character_id, achievement_id),
        )
        return await cur.fetchone() is not None

    async def grant_achievement(
        self, campaign_id: int, character_id: int, achievement_id: str, rarity: str
    ) -> None:
        await self.conn.execute(
            "INSERT INTO achievement_grants (campaign_id, character_id, achievement_id, rarity) "
            "VALUES (?, ?, ?, ?)",
            (campaign_id, character_id, achievement_id, rarity),
        )
        await self.conn.commit()

    async def list_achievements(self, character_id: int) -> list[tuple[str, str]]:
        cur = await self.conn.execute(
            "SELECT achievement_id, rarity FROM achievement_grants WHERE character_id = ? "
            "ORDER BY id",
            (character_id,),
        )
        return [(row["achievement_id"], row["rarity"]) for row in await cur.fetchall()]

    # -- the chronicle -----------------------------------------------------

    async def list_chapters(self, campaign_id: int) -> list[Chapter]:
        cur = await self.conn.execute(
            "SELECT * FROM chronicle_chapters WHERE campaign_id = ? ORDER BY number",
            (campaign_id,),
        )
        return [self._chapter(row) for row in await cur.fetchall()]

    async def last_chapter(self, campaign_id: int) -> Chapter | None:
        cur = await self.conn.execute(
            "SELECT * FROM chronicle_chapters WHERE campaign_id = ? ORDER BY number DESC LIMIT 1",
            (campaign_id,),
        )
        row = await cur.fetchone()
        return self._chapter(row) if row else None

    async def add_chapter(
        self,
        campaign_id: int,
        title: str,
        body: str,
        through_message_id: int,
        through_grant_id: int,
    ) -> Chapter:
        """Append a chapter. Numbering is derived, so callers can't skip or collide."""
        async with self._write_lock:
            cur = await self.conn.execute(
                "SELECT coalesce(max(number), 0) + 1 AS next FROM chronicle_chapters "
                "WHERE campaign_id = ?",
                (campaign_id,),
            )
            row = await cur.fetchone()
            number = int(row["next"]) if row else 1

            cur = await self.conn.execute(
                "INSERT INTO chronicle_chapters (campaign_id, number, title, body, "
                "through_message_id, through_grant_id) VALUES (?, ?, ?, ?, ?, ?)",
                (campaign_id, number, title, body, through_message_id, through_grant_id),
            )
            await self.conn.commit()
            chapter_id = int(cur.lastrowid or 0)

        cur = await self.conn.execute(
            "SELECT * FROM chronicle_chapters WHERE id = ?", (chapter_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise MissingRow(f"chapter {chapter_id} vanished immediately after insert")
        return self._chapter(row)

    async def delete_chapter(self, chapter_id: int) -> None:
        await self.conn.execute("DELETE FROM chronicle_chapters WHERE id = ?", (chapter_id,))
        await self.conn.commit()

    async def replace_chapters(self, campaign_id: int, chapters: list[Chapter]) -> None:
        """Swap the whole book in one transaction.

        The final polish pass rewrites every chapter; committing them one at a time
        would leave a half-rewritten book if it failed partway.
        """
        async with self.transaction():
            await self.conn.execute(
                "DELETE FROM chronicle_chapters WHERE campaign_id = ?", (campaign_id,)
            )
            for number, chapter in enumerate(chapters, start=1):
                await self.conn.execute(
                    "INSERT INTO chronicle_chapters (campaign_id, number, title, body, "
                    "through_message_id, through_grant_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        campaign_id,
                        number,
                        chapter.title,
                        chapter.body,
                        chapter.through_message_id,
                        chapter.through_grant_id,
                    ),
                )

    async def latest_grant_id(self, campaign_id: int, before: str | None = None) -> int:
        """The newest achievement grant, optionally only those earned before `before`.

        A chapter's achievements are the ones earned during its span, not every
        achievement in the campaign. `achievement_grants` has no message id to join
        on, so a timestamp is the boundary -- and it has to be the *next* span's first
        message rather than this span's last, because an achievement is awarded just
        after the message that earned it and can carry a later second.
        """
        sql = "SELECT coalesce(max(id), 0) AS latest FROM achievement_grants WHERE campaign_id = ?"
        params: tuple[Any, ...] = (campaign_id,)
        if before is not None:
            sql += " AND awarded_at < ?"
            params += (before,)

        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        return int(row["latest"]) if row else 0

    async def grants_between(self, campaign_id: int, after_id: int, through_id: int) -> list[Grant]:
        """Achievements earned in a chapter's span, by grant id rather than clock time."""
        cur = await self.conn.execute(
            "SELECT g.id, g.achievement_id, g.rarity, c.name AS character_name "
            "FROM achievement_grants g JOIN characters c ON c.id = g.character_id "
            "WHERE g.campaign_id = ? AND g.id > ? AND g.id <= ? ORDER BY g.id",
            (campaign_id, after_id, through_id),
        )
        return [
            Grant(
                id=row["id"],
                achievement_id=row["achievement_id"],
                rarity=row["rarity"],
                character_name=row["character_name"],
            )
            for row in await cur.fetchall()
        ]

    # -- telegram asset cache ----------------------------------------------

    async def get_asset(self, key: str) -> str | None:
        cur = await self.conn.execute("SELECT file_id FROM bot_assets WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["file_id"] if row else None

    async def set_asset(self, key: str, file_id: str) -> None:
        await self.conn.execute(
            "INSERT INTO bot_assets (key, file_id) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET file_id = excluded.file_id",
            (key, file_id),
        )
        await self.conn.commit()

    # -- row mappers -------------------------------------------------------

    @staticmethod
    def _campaign(row: aiosqlite.Row) -> Campaign:
        return Campaign(
            id=row["id"],
            chat_id=row["chat_id"],
            genre=row["genre"],
            genre_skin=_json_load(row["genre_skin"], {}),
            tone=row["tone"],
            status=row["status"],
            summary=row["summary"],
            summary_through_id=row["summary_through_id"],
        )

    @staticmethod
    def _item(row: aiosqlite.Row) -> Item:
        return Item(
            id=row["id"],
            character_id=row["character_id"],
            name=row["name"],
            kind=row["kind"],
            description=row["description"],
            quantity=row["quantity"],
            equippable=bool(row["equippable"]),
            equipped=bool(row["equipped"]),
            consumable=bool(row["consumable"]),
            stat_mods=_json_load(row["stat_mods"], {}),
        )

    @staticmethod
    def _pending_roll(row: aiosqlite.Row) -> PendingRoll:
        return PendingRoll(
            id=row["id"],
            campaign_id=row["campaign_id"],
            character_id=row["character_id"],
            ability=row["ability"],
            dc=row["dc"],
            reason=row["reason"],
        )

    @staticmethod
    def _message(row: aiosqlite.Row) -> Message:
        return Message(
            id=row["id"],
            role=row["role"],
            character_id=row["character_id"],
            content=row["content"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _chapter(row: aiosqlite.Row) -> Chapter:
        return Chapter(
            id=row["id"],
            number=row["number"],
            title=row["title"],
            body=row["body"],
            through_message_id=row["through_message_id"],
            through_grant_id=row["through_grant_id"],
        )

    async def _character(self, row: aiosqlite.Row) -> Character:
        return Character(
            id=row["id"],
            campaign_id=row["campaign_id"],
            user_id=row["user_id"],
            user_display=row["user_display"],
            name=row["name"],
            archetype=row["archetype"],
            origin=row["origin"],
            concept=row["concept"],
            level=row["level"],
            xp=row["xp"],
            hp=row["hp"],
            max_hp=row["max_hp"],
            abilities=normalise_abilities(_json_load(row["abilities"], {})),
            status=row["status"],
            items=tuple(await self.list_items(row["id"])),
        )
