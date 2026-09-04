from __future__ import annotations

import csv
import socket
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from music_taste.db import database
from music_taste.ingest import ingest_file
from music_taste.models import MatchCandidate
from music_taste.musicbrainz import resolve_musicbrainz
from music_taste.normalize import build_candidates
from music_taste.reporting import CSV_COLUMNS, generate_report
from music_taste.review import export_review, import_review
from music_taste.youtube import enrich_youtube

FIXTURES = Path(__file__).parent / "fixtures"
RECORDING_REVIEW = "11111111-1111-4111-8111-111111111111"
RECORDING_ACCEPTED = "22222222-2222-4222-8222-222222222222"
ARTIST_GROUP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ARTIST_PERSON = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _youtube_item(video_id: str) -> dict[str, object]:
    if video_id == "repeat123":
        title, channel, duration = (
            "Artist One - Track One (Official Audio)",
            "Artist One - Topic",
            "PT3M20S",
        )
    else:
        title, channel, duration = "Solo Two - Track Two", "Solo Two", "PT4M"
    return {
        "id": video_id,
        "snippet": {
            "categoryId": "10",
            "title": title,
            "channelId": f"channel-{video_id}",
            "channelTitle": channel,
        },
        "contentDetails": {"duration": duration},
        "topicDetails": {"topicCategories": ["https://example.invalid/wiki/Music"]},
    }


class _FakeResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def resolve(self, candidate, *, refresh: bool = False):
        self.calls.append(candidate.candidate_key)
        if candidate.video_id == "repeat123":
            return [
                MatchCandidate(
                    recording_mbid=RECORDING_REVIEW,
                    recording_title="Track One",
                    artist_mbid=ARTIST_GROUP,
                    artist_name="Artist One",
                    artist_type="Group",
                    release_group_mbid=None,
                    release_group_title=None,
                    score=0.94,
                    provider_score=1.0,
                ),
                MatchCandidate(
                    recording_mbid="33333333-3333-4333-8333-333333333333",
                    recording_title="Track One (alternate)",
                    artist_mbid=ARTIST_GROUP,
                    artist_name="Artist One",
                    artist_type="Group",
                    release_group_mbid=None,
                    release_group_title=None,
                    score=0.91,
                    provider_score=0.98,
                ),
            ]
        return [
            MatchCandidate(
                recording_mbid=RECORDING_ACCEPTED,
                recording_title="Track Two",
                artist_mbid=ARTIST_PERSON,
                artist_name="Solo Two",
                artist_type="Person",
                release_group_mbid=None,
                release_group_title=None,
                score=0.98,
                provider_score=1.0,
            )
        ]


def _accept_exported_review(path: Path) -> None:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        fields = reader.fieldnames
    assert fields is not None
    assert len(rows) == 1
    rows[0]["decision"] = "accept"
    rows[0]["notes"] = "auditoria sintética"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_offline_pipeline_is_resumable_private_and_reconciled(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def network_forbidden(*args, **kwargs):
        raise AssertionError("the integration suite attempted real network access")

    monkeypatch.setattr(socket, "create_connection", network_forbidden)
    api_key = "API-KEY-MUST-NEVER-BE-STORED-OR-PRINTED"
    database_path = tmp_path / "music.sqlite"
    output = tmp_path / "report"
    requests: list[list[str]] = []

    def youtube_handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        assert params["key"] == [api_key]
        ids = params["id"][0].split(",")
        requests.append(ids)
        return httpx.Response(
            200,
            json={"items": [_youtube_item(video_id) for video_id in ids]},
            request=request,
        )

    resolver = _FakeResolver()
    with database(database_path) as connection:
        search = ingest_file(connection, FIXTURES / "takeout_search.html")
        watch = ingest_file(connection, FIXTURES / "takeout_watch.html")
        assert (search.inserted, watch.inserted) == (2, 5)

        with httpx.Client(transport=httpx.MockTransport(youtube_handler)) as client:
            metadata = enrich_youtube(
                connection,
                api_key=api_key,
                client=client,
                sleep=lambda _: None,
            )
        assert metadata.processed == 2
        assert requests == [["nochannel", "repeat123"]]

        candidates = build_candidates(connection)
        assert candidates["created"] == 2
        resolution = resolve_musicbrainz(
            connection,
            contact="offline@example.invalid",
            client=resolver,
        )
        assert resolution == {"processed": 2, "accepted": 1, "review": 1, "errors": 0}

        review_path = tmp_path / "review.csv"
        assert export_review(connection, review_path) == 1
        _accept_exported_review(review_path)
        assert import_review(connection, review_path)["accepted"] == 1

        connection.execute(
            """
            INSERT INTO taxonomy_assignments(
                provider,taxonomy,value,value_norm,entity_type,entity_id,
                source_level,inherited,confidence
            ) VALUES
              ('fixture','genre','Rock','rock','track',?,'track',0,.9),
              ('fixture','genre','Electronic','electronic','track',?,'track',0,.8),
              ('fixture','mood','Atmospheric','atmospheric','artist',?,'artist',1,.7)
            """,
            (RECORDING_REVIEW, RECORDING_REVIEW, ARTIST_GROUP),
        )
        connection.commit()

        first_report = generate_report(connection, output)
        assert first_report["total_events"] == 7
        assert first_report["watch_events"] == 5
        assert first_report["search_events"] == 2
        assert first_report["accepted_watch_events"] == 3
        assert first_report["excluded_watch_events"] == 2

        # All stages are safe to resume. No provider is called for already stored work.
        assert ingest_file(
            connection, FIXTURES / "takeout_search.html"
        ).inserted == 0
        assert ingest_file(
            connection, FIXTURES / "takeout_watch.html"
        ).inserted == 0
        with httpx.Client(transport=httpx.MockTransport(youtube_handler)) as client:
            resumed_metadata = enrich_youtube(
                connection, api_key=api_key, client=client, sleep=lambda _: None
            )
        assert resumed_metadata.processed == 0
        assert build_candidates(connection)["created"] == 0
        assert resolve_musicbrainz(
            connection,
            contact="offline@example.invalid",
            client=resolver,
        )["processed"] == 0
        second_report = generate_report(connection, output)
        assert second_report == first_report

        assert connection.execute("SELECT count(*) FROM activity_events").fetchone()[0] == 7
        assert connection.execute("SELECT count(*) FROM music_candidates").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM entity_matches").fetchone()[0] == 2
        database_dump = "\n".join(connection.iterdump())
        assert api_key not in database_dump

    assert len(resolver.calls) == 2
    assert len(requests) == 1
    captured = capsys.readouterr()
    assert api_key not in captured.out + captured.err
    assert captured.out == captured.err == ""

    for filename in (*CSV_COLUMNS, "report.html"):
        assert (output / filename).is_file()
    with (output / "coverage_quality.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        coverage = {row["metric"]: int(row["value"]) for row in csv.DictReader(stream)}
    assert coverage["total_events"] == 7
    assert coverage["watch_events"] == 5
    assert coverage["search_events"] == 2
    assert coverage["accepted_watch_events"] == 3
    assert coverage["excluded_watch_events"] == 2

    with (output / "taxonomy_distribution.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        taxonomy = list(csv.DictReader(stream))
    genres = [row for row in taxonomy if row["taxonomy"] == "genre"]
    assert {row["value"] for row in genres} == {"Rock", "Electronic"}
    # Two repeated watches, split equally across two genre tags.
    assert sum(float(row["weighted_watch_events"]) for row in genres) == 2.0

    features = (output / "analysis_features.csv").read_text(encoding="utf-8-sig")
    assert RECORDING_REVIEW in features
    assert RECORDING_ACCEPTED in features
    html = (output / "report.html").read_text(encoding="utf-8")
    assert "Pesquisas (interesse)" in html
    assert "reprodução completa" in html
