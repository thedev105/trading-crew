from pathlib import Path

from polytrading.cli import main


def test_paper_open_without_confirm_writes_nothing_and_prints_preview(
    tmp_path: Path, capsys
) -> None:
    db = tmp_path / "test.duckdb"
    exit_code = main(
        [
            "trial",
            "paper",
            "open",
            "--evaluation-id",
            "00000000-0000-0000-0000-000000000000",
            "--db",
            str(db),
        ]
    )
    assert exit_code != 0
    assert "confirm" in capsys.readouterr().err.lower()


def test_paper_open_rejects_missing_evaluation(tmp_path: Path, capsys) -> None:
    from polytrading.storage.store import DuckDBStore

    db = tmp_path / "test.duckdb"
    DuckDBStore(db).close()
    exit_code = main(
        [
            "trial",
            "paper",
            "open",
            "--evaluation-id",
            "00000000-0000-0000-0000-000000000000",
            "--db",
            str(db),
            "--confirm",
        ]
    )
    assert exit_code != 0
    error = capsys.readouterr().err.lower()
    assert "not found" in error or "shadow" in error
