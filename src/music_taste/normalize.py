from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from .db import json_text

_EDITORIAL_SUFFIXES = (
    "official music video",
    "official video",
    "official audio",
    "official lyric video",
    "lyric video",
    "lyrics",
    "audio",
    "video clip",
    "visualizer",
    "remastered",
    "remaster",
    "live",
    "hd",
    "hq",
)
_BRACKETED_SUFFIX_RE = re.compile(
    r"\s*[\[(](?:[^\])]*?\b(?:"
    + "|".join(re.escape(value) for value in _EDITORIAL_SUFFIXES)
    + r")\b[^\])]*?)[\])]\s*$",
    re.IGNORECASE,
)
_DASHED_SUFFIX_RE = re.compile(
    r"\s*[-|]\s*(?:"
    + "|".join(re.escape(value) for value in _EDITORIAL_SUFFIXES)
    + r")(?:\s+\d{4})?\s*$",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?)\s+", re.IGNORECASE)
_CHANNEL_SUFFIX_RE = re.compile(
    r"\s*(?:-\s*)?(?:topic|vevo|official(?:\s+channel)?|music)\s*$",
    re.IGNORECASE,
)
_MUSIC_QUERY_RE = re.compile(
    r"\b(?:song|music|lyrics?|lyric video|official audio|album|ep|remix|"
    r"acoustic|live|cover|soundtrack|playlist|feat\.?|ft\.?)\b",
    re.IGNORECASE,
)
_NON_MUSIC_QUERY_RE = re.compile(
    r"\b(?:tutorial|review|reaction|interview|podcast|trailer|gameplay|news|"
    r"how to|como fazer|aula)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class InferredTrack:
    title: str
    artist: str | None
    method: str


def _display_text(value: str | None) -> str:
    """Normalize whitespace while retaining spelling, accents, and case."""
    return re.sub(r"\s+", " ", value or "").strip()


def comparison_text(value: str | None) -> str:
    """Return a stable accent-insensitive value suitable for matching/keys."""
    text = unicodedata.normalize("NFKD", _display_text(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def strip_editorial_suffixes(title: str | None) -> str:
    """Remove YouTube editorial decorations from a matching copy of a title."""
    cleaned = _display_text(title)
    previous = None
    while cleaned and cleaned != previous:
        previous = cleaned
        cleaned = _BRACKETED_SUFFIX_RE.sub("", cleaned).strip()
        cleaned = _DASHED_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned.strip(" -|")


def normalize_title(title: str | None) -> str:
    """Normalize a title for MusicBrainz matching, never mutating source data."""
    return _display_text(strip_editorial_suffixes(title))


def normalize_artist(artist: str | None) -> str | None:
    artist = _display_text(artist)
    if not artist:
        return None
    artist = _CHANNEL_SUFFIX_RE.sub("", artist).strip(" -|")
    return artist or None


def infer_artist_title(title: str | None, channel: str | None = None) -> InferredTrack:
    cleaned = normalize_title(title)
    # En/em dashes and a spaced hyphen are much less likely to be part of a name.
    parts = re.split(r"\s+(?:[-–—]|\|)\s+", cleaned, maxsplit=1)
    if len(parts) == 2 and all(parts):
        left, right = (_display_text(part) for part in parts)
        return InferredTrack(title=right, artist=normalize_artist(left), method="title_separator")

    channel_artist = normalize_artist(channel)
    if channel_artist and comparison_text(channel_artist) not in {
        "youtube",
        "youtube music",
        "various artists",
    }:
        return InferredTrack(title=cleaned, artist=channel_artist, method="channel")
    return InferredTrack(title=cleaned, artist=None, method="title_only")


def _plausible_music_query(query: str) -> bool:
    query = _display_text(query)
    if not query or _NON_MUSIC_QUERY_RE.search(query):
        return False
    if _MUSIC_QUERY_RE.search(query):
        return True
    # Artist - Track is a useful high precision structure for bare searches.
    return len(re.split(r"\s+(?:[-–—]|\|)\s+", query, maxsplit=1)) == 2


def _candidate_key(source_kind: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{source_kind}:{digest}"


def build_candidates(connection: sqlite3.Connection) -> dict[str, int]:
    """Create deduplicated candidates and link every contributing activity event."""
    created = 0
    updated = 0
    linked = 0
    skipped = 0

    watch_rows = connection.execute(
        """
        SELECT e.id AS event_id, e.video_id, e.title AS event_title, e.channel_name,
               vm.title AS metadata_title, vm.channel_title, vm.duration_seconds,
               vm.music_status, vm.music_score, vm.reasons_json
          FROM activity_events e
          LEFT JOIN video_metadata vm ON vm.video_id = e.video_id
         WHERE e.event_type = 'watch' AND e.video_id IS NOT NULL
        """
    ).fetchall()
    search_rows = connection.execute(
        """
        SELECT id AS event_id, query_text
          FROM activity_events
         WHERE event_type = 'search' AND query_text IS NOT NULL
        """
    ).fetchall()

    def upsert_and_link(
        *, event_id: int, source_kind: str, identity: str, video_id: str | None,
        query_text: str | None, title: str, artist: str | None,
        duration: int | None, evidence: dict[str, object]
    ) -> None:
        nonlocal created, updated, linked
        key = _candidate_key(source_kind, identity)
        existed = connection.execute(
            "SELECT id FROM music_candidates WHERE candidate_key = ?", (key,)
        ).fetchone()
        connection.execute(
            """
            INSERT INTO music_candidates(
                candidate_key, source_kind, video_id, query_text, normalized_title,
                normalized_artist, duration_seconds, evidence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_key) DO UPDATE SET
                video_id = excluded.video_id,
                query_text = excluded.query_text,
                normalized_title = excluded.normalized_title,
                normalized_artist = excluded.normalized_artist,
                duration_seconds = excluded.duration_seconds,
                evidence_json = excluded.evidence_json
            """,
            (key, source_kind, video_id, query_text, title, artist, duration, json_text(evidence)),
        )
        candidate_id = connection.execute(
            "SELECT id FROM music_candidates WHERE candidate_key = ?", (key,)
        ).fetchone()["id"]
        before = connection.total_changes
        connection.execute(
            "INSERT OR IGNORE INTO event_candidate_links(event_id, candidate_id) VALUES (?, ?)",
            (event_id, candidate_id),
        )
        linked += connection.total_changes - before
        if existed is None:
            created += 1
        else:
            updated += 1

    for row in watch_rows:
        reasons = json.loads(row["reasons_json"] or "[]")
        is_strong_unknown = row["music_status"] == "unknown" and (
            float(row["music_score"] or 0) >= 0.65
            or any("music" in str(reason).casefold() for reason in reasons)
        )
        if row["music_status"] != "music" and not is_strong_unknown:
            skipped += 1
            continue
        source_title = row["metadata_title"] or row["event_title"] or ""
        inferred = infer_artist_title(source_title, row["channel_title"] or row["channel_name"])
        if not inferred.title:
            skipped += 1
            continue
        upsert_and_link(
            event_id=row["event_id"],
            source_kind="watch",
            identity=f"video:{row['video_id']}",
            video_id=row["video_id"],
            query_text=None,
            title=inferred.title,
            artist=inferred.artist,
            duration=row["duration_seconds"],
            evidence={
                "inference": inferred.method,
                "music_status": row["music_status"],
                "music_score": row["music_score"],
                "reasons": reasons,
                "source_title": source_title,
            },
        )

    for row in search_rows:
        query = _display_text(row["query_text"])
        if not _plausible_music_query(query):
            skipped += 1
            continue
        inferred = infer_artist_title(query)
        identity = f"query:{comparison_text(inferred.artist)}:{comparison_text(inferred.title)}"
        upsert_and_link(
            event_id=row["event_id"],
            source_kind="search",
            identity=identity,
            video_id=None,
            query_text=query,
            title=inferred.title,
            artist=inferred.artist,
            duration=None,
            evidence={"inference": inferred.method, "plausible_music_query": True},
        )

    connection.commit()
    return {
        "created": created,
        "updated": updated,
        "linked": linked,
        "skipped": skipped,
        "candidates": connection.execute("SELECT COUNT(*) FROM music_candidates").fetchone()[0],
    }

