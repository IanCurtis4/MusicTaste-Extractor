from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, FeatureNotFound, Tag

from .models import ActivityEvent, EventType, IngestResult

PARSER_VERSION = "1"

_MONTHS = {
    "jan": 1,
    "fev": 2,
    "mar": 3,
    "abr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "set": 9,
    "out": 10,
    "nov": 11,
    "dez": 12,
}

_TIMEZONES = {
    "BRT": timezone(timedelta(hours=-3), name="BRT"),
    "BRST": timezone(timedelta(hours=-2), name="BRST"),
    "UTC": UTC,
    "GMT": UTC,
}

_TIMESTAMP_RE = re.compile(
    r"(?P<raw>"
    r"(?P<day>\d{1,2})\s+de\s+"
    r"(?P<month>[A-Za-zÀ-ÿ.]+)\s+de\s+"
    r"(?P<year>\d{4}),\s*"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})\s+"
    r"(?P<tz>[A-Za-z][A-Za-z0-9:+-]*)"
    r")",
    re.IGNORECASE,
)


def file_fingerprint(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Return a SHA-256 fingerprint without loading the source file into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plain_token(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).lower()


def _coerce_event_type(value: EventType | str | None) -> EventType | None:
    if value is None:
        return None
    if isinstance(value, EventType):
        return value
    try:
        return EventType(value.lower())
    except (AttributeError, ValueError) as exc:
        raise ValueError("source_kind must be 'search' or 'watch'") from exc


def _event_type_from_url(url: str) -> EventType | None:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path == "/watch":
        return EventType.WATCH
    if path == "/results" and "search_query" in parse_qs(
        parsed.query, keep_blank_values=True
    ):
        return EventType.SEARCH
    return None


def _kind_from_filename(path: Path) -> EventType | None:
    name = _plain_token(path.stem)
    if "pesquisa" in name or "search" in name:
        return EventType.SEARCH
    if "visualiza" in name or "watch" in name:
        return EventType.WATCH
    return None


def _make_soup(path: Path) -> BeautifulSoup:
    # lxml is the normal, faster parser. The fallback keeps diagnostics and tests
    # usable in a minimally provisioned environment.
    with path.open("rb") as source:
        try:
            return BeautifulSoup(source, "lxml")
        except FeatureNotFound:
            source.seek(0)
            return BeautifulSoup(source, "html.parser")


def _semantic_link(card: Tag) -> tuple[Tag | None, EventType | None]:
    for anchor in card.find_all("a", href=True):
        event_type = _event_type_from_url(str(anchor.get("href", "")))
        if event_type is not None:
            return anchor, event_type
    return None, None


def _infer_file_kind(
    cards: Iterable[Tag], explicit_kind: EventType | None, path: Path
) -> EventType | None:
    if explicit_kind is not None:
        return explicit_kind
    kinds = Counter(
        event_type
        for card in cards
        for _, event_type in [_semantic_link(card)]
        if event_type is not None
    )
    if kinds:
        return kinds.most_common(1)[0][0]
    return _kind_from_filename(path)


def _parse_timestamp(card: Tag) -> tuple[str, str | None, str | None, str | None]:
    text = card.get_text(" ", strip=True)
    match = _TIMESTAMP_RE.search(text)
    if match is None:
        return "", None, None, "timestamp_missing"

    raw = match.group("raw")
    timezone_name = match.group("tz").upper()
    source_timezone = timezone_name
    source_tz = _TIMEZONES.get(timezone_name)
    if source_tz is None:
        return raw, None, source_timezone, f"unknown_timezone:{timezone_name}"

    month_token = _plain_token(match.group("month")).rstrip(".")[:3]
    month = _MONTHS.get(month_token)
    if month is None:
        return raw, None, source_timezone, "invalid_timestamp"

    try:
        local = datetime(
            int(match.group("year")),
            month,
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            tzinfo=source_tz,
        )
    except ValueError:
        return raw, None, source_timezone, "invalid_timestamp"

    return raw, local.astimezone(UTC).isoformat(), source_timezone, None


def _channel_link(card: Tag, primary: Tag | None) -> Tag | None:
    for anchor in card.find_all("a", href=True):
        if anchor is primary:
            continue
        parsed = urlparse(str(anchor.get("href", "")))
        path = parsed.path
        if path.startswith(("/channel/", "/@", "/user/", "/c/")):
            return anchor
    return None


def _identity_subject(event: ActivityEvent) -> str:
    if event.event_type is EventType.WATCH and event.video_id:
        return f"video:{event.video_id}"
    if event.event_type is EventType.SEARCH and event.query_text is not None:
        return f"query:{event.query_text}"
    if event.target_url:
        return f"url:{event.target_url}"
    return f"fallback:{event.title or ''}"


def _event_key(event: ActivityEvent, occurrence: int) -> str:
    identity = (
        event.event_type.value,
        " ".join(event.occurred_at_raw.split()),
        _identity_subject(event),
        occurrence,
    )
    payload = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_card(
    card: Tag,
    fallback_kind: EventType,
    fingerprint: str,
    ordinal: int,
) -> ActivityEvent:
    primary, linked_kind = _semantic_link(card)
    event_type = linked_kind or fallback_kind
    errors: list[str] = []

    target_url = str(primary.get("href")) if primary is not None else None
    title = primary.get_text(" ", strip=True) or None if primary is not None else None
    video_id: str | None = None
    query_text: str | None = None

    if primary is None:
        errors.append("target_url_missing")
    else:
        parsed = urlparse(target_url or "")
        parameters = parse_qs(parsed.query, keep_blank_values=True)
        if event_type is EventType.WATCH:
            video_id = next((item for item in parameters.get("v", []) if item), None)
            if video_id is None:
                errors.append("video_id_missing")
            if title is None:
                errors.append("title_missing")
        else:
            values = parameters.get("search_query", [])
            query_text = values[0] if values else None
            if not query_text:
                errors.append("query_missing")

    channel = _channel_link(card, primary) if event_type is EventType.WATCH else None
    channel_name = channel.get_text(" ", strip=True) or None if channel is not None else None
    channel_url = str(channel.get("href")) if channel is not None else None

    occurred_raw, occurred_utc, source_timezone, timestamp_error = _parse_timestamp(card)
    if timestamp_error:
        errors.append(timestamp_error)

    return ActivityEvent(
        event_key="",
        event_type=event_type,
        occurred_at_raw=occurred_raw,
        occurred_at_utc=occurred_utc,
        source_timezone=source_timezone,
        target_url=target_url,
        video_id=video_id,
        query_text=query_text,
        title=title if event_type is EventType.WATCH else None,
        channel_name=channel_name,
        channel_url=channel_url,
        source_file_fingerprint=fingerprint,
        source_ordinal=ordinal,
        parse_status="error" if errors else "ok",
        parse_error=";".join(errors) if errors else None,
    )


def _insert_event(connection: sqlite3.Connection, event: ActivityEvent) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO activity_events (
            event_key, event_type, occurred_at_raw, occurred_at_utc,
            source_timezone, target_url, video_id, query_text, title,
            channel_name, channel_url, source_file_fingerprint,
            source_ordinal, parse_status, parse_error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_key,
            event.event_type.value,
            event.occurred_at_raw,
            event.occurred_at_utc,
            event.source_timezone,
            event.target_url,
            event.video_id,
            event.query_text,
            event.title,
            event.channel_name,
            event.channel_url,
            event.source_file_fingerprint,
            event.source_ordinal,
            event.parse_status,
            event.parse_error,
        ),
    )
    return cursor.rowcount == 1


def ingest_file(
    connection: sqlite3.Connection,
    path: str | Path,
    source_kind: EventType | str | None = None,
) -> IngestResult:
    """Parse one Google Takeout HTML file and persist its activity events.

    Event types are determined from semantic YouTube URLs. ``source_kind`` is
    only a fallback for structurally damaged cards that have lost their link.
    """

    source_path = Path(path)
    fingerprint = file_fingerprint(source_path)

    previous = connection.execute(
        """
        SELECT event_count, parse_error_count
        FROM ingest_runs
        WHERE fingerprint = ?
        """,
        (fingerprint,),
    ).fetchone()
    if previous is not None:
        return IngestResult(
            path=source_path,
            fingerprint=fingerprint,
            discovered=int(previous[0]),
            inserted=0,
            duplicates=int(previous[0]),
            errors=int(previous[1]),
        )

    soup = _make_soup(source_path)
    cards = list(soup.select(".outer-cell"))
    explicit_kind = _coerce_event_type(source_kind)
    run_kind = _infer_file_kind(cards, explicit_kind, source_path)
    if run_kind is None:
        raise ValueError(
            "Could not infer source kind from semantic URLs; pass source_kind explicitly."
        )

    parsed_events = [
        _parse_card(card, run_kind, fingerprint, ordinal)
        for ordinal, card in enumerate(cards)
    ]

    occurrences: Counter[tuple[str, str, str]] = Counter()
    for event in parsed_events:
        tuple_key = (
            event.event_type.value,
            " ".join(event.occurred_at_raw.split()),
            _identity_subject(event),
        )
        occurrence = occurrences[tuple_key]
        occurrences[tuple_key] += 1
        event.event_key = _event_key(event, occurrence)

    inserted = 0
    with connection:
        connection.execute(
            """
            INSERT INTO ingest_runs (
                source_path, fingerprint, source_kind, file_size,
                event_count, inserted_count, parse_error_count, parser_version
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                str(source_path.resolve()),
                fingerprint,
                run_kind.value,
                source_path.stat().st_size,
                len(parsed_events),
                sum(event.parse_status != "ok" for event in parsed_events),
                PARSER_VERSION,
            ),
        )
        for event in parsed_events:
            inserted += _insert_event(connection, event)
        connection.execute(
            "UPDATE ingest_runs SET inserted_count = ? WHERE fingerprint = ?",
            (inserted, fingerprint),
        )

    discovered = len(parsed_events)
    return IngestResult(
        path=source_path,
        fingerprint=fingerprint,
        discovered=discovered,
        inserted=inserted,
        duplicates=discovered - inserted,
        errors=sum(event.parse_status != "ok" for event in parsed_events),
    )
