from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import httpx

from music_taste.models import EntityLevel, TaxonomyAssignment, TaxonomyKind

_LEVEL_ORDER = {
    EntityLevel.ARTIST: 0,
    EntityLevel.ALBUM: 1,
    EntityLevel.TRACK: 2,
}


def normalize_taxonomy_value(value: str) -> str:
    """Return the stable, human-language-independent identity for a tag."""
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def select_most_specific(
    assignments: Iterable[TaxonomyAssignment],
    *,
    target_level: EntityLevel | str = EntityLevel.TRACK,
) -> list[TaxonomyAssignment]:
    """Select the best available level independently for each taxonomy kind.

    A track genre can therefore coexist with an album mood when the source does
    not publish track-level moods.  Values are de-duplicated after Unicode and
    whitespace normalization.  ``inherited`` is derived here rather than trusted
    from a provider.
    """
    target = EntityLevel(target_level)
    materialized = list(assignments)
    best_level: dict[TaxonomyKind, EntityLevel] = {}
    for assignment in materialized:
        taxonomy = TaxonomyKind(assignment.taxonomy)
        level = EntityLevel(assignment.source_level)
        if _LEVEL_ORDER[level] > _LEVEL_ORDER[target]:
            continue
        current = best_level.get(taxonomy)
        if current is None or _LEVEL_ORDER[level] > _LEVEL_ORDER[current]:
            best_level[taxonomy] = level

    selected: list[TaxonomyAssignment] = []
    seen: set[tuple[TaxonomyKind, str]] = set()
    for assignment in materialized:
        taxonomy = TaxonomyKind(assignment.taxonomy)
        level = EntityLevel(assignment.source_level)
        value_norm = normalize_taxonomy_value(assignment.value)
        if not value_norm or taxonomy not in best_level or level != best_level[taxonomy]:
            continue
        identity = (taxonomy, value_norm)
        if identity in seen:
            continue
        seen.add(identity)
        selected.append(
            replace(
                assignment,
                taxonomy=taxonomy,
                source_level=level,
                inherited=level != target,
            )
        )
    return selected


_INSERT_ASSIGNMENT = """
INSERT INTO taxonomy_assignments (
    provider, taxonomy, value, value_norm, entity_type, entity_id,
    source_level, inherited, source_url, confidence, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
ON CONFLICT(provider, taxonomy, value_norm, entity_type, entity_id, source_level)
DO UPDATE SET
    value=excluded.value,
    inherited=excluded.inherited,
    source_url=excluded.source_url,
    confidence=excluded.confidence,
    fetched_at=CURRENT_TIMESTAMP
"""


def persist_assignment(
    connection: sqlite3.Connection, assignment: TaxonomyAssignment
) -> bool:
    """Upsert one assignment, returning whether a new logical row was added."""
    value = " ".join(unicodedata.normalize("NFKC", assignment.value).split())
    value_norm = normalize_taxonomy_value(value)
    if not value_norm:
        raise ValueError("taxonomy value cannot be empty")
    taxonomy = TaxonomyKind(assignment.taxonomy).value
    level = EntityLevel(assignment.source_level).value
    existed = connection.execute(
        """SELECT 1 FROM taxonomy_assignments
           WHERE provider=? AND taxonomy=? AND value_norm=?
             AND entity_type=? AND entity_id=? AND source_level=?""",
        (
            assignment.provider,
            taxonomy,
            value_norm,
            assignment.entity_type,
            assignment.entity_id,
            level,
        ),
    ).fetchone()
    connection.execute(
        _INSERT_ASSIGNMENT,
        (
            assignment.provider,
            taxonomy,
            value,
            value_norm,
            assignment.entity_type,
            assignment.entity_id,
            level,
            int(assignment.inherited),
            assignment.source_url,
            float(assignment.confidence),
        ),
    )
    return existed is None


def persist_assignments(
    connection: sqlite3.Connection,
    assignments: Iterable[TaxonomyAssignment],
) -> int:
    """Idempotently persist assignments and return the number of new rows."""
    return sum(persist_assignment(connection, item) for item in assignments)


def _named_values(value: Any) -> Iterable[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def musicbrainz_assignments_from_entity(
    entity: Mapping[str, Any],
    *,
    entity_type: str,
    entity_id: str,
    source_level: EntityLevel | str,
    source_url: str | None = None,
    target_level: EntityLevel | str = EntityLevel.TRACK,
) -> list[TaxonomyAssignment]:
    """Convert an already-fetched MusicBrainz entity/response into assignments.

    MusicBrainz exposes both curated ``genres`` and folksonomy ``tags``.  This
    helper performs no I/O, so callers can reuse resolver responses without an
    additional request.
    """
    level = EntityLevel(source_level)
    target = EntityLevel(target_level)
    result: list[TaxonomyAssignment] = []
    for field, kind in (("genres", TaxonomyKind.GENRE), ("tags", TaxonomyKind.TAG)):
        for name in _named_values(entity.get(field)):
            if not normalize_taxonomy_value(name):
                continue
            result.append(
                TaxonomyAssignment(
                    provider="musicbrainz",
                    taxonomy=kind,
                    value=name,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    source_level=level,
                    inherited=level != target,
                    source_url=source_url,
                    confidence=1.0,
                )
            )
    return select_most_specific(result, target_level=target)


# Descriptive aliases keep the small persistence API easy to discover.
store_taxonomy_assignment = persist_assignment
store_taxonomy_assignments = persist_assignments


MUSICBRAINZ_ROOT = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_CLIENT = "music-taste-extractor/0.1.0"
_MUSICBRAINZ_RETRYABLE = {429, 500, 502, 503, 504}


def _musicbrainz_cache_key(entity_type: str, entity_id: str) -> str:
    material = f"taxonomy:{entity_type}:{entity_id}:tags+url-rels"
    return "musicbrainz:" + hashlib.sha256(material.encode()).hexdigest()


class _MusicBrainzTaxonomyClient:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        contact: str,
        client: Any,
        sleep: Callable[[float], None],
        rate_limit_seconds: float = 1.0,
        max_attempts: int = 4,
    ) -> None:
        if not contact.strip():
            raise ValueError("MUSIC_TASTE_CONTACT is required for the MusicBrainz User-Agent")
        self.connection = connection
        self.client = client
        self.sleep = sleep
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.max_attempts = max(1, max_attempts)
        self.headers = {
            "User-Agent": f"{MUSICBRAINZ_CLIENT} ({contact.strip()})",
            "Accept": "application/json",
        }
        self.requests = 0
        self._has_requested = False

    def get_entity(
        self, entity_type: str, entity_id: str, *, refresh: bool = False
    ) -> Mapping[str, Any]:
        cache_key = _musicbrainz_cache_key(entity_type, entity_id)
        if not refresh:
            cached = self.connection.execute(
                """SELECT response_json FROM http_cache
                   WHERE cache_key=? AND provider='musicbrainz' AND status_code=200""",
                (cache_key,),
            ).fetchone()
            if cached is not None and cached["response_json"]:
                payload = json.loads(cached["response_json"])
                return payload if isinstance(payload, Mapping) else {}

        url_type = "release-group" if entity_type == "album" else (
            "recording" if entity_type == "track" else entity_type
        )
        url = f"{MUSICBRAINZ_ROOT}/{url_type}/{entity_id}"
        for attempt in range(self.max_attempts):
            if self._has_requested:
                self.sleep(self.rate_limit_seconds)
            self._has_requested = True
            try:
                self.requests += 1
                response = self.client.get(
                    url,
                    params={"inc": "tags+url-rels", "fmt": "json"},
                    headers=self.headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt + 1 == self.max_attempts:
                    raise RuntimeError("MusicBrainz taxonomy request failed") from exc
                self.sleep(min(float(2**attempt), 8.0))
                continue
            if response.status_code == 200:
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise RuntimeError("MusicBrainz returned an invalid taxonomy response")
                serialized = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                self.connection.execute(
                    """INSERT INTO http_cache
                       (cache_key, provider, status_code, response_json, error, fetched_at)
                       VALUES (?, 'musicbrainz', 200, ?, NULL, CURRENT_TIMESTAMP)
                       ON CONFLICT(cache_key) DO UPDATE SET
                           status_code=200, response_json=excluded.response_json,
                           error=NULL, fetched_at=CURRENT_TIMESTAMP""",
                    (cache_key, serialized),
                )
                return payload
            if response.status_code not in _MUSICBRAINZ_RETRYABLE:
                response.raise_for_status()
            if attempt + 1 == self.max_attempts:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else float(2**attempt)
            except ValueError:
                delay = float(2**attempt)
            self.sleep(min(max(delay, 0.0), 8.0))
        raise AssertionError("unreachable")


def _allmusic_relation_urls(entity: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    relations = entity.get("relations")
    if not isinstance(relations, Sequence) or isinstance(relations, (str, bytes)):
        return urls
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        url_data = relation.get("url")
        resource = url_data.get("resource") if isinstance(url_data, Mapping) else None
        relation_type = str(relation.get("type") or "").casefold()
        if isinstance(resource, str) and (
            relation_type == "allmusic" or "allmusic.com/" in resource.casefold()
        ):
            urls.append(resource)
    return list(dict.fromkeys(urls))


def enrich_musicbrainz_taxonomies(
    connection: sqlite3.Connection,
    contact: str | None = None,
    client: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    refresh: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Fetch tags and AllMusic relationships for accepted canonical matches."""
    resolved_contact = contact or os.getenv("MUSIC_TASTE_CONTACT") or ""
    if not resolved_contact.strip():
        raise ValueError("MUSIC_TASTE_CONTACT is required for the MusicBrainz User-Agent")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    rows = connection.execute(
        """SELECT DISTINCT recording_mbid, artist_mbid, release_group_mbid
           FROM entity_matches
           WHERE status='accepted' AND recording_mbid IS NOT NULL
           ORDER BY recording_mbid"""
    ).fetchall()
    discovered = len(rows)
    rows = rows[:limit] if limit is not None else rows
    owns_client = client is None
    http_client = client or httpx.Client(timeout=httpx.Timeout(30.0))
    provider = _MusicBrainzTaxonomyClient(
        connection,
        contact=resolved_contact,
        client=http_client,
        sleep=sleep,
    )
    processed = errors = inserted = links = 0
    try:
        for row in rows:
            target_id = str(row["recording_mbid"])
            gathered: list[TaxonomyAssignment] = []
            failed = False
            sources = (
                (EntityLevel.TRACK, row["recording_mbid"]),
                (EntityLevel.ALBUM, row["release_group_mbid"]),
                (EntityLevel.ARTIST, row["artist_mbid"]),
            )
            for level, source_id in sources:
                if not source_id:
                    continue
                try:
                    payload = provider.get_entity(
                        level.value, str(source_id), refresh=refresh
                    )
                # Preserve completed entities and continue after an isolated
                # provider/transport failure; the summary reports the error.
                except Exception:  # noqa: BLE001
                    errors += 1
                    failed = True
                    break
                gathered.extend(
                    musicbrainz_assignments_from_entity(
                        payload,
                        entity_type="track",
                        entity_id=target_id,
                        source_level=level,
                        target_level=EntityLevel.TRACK,
                        source_url=f"https://musicbrainz.org/{'release-group' if level == EntityLevel.ALBUM else ('recording' if level == EntityLevel.TRACK else 'artist')}/{source_id}",
                    )
                )
                for url in _allmusic_relation_urls(payload):
                    cursor = connection.execute(
                        """INSERT OR IGNORE INTO external_links
                           (entity_type, entity_id, provider, url, fetched_at)
                           VALUES (?, ?, 'allmusic', ?, CURRENT_TIMESTAMP)""",
                        (level.value, str(source_id), url),
                    )
                    links += max(cursor.rowcount, 0)
            if failed:
                connection.rollback()
                continue
            inserted += persist_assignments(
                connection,
                select_most_specific(gathered, target_level=EntityLevel.TRACK),
            )
            connection.commit()
            processed += 1
    finally:
        if owns_client:
            http_client.close()
    return {
        "discovered": discovered,
        "processed": processed,
        "skipped": max(discovered - len(rows), 0),
        "assignments": inserted,
        "external_links": links,
        "requests": provider.requests,
        "errors": errors,
    }
