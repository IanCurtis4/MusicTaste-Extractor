from __future__ import annotations

import random
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Tag

from music_taste.models import EntityLevel, TaxonomyAssignment, TaxonomyKind
from music_taste.taxonomy import persist_assignments, select_most_specific

ALLMUSIC_ROOT = "https://www.allmusic.com"
ROBOTS_URL = f"{ALLMUSIC_ROOT}/robots.txt"
USER_AGENT = "MusicTasteExtractor/0.1 (personal, non-commercial research)"
MAX_ATTEMPTS = 3


class AllMusicError(RuntimeError):
    """A safe, content-free failure from the experimental provider."""


class AllMusicTermsError(AllMusicError):
    pass


class AllMusicRobotsError(AllMusicError):
    pass


class AllMusicBlockedError(AllMusicError):
    pass


class AllMusicSelectorDriftError(AllMusicError):
    pass


@dataclass(frozen=True, slots=True)
class _WorkItem:
    entity_type: str
    entity_id: str
    target_level: EntityLevel
    recording_title: str | None
    artist_name: str | None
    urls: tuple[tuple[EntityLevel, str, str], ...]


_SECTION_NAMES: dict[str, TaxonomyKind] = {
    "genre": TaxonomyKind.GENRE,
    "genres": TaxonomyKind.GENRE,
    "style": TaxonomyKind.STYLE,
    "styles": TaxonomyKind.STYLE,
    "mood": TaxonomyKind.MOOD,
    "moods": TaxonomyKind.MOOD,
    "theme": TaxonomyKind.THEME,
    "themes": TaxonomyKind.THEME,
}
_BLOCKED_RE = re.compile(
    r"captcha|verify (?:that )?you are human|access denied|cloudflare|"
    r"security challenge|unusual traffic|sign in to continue|login required",
    re.IGNORECASE,
)


def _section_kind(node: Tag) -> TaxonomyKind | None:
    candidates: list[str] = []
    classes = node.get("class") or []
    candidates.extend(str(value) for value in classes)
    for attribute in ("id", "data-section", "data-testid"):
        value = node.get(attribute)
        if value:
            candidates.append(str(value))
    for candidate in candidates:
        words = re.split(r"[^a-z]+", candidate.casefold())
        for word in words:
            if word in _SECTION_NAMES:
                return _SECTION_NAMES[word]
    return None


def _values_from_section(section: Tag) -> list[str]:
    value_nodes = section.select("a, li, [class*='tag'], [class*='item']")
    if not value_nodes:
        value_nodes = [section]
    values: list[str] = []
    for node in value_nodes:
        text = " ".join(node.get_text(" ", strip=True).split())
        if text and text.casefold() not in _SECTION_NAMES:
            values.append(text)
    return values


def _assert_not_blocked(html: str) -> None:
    sample = html[:200_000]
    soup = BeautifulSoup(sample, "lxml")
    if _BLOCKED_RE.search(soup.get_text(" ", strip=True)):
        raise AllMusicBlockedError("AllMusic returned a CAPTCHA, challenge, or login wall")
    if soup.select_one("input[type='password'], form[action*='login'], form[action*='captcha']"):
        raise AllMusicBlockedError("AllMusic returned a CAPTCHA, challenge, or login wall")


def parse_allmusic_html(
    html: str,
    *,
    entity_type: str,
    entity_id: str,
    source_level: EntityLevel | str,
    source_url: str | None = None,
    target_level: EntityLevel | str = EntityLevel.TRACK,
) -> list[TaxonomyAssignment]:
    """Pure parser for the taxonomy portions of plausible AllMusic layouts."""
    _assert_not_blocked(html)
    soup = BeautifulSoup(html, "lxml")
    found_sections: list[tuple[TaxonomyKind, Tag]] = []

    for node in soup.find_all(["div", "section", "ul", "dl"]):
        kind = _section_kind(node)
        if kind is not None:
            found_sections.append((kind, node))

    # Some layouts use a heading/definition term followed by a generic container.
    for label in soup.find_all(["h2", "h3", "h4", "dt"]):
        name = " ".join(label.get_text(" ", strip=True).split()).casefold().rstrip(":")
        kind = _SECTION_NAMES.get(name)
        sibling = label.find_next_sibling() if kind is not None else None
        if kind is not None and isinstance(sibling, Tag):
            found_sections.append((kind, sibling))

    if not found_sections:
        raise AllMusicSelectorDriftError(
            "AllMusic taxonomy selectors no longer match the returned page"
        )

    level = EntityLevel(source_level)
    target = EntityLevel(target_level)
    assignments: list[TaxonomyAssignment] = []
    for kind, section in found_sections:
        for value in _values_from_section(section):
            assignments.append(
                TaxonomyAssignment(
                    provider="allmusic",
                    taxonomy=kind,
                    value=value,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    source_level=level,
                    inherited=level != target,
                    source_url=source_url,
                    confidence=1.0,
                )
            )
    return select_most_specific(assignments, target_level=target)


class _AllMusicClient:
    def __init__(
        self,
        client: Any,
        *,
        sleep: Callable[[float], None],
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.client = client
        self.sleep = sleep
        self.jitter = jitter
        self.requests = 0
        self._made_page_request = False

    def robots(self) -> RobotFileParser:
        try:
            self.requests += 1
            response = self.client.get(ROBOTS_URL, headers={"User-Agent": USER_AGENT})
        except (httpx.HTTPError, OSError) as exc:
            raise AllMusicRobotsError("AllMusic robots.txt could not be verified") from exc
        if response.status_code != 200 or not response.text.strip():
            raise AllMusicRobotsError("AllMusic robots.txt could not be verified")
        meaningful_lines = [
            line.strip()
            for line in response.text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not any(line.casefold().startswith("user-agent:") for line in meaningful_lines):
            raise AllMusicRobotsError("AllMusic robots.txt was ambiguous or invalid")
        parser = RobotFileParser()
        parser.set_url(ROBOTS_URL)
        parser.parse(response.text.splitlines())
        return parser

    def page(self, url: str, robots: RobotFileParser | None) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc.casefold() not in {
            "allmusic.com",
            "www.allmusic.com",
        }:
            raise AllMusicError("Refusing a non-AllMusic or non-HTTPS source URL")
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            raise AllMusicRobotsError("AllMusic robots.txt disallows the requested page")

        for attempt in range(MAX_ATTEMPTS):
            if self._made_page_request:
                self.sleep(self.jitter(3.0, 5.0))
            self._made_page_request = True
            try:
                self.requests += 1
                response = self.client.get(url, headers={"User-Agent": USER_AGENT})
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 == MAX_ATTEMPTS:
                    raise AllMusicError("AllMusic request failed after bounded retries") from exc
                self.sleep(min(float(2**attempt), 5.0))
                continue

            if response.status_code in {429} or 500 <= response.status_code < 600:
                if attempt + 1 == MAX_ATTEMPTS:
                    if response.status_code == 429:
                        raise AllMusicBlockedError("AllMusic persistently rate-limited the request")
                    raise AllMusicError("AllMusic request failed after bounded retries")
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else float(2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                self.sleep(min(max(delay, 0.0), 5.0))
                continue
            if response.status_code in {401, 403}:
                raise AllMusicBlockedError("AllMusic refused access to the requested page")
            if response.is_error:
                raise AllMusicError(f"AllMusic returned HTTP {response.status_code}")
            _assert_not_blocked(response.text)
            return response.text
        raise AssertionError("unreachable")


def _load_work(connection: sqlite3.Connection) -> list[_WorkItem]:
    rows = connection.execute(
        """SELECT DISTINCT recording_mbid, recording_title, artist_mbid, artist_name,
                          release_group_mbid
           FROM entity_matches
           WHERE status='accepted' AND (recording_mbid IS NOT NULL OR artist_mbid IS NOT NULL)
           ORDER BY recording_mbid, artist_mbid"""
    ).fetchall()
    work: list[_WorkItem] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if row["recording_mbid"]:
            entity_type, entity_id, target = "track", str(row["recording_mbid"]), EntityLevel.TRACK
        else:
            entity_type, entity_id, target = "artist", str(row["artist_mbid"]), EntityLevel.ARTIST
        identity = (entity_type, entity_id)
        if identity in seen:
            continue
        seen.add(identity)
        sources = (
            (
                (EntityLevel.TRACK, row["recording_mbid"]),
                (EntityLevel.ALBUM, row["release_group_mbid"]),
                (EntityLevel.ARTIST, row["artist_mbid"]),
            )
            if target == EntityLevel.TRACK
            else ((EntityLevel.ARTIST, row["artist_mbid"]),)
        )
        urls: list[tuple[EntityLevel, str, str]] = []
        for level, source_id in sources:
            if not source_id:
                continue
            links = connection.execute(
                """SELECT url FROM external_links
                   WHERE entity_type=? AND entity_id=? AND lower(provider)='allmusic'
                   ORDER BY url""",
                (level.value, source_id),
            ).fetchall()
            urls.extend((level, str(source_id), str(link["url"])) for link in links)
        work.append(
            _WorkItem(
                entity_type=entity_type,
                entity_id=entity_id,
                target_level=target,
                recording_title=row["recording_title"],
                artist_name=row["artist_name"],
                urls=tuple(urls),
            )
        )
    return work


def _first_result_url(html: str, *, target_level: EntityLevel) -> str | None:
    _assert_not_blocked(html)
    soup = BeautifulSoup(html, "lxml")
    needles = {
        EntityLevel.TRACK: ("/song/",),
        EntityLevel.ALBUM: ("/album/",),
        EntityLevel.ARTIST: ("/artist/",),
    }[target_level]
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href"))
        if any(needle in href for needle in needles):
            return urljoin(ALLMUSIC_ROOT, href)
    return None


def _target_url(
    item: _WorkItem,
    provider: _AllMusicClient,
    robots: RobotFileParser | None,
) -> tuple[EntityLevel, str, str] | None:
    if item.urls:
        # Links were accumulated in specificity order.
        return item.urls[0]
    query = " ".join(filter(None, (item.artist_name, item.recording_title))).strip()
    if not query:
        return None
    search_url = f"{ALLMUSIC_ROOT}/search/all/{quote(query, safe='')}"
    search_html = provider.page(search_url, robots)
    result_url = _first_result_url(search_html, target_level=item.target_level)
    if result_url is None:
        return None
    return item.target_level, item.entity_id, result_url


def enrich_allmusic(
    connection: sqlite3.Connection,
    acknowledge_terms_risk: bool = False,
    limit: int | None = None,
    client: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
    robots_check: bool = True,
) -> dict[str, int]:
    """Run the opt-in, conservative AllMusic adapter.

    It stores normalized taxonomy rows and their public source URLs only.  Raw
    response HTML is never written to SQLite or the filesystem.
    """
    if not acknowledge_terms_risk:
        raise AllMusicTermsError(
            "AllMusic enrichment requires acknowledge_terms_risk=True"
        )
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")

    all_work = _load_work(connection)
    discovered = len(all_work)
    work = [
        item
        for item in all_work
        if connection.execute(
            """SELECT 1 FROM taxonomy_assignments
               WHERE provider='allmusic' AND entity_type=? AND entity_id=? LIMIT 1""",
            (item.entity_type, item.entity_id),
        ).fetchone()
        is None
    ]
    work = work[:limit] if limit is not None else work
    owns_client = client is None
    http_client = client or httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=True)
    provider = _AllMusicClient(http_client, sleep=sleep)
    processed = skipped = inserted = 0
    try:
        robots = provider.robots() if robots_check and work else None
        for item in work:
            resolved = _target_url(item, provider, robots)
            if resolved is None:
                skipped += 1
                continue
            source_level, source_entity_id, url = resolved
            html = provider.page(url, robots)
            assignments = parse_allmusic_html(
                html,
                entity_type=item.entity_type,
                entity_id=item.entity_id,
                source_level=source_level,
                source_url=url,
                target_level=item.target_level,
            )
            inserted += persist_assignments(connection, assignments)
            connection.execute(
                """INSERT OR IGNORE INTO external_links
                   (entity_type, entity_id, provider, url, fetched_at)
                   VALUES (?, ?, 'allmusic', ?, CURRENT_TIMESTAMP)""",
                (source_level.value, source_entity_id, url),
            )
            connection.commit()
            processed += 1
    finally:
        if owns_client:
            http_client.close()
    return {
        "discovered": discovered,
        "processed": processed,
        "skipped": skipped + max(discovered - len(work), 0),
        "assignments": inserted,
        "requests": provider.requests,
    }
