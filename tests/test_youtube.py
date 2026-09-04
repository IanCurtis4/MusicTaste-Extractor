from __future__ import annotations

import sqlite3
from urllib.parse import parse_qs

import httpx
import pytest

from music_taste.db import initialize
from music_taste.models import MusicStatus
from music_taste.youtube import (
    YouTubeAPIError,
    classify_music,
    enrich_youtube,
    iso8601_duration_seconds,
)


def database_with_videos(count: int) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize(connection)
    for index in range(count):
        connection.execute(
            """INSERT INTO activity_events (
                event_key, event_type, occurred_at_raw, video_id,
                source_file_fingerprint, source_ordinal
            ) VALUES (?, 'watch', 'raw', ?, 'fixture', ?)""",
            (f"event-{index}", f"video-{index:03}", index),
        )
    connection.commit()
    return connection


def item(video_id: str, *, category: str = "10", duration: str = "PT3M12S") -> dict:
    return {
        "id": video_id,
        "etag": f"etag-{video_id}",
        "snippet": {
            "categoryId": category,
            "title": "Artist - Track (Official Audio)",
            "channelId": "channel-id",
            "channelTitle": "Artist - Topic",
        },
        "contentDetails": {"duration": duration},
        "topicDetails": {"topicCategories": ["https://en.wikipedia.org/wiki/Music"]},
    }


def test_duration_parser() -> None:
    assert iso8601_duration_seconds("PT3M12S") == 192
    assert iso8601_duration_seconds("PT1H2M3S") == 3723
    assert iso8601_duration_seconds("P1DT2S") == 86402
    assert iso8601_duration_seconds(None) is None
    assert iso8601_duration_seconds("not-a-duration") is None


def test_classification_is_auditable() -> None:
    status, score, reasons = classify_music(
        category_id="10", title="Anything", channel_title="Channel"
    )
    assert status is MusicStatus.MUSIC
    assert score == 1.0
    assert "category:music" in reasons

    status, score, reasons = classify_music(
        category_id=None,
        title="Artist - Song (Lyrics)",
        channel_title="ArtistVEVO",
        topic_categories=(),
    )
    assert status is MusicStatus.MUSIC
    assert {"title:lyrics", "channel:vevo"} <= set(reasons)

    status, score, reasons = classify_music(
        category_id="22", title="Phone review and unboxing", channel_title="Tech"
    )
    assert status is MusicStatus.NON_MUSIC
    assert score <= 0.3
    assert any(reason.startswith("category:non_music") for reason in reasons)

    status, score, reasons = classify_music(
        category_id=None, title="An ambiguous title", channel_title="Someone"
    )
    assert status is MusicStatus.UNKNOWN
    assert reasons == ("insufficient_evidence",)


def test_enrichment_batches_at_50_and_persists_metadata() -> None:
    connection = database_with_videos(51)
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(request.url.query.decode())
        ids = params["id"][0].split(",")
        requests.append(ids)
        assert params["part"] == ["snippet,contentDetails,topicDetails"]
        assert params["key"] == ["secret-key"]
        return httpx.Response(200, json={"items": [item(video_id) for video_id in ids]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = enrich_youtube(connection, api_key="secret-key", client=client)

    assert [len(batch) for batch in requests] == [50, 1]
    assert summary.processed == 51
    assert summary.available == 51
    assert summary.requests == 2
    row = connection.execute(
        "SELECT duration_seconds, music_status, reasons_json FROM video_metadata LIMIT 1"
    ).fetchone()
    assert row["duration_seconds"] == 192
    assert row["music_status"] == "music"
    assert "category:music" in row["reasons_json"]


def test_missing_item_is_stored_as_unavailable_unknown() -> None:
    connection = database_with_videos(2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [item("video-000")]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = enrich_youtube(connection, api_key="key", client=client)

    row = connection.execute(
        "SELECT availability, music_status, reasons_json FROM video_metadata WHERE video_id=?",
        ("video-001",),
    ).fetchone()
    assert summary.unavailable == 1
    assert dict(row) == {
        "availability": "unavailable",
        "music_status": "unknown",
        "reasons_json": '["api:item_absent"]',
    }


def test_resume_skips_cached_ids_and_refresh_reloads() -> None:
    connection = database_with_videos(2)
    called_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = parse_qs(request.url.query.decode())["id"][0].split(",")
        called_ids.extend(ids)
        return httpx.Response(200, json={"items": [item(video_id) for video_id in ids]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = enrich_youtube(connection, api_key="key", client=client)
        second = enrich_youtube(connection, api_key="key", client=client)
        refreshed = enrich_youtube(connection, api_key="key", client=client, refresh=True)

    assert first.processed == 2
    assert second.processed == 0
    assert second.skipped == 2
    assert refreshed.processed == 2
    assert called_ids == ["video-000", "video-001", "video-000", "video-001"]


def test_api_key_can_come_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = database_with_videos(1)
    monkeypatch.setenv("YOUTUBE_API_KEY", "environment-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert parse_qs(request.url.query.decode())["key"] == ["environment-secret"]
        return httpx.Response(200, json={"items": [item("video-000")]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert enrich_youtube(connection, client=client).processed == 1


def test_missing_api_key_fails_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="YouTube API key"):
        enrich_youtube(database_with_videos(1))


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_transient_http_errors_are_retried(status_code: int) -> None:
    connection = database_with_videos(1)
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(status_code, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"items": [item("video-000")]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary = enrich_youtube(
            connection, api_key="top-secret", client=client, sleep=sleeps.append
        )

    assert attempts == 3
    assert sleeps == [0.0, 0.0]
    assert summary.requests == 3


def test_timeout_exhaustion_does_not_expose_secret() -> None:
    connection = database_with_videos(1)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request URL included top-secret", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(YouTubeAPIError) as exc_info,
    ):
        enrich_youtube(
            connection, api_key="top-secret", client=client, sleep=lambda _: None
        )

    assert "top-secret" not in str(exc_info.value)
    assert connection.execute("SELECT COUNT(*) FROM video_metadata").fetchone()[0] == 0
