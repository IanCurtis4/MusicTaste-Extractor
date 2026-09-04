from __future__ import annotations

import sqlite3

import httpx
import pytest

from music_taste.allmusic import (
    AllMusicBlockedError,
    AllMusicRobotsError,
    AllMusicSelectorDriftError,
    AllMusicTermsError,
    enrich_allmusic,
    parse_allmusic_html,
)
from music_taste.db import initialize

ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
DETAIL_HTML = """
<html><body>
  <section class="genres"><a>Pop/Rock</a><a>Electronic</a></section>
  <div class="styles"><a>Dream Pop</a><a>Shoegaze</a></div>
  <dl><dt>Moods</dt><dd><a>Atmospheric</a></dd></dl>
  <section data-section="themes"><ul><li>Late Night</li></ul></section>
</body></html>
"""


def database_with_match(*, source_level: str = "track") -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize(connection)
    connection.execute(
        """INSERT INTO music_candidates
           (id, candidate_key, source_kind, normalized_title)
           VALUES (1, 'candidate', 'watch', 'Track')"""
    )
    connection.execute(
        """INSERT INTO entity_matches (
             candidate_id, recording_mbid, recording_title, artist_mbid,
             artist_name, artist_type, release_group_mbid, release_group_title,
             score, runner_up_score, margin, status, method, provider
           ) VALUES (1, 'recording-id', 'Track', 'artist-id', 'Artist', 'Group',
                     'album-id', 'Album', .99, .5, .49, 'accepted', 'fixture', 'musicbrainz')"""
    )
    ids = {"track": "recording-id", "album": "album-id", "artist": "artist-id"}
    connection.execute(
        """INSERT INTO external_links(entity_type, entity_id, provider, url)
           VALUES (?, ?, 'allmusic', ?)""",
        (source_level, ids[source_level], f"https://www.allmusic.com/{source_level}/fixture"),
    )
    connection.commit()
    return connection


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_parser_extracts_four_taxonomies_and_detects_drift() -> None:
    values = parse_allmusic_html(
        DETAIL_HTML,
        entity_type="track",
        entity_id="recording-id",
        source_level="track",
        source_url="https://www.allmusic.com/song/fixture",
    )
    assert {(item.taxonomy.value, item.value) for item in values} == {
        ("genre", "Pop/Rock"),
        ("genre", "Electronic"),
        ("style", "Dream Pop"),
        ("style", "Shoegaze"),
        ("mood", "Atmospheric"),
        ("theme", "Late Night"),
    }
    assert not any(item.inherited for item in values)
    with pytest.raises(AllMusicSelectorDriftError):
        parse_allmusic_html(
            "<html><body><main>new layout</main></body></html>",
            entity_type="track",
            entity_id="id",
            source_level="track",
        )


def test_acknowledgement_is_required_before_any_request() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    with mock_client(handler) as client, pytest.raises(AllMusicTermsError):
        enrich_allmusic(database_with_match(), client=client)
    assert requests == 0


def test_robots_is_first_and_disallow_aborts_before_page() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    with mock_client(handler) as client, pytest.raises(AllMusicRobotsError):
        enrich_allmusic(
            database_with_match(), True, client=client, sleep=lambda _: None
        )
    assert urls == ["https://www.allmusic.com/robots.txt"]


def test_ambiguous_robots_failure_aborts() -> None:
    with (
        mock_client(lambda request: httpx.Response(503, text="unavailable")) as client,
        pytest.raises(AllMusicRobotsError),
    ):
        enrich_allmusic(database_with_match(), True, client=client)

    with (
        mock_client(
            lambda request: httpx.Response(200, text="<html>not robots</html>")
        ) as client,
        pytest.raises(AllMusicRobotsError),
    ):
        enrich_allmusic(database_with_match(), True, client=client)


def test_external_link_precedes_search_persists_only_normalized_data() -> None:
    connection = database_with_match(source_level="album")
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        assert request.url.path == "/album/fixture"
        return httpx.Response(200, text=DETAIL_HTML)

    with mock_client(handler) as client:
        first = enrich_allmusic(connection, True, client=client, sleep=lambda _: None)
        second = enrich_allmusic(connection, True, client=client, sleep=lambda _: None)

    assert not any("/search/" in url for url in urls)
    assert first["assignments"] == 6
    assert second["assignments"] == 0
    assert second["requests"] == 0
    rows = connection.execute(
        "SELECT value_norm, source_level, inherited FROM taxonomy_assignments"
    ).fetchall()
    assert len(rows) == 6
    assert all(row["source_level"] == "album" and row["inherited"] == 1 for row in rows)
    assert connection.execute(
        "SELECT COUNT(*) FROM http_cache WHERE response_json IS NOT NULL"
    ).fetchone()[0] == 0
    dump = "\n".join(connection.iterdump())
    assert "<html>" not in dump


@pytest.mark.parametrize("status", [401, 403])
def test_auth_blocks_abort(status: int) -> None:
    connection = database_with_match()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(status, text="blocked")

    with mock_client(handler) as client, pytest.raises(AllMusicBlockedError):
        enrich_allmusic(connection, True, client=client, sleep=lambda _: None)


def test_persistent_rate_limit_and_captcha_abort() -> None:
    attempts = 0

    def rate_limited(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": "0"})

    with mock_client(rate_limited) as client, pytest.raises(AllMusicBlockedError):
        enrich_allmusic(database_with_match(), True, client=client, sleep=lambda _: None)
    assert attempts == 3

    def captcha(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW)
        return httpx.Response(200, text="<html>Verify that you are human</html>")

    with mock_client(captcha) as client, pytest.raises(AllMusicBlockedError):
        enrich_allmusic(database_with_match(), True, client=client, sleep=lambda _: None)
