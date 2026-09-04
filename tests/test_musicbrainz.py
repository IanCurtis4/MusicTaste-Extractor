
import httpx

from music_taste.db import connect, initialize
from music_taste.models import EventType, MusicCandidate
from music_taste.musicbrainz import MusicBrainzClient, resolve_musicbrainz, score_match

RID1 = "11111111-1111-4111-8111-111111111111"
RID2 = "22222222-2222-4222-8222-222222222222"
AID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RGID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def candidate():
    return MusicCandidate("key", EventType.WATCH, "Track", "Artist", duration_seconds=200)


def response(recording_id=RID1, title="Track", score=100, length=200_000):
    return {
        "recordings": [{
            "id": recording_id,
            "title": title,
            "score": score,
            "length": length,
            "artist-credit": [{"name": "Artist", "artist": {"id": AID, "name": "Artist", "type": "Group"}}],
            "releases": [{"release-group": {"id": RGID, "title": "Album"}}],
        }]
    }


def test_score_is_bounded_and_uses_duration():
    exact = score_match(candidate(), recording_title="Track", artist_name="Artist", duration_seconds=200, provider_score=1)
    wrong = score_match(candidate(), recording_title="Other", artist_name="Someone", duration_seconds=500, provider_score=0)
    assert exact == 1
    assert 0 <= wrong < exact <= 1

    sparse = MusicCandidate("sparse", EventType.SEARCH, "Track")
    assert score_match(sparse, recording_title="Track", artist_name=None, duration_seconds=None, provider_score=1) < 0.9


def test_client_cache_user_agent_and_retry_without_real_network():
    db = connect(":memory:")
    initialize(db)
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=response(), request=request)

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    resolver = MusicBrainzClient(db, contact="me@example.com", client=http_client, rate_limit_seconds=0, sleep=sleeps.append)
    matches = resolver.resolve(candidate())
    assert matches[0].artist_type == "Group"
    assert matches[0].release_group_mbid == RGID
    assert "me@example.com" in calls[0].headers["User-Agent"]
    assert sleeps == [1.0]
    resolver.resolve(candidate())
    assert len(calls) == 2  # second resolve was served by SQLite cache


def test_resolve_threshold_and_tie_go_to_review():
    db = connect(":memory:")
    initialize(db)
    for key, title in (("one", "Track"), ("two", "Tie")):
        db.execute(
            """INSERT INTO music_candidates(
               candidate_key,source_kind,normalized_title,normalized_artist,evidence_json
            ) VALUES (?, 'watch', ?, 'Artist', '{}')""",
            (key, title),
        )

    class FakeResolver:
        def resolve(self, item, refresh=False):
            payloads = [response(RID1, item.normalized_title)]
            if item.normalized_title == "Tie":
                payloads[0]["recordings"].append(response(RID2, "Tie")["recordings"][0])
            data = {"recordings": payloads[0]["recordings"]}
            # Reuse parser/scorer without a network request by priming the cache.
            client = MusicBrainzClient(db, contact="x@y.z", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data, request=r))), rate_limit_seconds=0)
            return client.resolve(item, refresh=True)

    summary = resolve_musicbrainz(db, contact="x@y.z", client=FakeResolver())
    assert summary == {"processed": 2, "accepted": 1, "review": 1, "errors": 0}
    statuses = [r[0] for r in db.execute("SELECT status FROM entity_matches ORDER BY candidate_id")]
    assert statuses == ["accepted", "review"]
