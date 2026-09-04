from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from music_taste.models import MusicStatus, VideoMetadata

YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_PARTS = "snippet,contentDetails,topicDetails"
MAX_BATCH_SIZE = 50
DEFAULT_MAX_ATTEMPTS = 4


@dataclass(frozen=True, slots=True)
class YouTubeEnrichmentSummary:
    discovered: int
    processed: int
    skipped: int
    available: int
    unavailable: int
    requests: int


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)


def iso8601_duration_seconds(value: str | None) -> int | None:
    """Convert the subset of ISO-8601 durations returned by YouTube to seconds."""
    if not value:
        return None
    match = _DURATION_RE.fullmatch(value)
    if match is None or not any(match.groupdict().values()):
        return None
    parts = {name: float(number or 0) for name, number in match.groupdict().items()}
    return round(
        parts["days"] * 86_400
        + parts["hours"] * 3_600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


_MUSIC_TOPIC_TERMS = (
    "music",
    "musical",
    "song",
    "album",
    "singer",
    "musician",
    "record_producer",
)
_TITLE_SIGNALS: tuple[tuple[re.Pattern[str], float, str], ...] = (
    (re.compile(r"\bofficial\s+(?:audio|video|music\s+video)\b", re.IGNORECASE), 0.35, "title:official_media"),
    (re.compile(r"\b(?:lyric|lyrics|letra|letras)\b", re.IGNORECASE), 0.25, "title:lyrics"),
    (re.compile(r"\bmusic\s+video\b", re.IGNORECASE), 0.30, "title:music_video"),
    (re.compile(r"\bremaster(?:ed)?\b", re.IGNORECASE), 0.20, "title:remastered"),
    (re.compile(r"\b(?:live|ao vivo)\b", re.IGNORECASE), 0.10, "title:live"),
)
_CHANNEL_SIGNALS: tuple[tuple[re.Pattern[str], float, str], ...] = (
    (re.compile(r"(?:\s-\sTopic|\bTopic)$", re.IGNORECASE), 0.35, "channel:topic"),
    (re.compile(r"VEVO$", re.IGNORECASE), 0.35, "channel:vevo"),
    (re.compile(r"\b(?:records|recordings|music)\b", re.IGNORECASE), 0.15, "channel:music_label"),
)
_NON_MUSIC_SIGNALS: tuple[tuple[re.Pattern[str], float, str], ...] = (
    (re.compile(r"\b(?:gameplay|walkthrough|tutorial|podcast)\b", re.IGNORECASE), -0.25, "title:non_music_term"),
    (re.compile(r"\b(?:news|notícias|review|unboxing)\b", re.IGNORECASE), -0.20, "title:non_music_term"),
)


def classify_music(
    *,
    category_id: str | None,
    title: str | None,
    channel_title: str | None,
    topic_categories: Sequence[str] = (),
) -> tuple[MusicStatus, float, tuple[str, ...]]:
    """Classify metadata and return an auditable probability-like score and reasons."""
    score = 0.5
    reasons: list[str] = []

    if category_id == "10":
        score += 0.5
        reasons.append("category:music")
    elif category_id:
        score -= 0.35
        reasons.append(f"category:non_music:{category_id}")

    music_topics = [
        topic
        for topic in topic_categories
        if any(term in topic.casefold() for term in _MUSIC_TOPIC_TERMS)
    ]
    if music_topics:
        score += 0.4
        reasons.append("topic:music")

    title_value = title or ""
    channel_value = channel_title or ""
    for pattern, weight, reason in _TITLE_SIGNALS:
        if pattern.search(title_value):
            score += weight
            reasons.append(reason)
    for pattern, weight, reason in _CHANNEL_SIGNALS:
        if pattern.search(channel_value):
            score += weight
            reasons.append(reason)
    for pattern, weight, reason in _NON_MUSIC_SIGNALS:
        if pattern.search(title_value):
            score += weight
            reasons.append(reason)

    score = round(max(0.0, min(1.0, score)), 3)
    if score >= 0.7:
        status = MusicStatus.MUSIC
    elif score <= 0.3:
        status = MusicStatus.NON_MUSIC
    else:
        status = MusicStatus.UNKNOWN
    if not reasons:
        reasons.append("insufficient_evidence")
    return status, score, tuple(reasons)


def _batches(values: Sequence[str], size: int = MAX_BATCH_SIZE) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield list(values[offset : offset + size])


class YouTubeAPIError(RuntimeError):
    """A sanitized YouTube API failure that never includes credentials."""


class YouTubeMetadataClient:
    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._sleep = sleep
        self._max_attempts = max_attempts
        self.request_count = 0

    def fetch_many(self, video_ids: Sequence[str]) -> Sequence[VideoMetadata]:
        results: list[VideoMetadata] = []
        for batch in _batches(list(video_ids)):
            results.extend(self._fetch_batch(batch))
        return results

    def _fetch_batch(self, video_ids: Sequence[str]) -> list[VideoMetadata]:
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            try:
                self.request_count += 1
                response = self._client.get(
                    YOUTUBE_VIDEOS_URL,
                    params={
                        "part": YOUTUBE_PARTS,
                        "id": ",".join(video_ids),
                        "key": self._api_key,
                    },
                )
            except httpx.TimeoutException as exc:
                if attempt + 1 == self._max_attempts:
                    raise YouTubeAPIError(
                        f"YouTube request timed out after {self._max_attempts} attempts"
                    ) from exc
                self._sleep(2**attempt)
                continue

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt + 1 == self._max_attempts:
                    raise YouTubeAPIError(
                        f"YouTube API failed with HTTP {response.status_code} "
                        f"after {self._max_attempts} attempts"
                    )
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = max(float(retry_after), 0.0) if retry_after else float(2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                self._sleep(delay)
                continue

            if response.is_error:
                raise YouTubeAPIError(f"YouTube API failed with HTTP {response.status_code}")
            break

        if response is None:  # defensive; the loop either returns a response or raises
            raise YouTubeAPIError("YouTube API request failed")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise YouTubeAPIError("YouTube API returned invalid JSON") from exc
        return [_metadata_from_item(item) for item in payload.get("items", [])]


def _metadata_from_item(item: dict[str, Any]) -> VideoMetadata:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    topics = item.get("topicDetails") or {}
    topic_categories = tuple(str(value) for value in topics.get("topicCategories", []))
    status, score, reasons = classify_music(
        category_id=str(snippet["categoryId"]) if snippet.get("categoryId") is not None else None,
        title=snippet.get("title"),
        channel_title=snippet.get("channelTitle"),
        topic_categories=topic_categories,
    )
    return VideoMetadata(
        video_id=str(item["id"]),
        category_id=str(snippet["categoryId"]) if snippet.get("categoryId") is not None else None,
        title=snippet.get("title"),
        channel_id=snippet.get("channelId"),
        channel_title=snippet.get("channelTitle"),
        duration_seconds=iso8601_duration_seconds(details.get("duration")),
        topic_categories=topic_categories,
        availability="available",
        music_status=status,
        music_score=score,
        reasons=reasons,
        etag=item.get("etag"),
    )


def _unavailable_metadata(video_id: str) -> VideoMetadata:
    return VideoMetadata(
        video_id=video_id,
        category_id=None,
        title=None,
        channel_id=None,
        channel_title=None,
        duration_seconds=None,
        availability="unavailable",
        music_status=MusicStatus.UNKNOWN,
        music_score=0.0,
        reasons=("api:item_absent",),
    )


_UPSERT_METADATA = """
INSERT INTO video_metadata (
    video_id, category_id, title, channel_id, channel_title, duration_seconds,
    topic_categories_json, availability, music_status, music_score,
    reasons_json, etag, fetched_at, error
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)
ON CONFLICT(video_id) DO UPDATE SET
    category_id=excluded.category_id,
    title=excluded.title,
    channel_id=excluded.channel_id,
    channel_title=excluded.channel_title,
    duration_seconds=excluded.duration_seconds,
    topic_categories_json=excluded.topic_categories_json,
    availability=excluded.availability,
    music_status=excluded.music_status,
    music_score=excluded.music_score,
    reasons_json=excluded.reasons_json,
    etag=excluded.etag,
    fetched_at=CURRENT_TIMESTAMP,
    error=NULL
"""


def _store_metadata(connection: sqlite3.Connection, metadata: VideoMetadata) -> None:
    connection.execute(
        _UPSERT_METADATA,
        (
            metadata.video_id,
            metadata.category_id,
            metadata.title,
            metadata.channel_id,
            metadata.channel_title,
            metadata.duration_seconds,
            json.dumps(metadata.topic_categories, ensure_ascii=False, separators=(",", ":")),
            metadata.availability,
            metadata.music_status.value,
            metadata.music_score,
            json.dumps(metadata.reasons, ensure_ascii=False, separators=(",", ":")),
            metadata.etag,
        ),
    )


def enrich_youtube(
    connection: sqlite3.Connection,
    api_key: str | None = None,
    refresh: bool = False,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> YouTubeEnrichmentSummary:
    """Fetch and persist metadata for distinct video IDs, committing each batch."""
    resolved_key = api_key or os.environ.get("YOUTUBE_API_KEY")
    if not resolved_key:
        raise ValueError("A YouTube API key is required via api_key or YOUTUBE_API_KEY")

    discovered = connection.execute(
        "SELECT COUNT(DISTINCT video_id) FROM activity_events WHERE video_id IS NOT NULL"
    ).fetchone()[0]
    if refresh:
        rows = connection.execute(
            "SELECT DISTINCT video_id FROM activity_events "
            "WHERE video_id IS NOT NULL ORDER BY video_id"
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT DISTINCT e.video_id FROM activity_events AS e "
            "LEFT JOIN video_metadata AS m ON m.video_id = e.video_id "
            "WHERE e.video_id IS NOT NULL AND m.video_id IS NULL ORDER BY e.video_id"
        ).fetchall()
    pending = [str(row[0]) for row in rows]
    skipped = discovered - len(pending)
    if not pending:
        return YouTubeEnrichmentSummary(discovered, 0, skipped, 0, 0, 0)

    owns_client = client is None
    http_client = client or httpx.Client(timeout=httpx.Timeout(20.0))
    provider = YouTubeMetadataClient(resolved_key, client=http_client, sleep=sleep)
    available = 0
    unavailable = 0
    processed = 0
    try:
        for batch in _batches(pending):
            fetched = {item.video_id: item for item in provider.fetch_many(batch)}
            for video_id in batch:
                metadata = fetched.get(video_id) or _unavailable_metadata(video_id)
                _store_metadata(connection, metadata)
                if metadata.availability == "available":
                    available += 1
                else:
                    unavailable += 1
                processed += 1
            connection.commit()
    finally:
        if owns_client:
            http_client.close()

    return YouTubeEnrichmentSummary(
        discovered=discovered,
        processed=processed,
        skipped=skipped,
        available=available,
        unavailable=unavailable,
        requests=provider.request_count,
    )
