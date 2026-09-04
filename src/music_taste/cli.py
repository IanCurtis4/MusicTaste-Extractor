from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Annotated

import typer

from music_taste.db import database
from music_taste.models import EventType

app = typer.Typer(
    name="music-taste",
    help="Analisa localmente históricos do YouTube exportados pelo Google Takeout.",
    no_args_is_help=True,
)
enrich_app = typer.Typer(help="Enriquece os eventos com metadados externos.")
resolve_app = typer.Typer(help="Resolve candidatos em entidades musicais canônicas.")
review_app = typer.Typer(help="Exporta e importa decisões de revisão manual.")
app.add_typer(enrich_app, name="enrich")
app.add_typer(resolve_app, name="resolve")
app.add_typer(review_app, name="review")


DB_OPTION = Annotated[
    Path,
    typer.Option("--db", help="Banco SQLite local.", dir_okay=False),
]


def _summary(label: str, result: object) -> None:
    """Print counts and statuses, never raw titles, queries, URLs or credentials."""
    if is_dataclass(result) and not isinstance(result, type):
        values = asdict(result)
    elif hasattr(result, "__dict__"):
        values = vars(result)
    elif isinstance(result, dict):
        values = result
    else:
        typer.echo(f"{label}: {result}")
        return
    safe = {
        key: value
        for key, value in values.items()
        if key
        not in {
            "path",
            "source_path",
            "query_text",
            "title",
            "url",
            "target_url",
            "api_key",
        }
    }
    typer.echo(f"{label}: " + ", ".join(f"{key}={value}" for key, value in safe.items()))


@app.command("ingest")
def ingest_command(
    db_path: DB_OPTION = Path("data/music_taste.sqlite"),
    search_history: Annotated[
        Path | None,
        typer.Option("--search-history", exists=True, dir_okay=False),
    ] = None,
    watch_history: Annotated[
        Path | None,
        typer.Option("--watch-history", exists=True, dir_okay=False),
    ] = None,
) -> None:
    """Importa um ou ambos os HTMLs do Takeout."""
    if search_history is None and watch_history is None:
        raise typer.BadParameter("Informe --search-history e/ou --watch-history.")
    from music_taste.ingest import ingest_file

    with database(db_path) as connection:
        if search_history is not None:
            _summary(
                "pesquisas",
                ingest_file(connection, search_history, EventType.SEARCH),
            )
        if watch_history is not None:
            _summary(
                "visualizações",
                ingest_file(connection, watch_history, EventType.WATCH),
            )


@enrich_app.command("youtube")
def youtube_command(
    db_path: DB_OPTION = Path("data/music_taste.sqlite"),
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", envvar="YOUTUBE_API_KEY", hide_input=True),
    ] = None,
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
) -> None:
    """Busca metadados públicos de vídeos na YouTube Data API."""
    from music_taste.youtube import enrich_youtube

    with database(db_path) as connection:
        _summary(
            "youtube",
            enrich_youtube(connection, api_key=api_key, refresh=refresh),
        )


@resolve_app.command("musicbrainz")
def musicbrainz_command(
    db_path: DB_OPTION = Path("data/music_taste.sqlite"),
    contact: Annotated[
        str | None,
        typer.Option("--contact", envvar="MUSIC_TASTE_CONTACT"),
    ] = None,
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
    limit: Annotated[int | None, typer.Option(min=1)] = None,
    taxonomy: Annotated[
        bool,
        typer.Option("--taxonomy/--no-taxonomy", help="Busca tags e relações canônicas."),
    ] = True,
) -> None:
    """Cria candidatos e os resolve usando o MusicBrainz."""
    from music_taste.musicbrainz import resolve_musicbrainz
    from music_taste.normalize import build_candidates
    from music_taste.taxonomy import enrich_musicbrainz_taxonomies

    with database(db_path) as connection:
        _summary("candidatos", build_candidates(connection))
        _summary(
            "musicbrainz",
            resolve_musicbrainz(
                connection,
                contact=contact,
                refresh=refresh,
                limit=limit,
            ),
        )
        if taxonomy:
            _summary(
                "taxonomias musicbrainz",
                enrich_musicbrainz_taxonomies(
                    connection,
                    contact=contact,
                    refresh=refresh,
                    limit=limit,
                ),
            )


@review_app.command("export")
def review_export_command(
    output: Annotated[Path, typer.Argument(dir_okay=False)],
    db_path: DB_OPTION = Path("data/music_taste.sqlite"),
) -> None:
    """Exporta matches ambíguos para um CSV editável."""
    from music_taste.review import export_review

    with database(db_path) as connection:
        _summary("revisão exportada", {"rows": export_review(connection, output)})


@review_app.command("import")
def review_import_command(
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    db_path: DB_OPTION = Path("data/music_taste.sqlite"),
) -> None:
    """Aplica decisões de um CSV anteriormente exportado."""
    from music_taste.review import import_review

    with database(db_path) as connection:
        _summary("revisão importada", import_review(connection, source))


@enrich_app.command("allmusic")
def allmusic_command(
    db_path: DB_OPTION = Path("data/music_taste.sqlite"),
    acknowledge_terms_risk: Annotated[
        bool,
        typer.Option(
            "--acknowledge-terms-risk",
            help="Confirma ciência de que os termos atuais proíbem scraping.",
        ),
    ] = False,
    limit: Annotated[int | None, typer.Option(min=1)] = None,
) -> None:
    """Executa o adaptador experimental e desativado por padrão do AllMusic."""
    if not acknowledge_terms_risk:
        raise typer.BadParameter(
            "O AllMusic exige --acknowledge-terms-risk; consulte os termos antes de continuar."
        )
    from music_taste.allmusic import enrich_allmusic

    with database(db_path) as connection:
        _summary(
            "allmusic",
            enrich_allmusic(
                connection,
                acknowledge_terms_risk=True,
                limit=limit,
            ),
        )


@app.command("report")
def report_command(
    db_path: DB_OPTION = Path("data/music_taste.sqlite"),
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", file_okay=False),
    ] = Path("output"),
) -> None:
    """Gera CSVs analíticos e relatório HTML autocontido."""
    from music_taste.reporting import generate_report

    with database(db_path) as connection:
        _summary("relatório", generate_report(connection, output_dir))


@app.command("run")
def run_command(
    search_history: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    watch_history: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    db_path: DB_OPTION = Path("data/music_taste.sqlite"),
    output_dir: Annotated[Path, typer.Option(file_okay=False)] = Path("output"),
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", envvar="YOUTUBE_API_KEY", hide_input=True),
    ] = None,
    contact: Annotated[
        str | None,
        typer.Option("--contact", envvar="MUSIC_TASTE_CONTACT"),
    ] = None,
) -> None:
    """Executa o pipeline padrão; nunca ativa o AllMusic."""
    api_key = api_key or os.getenv("YOUTUBE_API_KEY")
    contact = contact or os.getenv("MUSIC_TASTE_CONTACT")
    if not api_key:
        raise typer.BadParameter("Defina YOUTUBE_API_KEY ou informe --api-key.")
    if not contact:
        raise typer.BadParameter("Defina MUSIC_TASTE_CONTACT ou informe --contact.")

    from music_taste.ingest import ingest_file
    from music_taste.musicbrainz import resolve_musicbrainz
    from music_taste.normalize import build_candidates
    from music_taste.reporting import generate_report
    from music_taste.taxonomy import enrich_musicbrainz_taxonomies
    from music_taste.youtube import enrich_youtube

    with database(db_path) as connection:
        _summary("pesquisas", ingest_file(connection, search_history, EventType.SEARCH))
        _summary("visualizações", ingest_file(connection, watch_history, EventType.WATCH))
        _summary("youtube", enrich_youtube(connection, api_key=api_key))
        _summary("candidatos", build_candidates(connection))
        _summary(
            "musicbrainz",
            resolve_musicbrainz(connection, contact=contact),
        )
        _summary(
            "taxonomias musicbrainz",
            enrich_musicbrainz_taxonomies(connection, contact=contact),
        )
        _summary("relatório", generate_report(connection, output_dir))
