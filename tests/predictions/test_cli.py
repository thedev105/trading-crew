import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import polytrading.cli as cli
import polytrading.predictions.cli as predictions_cli
from polytrading.cli import build_parser, main
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.domain_helpers import NOW
from tests.predictions.manifest_helpers import venue_manifest

MARKETS_FIXTURE = Path("tests/fixtures/predictions/polymarket/gamma_markets_page_1.json")
LIMITLESS_MARKETS_FIXTURE = Path("tests/fixtures/predictions/limitless/markets_active_page_1.json")


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


def test_predictions_venues_status_reports_missing_manifests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    exit_code = main(["predictions", "venues", "status", "--db", str(database), "--format", "json"])
    assert exit_code == 0

    output = json.loads(capsys.readouterr().out)
    limitless_row = next(row for row in output["venues"] if row["venue"] == "limitless")
    assert limitless_row["collection_allowed"] is False
    assert limitless_row["reason"] == "MANIFEST_NOT_FOUND"


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


def test_collect_limitless_fails_closed_without_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def reject_network(*_a: object, **_k: object) -> httpx.AsyncClient:
        raise AssertionError("collect must not open a network client when gate-rejected")

    monkeypatch.setattr(predictions_cli, "make_public_http_client", reject_network)
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()  # migrated, empty

    exit_code = main(["predictions", "collect", "limitless", "--db", str(database)])
    assert exit_code == 2
    assert "MANIFEST_NOT_FOUND" in capsys.readouterr().err


def test_collect_limitless_with_permitting_manifest_stores_markets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets/active":
            return httpx.Response(
                200,
                content=LIMITLESS_MARKETS_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    def fake_client(**_kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(predictions_cli, "make_public_http_client", fake_client)

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.LIMITLESS,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "limitless", "--db", str(database)])
    assert exit_code == 0
    assert "collected 3 limitless markets" in capsys.readouterr().out

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        markets = verify_store.markets_as_of(
            PredictionVenue.LIMITLESS, datetime.now(UTC) + timedelta(days=1)
        )
    finally:
        verify_store.close()
    assert len(markets) == 3


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


def test_predictions_health_rejects_a_naive_as_of(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    exit_code = main(
        ["predictions", "health", "--db", str(database), "--as-of", "2026-08-15T12:00:00"]
    )
    assert exit_code == 2


def test_collect_polymarket_persists_markets_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets":
            return httpx.Response(
                200,
                content=MARKETS_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    def fake_client(**_kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(predictions_cli, "make_public_http_client", fake_client)

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "polymarket", "--db", str(database)])
    assert exit_code == 0
    assert "collected 2 polymarket markets" in capsys.readouterr().out

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        markets = verify_store.markets_as_of(
            PredictionVenue.POLYMARKET, datetime.now(UTC) + timedelta(days=1)
        )
    finally:
        verify_store.close()
    assert len(markets) == 2


def test_collect_polymarket_exits_one_on_a_network_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def raise_network_error(**_kwargs: object) -> httpx.AsyncClient:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(predictions_cli, "make_public_http_client", raise_network_error)

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "polymarket", "--db", str(database)])
    assert exit_code == 1


def test_predictions_dashboard_dispatches_to_validate_and_serve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        predictions_cli,
        "validate_prediction_dashboard_database",
        lambda db: calls.append(f"validate:{db}"),
    )
    monkeypatch.setattr(
        predictions_cli,
        "serve_prediction_dashboard",
        lambda db, port: calls.append(f"serve:{db}:{port}"),
    )

    database = tmp_path / "predictions.duckdb"
    exit_code = main(["predictions", "dashboard", "--db", str(database), "--port", "8787"])

    assert exit_code == 0
    assert calls == [f"validate:{database}", f"serve:{database}:8787"]
