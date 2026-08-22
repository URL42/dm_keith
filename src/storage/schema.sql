-- DM Keith schema. Applied idempotently on every boot by storage/db.py.
-- Everything is keyed on campaigns; a campaign belongs to exactly one Telegram chat.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS campaigns (
    id                 INTEGER PRIMARY KEY,
    chat_id            INTEGER NOT NULL,
    genre              TEXT    NOT NULL,
    genre_skin         TEXT    NOT NULL DEFAULT '{}',  -- JSON: ability display names + flavor
    tone               TEXT    NOT NULL DEFAULT 'pg13',
    status             TEXT    NOT NULL DEFAULT 'creating',  -- creating | active | ended
    summary            TEXT    NOT NULL DEFAULT '',
    summary_through_id INTEGER NOT NULL DEFAULT 0,    -- watermark into messages.id
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Only one live campaign per chat; ended ones are kept for history.
CREATE UNIQUE INDEX IF NOT EXISTS idx_campaigns_live_chat
    ON campaigns (chat_id) WHERE status != 'ended';

CREATE TABLE IF NOT EXISTS characters (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL,          -- Telegram user id
    user_display TEXT    NOT NULL DEFAULT '',
    name         TEXT    NOT NULL,
    archetype    TEXT    NOT NULL DEFAULT '',   -- genre-skinned "class"
    origin       TEXT    NOT NULL DEFAULT '',   -- genre-skinned "race/background"
    concept      TEXT    NOT NULL DEFAULT '',
    level        INTEGER NOT NULL DEFAULT 1,
    xp           INTEGER NOT NULL DEFAULT 0,
    hp           INTEGER NOT NULL DEFAULT 10,
    max_hp       INTEGER NOT NULL DEFAULT 10,
    abilities    TEXT    NOT NULL DEFAULT '{}',  -- JSON: canonical str/dex/con/int/wis/cha
    status       TEXT    NOT NULL DEFAULT 'active',  -- active | dying | dead | retired
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (campaign_id, user_id)   -- one character per player per campaign
);

CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL REFERENCES characters (id) ON DELETE CASCADE,
    -- NOCASE: the DM invents item names from free text, so "Health Potion" and
    -- "health potion" have to be the same item -- for stacking and for lookups.
    name         TEXT    NOT NULL COLLATE NOCASE,
    kind         TEXT    NOT NULL DEFAULT 'misc',
    description  TEXT    NOT NULL DEFAULT '',
    quantity     INTEGER NOT NULL DEFAULT 1,
    equippable   INTEGER NOT NULL DEFAULT 0,
    equipped     INTEGER NOT NULL DEFAULT 0,
    consumable   INTEGER NOT NULL DEFAULT 0,
    stat_mods    TEXT,                         -- JSON {"str": 1} or NULL
    UNIQUE (character_id, name)
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
    role         TEXT    NOT NULL,   -- player | dm | system
    character_id INTEGER REFERENCES characters (id) ON DELETE SET NULL,
    content      TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_campaign ON messages (campaign_id, id);

CREATE TABLE IF NOT EXISTS story_events (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL,   -- npc | quest | decision | location | lore
    summary      TEXT    NOT NULL,
    entities     TEXT    NOT NULL DEFAULT '[]',  -- JSON list of names
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_story_events_campaign ON story_events (campaign_id, id);

CREATE TABLE IF NOT EXISTS dice_rolls (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
    character_id INTEGER REFERENCES characters (id) ON DELETE SET NULL,
    expression   TEXT    NOT NULL,
    detail       TEXT    NOT NULL,
    total        INTEGER NOT NULL,
    source       TEXT    NOT NULL DEFAULT 'dm',   -- dm | player
    reason       TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS achievement_grants (
    id             INTEGER PRIMARY KEY,
    campaign_id    INTEGER NOT NULL REFERENCES campaigns (id) ON DELETE CASCADE,
    character_id   INTEGER NOT NULL REFERENCES characters (id) ON DELETE CASCADE,
    achievement_id TEXT    NOT NULL,
    rarity         TEXT    NOT NULL,
    awarded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_grants_character ON achievement_grants (character_id, achievement_id);

-- Telegram file_id cache so we upload each sound effect only once.
CREATE TABLE IF NOT EXISTS bot_assets (
    key     TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
);
