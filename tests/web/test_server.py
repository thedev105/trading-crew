import json
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import pytest

from polytrading.storage.store import DuckDBStore
from polytrading.web.server import DashboardApplication, validate_dashboard_database

AS_OF = datetime(2026, 8, 13, 12, 6, tzinfo=UTC)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "dashboard.duckdb"
    DuckDBStore(path).close()
    return path


def test_dashboard_api_returns_one_secured_point_in_time_snapshot(database_path: Path) -> None:
    app = DashboardApplication(database_path, clock=lambda: AS_OF)

    response = app.respond("GET", "/api/v1/dashboard", "127.0.0.1:8787")

    assert response.status is HTTPStatus.OK
    assert response.content_type == "application/json; charset=utf-8"
    assert json.loads(response.body)["as_of"] == "2026-08-13T12:06:00Z"
    assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_dashboard_rejects_every_mutating_http_method(database_path: Path, method: str) -> None:
    response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
        method, "/api/v1/dashboard", "localhost:8787"
    )

    assert response.status is HTTPStatus.METHOD_NOT_ALLOWED
    assert response.headers["Allow"] == "GET"
    assert json.loads(response.body) == {"error": {"code": "METHOD_NOT_ALLOWED"}}


@pytest.mark.parametrize("host", ["", "evil.example", "127.0.0.2", "localhost:0", "localhost:x"])
def test_dashboard_rejects_non_loopback_or_malformed_host(database_path: Path, host: str) -> None:
    response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
        "GET", "/healthz", host
    )

    assert response.status is HTTPStatus.BAD_REQUEST
    assert json.loads(response.body) == {"error": {"code": "INVALID_HOST"}}


@pytest.mark.parametrize("host", ["localhost", "localhost:8787", "127.0.0.1", "127.0.0.1:65535"])
def test_dashboard_accepts_only_supported_loopback_hosts(database_path: Path, host: str) -> None:
    response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
        "GET", "/healthz", host
    )

    assert response.status is HTTPStatus.OK
    assert json.loads(response.body) == {"status": "ok"}
    assert b"dashboard.duckdb" not in response.body


def test_dashboard_rejects_api_query_and_unknown_route(database_path: Path) -> None:
    app = DashboardApplication(database_path, clock=lambda: AS_OF)

    query = app.respond("GET", "/api/v1/dashboard?db=other.duckdb", "127.0.0.1:8787")
    missing = app.respond("GET", "/api/v1/unknown", "127.0.0.1:8787")

    assert query.status is HTTPStatus.BAD_REQUEST
    assert json.loads(query.body) == {"error": {"code": "QUERY_NOT_ALLOWED"}}
    assert missing.status is HTTPStatus.NOT_FOUND


def test_dashboard_returns_stable_database_error_without_path_details(
    database_path: Path,
) -> None:
    app = DashboardApplication(database_path, clock=lambda: AS_OF)
    database_path.unlink()

    response = app.respond("GET", "/api/v1/dashboard", "127.0.0.1:8787")

    assert response.status is HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(response.body) == {"error": {"code": "DATABASE_UNAVAILABLE"}}
    assert str(database_path).encode() not in response.body


def test_database_validation_requires_existing_current_schema(tmp_path: Path) -> None:
    missing = tmp_path / "missing.duckdb"
    with pytest.raises(ValueError, match="existing database file"):
        validate_dashboard_database(missing)
    with pytest.raises(ValueError, match="existing database file"):
        validate_dashboard_database(tmp_path)

    outdated = tmp_path / "outdated.duckdb"
    outdated.write_bytes(b"not a DuckDB database")
    with pytest.raises(RuntimeError, match="dashboard database is unavailable"):
        validate_dashboard_database(outdated)
