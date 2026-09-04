from __future__ import annotations

import sqlite3

from music_taste.db import initialize
from music_taste.models import EntityLevel, TaxonomyAssignment, TaxonomyKind
from music_taste.taxonomy import (
    enrich_musicbrainz_taxonomies,
    musicbrainz_assignments_from_entity,
    normalize_taxonomy_value,
    persist_assignments,
    select_most_specific,
)


def memory_database() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize(connection)
    return connection


def assignment(value: str, level: EntityLevel, kind: TaxonomyKind) -> TaxonomyAssignment:
    return TaxonomyAssignment(
        provider="fixture",
        taxonomy=kind,
        value=value,
        entity_type="track",
        entity_id="recording-id",
        source_level=level,
        inherited=False,
        source_url="https://example.invalid/source",
    )


def test_normalization_and_persistence_are_idempotent() -> None:
    connection = memory_database()
    first = assignment("  Dream   Pop  ", EntityLevel.TRACK, TaxonomyKind.GENRE)
    second = assignment("dream pop", EntityLevel.TRACK, TaxonomyKind.GENRE)

    assert normalize_taxonomy_value("Ｄｒｅａｍ   POP") == "dream pop"
    assert persist_assignments(connection, [first]) == 1
    assert persist_assignments(connection, [second]) == 0
    row = connection.execute("SELECT * FROM taxonomy_assignments").fetchone()
    assert row["value_norm"] == "dream pop"
    assert row["value"] == "dream pop"
    assert connection.execute("SELECT COUNT(*) FROM taxonomy_assignments").fetchone()[0] == 1


def test_specificity_is_per_taxonomy_and_sets_inheritance() -> None:
    selected = select_most_specific(
        [
            assignment("Rock", EntityLevel.ARTIST, TaxonomyKind.GENRE),
            assignment("Alternative Rock", EntityLevel.ALBUM, TaxonomyKind.GENRE),
            assignment("Energetic", EntityLevel.ARTIST, TaxonomyKind.MOOD),
        ],
        target_level=EntityLevel.TRACK,
    )
    assert [(item.taxonomy, item.value, item.inherited) for item in selected] == [
        (TaxonomyKind.GENRE, "Alternative Rock", True),
        (TaxonomyKind.MOOD, "Energetic", True),
    ]
    assert select_most_specific(
        [assignment("Invalid child", EntityLevel.TRACK, TaxonomyKind.TAG)],
        target_level=EntityLevel.ARTIST,
    ) == []


def test_musicbrainz_helper_uses_existing_response_without_io() -> None:
    assignments = musicbrainz_assignments_from_entity(
        {
            "id": "artist-id",
            "genres": [{"name": "Art Rock", "count": 4}],
            "tags": [{"name": "Progressive"}, {"name": "progressive"}],
        },
        entity_type="track",
        entity_id="recording-id",
        source_level="artist",
        target_level="track",
    )
    assert [(item.taxonomy.value, item.value) for item in assignments] == [
        ("genre", "Art Rock"),
        ("tag", "Progressive"),
    ]
    assert all(item.provider == "musicbrainz" for item in assignments)
    assert all(item.inherited for item in assignments)


def test_musicbrainz_enrichment_uses_hierarchy_cache_rate_limit_and_relations() -> None:
    import httpx

    connection = memory_database()
    connection.execute(
        """INSERT INTO music_candidates
           (id, candidate_key, source_kind, normalized_title)
           VALUES (1, 'candidate', 'watch', 'Track')"""
    )
    connection.execute(
        """INSERT INTO entity_matches (
             candidate_id, recording_mbid, artist_mbid, release_group_mbid,
             score, status, method, provider
           ) VALUES (1, 'recording-id', 'artist-id', 'album-id',
                     1, 'accepted', 'fixture', 'musicbrainz')"""
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert "me@example.com" in request.headers["User-Agent"]
        assert request.url.params["inc"] == "tags+url-rels"
        if "/recording/" in request.url.path:
            return httpx.Response(200, json={"tags": [{"name": "Track Tag"}]})
        if "/release-group/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "genres": [{"name": "Album Genre"}],
                    "relations": [{
                        "type": "allmusic",
                        "url": {"resource": "https://www.allmusic.com/album/fixture"},
                    }],
                },
            )
        return httpx.Response(200, json={"tags": [{"name": "Artist Tag"}]})

    sleeps: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = enrich_musicbrainz_taxonomies(
            connection,
            contact="me@example.com",
            client=client,
            sleep=sleeps.append,
        )
        second = enrich_musicbrainz_taxonomies(
            connection,
            contact="me@example.com",
            client=client,
            sleep=sleeps.append,
        )

    assert first["requests"] == 3
    assert second["requests"] == 0
    assert len(calls) == 3
    assert sleeps == [1.0, 1.0]
    rows = connection.execute(
        "SELECT taxonomy, value, source_level, inherited FROM taxonomy_assignments ORDER BY taxonomy"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {"taxonomy": "genre", "value": "Album Genre", "source_level": "album", "inherited": 1},
        {"taxonomy": "tag", "value": "Track Tag", "source_level": "track", "inherited": 0},
    ]
    link = connection.execute("SELECT * FROM external_links").fetchone()
    assert link["entity_type"] == "album"
    assert link["entity_id"] == "album-id"
