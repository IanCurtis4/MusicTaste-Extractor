from __future__ import annotations

import csv
from pathlib import Path

import pytest

from music_taste.db import connect, initialize
from music_taste.reporting import CSV_COLUMNS, generate_report

RECORDING_A = "11111111-1111-4111-8111-111111111111"
RECORDING_B = "22222222-2222-4222-8222-222222222222"
ARTIST_GROUP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ARTIST_PERSON = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ALBUM_A = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _fixture_db():
    db = connect(":memory:")
    initialize(db)
    events = [
        ("w1", "watch", "2026-01-02T12:00:00+00:00", "v1", None),
        ("w2", "watch", "2026-01-03T12:00:00+00:00", "v1", None),
        ("w3", "watch", "2026-02-01T12:00:00+00:00", "v2", None),
        ("w4", "watch", "2026-02-02T12:00:00+00:00", "v3", None),
        ("s1", "search", "2026-01-04T12:00:00+00:00", None, "Band A - Track A"),
        ("s2", "search", "2026-02-04T12:00:00+00:00", None, "unrelated query"),
    ]
    for ordinal, (key, kind, occurred, video, query) in enumerate(events):
        db.execute(
            """INSERT INTO activity_events(
                   event_key,event_type,occurred_at_raw,occurred_at_utc,video_id,query_text,
                   source_file_fingerprint,source_ordinal
               ) VALUES (?, ?, ?, ?, ?, ?, 'fixture', ?)""",
            (key, kind, occurred, occurred, video, query, ordinal),
        )

    candidates = [
        ("watch-a", "watch", "Track A", "Band A"),
        ("watch-b", "watch", "Track B", "Solo B"),
        ("watch-unresolved", "watch", "Unknown", None),
        ("search-a", "search", "Track A", "Band A"),
    ]
    ids = {}
    for key, kind, title, artist in candidates:
        cursor = db.execute(
            """INSERT INTO music_candidates(
                   candidate_key,source_kind,normalized_title,normalized_artist,evidence_json
               ) VALUES (?, ?, ?, ?, '{}')""",
            (key, kind, title, artist),
        )
        ids[key] = cursor.lastrowid

    event_ids = {row["event_key"]: row["id"] for row in db.execute("SELECT id,event_key FROM activity_events")}
    for event_key, candidate_key in (
        ("w1", "watch-a"), ("w2", "watch-a"), ("w3", "watch-b"),
        ("w4", "watch-unresolved"), ("s1", "search-a"),
    ):
        db.execute(
            "INSERT INTO event_candidate_links(event_id,candidate_id) VALUES (?,?)",
            (event_ids[event_key], ids[candidate_key]),
        )

    matches = [
        ("watch-a", RECORDING_A, "Track A", ARTIST_GROUP, "Band A", "gRoUp", ALBUM_A, "Album A", .98, "accepted"),
        ("watch-b", RECORDING_B, "Track B", ARTIST_PERSON, "Solo B", "Person", None, None, .95, "accepted"),
        ("watch-unresolved", None, None, None, None, None, None, None, .50, "review"),
        ("search-a", RECORDING_A, "Track A", ARTIST_GROUP, "Band A", "GROUP", ALBUM_A, "Album A", .97, "accepted"),
    ]
    for key, recording, track, artist_mbid, artist, artist_type, album, album_title, score, status in matches:
        db.execute(
            """INSERT INTO entity_matches(
                   candidate_id,recording_mbid,recording_title,artist_mbid,artist_name,
                   artist_type,release_group_mbid,release_group_title,score,status,method,provider
               ) VALUES (?,?,?,?,?,?,?,?,?,?,'fixture','musicbrainz')""",
            (ids[key], recording, track, artist_mbid, artist, artist_type, album, album_title, score, status),
        )

    taxonomy = [
        # Two track-level genres: each Track A watch contributes 1/2 to each.
        ("musicbrainz", "genre", "Rock", "rock", "recording", RECORDING_A, "track", 0, .9),
        ("musicbrainz", "genre", "Pop", "pop", "recording", RECORDING_A, "track", 0, .8),
        # This less-specific genre must be ignored because track genres exist.
        ("musicbrainz", "genre", "Alternative", "alternative", "track", RECORDING_A, "album", 1, .7),
        # No track-level mood: artist is selected and marked inherited.
        ("allmusic", "mood", "Energetic", "energetic", "track", RECORDING_A, "artist", 1, .6),
        ("musicbrainz", "style", "Singer-Songwriter", "singer songwriter", "artist", ARTIST_PERSON, "artist", 1, .75),
    ]
    for row in taxonomy:
        db.execute(
            """INSERT INTO taxonomy_assignments(
                   provider,taxonomy,value,value_norm,entity_type,entity_id,
                   source_level,inherited,confidence
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            row,
        )
    db.commit()
    return db


def _read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def test_generate_report_reconciles_and_separates_search_from_watch(tmp_path):
    db = _fixture_db()
    summary = generate_report(db, tmp_path)
    assert summary["total_events"] == 6
    assert summary["watch_events"] == 4
    assert summary["search_events"] == 2
    assert summary["accepted_watch_events"] == 3
    assert summary["excluded_watch_events"] == 1
    assert summary["csv_files"] == len(CSV_COLUMNS) == 9

    tracks = _read_csv(tmp_path / "top_tracks.csv")
    assert sum(int(row["watch_events"]) for row in tracks) == 3
    assert tracks[0]["track"] == "Track A"
    assert tracks[0]["watch_events"] == "2"  # accepted search is not a play

    timeline = _read_csv(tmp_path / "timeline_monthly.csv")
    assert sum(int(row["watch_events"]) for row in timeline) == 4
    assert sum(int(row["accepted_music_watch_events"]) for row in timeline) == 3
    assert sum(int(row["excluded_watch_events"]) for row in timeline) == 1
    assert sum(int(row["search_events"]) for row in timeline) == 2

    searches = _read_csv(tmp_path / "search_interest.csv")
    assert sum(int(row["search_events"]) for row in searches) == 2
    accepted_search = next(row for row in searches if row["accepted_music_match"] == "1")
    assert accepted_search["track"] == "Track A"


def test_taxonomy_precedence_weight_and_group_filter(tmp_path):
    generate_report(_fixture_db(), tmp_path)
    taxonomy = _read_csv(tmp_path / "taxonomy_distribution.csv")
    genres = [row for row in taxonomy if row["taxonomy"] == "genre"]
    assert {row["value"] for row in genres} == {"Rock", "Pop"}
    assert sum(float(row["weighted_watch_events"]) for row in genres) == pytest.approx(2)
    assert {float(row["weighted_watch_events"]) for row in genres} == {1.0}
    mood = next(row for row in taxonomy if row["taxonomy"] == "mood")
    assert mood["source_level"] == "artist"
    assert mood["inherited"] == "1"

    groups = _read_csv(tmp_path / "top_groups.csv")
    assert len(groups) == 1
    assert groups[0]["group"] == "Band A"
    assert groups[0]["watch_events"] == "2"


def test_outputs_use_bom_features_avoid_duration_claim_and_html_is_self_contained(tmp_path):
    generate_report(_fixture_db(), tmp_path)
    for filename, columns in CSV_COLUMNS.items():
        raw = (tmp_path / filename).read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")
        with (tmp_path / filename).open(encoding="utf-8-sig", newline="") as stream:
            assert tuple(next(csv.reader(stream))) == columns

    feature_header = CSV_COLUMNS["analysis_features.csv"]
    assert not any("duration" in column or "complete" in column for column in feature_header)
    features = _read_csv(tmp_path / "analysis_features.csv")
    track_a = next(row for row in features if row["recording_mbid"] == RECORDING_A)
    assert track_a["watch_event_count"] == "2"
    assert track_a["search_event_count"] == "1"
    assert track_a["genre_tags"] == "Pop|Rock"

    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "Análise do gosto musical" in html
    assert "plotly.js" in html.casefold()
    assert "<script src=" not in html.casefold()
    assert '<link rel="stylesheet" href="http' not in html.casefold()
    assert "reprodução completa" in html
