import json
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

from polytrading.predictions.dashboard_server import PredictionDashboardApplication
from polytrading.predictions.storage.store import PredictionMarketStore

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)


def test_dashboard_serves_the_snapshot_at_the_json_endpoint(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")

    assert response.status == HTTPStatus.OK
    document = json.loads(response.body)
    assert document["as_of"] == "2026-08-16T12:00:00Z"
    assert {venue["venue"] for venue in document["health"]["venues"]} == {
        "polymarket",
        "kalshi",
        "limitless",
    }


def test_dashboard_rejects_a_non_loopback_host(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "example.com")

    assert response.status == HTTPStatus.BAD_REQUEST
    assert json.loads(response.body)["error"]["code"] == "INVALID_HOST"


def test_dashboard_rejects_non_get_methods(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("POST", "/api/v1/predictions-dashboard", "127.0.0.1")

    assert response.status == HTTPStatus.METHOD_NOT_ALLOWED


def test_dashboard_serves_static_assets(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    index = application.respond("GET", "/", "127.0.0.1")
    css = application.respond("GET", "/assets/app.css", "127.0.0.1")
    js = application.respond("GET", "/assets/app.js", "127.0.0.1")

    assert index.status == HTTPStatus.OK
    assert b"predictions dashboard" in index.body
    assert css.status == HTTPStatus.OK
    assert js.status == HTTPStatus.OK


def test_dashboard_rejects_a_query_string_on_the_api_endpoint(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard?x=1", "127.0.0.1")
    assert response.status == HTTPStatus.BAD_REQUEST


def test_dashboard_returns_not_found_for_an_unknown_path(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/no-such-path", "127.0.0.1")
    assert response.status == HTTPStatus.NOT_FOUND
