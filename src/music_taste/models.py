from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class EventType(StrEnum):
    SEARCH = "search"
    WATCH = "watch"


class MusicStatus(StrEnum):
    MUSIC = "music"
    NON_MUSIC = "non_music"
    UNKNOWN = "unknown"


class MatchStatus(StrEnum):
    ACCEPTED = "accepted"
    REVIEW = "review"
    REJECTED = "rejected"


class TaxonomyKind(StrEnum):
    GENRE = "genre"
    STYLE = "style"
    MOOD = "mood"
    THEME = "theme"
    TAG = "tag"


class EntityLevel(StrEnum):
    TRACK = "track"
    ALBUM = "album"
    ARTIST = "artist"


@dataclass(slots=True)
class ActivityEvent:
    event_key: str
    event_type: EventType
    occurred_at_raw: str
    occurred_at_utc: str | None
    source_timezone: str | None
    target_url: str | None
    video_id: str | None = None
    query_text: str | None = None
    title: str | None = None
    channel_name: str | None = None
    channel_url: str | None = None
    source_file_fingerprint: str = ""
    source_ordinal: int = 0
    parse_status: str = "ok"
    parse_error: str | None = None


@dataclass(slots=True)
class VideoMetadata:
    video_id: str
    category_id: str | None
    title: str | None
    channel_id: str | None
    channel_title: str | None
    duration_seconds: int | None
    topic_categories: tuple[str, ...] = ()
    availability: str = "available"
    music_status: MusicStatus = MusicStatus.UNKNOWN
    music_score: float = 0.0
    reasons: tuple[str, ...] = ()
    etag: str | None = None


@dataclass(slots=True)
class MusicCandidate:
    candidate_key: str
    source_kind: EventType
    normalized_title: str
    normalized_artist: str | None = None
    video_id: str | None = None
    query_text: str | None = None
    duration_seconds: int | None = None
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class MatchCandidate:
    recording_mbid: str
    recording_title: str
    artist_mbid: str | None
    artist_name: str | None
    artist_type: str | None
    release_group_mbid: str | None
    release_group_title: str | None
    score: float
    provider_score: float
    duration_delta_seconds: int | None = None


@dataclass(slots=True)
class TaxonomyAssignment:
    provider: str
    taxonomy: TaxonomyKind
    value: str
    entity_type: str
    entity_id: str
    source_level: EntityLevel
    inherited: bool
    source_url: str | None
    confidence: float = 1.0


class VideoMetadataProvider(Protocol):
    def fetch_many(self, video_ids: Sequence[str]) -> Sequence[VideoMetadata]: ...


class MusicResolver(Protocol):
    def resolve(self, candidate: MusicCandidate) -> Sequence[MatchCandidate]: ...


class TaxonomyProvider(Protocol):
    def enrich(self, entity_type: str, entity_id: str) -> Sequence[TaxonomyAssignment]: ...


@dataclass(slots=True)
class IngestResult:
    path: Path
    fingerprint: str
    discovered: int
    inserted: int
    duplicates: int
    errors: int
