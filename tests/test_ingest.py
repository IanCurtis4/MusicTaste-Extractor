from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from music_taste.db import initialize
from music_taste.ingest import file_fingerprint, ingest_file

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def connection() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize(db)
    try:
        yield db
    finally:
        db.close()


def test_ingests_searches_from_url_and_decodes_unicode(
    connection: sqlite3.Connection,
) -> None:
    result = ingest_file(connection, FIXTURES / "takeout_search.html")

    assert (result.discovered, result.inserted, result.duplicates, result.errors) == (
        2,
        2,
        0,
        0,
    )
    rows = connection.execute(
        "SELECT * FROM activity_events ORDER BY source_ordinal"
    ).fetchall()
    assert rows[0]["event_type"] == "search"
    assert rows[0]["query_text"] == "Björk Jóga"
    assert rows[0]["title"] is None
    assert rows[0]["occurred_at_raw"] == "3 de set. de 2026, 11:21:19 BRT"
    assert rows[0]["occurred_at_utc"] == "2026-09-03T14:21:19+00:00"
    assert rows[0]["source_timezone"] == "BRT"
    assert rows[1]["query_text"] == "Liniker Zero"
    assert rows[1]["occurred_at_utc"] == "2025-01-02T08:00:00+00:00"


def test_ingests_watches_channels_repetitions_and_safe_errors(
    connection: sqlite3.Connection,
) -> None:
    result = ingest_file(connection, FIXTURES / "takeout_watch.html")

    assert result.discovered == 5
    assert result.inserted == 5
    assert result.errors == 2
    rows = connection.execute(
        "SELECT * FROM activity_events ORDER BY source_ordinal"
    ).fetchall()

    assert rows[0]["video_id"] == rows[1]["video_id"] == "repeat123"
    assert rows[0]["event_key"] != rows[1]["event_key"]
    assert rows[0]["title"] == "Canção repetida — Ao vivo"
    assert rows[0]["channel_name"] == "Banda Exemplo"
    assert rows[0]["channel_url"].endswith("/channel/UCmusic")

    assert rows[2]["video_id"] == "nochannel"
    assert rows[2]["channel_name"] is None
    assert rows[2]["occurred_at_utc"] == "2024-12-02T01:59:59+00:00"

    assert rows[3]["parse_status"] == "error"
    assert rows[3]["parse_error"] == "target_url_missing"
    assert rows[3]["title"] is None

    assert rows[4]["parse_status"] == "error"
    assert rows[4]["parse_error"] == "video_id_missing;unknown_timezone:XYZ"
    assert rows[4]["occurred_at_utc"] is None
    assert rows[4]["source_timezone"] == "XYZ"
    assert "Vídeo" not in rows[4]["parse_error"]


def test_same_file_is_idempotent_and_keeps_one_ingest_run(
    connection: sqlite3.Connection,
) -> None:
    path = FIXTURES / "takeout_search.html"
    first = ingest_file(connection, path)
    second = ingest_file(connection, path)

    assert first.inserted == 2
    assert second.inserted == 0
    assert second.duplicates == 2
    assert connection.execute("SELECT count(*) FROM ingest_runs").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM activity_events").fetchone()[0] == 2


def test_overlapping_takeouts_dedupe_occurrences_but_keep_real_repeats(
    connection: sqlite3.Connection,
) -> None:
    first = ingest_file(connection, FIXTURES / "takeout_watch.html")
    overlap = ingest_file(connection, FIXTURES / "takeout_watch_overlap.html")

    assert first.inserted == 5
    assert overlap.discovered == 3
    assert overlap.inserted == 1
    assert overlap.duplicates == 2
    assert connection.execute(
        "SELECT count(*) FROM activity_events WHERE video_id = 'repeat123'"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT count(*) FROM activity_events WHERE video_id = 'new456'"
    ).fetchone()[0] == 1


def test_malformed_html_is_recovered_and_explicit_kind_handles_missing_url(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    malformed = tmp_path / "opaque.html"
    malformed.write_text(
        '<div class="outer-cell"><div><b>conteúdo truncado<br>'
        "7 de fev. de 2022, 12:00:00 BRT",
        encoding="utf-8",
    )

    result = ingest_file(connection, malformed, source_kind="watch")

    assert result.discovered == 1
    assert result.inserted == 1
    assert result.errors == 1
    row = connection.execute("SELECT * FROM activity_events").fetchone()
    assert row["event_type"] == "watch"
    assert row["parse_error"] == "target_url_missing"
    assert row["occurred_at_utc"] == "2022-02-07T15:00:00+00:00"


def test_unclassifiable_file_requires_source_kind(
    connection: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "opaque.html"
    source.write_text('<div class="outer-cell">sem URL ou data</div>', encoding="utf-8")

    with pytest.raises(ValueError, match="source kind"):
        ingest_file(connection, source)

    assert connection.execute("SELECT count(*) FROM ingest_runs").fetchone()[0] == 0


def test_fingerprint_is_sha256(tmp_path: Path) -> None:
    source = tmp_path / "large-enough.html"
    payload = (b"abc123" * 400_000) + b"tail"
    source.write_bytes(payload)

    assert file_fingerprint(source, chunk_size=127) == hashlib.sha256(payload).hexdigest()
