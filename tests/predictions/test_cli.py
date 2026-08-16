from datetime import timedelta
from pathlib import Path

import httpx
import pytest

import polytrading.cli as cli
from polytrading.cli import build_parser, main
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.domain_helpers import NOW
from tests.predictions.manifest_helpers import venue_manifest


def test_predictions_collect_is_a_subcommand_tree_not_a_venue_flag() -> None:
    parsed = build_parser().parse_args(
        ["predictions", "collect", "polymarket", "--db", "var/predictions.duckdb"]
    )
    assert parsed.command == "predictions"
    assert parsed.predictions_command == "collect"
    assert parsed.predictions_collect_command == "polymarket"
    assert not hasattr(parsed, "venue")


def test_predictions_command_does_not_collide_with_existing_top_level_names() -> None:
    existing = {"replay", "dashboard", "carry", "fees", "funding", "trial", "collect", "ai"}
    parsed = build_parser().parse_args(["predictions", "venues", "status", "--db", "x.duckdb"])
    assert parsed.command == "predictions"
    assert "predictions" not in existing


def test_predictions_venues_status_reports_missing_manifests(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    exit_code = main(["predictions", "venues", "status", "--db", str(database), "--format", "json"])
    assert exit_code == 0


def test_predictions_venues_status_rejects_a_missing_database(tmp_path: Path) -> None:
    exit_code = main(["predictions", "venues", "status", "--db", str(tmp_path / "missing.duckdb")])
    assert exit_code == 2


def test_collect_polymarket_exits_two_before_any_network_call_when_watchlisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def reject_network(*_a: object, **_k: object) -> httpx.AsyncClient:
        raise AssertionError("collect must not open a network client when gate-rejected")

    monkeypatch.setattr(cli, "make_public_http_client", reject_network)
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.WATCHLIST,
            reviewed_at=NOW - timedelta(hours=1),
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "polymarket", "--db", str(database)])
    assert exit_code == 2


def test_predictions_health_exits_zero_when_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.KALSHI,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(
        [
            "predictions",
            "health",
            "--db",
            str(database),
            "--as-of",
            "2026-08-15T12:00:00Z",
            "--format",
            "json",
        ]
    )
    # No book evidence collected yet, so both venues are NOT_COLLECTED -> exit 1.
    assert exit_code == 1


def test_predictions_health_rejects_a_missing_database(tmp_path: Path) -> None:
    exit_code = main(["predictions", "health", "--db", str(tmp_path / "missing.duckdb")])
    assert exit_code == 2


def test_predictions_health_rejects_invalid_as_of(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    exit_code = main(["predictions", "health", "--db", str(database), "--as-of", "not-a-timestamp"])
    assert exit_code == 2


def test_predictions_venues_status_text_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    assert main(["predictions", "venues", "status", "--db", str(database)]) == 0
    output = capsys.readouterr().out
    assert "polymarket" in output
    assert "kalshi" in output
