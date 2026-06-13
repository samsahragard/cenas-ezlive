-- ============================================================================
-- Sports Tracker — database schema
-- Target: SQLite 3.24+ (Mini_IT13 runtime) and Postgres 9.5+ (Render).
-- All timestamps are stored as ISO-8601 UTC strings (e.g. 2026-06-13T19:08:00Z).
-- Display conversion to Houston/Central time happens in the app layer
-- (sports_core.py), never in the database, so there is one source of truth.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- games
-- One row per real-world game. Duplicates are impossible because of the
-- UNIQUE(provider, provider_game_id) constraint: every sync upserts against it,
-- so a game that was "already shown in the app" is updated in place, never
-- inserted twice. content_hash lets the sync skip writes when nothing changed.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sports_games (
    id                INTEGER PRIMARY KEY,          -- Postgres: BIGSERIAL PRIMARY KEY
    provider          TEXT NOT NULL,                -- 'espn' | 'sportradar' | 'fixture'
    provider_game_id  TEXT NOT NULL,                -- stable external id (dedup anchor)

    sport             TEXT NOT NULL,                -- soccer|baseball|basketball|hockey|football
    league            TEXT NOT NULL,                -- 'MLB' | 'NBA' | 'FIFA World Cup' ...
    season            TEXT,

    status            TEXT NOT NULL,                -- normalized: see sports_core.STATUS_*
    status_detail     TEXT,                         -- "Top 5th", "78'", "Q3 4:21", "Final/OT"

    start_utc         TEXT NOT NULL,                -- canonical UTC ISO-8601

    home_team         TEXT NOT NULL,
    home_abbr         TEXT,
    home_score        INTEGER,
    home_color        TEXT,                          -- hex, for the UI monogram
    away_team         TEXT NOT NULL,
    away_abbr         TEXT,
    away_score        INTEGER,
    away_color        TEXT,

    venue             TEXT,
    venue_city        TEXT,

    tv_national       TEXT,                          -- denormalized for fast search/list
    broadcast_json    TEXT,                          -- full resolved broadcast payload (JSON)

    content_hash      TEXT NOT NULL,                 -- hash of mutable fields
    first_seen_utc    TEXT NOT NULL,
    last_updated_utc  TEXT NOT NULL,

    UNIQUE (provider, provider_game_id)
);

CREATE INDEX IF NOT EXISTS idx_games_start    ON sports_games (start_utc);
CREATE INDEX IF NOT EXISTS idx_games_status   ON sports_games (status);
CREATE INDEX IF NOT EXISTS idx_games_league   ON sports_games (league);
CREATE INDEX IF NOT EXISTS idx_games_sport    ON sports_games (sport);

-- ----------------------------------------------------------------------------
-- favorites
-- A favorite can pin a single game, a team, or a whole league. The Favorites
-- tab unions all three against the games table.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sports_favorites (
    id            INTEGER PRIMARY KEY,               -- Postgres: BIGSERIAL PRIMARY KEY
    user_id       TEXT NOT NULL,                     -- manager / staff id
    scope         TEXT NOT NULL,                     -- 'game' | 'team' | 'league'
    ref           TEXT NOT NULL,                     -- game provider_id, team name/abbr, or league
    created_utc   TEXT NOT NULL,
    UNIQUE (user_id, scope, ref)
);

CREATE INDEX IF NOT EXISTS idx_fav_user ON sports_favorites (user_id);

-- ----------------------------------------------------------------------------
-- sync_log
-- Observability for each fetch run (mirrors the reliability/auto-recovery
-- pattern already used elsewhere in the platform).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sports_sync_log (
    id              INTEGER PRIMARY KEY,             -- Postgres: BIGSERIAL PRIMARY KEY
    provider        TEXT NOT NULL,
    league          TEXT,
    run_utc         TEXT NOT NULL,
    games_seen      INTEGER NOT NULL DEFAULT 0,
    games_inserted  INTEGER NOT NULL DEFAULT 0,
    games_updated   INTEGER NOT NULL DEFAULT 0,
    games_skipped   INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,                   -- 'ok' | 'error'
    message         TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_run ON sports_sync_log (run_utc);
