from __future__ import annotations

import sqlite3

import pytest

from music_taste.db import SCHEMA_VERSION, connect, initialize

EXPECTED_TABLES = {
    "schema_info",
    "ingest_runs",
    "activity_events",
    "video_metadata",
    "music_candidates",
    "event_candidate_links",
    "entity_matches",
    "review_decisions",
    "taxonomy_assignments",
    "external_links",
    "http_cache",
    "pipeline_runs",
}


def test_schema_is_complete_versioned_and_idempotent() -> None:
    connection = connect(":memory:")
    initialize(connection)
    initialize(connection)

    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert EXPECTED_TABLES <= tables
    assert [
        row[0] for row in connection.execute("SELECT version FROM schema_info")
    ] == [SCHEMA_VERSION]


def test_initialize_rejects_an_unknown_schema_version() -> None:
    connection = connect(":memory:")
    initialize(connection)
    connection.execute("UPDATE schema_info SET version = ?", (SCHEMA_VERSION + 1,))

    with pytest.raises(RuntimeError, match="Unsupported database schema"):
        initialize(connection)


def test_foreign_keys_are_enabled_and_cascade_link_rows() -> None:
    connection = connect(":memory:")
    initialize(connection)
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO event_candidate_links(event_id, candidate_id) VALUES (404, 405)"
        )

    event_id = connection.execute(
        """
        INSERT INTO activity_events(
            event_key, event_type, occurred_at_raw,
            source_file_fingerprint, source_ordinal
        ) VALUES ('event', 'watch', 'raw', 'fixture', 0)
        """
    ).lastrowid
    candidate_id = connection.execute(
        """
        INSERT INTO music_candidates(
            candidate_key, source_kind, normalized_title, evidence_json
        ) VALUES ('candidate', 'watch', 'Track', '{}')
        """
    ).lastrowid
    connection.execute(
        "INSERT INTO event_candidate_links(event_id, candidate_id) VALUES (?, ?)",
        (event_id, candidate_id),
    )
    connection.execute("DELETE FROM activity_events WHERE id = ?", (event_id,))

    assert connection.execute("SELECT count(*) FROM event_candidate_links").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM music_candidates").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("table", "column", "invalid"),
    [
        ("activity_events", "event_type", "browse"),
        ("video_metadata", "music_status", "maybe"),
        ("entity_matches", "status", "pending"),
        ("taxonomy_assignments", "taxonomy", "tempo"),
    ],
)
def test_schema_check_constraints_reject_invalid_enums(
    table: str, column: str, invalid: str
) -> None:
    connection = connect(":memory:")
    initialize(connection)

    statements = {
        "activity_events": """
            INSERT INTO activity_events(
                event_key,event_type,occurred_at_raw,source_file_fingerprint,source_ordinal
            ) VALUES ('e', ?, 'raw', 'fixture', 0)
        """,
        "video_metadata": """
            INSERT INTO video_metadata(
                video_id,availability,music_status
            ) VALUES ('v', 'available', ?)
        """,
        "entity_matches": """
            INSERT INTO entity_matches(
                candidate_id,score,status,method,provider
            ) VALUES (1, 0, ?, 'test', 'test')
        """,
        "taxonomy_assignments": """
            INSERT INTO taxonomy_assignments(
                provider,taxonomy,value,value_norm,entity_type,entity_id,
                source_level,inherited
            ) VALUES ('test', ?, 'x', 'x', 'recording', 'id', 'track', 0)
        """,
    }
    if table == "entity_matches":
        connection.execute(
            """
            INSERT INTO music_candidates(
                candidate_key,source_kind,normalized_title,evidence_json
            ) VALUES ('c','watch','Track','{}')
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(statements[table], (invalid,))
