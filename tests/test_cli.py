from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from music_taste.cli import app

runner = CliRunner()


def test_root_help_lists_the_public_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for command in ("ingest", "enrich", "resolve", "review", "report", "run"):
        assert command in result.output


def test_nested_command_help_is_available() -> None:
    for arguments in (
        ["enrich", "--help"],
        ["enrich", "youtube", "--help"],
        ["resolve", "musicbrainz", "--help"],
        ["review", "export", "--help"],
    ):
        result = runner.invoke(app, arguments)
        assert result.exit_code == 0, f"{arguments}: {result.output}"


def test_allmusic_refuses_to_run_without_explicit_acknowledgement(tmp_path: Path) -> None:
    database = tmp_path / "must-not-exist.sqlite"

    result = runner.invoke(
        app,
        ["enrich", "allmusic", "--db", str(database)],
    )

    assert result.exit_code != 0
    assert "--acknowledge-terms-risk" in result.output
    assert not database.exists()


def test_ingest_cli_summary_does_not_echo_history_content(tmp_path: Path) -> None:
    private_query = "SEGREDO-musical-único"
    source = tmp_path / "private-history.html"
    source.write_text(
        """
        <div class="outer-cell">
          <a href="https://www.youtube.com/results?search_query=SEGREDO-musical-%C3%BAnico">
            SEGREDO-musical-único
          </a>
          3 de set. de 2026, 11:21:19 BRT
        </div>
        """,
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "ingest",
            "--db",
            str(tmp_path / "music.sqlite"),
            "--search-history",
            str(source),
        ],
    )

    assert result.exit_code == 0, result.output
    assert private_query not in result.output
    assert source.name not in result.output
    assert "query_text" not in result.output
    assert "target_url" not in result.output
