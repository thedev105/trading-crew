from datetime import UTC, datetime
from pathlib import Path

import pytest

import polytrading.predictions.dashboard_server as prediction_dashboard_server
from polytrading.predictions.dashboard_server import (
    PredictionDashboardApplication,
    PredictionDashboardLifecycleError,
    serve_prediction_dashboard,
    validate_prediction_dashboard_database,
)
from polytrading.predictions.storage.store import PredictionMarketStore
from polytrading.storage.store import DuckDBStore

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


def test_validate_requires_an_existing_database_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing database file"):
        validate_prediction_dashboard_database(tmp_path / "missing.duckdb")
    with pytest.raises(ValueError, match="existing database file"):
        validate_prediction_dashboard_database(tmp_path)


def test_validate_accepts_a_fresh_predictions_database(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    validate_prediction_dashboard_database(database)


def test_prediction_dashboard_rejects_a_perpetual_futures_database(tmp_path: Path) -> None:
    perpetual_futures_db = tmp_path / "forward.duckdb"
    DuckDBStore(perpetual_futures_db).close()

    with pytest.raises(PredictionDashboardLifecycleError):
        validate_prediction_dashboard_database(perpetual_futures_db)


def test_existing_dashboard_equally_rejects_a_fresh_predictions_database(tmp_path: Path) -> None:
    from polytrading.web.server import DashboardLifecycleError, validate_dashboard_database

    predictions_db = tmp_path / "predictions.duckdb"
    PredictionMarketStore(predictions_db).close()

    with pytest.raises(DashboardLifecycleError):
        validate_dashboard_database(predictions_db)


def test_dashboard_response_rejects_a_naive_clock(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: datetime(2026, 8, 16, 12))

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1")

    assert response.status.value == 503
    assert b"DATABASE_UNAVAILABLE" in response.body


def test_serve_prediction_dashboard_stops_cleanly_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    closed = []

    class StubServer:
        server_port = 8787

        def __init__(self, address: tuple[str, int], _handler: object) -> None:
            assert address == ("127.0.0.1", 8787)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(prediction_dashboard_server, "HTTPServer", StubServer)
    serve_prediction_dashboard(database, 8787, clock=lambda: NOW)
    assert closed == [True]


def test_serve_prediction_dashboard_sanitizes_a_server_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    secret = RuntimeError("https://private.example.test/leak?token=secret")

    class FailingServer:
        server_port = 8787

        def __init__(self, address: tuple[str, int], _handler: object) -> None:
            pass

        def serve_forever(self) -> None:
            raise secret

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(prediction_dashboard_server, "HTTPServer", FailingServer)

    with pytest.raises(PredictionDashboardLifecycleError, match="DASHBOARD_SERVER_ERROR") as caught:
        serve_prediction_dashboard(database, 8787, clock=lambda: NOW)
    assert caught.value.__cause__ is secret
    assert str(secret) not in str(caught.value)
