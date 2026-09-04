from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import plotly.graph_objects as go
from jinja2 import Environment, select_autoescape
from markupsafe import Markup

CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "coverage_quality.csv": ("metric", "value", "description"),
    "top_tracks.csv": (
        "recording_mbid", "track", "artist_mbid", "artist", "watch_events",
        "match_confidence", "provider",
    ),
    "top_artists.csv": (
        "artist_mbid", "artist", "artist_type", "watch_events",
        "distinct_tracks", "match_confidence",
    ),
    "top_groups.csv": (
        "artist_mbid", "group", "watch_events", "distinct_tracks", "match_confidence",
    ),
    "timeline_monthly.csv": (
        "month", "watch_events", "accepted_music_watch_events",
        "excluded_watch_events", "search_events",
    ),
    "taxonomy_distribution.csv": (
        "taxonomy", "provider", "value", "source_level", "inherited",
        "weighted_watch_events", "matched_watch_events", "mean_confidence",
    ),
    "search_interest.csv": (
        "query", "search_events", "accepted_music_match", "recording_mbid",
        "track", "artist_mbid", "artist", "match_confidence",
    ),
    "unresolved.csv": (
        "candidate_id", "source_kind", "normalized_title", "normalized_artist",
        "related_events", "status", "score", "margin", "provider",
    ),
    "analysis_features.csv": (
        "recording_mbid", "track", "artist_mbid", "artist", "artist_type",
        "release_group_mbid", "release_group", "watch_event_count",
        "search_event_count", "first_observed_at", "last_observed_at",
        "match_confidence", "taxonomy_assignment_count", "taxonomy_mean_confidence",
        "genre_tags", "style_tags", "mood_tags", "theme_tags", "tag_tags",
    ),
}

_LEVEL_ORDER = {"track": 0, "album": 1, "artist": 2}


def _canonical_artist_type(value: object) -> str | None:
    text = str(value or "").strip()
    return text.casefold().title() if text else None


def _rows(connection: sqlite3.Connection, query: str, params: Sequence[object] = ()) -> list[dict[str, object]]:
    cursor = connection.execute(query, params)
    names = [column[0] for column in cursor.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _scalar(connection: sqlite3.Connection, query: str, params: Sequence[object] = ()) -> int:
    row = connection.execute(query, params).fetchone()
    return int(row[0] if row and row[0] is not None else 0)


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    # utf-8-sig makes accented headers and values open correctly in Windows Excel.
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: "" if row.get(column) is None else row.get(column) for column in columns})


def _best_match_per_event(connection: sqlite3.Connection) -> dict[int, dict[str, object]]:
    rows = _rows(
        connection,
        """
        SELECT e.id AS event_id, e.event_type, e.occurred_at_utc,
               mc.id AS candidate_id, em.recording_mbid, em.recording_title,
               em.artist_mbid, em.artist_name, em.artist_type,
               em.release_group_mbid, em.release_group_title,
               em.score, em.provider
          FROM activity_events e
          JOIN event_candidate_links ecl ON ecl.event_id = e.id
          JOIN music_candidates mc ON mc.id = ecl.candidate_id
          JOIN entity_matches em ON em.candidate_id = mc.id
         WHERE em.status = 'accepted'
         ORDER BY e.id, em.score DESC, mc.id
        """,
    )
    # Defensive even though the current model normally links one candidate per event:
    # one activity event always contributes at most one unit to analytics.
    selected: dict[int, dict[str, object]] = {}
    for row in rows:
        selected.setdefault(int(row["event_id"]), row)
    return selected


def _rankings(
    accepted_events: Mapping[int, Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    tracks: dict[tuple[object, ...], dict[str, object]] = {}
    artists: dict[tuple[object, ...], dict[str, object]] = {}
    artist_tracks: defaultdict[tuple[object, ...], set[tuple[object, ...]]] = defaultdict(set)

    for row in accepted_events.values():
        if row["event_type"] != "watch":
            continue
        track_key = (row["recording_mbid"] or f"candidate:{row['candidate_id']}",)
        item = tracks.setdefault(track_key, {
            "recording_mbid": row["recording_mbid"],
            "track": row["recording_title"],
            "artist_mbid": row["artist_mbid"],
            "artist": row["artist_name"],
            "watch_events": 0,
            "confidence_total": 0.0,
            "provider": row["provider"],
        })
        item["watch_events"] = int(item["watch_events"]) + 1
        item["confidence_total"] = float(item["confidence_total"]) + float(row["score"] or 0)

        artist_type = _canonical_artist_type(row["artist_type"])
        artist_key = (
            row["artist_mbid"]
            or (str(row["artist_name"]).casefold() if row["artist_name"] else f"candidate:{row['candidate_id']}"),
        )
        artist = artists.setdefault(artist_key, {
            "artist_mbid": row["artist_mbid"],
            "artist": row["artist_name"],
            "artist_type": artist_type,
            "watch_events": 0,
            "confidence_total": 0.0,
        })
        artist["watch_events"] = int(artist["watch_events"]) + 1
        artist["confidence_total"] = float(artist["confidence_total"]) + float(row["score"] or 0)
        artist_tracks[artist_key].add(track_key)

    top_tracks: list[dict[str, object]] = []
    for item in tracks.values():
        count = int(item.pop("watch_events"))
        confidence = float(item.pop("confidence_total")) / count
        top_tracks.append({**item, "watch_events": count, "match_confidence": round(confidence, 6)})
    top_tracks.sort(key=lambda row: (-int(row["watch_events"]), str(row["artist"] or "").casefold(), str(row["track"] or "").casefold()))

    top_artists: list[dict[str, object]] = []
    for key, item in artists.items():
        count = int(item.pop("watch_events"))
        confidence = float(item.pop("confidence_total")) / count
        top_artists.append({
            **item,
            "watch_events": count,
            "distinct_tracks": len(artist_tracks[key]),
            "match_confidence": round(confidence, 6),
        })
    top_artists.sort(key=lambda row: (-int(row["watch_events"]), str(row["artist"] or "").casefold()))
    top_groups = [
        {
            "artist_mbid": row["artist_mbid"], "group": row["artist"],
            "watch_events": row["watch_events"], "distinct_tracks": row["distinct_tracks"],
            "match_confidence": row["match_confidence"],
        }
        for row in top_artists
        if str(row["artist_type"] or "").casefold() == "group"
    ]
    return top_tracks, top_artists, top_groups


def _month(value: object) -> str:
    text = str(value or "")
    return text[:7] if len(text) >= 7 else "sem_data"


def _timeline(
    connection: sqlite3.Connection,
    accepted_events: Mapping[int, Mapping[str, object]],
) -> list[dict[str, object]]:
    totals: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {"watch_events": 0, "search_events": 0}
    )
    event_rows = _rows(connection, "SELECT id, event_type, occurred_at_utc FROM activity_events")
    for row in event_rows:
        totals[_month(row["occurred_at_utc"])][f"{row['event_type']}_events"] += 1
    accepted_watch: defaultdict[str, int] = defaultdict(int)
    for row in accepted_events.values():
        if row["event_type"] == "watch":
            accepted_watch[_month(row["occurred_at_utc"])] += 1
    result = []
    for month in sorted(totals, key=lambda value: (value == "sem_data", value)):
        watch = totals[month]["watch_events"]
        accepted = accepted_watch[month]
        result.append({
            "month": month,
            "watch_events": watch,
            "accepted_music_watch_events": accepted,
            "excluded_watch_events": watch - accepted,
            "search_events": totals[month]["search_events"],
        })
    return result


def _selected_taxonomies(
    connection: sqlite3.Connection,
    accepted_events: Mapping[int, Mapping[str, object]],
) -> dict[int, list[dict[str, object]]]:
    assignments = _rows(
        connection,
        """SELECT provider, taxonomy, value, value_norm, entity_type, entity_id,
                         source_level, inherited, confidence
                    FROM taxonomy_assignments""",
    )
    candidates: dict[int, Mapping[str, object]] = {}
    for row in accepted_events.values():
        candidates.setdefault(int(row["candidate_id"]), row)

    result: dict[int, list[dict[str, object]]] = {}
    for candidate_id, match in candidates.items():
        relevant: list[dict[str, object]] = []
        ids = {
            "track": match["recording_mbid"],
            "album": match["release_group_mbid"],
            "artist": match["artist_mbid"],
        }
        recording_id = str(match["recording_mbid"]) if match["recording_mbid"] else None
        for assignment in assignments:
            level = str(assignment["source_level"]).casefold()
            entity_id = str(assignment["entity_id"])
            # Current providers project every selected source level onto the
            # target recording.  Accept source-native rows as well, which keeps
            # imported/older taxonomies compatible.
            projected_to_recording = recording_id is not None and entity_id == recording_id
            linked_to_source = ids.get(level) is not None and entity_id == str(ids[level])
            if projected_to_recording or linked_to_source:
                relevant.append(assignment)

        grouped: defaultdict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
        for assignment in relevant:
            grouped[(str(assignment["provider"]), str(assignment["taxonomy"]))].append(assignment)

        chosen: list[dict[str, object]] = []
        for values in grouped.values():
            best_level = min(_LEVEL_ORDER[str(item["source_level"]).casefold()] for item in values)
            level_values = [
                dict(item) for item in values
                if _LEVEL_ORDER[str(item["source_level"]).casefold()] == best_level
            ]
            deduplicated: dict[str, dict[str, object]] = {}
            for item in level_values:
                identity = str(item["value_norm"])
                previous = deduplicated.get(identity)
                if previous is None or float(item["confidence"] or 0) > float(previous["confidence"] or 0):
                    deduplicated[identity] = item
            for item in deduplicated.values():
                # Album/artist tags are inherited when projected onto a recording,
                # regardless of how the provider describes their source row.
                item["effective_inherited"] = int(
                    bool(item["inherited"]) or str(item["source_level"]).casefold() != "track"
                )
            chosen.extend(deduplicated.values())
        result[candidate_id] = chosen
    return result


def _taxonomy_distribution(
    accepted_events: Mapping[int, Mapping[str, object]],
    selected: Mapping[int, Sequence[Mapping[str, object]]],
) -> list[dict[str, object]]:
    aggregates: dict[tuple[object, ...], dict[str, float]] = {}
    for event in accepted_events.values():
        if event["event_type"] != "watch":
            continue
        assignments = selected.get(int(event["candidate_id"]), ())
        groups: defaultdict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
        for assignment in assignments:
            groups[(str(assignment["provider"]), str(assignment["taxonomy"]))].append(assignment)
        for (provider, taxonomy), values in groups.items():
            # Each watch is one unit inside a taxonomy, regardless of tag count.
            weight = 1.0 / len(values)
            for value in values:
                key = (
                    taxonomy, provider, value["value"], value["source_level"],
                    int(value["effective_inherited"]),
                )
                aggregate = aggregates.setdefault(key, {
                    "weighted": 0.0, "events": 0.0, "confidence_weighted": 0.0,
                })
                aggregate["weighted"] += weight
                aggregate["events"] += 1
                aggregate["confidence_weighted"] += float(value["confidence"] or 0) * weight

    result = []
    for key, aggregate in aggregates.items():
        taxonomy, provider, value, source_level, inherited = key
        weighted = aggregate["weighted"]
        result.append({
            "taxonomy": taxonomy,
            "provider": provider,
            "value": value,
            "source_level": source_level,
            "inherited": inherited,
            "weighted_watch_events": round(weighted, 6),
            "matched_watch_events": int(aggregate["events"]),
            "mean_confidence": round(aggregate["confidence_weighted"] / weighted, 6) if weighted else 0,
        })
    result.sort(key=lambda row: (str(row["taxonomy"]), -float(row["weighted_watch_events"]), str(row["value"]).casefold()))
    return result


def _search_interest(
    connection: sqlite3.Connection,
    accepted_events: Mapping[int, Mapping[str, object]],
) -> list[dict[str, object]]:
    events = _rows(
        connection,
        "SELECT id, query_text FROM activity_events WHERE event_type = 'search' ORDER BY id",
    )
    grouped: dict[tuple[object, ...], dict[str, object]] = {}
    for event in events:
        match = accepted_events.get(int(event["id"]))
        key = (
            event["query_text"], match["recording_mbid"] if match else None,
            match["artist_mbid"] if match else None,
        )
        item = grouped.setdefault(key, {
            "query": event["query_text"],
            "search_events": 0,
            "accepted_music_match": int(match is not None),
            "recording_mbid": match["recording_mbid"] if match else None,
            "track": match["recording_title"] if match else None,
            "artist_mbid": match["artist_mbid"] if match else None,
            "artist": match["artist_name"] if match else None,
            "confidence_total": 0.0,
        })
        item["search_events"] = int(item["search_events"]) + 1
        if match:
            item["confidence_total"] = float(item["confidence_total"]) + float(match["score"] or 0)
    result = []
    for item in grouped.values():
        count = int(item["search_events"])
        confidence = float(item.pop("confidence_total")) / count if item["accepted_music_match"] else None
        result.append({**item, "match_confidence": round(confidence, 6) if confidence is not None else None})
    result.sort(key=lambda row: (-int(row["search_events"]), str(row["query"] or "").casefold()))
    return result


def _unresolved(connection: sqlite3.Connection) -> list[dict[str, object]]:
    return _rows(
        connection,
        """
        SELECT mc.id AS candidate_id, mc.source_kind, mc.normalized_title,
               mc.normalized_artist, COUNT(DISTINCT ecl.event_id) AS related_events,
               COALESCE(em.status, 'unresolved') AS status, em.score, em.margin, em.provider
          FROM music_candidates mc
          LEFT JOIN event_candidate_links ecl ON ecl.candidate_id = mc.id
          LEFT JOIN entity_matches em ON em.candidate_id = mc.id
         WHERE em.status IS NULL OR em.status <> 'accepted'
         GROUP BY mc.id, mc.source_kind, mc.normalized_title, mc.normalized_artist,
                  em.status, em.score, em.margin, em.provider
         ORDER BY related_events DESC, mc.id
        """,
    )


def _analysis_features(
    accepted_events: Mapping[int, Mapping[str, object]],
    selected_taxonomies: Mapping[int, Sequence[Mapping[str, object]]],
) -> list[dict[str, object]]:
    features: dict[tuple[object, ...], dict[str, object]] = {}
    feature_candidates: defaultdict[tuple[object, ...], set[int]] = defaultdict(set)
    timestamps: defaultdict[tuple[object, ...], list[str]] = defaultdict(list)
    for event in accepted_events.values():
        # The recording MBID is the canonical feature identity. Display metadata
        # can differ in casing across independently resolved watch/search candidates.
        key = (event["recording_mbid"] or f"candidate:{event['candidate_id']}",)
        feature = features.setdefault(key, {
            "recording_mbid": event["recording_mbid"], "track": event["recording_title"],
            "artist_mbid": event["artist_mbid"], "artist": event["artist_name"],
            "artist_type": _canonical_artist_type(event["artist_type"]), "release_group_mbid": event["release_group_mbid"],
            "release_group": event["release_group_title"], "watch_event_count": 0,
            "search_event_count": 0, "confidence_total": 0.0, "event_count": 0,
        })
        counter = "watch_event_count" if event["event_type"] == "watch" else "search_event_count"
        feature[counter] = int(feature[counter]) + 1
        feature["confidence_total"] = float(feature["confidence_total"]) + float(event["score"] or 0)
        feature["event_count"] = int(feature["event_count"]) + 1
        feature_candidates[key].add(int(event["candidate_id"]))
        if event["occurred_at_utc"]:
            timestamps[key].append(str(event["occurred_at_utc"]))

    result: list[dict[str, object]] = []
    for key, feature in features.items():
        taxonomy_values: defaultdict[str, set[str]] = defaultdict(set)
        taxonomy_confidences: list[float] = []
        assignment_identities: set[tuple[object, ...]] = set()
        for candidate_id in feature_candidates[key]:
            for assignment in selected_taxonomies.get(candidate_id, ()):
                identity = (
                    assignment["provider"], assignment["taxonomy"], assignment["value_norm"],
                    assignment["source_level"], assignment["entity_id"],
                )
                if identity in assignment_identities:
                    continue
                assignment_identities.add(identity)
                taxonomy_values[str(assignment["taxonomy"])].add(str(assignment["value"]))
                taxonomy_confidences.append(float(assignment["confidence"] or 0))
        count = int(feature.pop("event_count"))
        confidence_total = float(feature.pop("confidence_total"))
        seen = sorted(timestamps[key])
        result.append({
            **feature,
            "first_observed_at": seen[0] if seen else None,
            "last_observed_at": seen[-1] if seen else None,
            "match_confidence": round(confidence_total / count, 6),
            "taxonomy_assignment_count": len(assignment_identities),
            "taxonomy_mean_confidence": (
                round(sum(taxonomy_confidences) / len(taxonomy_confidences), 6)
                if taxonomy_confidences else None
            ),
            **{
                f"{kind}_tags": "|".join(sorted(taxonomy_values[kind], key=str.casefold))
                for kind in ("genre", "style", "mood", "theme", "tag")
            },
        })
    result.sort(key=lambda row: (-int(row["watch_event_count"]), -int(row["search_event_count"]), str(row["track"] or "").casefold()))
    return result


def _coverage(
    connection: sqlite3.Connection,
    accepted_events: Mapping[int, Mapping[str, object]],
    unresolved_count: int,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    counts = {
        "total_events": _scalar(connection, "SELECT COUNT(*) FROM activity_events"),
        "watch_events": _scalar(connection, "SELECT COUNT(*) FROM activity_events WHERE event_type='watch'"),
        "search_events": _scalar(connection, "SELECT COUNT(*) FROM activity_events WHERE event_type='search'"),
        "parse_error_events": _scalar(connection, "SELECT COUNT(*) FROM activity_events WHERE parse_status<>'ok'"),
        "accepted_candidates": _scalar(connection, "SELECT COUNT(*) FROM entity_matches WHERE status='accepted'"),
        "review_candidates": _scalar(connection, "SELECT COUNT(*) FROM entity_matches WHERE status='review'"),
        "rejected_candidates": _scalar(connection, "SELECT COUNT(*) FROM entity_matches WHERE status='rejected'"),
        "unresolved_candidates": unresolved_count,
        "accepted_watch_events": sum(row["event_type"] == "watch" for row in accepted_events.values()),
        "accepted_search_events": sum(row["event_type"] == "search" for row in accepted_events.values()),
    }
    counts["excluded_watch_events"] = counts["watch_events"] - counts["accepted_watch_events"]
    descriptions = {
        "total_events": "Todos os eventos importados do Takeout.",
        "watch_events": "Visualizações; cada evento vale uma unidade de consumo provável.",
        "search_events": "Pesquisas, mantidas separadas de reproduções.",
        "parse_error_events": "Eventos preservados com erro estrutural ou de data.",
        "accepted_candidates": "Candidatos com resolução aceita.",
        "review_candidates": "Candidatos aguardando revisão manual.",
        "rejected_candidates": "Candidatos rejeitados.",
        "unresolved_candidates": "Candidatos não aceitos, inclusive sem tentativa de resolução.",
        "accepted_watch_events": "Visualizações ligadas a um match aceito e incluídas nos rankings.",
        "accepted_search_events": "Pesquisas ligadas a um match aceito; nunca somadas a plays.",
        "excluded_watch_events": "Visualizações fora dos rankings por falta de match aceito.",
    }
    return ([
        {"metric": key, "value": value, "description": descriptions[key]}
        for key, value in counts.items()
    ], counts)


_REPORT_TEMPLATE = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Análise do gosto musical</title>
  <style>
    :root { color-scheme: light; --ink:#172033; --muted:#667085; --card:#fff; --bg:#f3f6fb; --accent:#6d4aff; }
    * { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
    main { max-width:1180px; margin:auto; padding:32px 20px 60px; } h1,h2 { line-height:1.15; } h1 { margin-bottom:4px; }
    .muted { color:var(--muted); } .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:24px 0; }
    .card,.panel { background:var(--card); border-radius:14px; box-shadow:0 3px 18px #1d293914; padding:18px; }
    .card strong { display:block; font-size:28px; color:var(--accent); } .panel { margin:18px 0; overflow:auto; }
    table { width:100%; border-collapse:collapse; } th,td { padding:9px 10px; border-bottom:1px solid #e4e7ec; text-align:left; }
    th { color:#475467; font-size:13px; } code { background:#eef2f8; padding:2px 5px; border-radius:4px; }
  </style>
</head>
<body><main>
  <h1>Análise do gosto musical</h1>
  <p class="muted">Gerado em {{ generated_at }}. O relatório trata visualizações como consumo provável, não como prova de reprodução completa.</p>
  <section class="cards">
    <div class="card"><span>Visualizações</span><strong>{{ counts.watch_events }}</strong></div>
    <div class="card"><span>Consumo musical aceito</span><strong>{{ counts.accepted_watch_events }}</strong></div>
    <div class="card"><span>Pesquisas (interesse)</span><strong>{{ counts.search_events }}</strong></div>
    <div class="card"><span>Visualizações excluídas</span><strong>{{ counts.excluded_watch_events }}</strong></div>
    <div class="card"><span>Candidatos não resolvidos</span><strong>{{ counts.unresolved_candidates }}</strong></div>
  </section>
  <section class="panel"><h2>Cobertura, evolução e taxonomias</h2>{{ charts }}</section>
  <section class="panel"><h2>Faixas mais observadas</h2>
    <table><thead><tr><th>Faixa</th><th>Artista</th><th>Visualizações</th><th>Confiança</th></tr></thead><tbody>
    {% for row in tracks %}<tr><td>{{ row.track or '—' }}</td><td>{{ row.artist or '—' }}</td><td>{{ row.watch_events }}</td><td>{{ '%.1f%%'|format(row.match_confidence * 100) }}</td></tr>{% else %}<tr><td colspan="4">Nenhum match aceito.</td></tr>{% endfor %}
    </tbody></table>
  </section>
  <section class="panel"><h2>Artistas e grupos</h2>
    <table><thead><tr><th>Artista</th><th>Tipo</th><th>Visualizações</th><th>Faixas distintas</th></tr></thead><tbody>
    {% for row in artists %}<tr><td>{{ row.artist or '—' }}</td><td>{{ row.artist_type or '—' }}</td><td>{{ row.watch_events }}</td><td>{{ row.distinct_tracks }}</td></tr>{% else %}<tr><td colspan="4">Nenhum match aceito.</td></tr>{% endfor %}
    </tbody></table>
    <h2>Grupos/bandas</h2>
    <table><thead><tr><th>Grupo</th><th>Visualizações</th><th>Faixas distintas</th><th>Confiança</th></tr></thead><tbody>
    {% for row in groups %}<tr><td>{{ row.group or '—' }}</td><td>{{ row.watch_events }}</td><td>{{ row.distinct_tracks }}</td><td>{{ '%.1f%%'|format(row.match_confidence * 100) }}</td></tr>{% else %}<tr><td colspan="4">Nenhum grupo identificado.</td></tr>{% endfor %}
    </tbody></table>
  </section>
  <section class="panel"><h2>Taxonomias e proveniência</h2>
    <table><thead><tr><th>Taxonomia</th><th>Valor</th><th>Provedor</th><th>Nível</th><th>Herdada</th><th>Peso</th><th>Confiança</th></tr></thead><tbody>
    {% for row in taxonomy %}<tr><td>{{ row.taxonomy }}</td><td>{{ row.value }}</td><td>{{ row.provider }}</td><td>{{ row.source_level }}</td><td>{{ 'sim' if row.inherited else 'não' }}</td><td>{{ '%.2f'|format(row.weighted_watch_events) }}</td><td>{{ '%.1f%%'|format(row.mean_confidence * 100) }}</td></tr>{% else %}<tr><td colspan="7">Nenhuma taxonomia disponível.</td></tr>{% endfor %}
    </tbody></table>
  </section>
  <section class="panel"><h2>Interesse e intenção de pesquisa</h2>
    <p class="muted">Estas contagens não representam reproduções e não entram nos rankings de consumo.</p>
    <table><thead><tr><th>Consulta</th><th>Pesquisas</th><th>Match musical aceito</th><th>Faixa</th><th>Artista</th></tr></thead><tbody>
    {% for row in searches %}<tr><td>{{ row.query or '—' }}</td><td>{{ row.search_events }}</td><td>{{ 'sim' if row.accepted_music_match else 'não' }}</td><td>{{ row.track or '—' }}</td><td>{{ row.artist or '—' }}</td></tr>{% else %}<tr><td colspan="5">Nenhuma pesquisa importada.</td></tr>{% endfor %}
    </tbody></table>
  </section>
  <section class="panel"><h2>Qualidade, exclusões e proveniência</h2>
    <p>Somente <code>entity_matches.status = accepted</code> entra nos rankings. {{ counts.excluded_watch_events }} visualizações foram excluídas deles; {{ counts.review_candidates }} candidatos aguardam revisão e {{ counts.rejected_candidates }} foram rejeitados.</p>
    <p>Pesquisas representam intenção/interesse e aparecem separadas: elas nunca incrementam contagens de visualização. Tags são atribuídas por MBID e usam o nível mais específico disponível (<code>track &gt; album &gt; artist</code>). Tags de álbum/artista são marcadas como herdadas, e cada evento distribui peso <code>1/n</code> entre as tags da mesma taxonomia/provedor.</p>
    <p>Proveniência: Google Takeout (eventos), MusicBrainz/revisão manual (entidades) e os provedores indicados em <code>taxonomy_distribution.csv</code> (taxonomias). Consulte <code>coverage_quality.csv</code> e <code>unresolved.csv</code> para a auditoria completa.</p>
  </section>
</main></body></html>"""


def _charts(
    timeline: Sequence[Mapping[str, object]],
    artists: Sequence[Mapping[str, object]],
    taxonomy: Sequence[Mapping[str, object]],
) -> Markup:
    figures: list[go.Figure] = []
    timeline_figure = go.Figure()
    timeline_figure.add_bar(
        name="Consumo musical aceito", x=[row["month"] for row in timeline],
        y=[row["accepted_music_watch_events"] for row in timeline],
    )
    timeline_figure.add_scatter(
        name="Pesquisas (interesse)", x=[row["month"] for row in timeline],
        y=[row["search_events"] for row in timeline], mode="lines+markers",
    )
    timeline_figure.update_layout(title="Evolução mensal", barmode="group", template="plotly_white")
    figures.append(timeline_figure)

    top = list(artists[:15])[::-1]
    artist_figure = go.Figure(go.Bar(
        x=[row["watch_events"] for row in top], y=[row["artist"] or "Sem artista" for row in top],
        orientation="h",
    ))
    artist_figure.update_layout(title="Artistas por consumo provável", template="plotly_white")
    figures.append(artist_figure)

    top_taxonomy = sorted(taxonomy, key=lambda row: float(row["weighted_watch_events"]), reverse=True)[:20][::-1]
    taxonomy_figure = go.Figure(go.Bar(
        x=[row["weighted_watch_events"] for row in top_taxonomy],
        y=[f"{row['taxonomy']}: {row['value']}" for row in top_taxonomy], orientation="h",
    ))
    taxonomy_figure.update_layout(title="Taxonomias ponderadas (peso 1/n)", template="plotly_white")
    figures.append(taxonomy_figure)

    fragments = []
    for index, figure in enumerate(figures):
        fragments.append(figure.to_html(
            full_html=False,
            include_plotlyjs=index == 0,
            config={"displaylogo": False, "responsive": True},
        ))
    return Markup("\n".join(fragments))


def generate_report(connection: sqlite3.Connection, output_dir: str | Path) -> dict[str, object]:
    """Generate auditable CSVs and a self-contained Portuguese HTML report.

    The returned summary intentionally contains only aggregate counts and output
    paths so CLI logs never disclose titles, artists, queries, URLs, or credentials.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    accepted_events = _best_match_per_event(connection)
    top_tracks, top_artists, top_groups = _rankings(accepted_events)
    timeline = _timeline(connection, accepted_events)
    selected_taxonomies = _selected_taxonomies(connection, accepted_events)
    taxonomy_distribution = _taxonomy_distribution(accepted_events, selected_taxonomies)
    search_interest = _search_interest(connection, accepted_events)
    unresolved = _unresolved(connection)
    analysis_features = _analysis_features(accepted_events, selected_taxonomies)
    coverage, counts = _coverage(connection, accepted_events, len(unresolved))

    datasets: dict[str, list[dict[str, object]]] = {
        "coverage_quality.csv": coverage,
        "top_tracks.csv": top_tracks,
        "top_artists.csv": top_artists,
        "top_groups.csv": top_groups,
        "timeline_monthly.csv": timeline,
        "taxonomy_distribution.csv": taxonomy_distribution,
        "search_interest.csv": search_interest,
        "unresolved.csv": unresolved,
        "analysis_features.csv": analysis_features,
    }
    for filename, rows in datasets.items():
        _write_csv(output / filename, CSV_COLUMNS[filename], rows)

    environment = Environment(autoescape=select_autoescape(("html", "xml")))
    html = environment.from_string(_REPORT_TEMPLATE).render(
        generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        counts=counts,
        charts=_charts(timeline, top_artists, taxonomy_distribution),
        tracks=top_tracks[:20],
        artists=top_artists[:20],
        groups=top_groups[:20],
        taxonomy=taxonomy_distribution[:30],
        searches=search_interest[:20],
    )
    report_path = output / "report.html"
    report_path.write_text(html, encoding="utf-8")

    return {
        "output_dir": str(output.resolve()),
        "report_file": str(report_path.resolve()),
        "csv_files": len(datasets),
        "total_events": counts["total_events"],
        "watch_events": counts["watch_events"],
        "search_events": counts["search_events"],
        "accepted_watch_events": counts["accepted_watch_events"],
        "excluded_watch_events": counts["excluded_watch_events"],
        "unresolved_candidates": counts["unresolved_candidates"],
    }
