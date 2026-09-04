import csv

import pytest

from music_taste.db import connect, initialize
from music_taste.review import export_review, import_review

RID = "11111111-1111-4111-8111-111111111111"
AID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NEW_RID = "22222222-2222-4222-8222-222222222222"


def review_db():
    db = connect(":memory:")
    initialize(db)
    for key in ("one", "two"):
        cursor = db.execute(
            """INSERT INTO music_candidates(candidate_key,source_kind,normalized_title,evidence_json)
               VALUES (?, 'watch', 'Track', '{}')""",
            (key,),
        )
        db.execute(
            """INSERT INTO entity_matches(
                candidate_id,recording_mbid,recording_title,artist_mbid,artist_name,
                score,runner_up_score,margin,status,method,provider
            ) VALUES (?, ?, 'Track', ?, 'Artist', .8, .75, .05, 'review', 'search', 'musicbrainz')""",
            (cursor.lastrowid, RID, AID),
        )
    db.commit()
    return db


def test_review_csv_round_trip(tmp_path):
    db = review_db()
    path = tmp_path / "review.csv"
    assert export_review(db, path) == 2
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
        fields = list(rows[0])
    rows[0]["decision"] = "accept"
    rows[0]["notes"] = "looks right"
    rows[1]["decision"] = "replace"
    rows[1]["recording_mbid"] = NEW_RID
    rows[1]["recording_title"] = "Correct Track"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    result = import_review(db, path)
    assert result["accepted"] == 1
    assert result["replaced"] == 1
    matches = db.execute("SELECT status,method,recording_mbid FROM entity_matches ORDER BY candidate_id").fetchall()
    assert [row["status"] for row in matches] == ["accepted", "accepted"]
    assert matches[1]["recording_mbid"] == NEW_RID
    assert db.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 2


def test_invalid_csv_is_atomic(tmp_path):
    db = review_db()
    path = tmp_path / "review.csv"
    export_review(db, path)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
        fields = list(rows[0])
    rows[0]["decision"] = "accept"
    rows[1]["decision"] = "replace"
    rows[1]["recording_mbid"] = "not-a-mbid"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="invalid recording_mbid"):
        import_review(db, path)
    assert db.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 0

