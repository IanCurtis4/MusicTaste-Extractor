from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict
from difflib import SequenceMatcher

import httpx

from .db import json_text
from .models import EventType, MatchCandidate, MusicCandidate
from .normalize import comparison_text

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/recording"
CLIENT_NAME = "music-taste-extractor/0.1.0"


def _similarity(left: str | None, right: str | None) -> float:
    a, b = comparison_text(left), comparison_text(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def score_match(
    candidate: MusicCandidate,
    *,
    recording_title: str,
    artist_name: str | None,
    duration_seconds: int | None,
    provider_score: float,
) -> float:
    """Combine textual, duration and provider evidence into a score in [0, 1]."""
    # Weights deliberately remain absolute when evidence is absent. A title-only
    # query must not become an automatic match merely because its two available
    # fields agree; sparse candidates belong in manual review.
    score = _similarity(candidate.normalized_title, recording_title) * 0.45
    score += max(0.0, min(1.0, provider_score)) * 0.10
    if candidate.normalized_artist:
        score += _similarity(candidate.normalized_artist, artist_name) * 0.35
    if candidate.duration_seconds is not None and duration_seconds is not None:
        delta = abs(candidate.duration_seconds - duration_seconds)
        # Exact to 5 seconds is excellent; degrade linearly, reaching zero at 60 seconds.
        duration_score = 1.0 if delta <= 5 else max(0.0, 1.0 - ((delta - 5) / 55))
        score += duration_score * 0.10
    return round(max(0.0, min(1.0, score)), 6)


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _search_query(candidate: MusicCandidate) -> str:
    clauses = [f"recording:{_quoted(candidate.normalized_title)}"]
    if candidate.normalized_artist:
        clauses.append(f"artist:{_quoted(candidate.normalized_artist)}")
    return " AND ".join(clauses)


def _artist_credit(recording: dict[str, object]) -> tuple[str | None, str | None, str | None]:
    credits = recording.get("artist-credit") or []
    if not isinstance(credits, list) or not credits:
        return None, None, None
    names: list[str] = []
    first_artist: dict[str, object] | None = None
    for credit in credits:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist")
        if isinstance(artist, dict):
            first_artist = first_artist or artist
            names.append(str(credit.get("name") or artist.get("name") or ""))
        joinphrase = credit.get("joinphrase")
        if joinphrase and names:
            names[-1] += str(joinphrase)
    if first_artist is None:
        return None, "".join(names) or None, None
    return (
        str(first_artist.get("id")) if first_artist.get("id") else None,
        "".join(names) or str(first_artist.get("name") or "") or None,
        str(first_artist.get("type")) if first_artist.get("type") else None,
    )


def _release_group(recording: dict[str, object]) -> tuple[str | None, str | None]:
    releases = recording.get("releases") or []
    if not isinstance(releases, list):
        return None, None
    for release in releases:
        if not isinstance(release, dict):
            continue
        group = release.get("release-group")
        if isinstance(group, dict) and group.get("id"):
            return str(group["id"]), str(group.get("title") or "") or None
    return None, None


class MusicBrainzClient:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        contact: str,
        client: httpx.Client | None = None,
        rate_limit_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
    ) -> None:
        if not contact or not contact.strip():
            raise ValueError("MUSIC_TASTE_CONTACT is required for the MusicBrainz User-Agent")
        self.connection = connection
        self.client = client or httpx.Client(timeout=30.0)
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.sleep = sleep
        self.max_retries = max(0, max_retries)
        self.headers = {"User-Agent": f"{CLIENT_NAME} ({contact.strip()})", "Accept": "application/json"}
        self._last_request_at: float | None = None

    def _request(self, params: dict[str, object], *, refresh: bool = False) -> dict[str, object]:
        cache_material = json.dumps(params, sort_keys=True, separators=(",", ":"))
        cache_key = "musicbrainz:" + hashlib.sha256(cache_material.encode()).hexdigest()
        if not refresh:
            cached = self.connection.execute(
                "SELECT response_json, status_code FROM http_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if cached and cached["response_json"] and cached["status_code"] == 200:
                return json.loads(cached["response_json"])

        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            now = time.monotonic()
            if self._last_request_at is not None:
                remaining = self.rate_limit_seconds - (now - self._last_request_at)
                if remaining > 0:
                    self.sleep(remaining)
            try:
                response = self.client.get(MUSICBRAINZ_URL, params=params, headers=self.headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    self._cache_error(cache_key, None, str(exc))
                    raise
                self.sleep(min(8.0, 2.0**attempt))
                continue
            finally:
                self._last_request_at = time.monotonic()

            if response.status_code == 200:
                payload = response.json()
                self.connection.execute(
                    """
                    INSERT INTO http_cache(cache_key, provider, status_code, response_json, error)
                    VALUES (?, 'musicbrainz', 200, ?, NULL)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        status_code=200, response_json=excluded.response_json,
                        fetched_at=CURRENT_TIMESTAMP, error=NULL
                    """,
                    (cache_key, json_text(payload)),
                )
                self.connection.commit()
                return payload
            if response.status_code not in {429, 500, 502, 503, 504} or attempt >= self.max_retries:
                self._cache_error(cache_key, response.status_code, response.text[:500])
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else min(8.0, 2.0**attempt)
            self.sleep(delay)
        raise RuntimeError("MusicBrainz retry loop exhausted")

    def _cache_error(self, cache_key: str, status_code: int | None, error: str) -> None:
        self.connection.execute(
            """
            INSERT INTO http_cache(cache_key, provider, status_code, error)
            VALUES (?, 'musicbrainz', ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                status_code=excluded.status_code, fetched_at=CURRENT_TIMESTAMP, error=excluded.error
            """,
            (cache_key, status_code, error),
        )
        self.connection.commit()

    def resolve(self, candidate: MusicCandidate, *, refresh: bool = False) -> list[MatchCandidate]:
        payload = self._request(
            {"query": _search_query(candidate), "fmt": "json", "limit": 10},
            refresh=refresh,
        )
        matches: list[MatchCandidate] = []
        for recording in payload.get("recordings", []):
            if not isinstance(recording, dict) or not recording.get("id"):
                continue
            artist_mbid, artist_name, artist_type = _artist_credit(recording)
            release_group_mbid, release_group_title = _release_group(recording)
            length = recording.get("length")
            duration = round(float(length) / 1000) if isinstance(length, (int, float)) else None
            provider_score = float(recording.get("score") or 0) / 100.0
            score = score_match(
                candidate,
                recording_title=str(recording.get("title") or ""),
                artist_name=artist_name,
                duration_seconds=duration,
                provider_score=provider_score,
            )
            matches.append(
                MatchCandidate(
                    recording_mbid=str(recording["id"]),
                    recording_title=str(recording.get("title") or ""),
                    artist_mbid=artist_mbid,
                    artist_name=artist_name,
                    artist_type=artist_type,
                    release_group_mbid=release_group_mbid,
                    release_group_title=release_group_title,
                    score=score,
                    provider_score=provider_score,
                    duration_delta_seconds=(
                        abs(candidate.duration_seconds - duration)
                        if candidate.duration_seconds is not None and duration is not None
                        else None
                    ),
                )
            )
        return sorted(matches, key=lambda item: item.score, reverse=True)


def _row_candidate(row: sqlite3.Row) -> MusicCandidate:
    return MusicCandidate(
        candidate_key=row["candidate_key"],
        source_kind=EventType(row["source_kind"]),
        normalized_title=row["normalized_title"],
        normalized_artist=row["normalized_artist"],
        video_id=row["video_id"],
        query_text=row["query_text"],
        duration_seconds=row["duration_seconds"],
        evidence=json.loads(row["evidence_json"] or "{}"),
    )


def resolve_musicbrainz(
    connection: sqlite3.Connection,
    contact: str | None = None,
    refresh: bool = False,
    limit: int | None = None,
    client: httpx.Client | MusicBrainzClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rate_limit_seconds: float = 1.0,
) -> dict[str, int]:
    """Resolve pending candidates and persist their best result and review status."""
    contact = contact or os.getenv("MUSIC_TASTE_CONTACT")
    resolver = client if (
        isinstance(client, MusicBrainzClient)
        or (client is not None and hasattr(client, "resolve") and not isinstance(client, httpx.Client))
    ) else MusicBrainzClient(
        connection,
        contact=contact or "",
        client=client,
        sleep=sleep,
        rate_limit_seconds=rate_limit_seconds,
    )
    query = """
        SELECT mc.* FROM music_candidates mc
        LEFT JOIN entity_matches em ON em.candidate_id = mc.id
        WHERE (? = 1 OR em.candidate_id IS NULL)
        ORDER BY mc.id
    """
    params: list[object] = [int(refresh)]
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(0, limit))
    rows = connection.execute(query, params).fetchall()
    accepted = review = errors = 0
    for row in rows:
        try:
            matches = resolver.resolve(_row_candidate(row), refresh=refresh)
        # A single provider/transport failure must not discard progress for the
        # remaining candidates; the summary exposes the error count.
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        best = matches[0] if matches else None
        runner_up = matches[1].score if len(matches) > 1 else 0.0
        margin = (best.score - runner_up) if best else 0.0
        status = "accepted" if best and best.score >= 0.90 and margin >= 0.10 else "review"
        accepted += status == "accepted"
        review += status == "review"
        values = asdict(best) if best else {}
        connection.execute(
            """
            INSERT INTO entity_matches(
                candidate_id, recording_mbid, recording_title, artist_mbid, artist_name,
                artist_type, release_group_mbid, release_group_title, score,
                runner_up_score, margin, status, method, provider
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'musicbrainz_search', 'musicbrainz')
            ON CONFLICT(candidate_id) DO UPDATE SET
                recording_mbid=excluded.recording_mbid, recording_title=excluded.recording_title,
                artist_mbid=excluded.artist_mbid, artist_name=excluded.artist_name,
                artist_type=excluded.artist_type,
                release_group_mbid=excluded.release_group_mbid,
                release_group_title=excluded.release_group_title, score=excluded.score,
                runner_up_score=excluded.runner_up_score, margin=excluded.margin,
                status=excluded.status, method=excluded.method, provider=excluded.provider,
                reviewed_at=NULL, updated_at=CURRENT_TIMESTAMP
            """,
            (
                row["id"], values.get("recording_mbid"), values.get("recording_title"),
                values.get("artist_mbid"), values.get("artist_name"), values.get("artist_type"),
                values.get("release_group_mbid"), values.get("release_group_title"),
                values.get("score", 0.0), runner_up, margin, status,
            ),
        )
        connection.commit()
    return {"processed": len(rows), "accepted": accepted, "review": review, "errors": errors}
