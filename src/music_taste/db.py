from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('search', 'watch')),
    file_size INTEGER NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    parse_error_count INTEGER NOT NULL DEFAULT 0,
    parser_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL CHECK (event_type IN ('search', 'watch')),
    occurred_at_raw TEXT NOT NULL,
    occurred_at_utc TEXT,
    source_timezone TEXT,
    target_url TEXT,
    video_id TEXT,
    query_text TEXT,
    title TEXT,
    channel_name TEXT,
    channel_url TEXT,
    source_file_fingerprint TEXT NOT NULL,
    source_ordinal INTEGER NOT NULL,
    parse_status TEXT NOT NULL DEFAULT 'ok',
    parse_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON activity_events(event_type, occurred_at_utc);
CREATE INDEX IF NOT EXISTS idx_events_video ON activity_events(video_id);

CREATE TABLE IF NOT EXISTS video_metadata (
    video_id TEXT PRIMARY KEY,
    category_id TEXT,
    title TEXT,
    channel_id TEXT,
    channel_title TEXT,
    duration_seconds INTEGER,
    topic_categories_json TEXT NOT NULL DEFAULT '[]',
    availability TEXT NOT NULL,
    music_status TEXT NOT NULL CHECK (music_status IN ('music', 'non_music', 'unknown')),
    music_score REAL NOT NULL DEFAULT 0,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    etag TEXT,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    error TEXT
);

CREATE TABLE IF NOT EXISTS music_candidates (
    id INTEGER PRIMARY KEY,
    candidate_key TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('search', 'watch')),
    video_id TEXT,
    query_text TEXT,
    normalized_title TEXT NOT NULL,
    normalized_artist TEXT,
    duration_seconds INTEGER,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS event_candidate_links (
    event_id INTEGER NOT NULL REFERENCES activity_events(id) ON DELETE CASCADE,
    candidate_id INTEGER NOT NULL REFERENCES music_candidates(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS entity_matches (
    candidate_id INTEGER PRIMARY KEY REFERENCES music_candidates(id) ON DELETE CASCADE,
    recording_mbid TEXT,
    recording_title TEXT,
    artist_mbid TEXT,
    artist_name TEXT,
    artist_type TEXT,
    release_group_mbid TEXT,
    release_group_title TEXT,
    score REAL NOT NULL DEFAULT 0,
    runner_up_score REAL,
    margin REAL,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'review', 'rejected')),
    method TEXT NOT NULL,
    provider TEXT NOT NULL,
    reviewed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_matches_artist ON entity_matches(artist_mbid);
CREATE INDEX IF NOT EXISTS idx_matches_recording ON entity_matches(recording_mbid);

CREATE TABLE IF NOT EXISTS review_decisions (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES music_candidates(id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject', 'replace')),
    recording_mbid TEXT,
    artist_mbid TEXT,
    notes TEXT,
    decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS taxonomy_assignments (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    taxonomy TEXT NOT NULL CHECK (taxonomy IN ('genre', 'style', 'mood', 'theme', 'tag')),
    value TEXT NOT NULL,
    value_norm TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    source_level TEXT NOT NULL CHECK (source_level IN ('track', 'album', 'artist')),
    inherited INTEGER NOT NULL CHECK (inherited IN (0, 1)),
    source_url TEXT,
    confidence REAL NOT NULL DEFAULT 1,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider, taxonomy, value_norm, entity_type, entity_id, source_level)
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_entity ON taxonomy_assignments(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS external_links (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    url TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(entity_type, entity_id, provider, url)
);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    status_code INTEGER,
    response_json TEXT,
    etag TEXT,
    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY,
    stage TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    status TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL DEFAULT '{}'
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    row = connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
    if row is None:
        connection.execute("INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] != SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported database schema {row['version']}; expected {SCHEMA_VERSION}."
        )
    connection.commit()


@contextmanager
def database(path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect(path)
    try:
        initialize(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
