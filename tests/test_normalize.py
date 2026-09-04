import json

from music_taste.db import connect, initialize
from music_taste.normalize import (
    build_candidates,
    comparison_text,
    infer_artist_title,
    normalize_title,
)


def test_normalization_and_inference_preserve_meaning():
    assert normalize_title("  Björk – Jóga (Official Music Video) ") == "Björk – Jóga"
    assert comparison_text("Jóga") == "joga"
    inferred = infer_artist_title("Björk - Jóga [Official Audio]", "BjörkVEVO")
    assert inferred.artist == "Björk"
    assert inferred.title == "Jóga"
    assert inferred.method == "title_separator"


def test_build_candidates_deduplicates_and_links_events():
    db = connect(":memory:")
    initialize(db)
    events = [
        ("e1", "watch", "raw", "v1", None, "Original event title", "Artist - Topic"),
        ("e2", "watch", "raw", "v1", None, "Original event title", "Artist - Topic"),
        ("e3", "search", "raw", None, "Other Artist - Song Name", None, None),
        ("e4", "search", "raw", None, "python tutorial", None, None),
    ]
    for ordinal, (key, kind, raw, video, query, title, channel) in enumerate(events):
        db.execute(
            """INSERT INTO activity_events(
                event_key,event_type,occurred_at_raw,video_id,query_text,title,channel_name,
                source_file_fingerprint,source_ordinal
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (key, kind, raw, video, query, title, channel, "fp", ordinal),
        )
    db.execute(
        """INSERT INTO video_metadata(
            video_id,title,channel_title,duration_seconds,availability,music_status,music_score,reasons_json
        ) VALUES ('v1','Artist - Track (Official Audio)','Artist - Topic',201,'available','music',1,'["category_10"]')"""
    )
    result = build_candidates(db)
    assert result["created"] == 2
    assert result["linked"] == 3
    assert db.execute("SELECT COUNT(*) FROM music_candidates").fetchone()[0] == 2
    watch = db.execute("SELECT * FROM music_candidates WHERE source_kind='watch'").fetchone()
    assert watch["normalized_title"] == "Track"
    assert watch["normalized_artist"] == "Artist"
    assert json.loads(watch["evidence_json"])["source_title"] == "Artist - Track (Official Audio)"
    again = build_candidates(db)
    assert again["created"] == 0
    assert again["linked"] == 0

