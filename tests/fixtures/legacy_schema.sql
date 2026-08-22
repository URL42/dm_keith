-- Dungeon Master Keith SQLite schema
-- This schema focuses on user profiles, session state, and achievement logging.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'narrator',
    profanity_level INTEGER NOT NULL DEFAULT 3,
    rating TEXT NOT NULL DEFAULT 'PG-13',
    tangents_level INTEGER NOT NULL DEFAULT 1,
    achievement_density TEXT NOT NULL DEFAULT 'normal',
    story_mode_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS achievement_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    achievement_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT,
    rarity TEXT NOT NULL,
    awarded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    detail JSON,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_achievement_grants_user
    ON achievement_grants (achievement_id, user_id, awarded_at DESC);

CREATE INDEX IF NOT EXISTS idx_achievement_grants_session
    ON achievement_grants (achievement_id, session_id, awarded_at DESC);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    session_id TEXT,
    event_type TEXT NOT NULL,
    payload JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_log_session
    ON event_log (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS story_profiles (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    character_name TEXT,
    pronouns TEXT,
    race TEXT,
    character_class TEXT,
    backstory TEXT,
    level INTEGER NOT NULL DEFAULT 1,
    experience INTEGER NOT NULL DEFAULT 0,
    ability_scores JSON,
    inventory JSON,
    metadata JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS story_state (
    session_id TEXT PRIMARY KEY,
    current_scene TEXT,
    scene_history JSON,
    flags JSON,
    stats JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS story_rolls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    expression TEXT NOT NULL,
    result_total INTEGER NOT NULL,
    result_detail JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions (id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_story_rolls_session
    ON story_rolls (session_id, created_at DESC);
