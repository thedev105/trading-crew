from pathlib import Path

from polytrading.cli import main


def test_monitor_with_no_open_positions_exits_zero(tmp_path: Path, capsys) -> None:
    from polytrading.storage.store import DuckDBStore

    db = tmp_path / "test.duckdb"
    DuckDBStore(db).close()
    exit_code = main(["trial", "paper", "monitor", "--db", str(db)])
    assert exit_code == 0
    assert "no open paper positions" in capsys.readouterr().out.lower()
