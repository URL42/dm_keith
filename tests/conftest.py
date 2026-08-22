"""Shared fixtures: a throwaway database per test, plus a seeded campaign."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from src.storage.db import connect
from src.storage.repo import Campaign, Character, Repo


@pytest_asyncio.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    conn = await connect(tmp_path / "test.sqlite3")
    try:
        yield Repo(conn)
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def campaign(repo: Repo) -> Campaign:
    return await repo.create_campaign(chat_id=-100123, genre="fantasy")


@pytest_asyncio.fixture
async def hero(repo: Repo, campaign: Campaign) -> Character:
    return await repo.create_character(
        campaign.id,
        user_id=42,
        name="Thorn",
        user_display="@hank",
        archetype="Ranger",
        origin="Woodsfolk",
        abilities={"str": 14, "dex": 15, "con": 13, "int": 10, "wis": 12, "cha": 8},
    )
