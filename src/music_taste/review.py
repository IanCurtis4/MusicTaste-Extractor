from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

FIELDNAMES = [
    "candidate_id",
    "source_kind",
    "normalized_artist",
    "normalized_title",
    "duration_seconds",
    "score",
    "runner_up_score",
    "margin",
    "recording_mbid",
    "recording_title",
    "artist_mbid",
    "artist_name",
    "artist_type",
    "release_group_mbid",
    "release_group_title",
    "decision",
    "notes",
]
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def export_review(connection: sqlite3.Connection, path: str | Path) -> int:
    rows = connection.execute(
        """
        SELECT mc.id AS candidate_id, mc.source_kind, mc.normalized_artist,
               mc.normalized_title, mc.duration_seconds, em.score,
               em.runner_up_score, em.margin, em.recording_mbid,
               em.recording_title, em.artist_mbid, em.artist_name, em.artist_type,
               em.release_group_mbid, em.release_group_title
          FROM music_candidates mc
          JOIN entity_matches em ON em.candidate_id = mc.id
         WHERE em.status = 'review'
         ORDER BY mc.id
        """
    ).fetchall()
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            row_values = dict(row)
            record = {field: row_values.get(field, "") for field in FIELDNAMES}
            record["decision"] = ""
            record["notes"] = ""
            writer.writerow(record)
    return len(rows)


def _optional(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _validate_mbid(name: str, value: str | None, *, required: bool = False) -> str | None:
    value = _optional(value)
    if required and not value:
        raise ValueError(f"{name} is required")
    if value and not _UUID_RE.fullmatch(value):
        raise ValueError(f"invalid {name}: {value!r}")
    return value


def import_review(connection: sqlite3.Connection, path: str | Path) -> dict[str, int]:
    """Apply reviewed CSV decisions atomically; blank decisions are ignored."""
    input_path = Path(path)
    with input_path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not {"candidate_id", "decision"}.issubset(reader.fieldnames):
            raise ValueError("review CSV must contain candidate_id and decision columns")
        rows = list(reader)

    actions: list[tuple[int, str, dict[str, str], str | None]] = []
    seen: set[int] = set()
    for line_number, row in enumerate(rows, start=2):
        decision = (row.get("decision") or "").strip().casefold()
        if not decision:
            continue
        if decision not in {"accept", "reject", "replace"}:
            raise ValueError(f"line {line_number}: decision must be accept, reject, or replace")
        try:
            candidate_id = int((row.get("candidate_id") or "").strip())
        except ValueError as exc:
            raise ValueError(f"line {line_number}: invalid candidate_id") from exc
        if candidate_id in seen:
            raise ValueError(f"line {line_number}: duplicate candidate_id {candidate_id}")
        seen.add(candidate_id)
        match = connection.execute(
            "SELECT * FROM entity_matches WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if match is None:
            raise ValueError(f"line {line_number}: unknown candidate_id {candidate_id}")
        recording_mbid = _validate_mbid(
            "recording_mbid",
            row.get("recording_mbid") if decision == "replace" else match["recording_mbid"],
            required=decision in {"accept", "replace"},
        )
        artist_mbid = _validate_mbid(
            "artist_mbid",
            row.get("artist_mbid") if decision == "replace" else match["artist_mbid"],
        )
        release_group_mbid = _validate_mbid(
            "release_group_mbid",
            row.get("release_group_mbid") if decision == "replace" else match["release_group_mbid"],
        )
        values = dict(row)
        values["recording_mbid"] = recording_mbid or ""
        values["artist_mbid"] = artist_mbid or ""
        values["release_group_mbid"] = release_group_mbid or ""
        actions.append((candidate_id, decision, values, _optional(row.get("notes"))))

    counts = {"accepted": 0, "rejected": 0, "replaced": 0, "ignored": len(rows) - len(actions)}
    with connection:
        for candidate_id, decision, row, notes in actions:
            if decision == "reject":
                connection.execute(
                    """
                    UPDATE entity_matches
                       SET status='rejected', method='manual_reject', reviewed_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE candidate_id=?
                    """,
                    (candidate_id,),
                )
                counts["rejected"] += 1
            elif decision == "accept":
                connection.execute(
                    """
                    UPDATE entity_matches
                       SET status='accepted', method='manual_accept', reviewed_at=CURRENT_TIMESTAMP,
                           updated_at=CURRENT_TIMESTAMP
                     WHERE candidate_id=?
                    """,
                    (candidate_id,),
                )
                counts["accepted"] += 1
            else:
                connection.execute(
                    """
                    UPDATE entity_matches SET
                        recording_mbid=?, recording_title=?, artist_mbid=?, artist_name=?,
                        artist_type=?, release_group_mbid=?, release_group_title=?, score=1,
                        runner_up_score=NULL, margin=1, status='accepted', method='manual_replace',
                        provider='manual', reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE candidate_id=?
                    """,
                    (
                        row["recording_mbid"], _optional(row.get("recording_title")),
                        _optional(row.get("artist_mbid")), _optional(row.get("artist_name")),
                        _optional(row.get("artist_type")), _optional(row.get("release_group_mbid")),
                        _optional(row.get("release_group_title")), candidate_id,
                    ),
                )
                counts["replaced"] += 1
            connection.execute(
                """
                INSERT INTO review_decisions(candidate_id, decision, recording_mbid, artist_mbid, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    candidate_id, decision, _optional(row.get("recording_mbid")),
                    _optional(row.get("artist_mbid")), notes,
                ),
            )
    counts["processed"] = len(actions)
    return counts
