import asyncio
import json
import socket
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import NoReturn

import duckdb
import pytest

import polytrading.web.server as web_server
from polytrading.storage.store import DuckDBStore
from polytrading.web.server import DashboardApplication, validate_dashboard_database

AS_OF = datetime(2026, 8, 13, 16, 6, tzinfo=UTC)


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
    document = json.loads(response.body)
    assert document["as_of"] == "2026-08-13T16:06:00Z"
    assert document["compatibility_dossier"]["status"] == "ineligible"
    assert document["compatibility_dossier"]["primary_reason_code"] == "quanto_structure_excluded"
    assert document["venue_discovery"]["selected_dossier_id"] is None
    assert [item["dossier_id"] for item in document["venue_discovery"]["candidates"]] == [
        "hyperliquid-dydx-core-v1"
    ]
    assert [item["asset"] for item in document["economics_rows"]] == ["BTC", "ETH", "SOL"]
    assert all(not item["report_available"] for item in document["economics_rows"])
    assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert "img-src 'self' data:" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Connection"] == "close"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_dashboard_rejects_every_mutating_http_method(database_path: Path, method: str) -> None:
    response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
        method, "/api/v1/dashboard", "localhost:8787"
    )

    assert response.status is HTTPStatus.METHOD_NOT_ALLOWED
    assert response.headers["Allow"] == "GET"
    assert json.loads(response.body) == {"error": {"code": "METHOD_NOT_ALLOWED"}}


def test_http_handler_routes_connect_through_the_get_only_policy(database_path: Path) -> None:
    application = DashboardApplication(database_path, clock=lambda: AS_OF)
    handler = web_server._handler_for(application)
    client, accepted = socket.socketpair()
    client.settimeout(1)
    try:
        client.sendall(b"CONNECT tunnel HTTP/1.1\r\nHost: localhost:8787\r\n\r\n")
        handler(accepted, ("127.0.0.1", 1), object())
        response = client.recv(4096)
    finally:
        client.close()
        accepted.close()

    header, body = response.split(b"\r\n\r\n", 1)
    assert header.startswith(b"HTTP/1.1 405 Method Not Allowed")
    assert b"Allow: GET" in header
    assert json.loads(body) == {"error": {"code": "METHOD_NOT_ALLOWED"}}


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


def test_dashboard_distinguishes_database_lock_without_leaking_path(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(*_args: object, **_kwargs: object) -> NoReturn:
        raise duckdb.IOException(f"Could not set lock on file {database_path}")

    monkeypatch.setattr(web_server, "DuckDBStore", busy)

    response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
        "GET", "/api/v1/dashboard", "127.0.0.1:8787"
    )

    assert response.status is HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(response.body) == {"error": {"code": "DATABASE_BUSY"}}
    assert str(database_path).encode() not in response.body
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Connection"] == "close"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    "error",
    (
        duckdb.IOException("different I/O failure"),
        duckdb.Error("Could not set lock on file /tmp/not-an-io-error.duckdb"),
        OSError("Could not set lock on file /tmp/not-a-duckdb-error.duckdb"),
        OSError("filesystem unavailable"),
        RuntimeError("schema is not current"),
    ),
)
def test_dashboard_keeps_non_lock_database_failures_unavailable(
    database_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    monkeypatch.setattr(web_server, "DuckDBStore", unavailable)

    response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
        "GET", "/api/v1/dashboard", "127.0.0.1:8787"
    )

    assert response.status is HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(response.body) == {"error": {"code": "DATABASE_UNAVAILABLE"}}


def test_dashboard_keeps_unexpected_failures_internal(
    database_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = (
        "/private/evidence/dashboard.duckdb "
        "https://private.example.test/dashboard?token=secret response-body=confidential"
    )

    def unexpected(*_args: object, **_kwargs: object) -> NoReturn:
        raise ValueError(secret)

    monkeypatch.setattr(web_server, "DuckDBStore", unexpected)

    with caplog.at_level("ERROR", logger=web_server.__name__):
        response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
            "GET", "/api/v1/dashboard", "127.0.0.1:8787"
        )

    assert response.status is HTTPStatus.INTERNAL_SERVER_ERROR
    assert json.loads(response.body) == {"error": {"code": "INTERNAL_ERROR"}}
    assert [record.getMessage() for record in caplog.records] == [
        "dashboard snapshot failure: INTERNAL_ERROR"
    ]
    assert all(record.exc_info is None for record in caplog.records)
    assert secret not in caplog.text


def test_dashboard_close_failure_returns_sanitized_database_unavailable(
    database_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = (
        "/private/evidence/dashboard.duckdb "
        "https://private.example.test/dashboard?token=secret "
        "response-body=confidential lock-owner=private"
    )

    class CloseFailingStore:
        def __init__(self, path: Path, *, read_only: bool = False) -> None:
            assert (path, read_only) == (database_path, True)

        def close(self) -> None:
            raise OSError(secret)

    class SuccessfulBuilder:
        def __init__(self, _store: object, _path: Path) -> None:
            pass

        def build(self, _as_of: datetime) -> object:
            return object()

    monkeypatch.setattr(web_server, "DuckDBStore", CloseFailingStore)
    monkeypatch.setattr(web_server, "DashboardBuilder", SuccessfulBuilder)
    monkeypatch.setattr(web_server, "render_dashboard_json", lambda _snapshot: b"{}")

    with caplog.at_level("ERROR", logger=web_server.__name__):
        response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
            "GET", "/api/v1/dashboard", "127.0.0.1:8787"
        )

    assert response.status is HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(response.body) == {"error": {"code": "DATABASE_UNAVAILABLE"}}
    assert secret.encode() not in response.body
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_dashboard_cleanup_cancellation_returns_sanitized_internal_error(
    database_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "dashboard cleanup cancellation /private/db?token=secret response-body=hidden"
    events: list[str] = []

    class Store:
        def __init__(self, path: Path, *, read_only: bool = False) -> None:
            assert (path, read_only) == (database_path, True)
            events.append("open")

        def close(self) -> None:
            events.append("close")
            raise asyncio.CancelledError(secret)

    class Builder:
        def __init__(self, _store: object, _path: Path) -> None:
            pass

        def build(self, _as_of: datetime) -> object:
            events.append("build")
            return object()

    monkeypatch.setattr(web_server, "DuckDBStore", Store)
    monkeypatch.setattr(web_server, "DashboardBuilder", Builder)
    monkeypatch.setattr(web_server, "render_dashboard_json", lambda _snapshot: b"{}")

    with caplog.at_level("ERROR", logger=web_server.__name__):
        response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
            "GET", "/api/v1/dashboard", "127.0.0.1:8787"
        )

    assert response.status is HTTPStatus.INTERNAL_SERVER_ERROR
    assert json.loads(response.body) == {"error": {"code": "INTERNAL_ERROR"}}
    assert events == ["open", "build", "close"]
    assert [record.getMessage() for record in caplog.records] == [
        "dashboard snapshot failure: INTERNAL_ERROR"
    ]
    assert secret.encode() not in response.body
    assert secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_dashboard_cleanup_database_busy_preserves_retry_classification(
    database_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = duckdb.IOException(f"Could not set lock on file {database_path}?token=secret")

    class Store:
        def __init__(self, path: Path, *, read_only: bool = False) -> None:
            assert (path, read_only) == (database_path, True)

        def close(self) -> None:
            raise secret

    class Builder:
        def __init__(self, _store: object, _path: Path) -> None:
            pass

        def build(self, _as_of: datetime) -> object:
            return object()

    monkeypatch.setattr(web_server, "DuckDBStore", Store)
    monkeypatch.setattr(web_server, "DashboardBuilder", Builder)
    monkeypatch.setattr(web_server, "render_dashboard_json", lambda _snapshot: b"{}")

    with caplog.at_level("ERROR", logger=web_server.__name__):
        response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
            "GET", "/api/v1/dashboard", "127.0.0.1:8787"
        )

    assert response.status is HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(response.body) == {"error": {"code": "DATABASE_BUSY"}}
    assert [record.getMessage() for record in caplog.records] == [
        "dashboard snapshot failure: DATABASE_BUSY"
    ]
    assert str(secret).encode() not in response.body
    assert str(secret) not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_dashboard_active_cancellation_wins_over_cleanup_cancellation(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = asyncio.CancelledError("primary dashboard cancellation token=primary")
    cleanup = asyncio.CancelledError("cleanup dashboard cancellation token=cleanup")
    events: list[str] = []

    class Store:
        def __init__(self, _path: Path, *, read_only: bool = False) -> None:
            assert read_only is True
            events.append("open")

        def close(self) -> None:
            events.append("close")
            raise cleanup

    class Builder:
        def __init__(self, _store: object, _path: Path) -> None:
            pass

        def build(self, _as_of: datetime) -> NoReturn:
            events.append("build")
            raise primary

    monkeypatch.setattr(web_server, "DuckDBStore", Store)
    monkeypatch.setattr(web_server, "DashboardBuilder", Builder)

    with pytest.raises(BaseException) as captured:
        DashboardApplication(database_path, clock=lambda: AS_OF).respond(
            "GET", "/api/v1/dashboard", "127.0.0.1:8787"
        )

    assert captured.value is primary
    assert events == ["open", "build", "close"]


def test_dashboard_busy_body_failure_wins_over_sanitized_close_failure(
    database_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_secret = f"Could not set lock on file {database_path}?token=secret"
    close_secret = "/private/evidence/dashboard.duckdb response-body=secret lock-owner=private"

    class CloseFailingStore:
        def __init__(self, path: Path, *, read_only: bool = False) -> None:
            assert (path, read_only) == (database_path, True)

        def close(self) -> None:
            raise OSError(close_secret)

    class BusyBuilder:
        def __init__(self, _store: object, _path: Path) -> None:
            pass

        def build(self, _as_of: datetime) -> object:
            raise duckdb.IOException(body_secret)

    monkeypatch.setattr(web_server, "DuckDBStore", CloseFailingStore)
    monkeypatch.setattr(web_server, "DashboardBuilder", BusyBuilder)

    with caplog.at_level("ERROR", logger=web_server.__name__):
        response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
            "GET", "/api/v1/dashboard", "127.0.0.1:8787"
        )

    assert response.status is HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(response.body) == {"error": {"code": "DATABASE_BUSY"}}
    assert body_secret.encode() not in response.body
    assert close_secret.encode() not in response.body
    assert body_secret not in caplog.text
    assert close_secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_dashboard_busy_body_failure_wins_over_cleanup_base_exception(
    database_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_secret = f"Could not set lock on file {database_path}?token=body"
    close_secret = "/private/dashboard.duckdb?token=cleanup response-body=hidden"
    events: list[str] = []

    class Store:
        def __init__(self, path: Path, *, read_only: bool = False) -> None:
            assert (path, read_only) == (database_path, True)
            events.append("open")

        def close(self) -> None:
            events.append("close")
            raise asyncio.CancelledError(close_secret)

    class Builder:
        def __init__(self, _store: object, _path: Path) -> None:
            pass

        def build(self, _as_of: datetime) -> NoReturn:
            events.append("build")
            raise duckdb.IOException(body_secret)

    monkeypatch.setattr(web_server, "DuckDBStore", Store)
    monkeypatch.setattr(web_server, "DashboardBuilder", Builder)

    with caplog.at_level("ERROR", logger=web_server.__name__):
        response = DashboardApplication(database_path, clock=lambda: AS_OF).respond(
            "GET", "/api/v1/dashboard", "127.0.0.1:8787"
        )

    assert response.status is HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(response.body) == {"error": {"code": "DATABASE_BUSY"}}
    assert [record.getMessage() for record in caplog.records] == [
        "dashboard snapshot failure: DATABASE_BUSY"
    ]
    assert events == ["open", "build", "close"]
    assert body_secret.encode() not in response.body
    assert close_secret.encode() not in response.body
    assert body_secret not in caplog.text
    assert close_secret not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_database_validation_requires_existing_current_schema(tmp_path: Path) -> None:
    missing = tmp_path / "missing.duckdb"
    with pytest.raises(ValueError, match="existing database file"):
        validate_dashboard_database(missing)
    with pytest.raises(ValueError, match="existing database file"):
        validate_dashboard_database(tmp_path)

    outdated = tmp_path / "outdated.duckdb"
    outdated.write_bytes(b"not a DuckDB database")
    with pytest.raises(RuntimeError, match="DATABASE_UNAVAILABLE"):
        validate_dashboard_database(outdated)


def test_dashboard_database_validation_sanitizes_close_failure(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = OSError(
        "/private/evidence/dashboard.duckdb "
        "https://private.example.test/dashboard?token=secret "
        "response-body=confidential lock-owner=private"
    )

    class CloseFailingStore:
        def __init__(self, path: Path, *, read_only: bool = False) -> None:
            assert (path, read_only) == (database_path, True)

        def close(self) -> None:
            raise secret

    monkeypatch.setattr(web_server, "DuckDBStore", CloseFailingStore)

    with pytest.raises(RuntimeError, match=r"^DATABASE_UNAVAILABLE$") as captured:
        validate_dashboard_database(database_path)

    assert captured.value.__cause__ is secret
    assert str(secret) not in str(captured.value)


def test_dashboard_database_validation_cleanup_cancellation_is_sanitized(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = asyncio.CancelledError(
        "validation cleanup /private/dashboard.duckdb?token=secret response-body=hidden"
    )
    events: list[str] = []

    class Store:
        def __init__(self, path: Path, *, read_only: bool = False) -> None:
            assert (path, read_only) == (database_path, True)
            events.append("open")

        def close(self) -> None:
            events.append("close")
            raise secret

    monkeypatch.setattr(web_server, "DuckDBStore", Store)

    with pytest.raises(RuntimeError, match=r"^INTERNAL_ERROR$") as captured:
        validate_dashboard_database(database_path)

    assert captured.value.__cause__ is secret
    assert str(secret) not in str(captured.value)
    assert events == ["open", "close"]


def test_dashboard_database_validation_cleanup_database_busy_preserves_code(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = duckdb.IOException(f"Could not set lock on file {database_path}?token=secret")

    class Store:
        def __init__(self, path: Path, *, read_only: bool = False) -> None:
            assert (path, read_only) == (database_path, True)

        def close(self) -> None:
            raise secret

    monkeypatch.setattr(web_server, "DuckDBStore", Store)

    with pytest.raises(RuntimeError, match=r"^DATABASE_BUSY$") as captured:
        validate_dashboard_database(database_path)

    assert captured.value.__cause__ is secret
    assert str(secret) not in str(captured.value)


def test_dashboard_database_validation_preserves_constructor_cancellation(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = asyncio.CancelledError("validation acquisition cancellation token=primary")
    close_calls: list[str] = []

    class Store:
        def __init__(self, _path: Path, *, read_only: bool = False) -> None:
            assert read_only is True
            raise primary

        def close(self) -> None:
            close_calls.append("close")

    monkeypatch.setattr(web_server, "DuckDBStore", Store)

    with pytest.raises(BaseException) as captured:
        validate_dashboard_database(database_path)

    assert captured.value is primary
    assert close_calls == []


@pytest.mark.parametrize("body_fails", (False, True))
def test_dashboard_server_sanitizes_close_and_preserves_primary_failure(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_fails: bool,
) -> None:
    body_secret = RuntimeError(
        "https://private.example.test/server?token=secret response-body=confidential"
    )
    close_secret = OSError("/private/evidence/server.lock lock-owner=private")

    class FailingServer:
        server_port = 8787

        def __init__(self, address: tuple[str, int], _handler: object) -> None:
            assert address == ("127.0.0.1", 8787)

        def serve_forever(self) -> None:
            if body_fails:
                raise body_secret

        def server_close(self) -> None:
            raise close_secret

    monkeypatch.setattr(web_server, "HTTPServer", FailingServer)

    with pytest.raises(RuntimeError, match=r"^DASHBOARD_SERVER_ERROR$") as captured:
        web_server.serve_dashboard(database_path, 8787)

    expected_cause = body_secret if body_fails else close_secret
    assert captured.value.__cause__ is expected_cause
    assert str(body_secret) not in str(captured.value)
    assert str(close_secret) not in str(captured.value)


def test_dashboard_server_body_failure_wins_over_cleanup_base_exception(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body_secret = RuntimeError(
        "https://private.example.test/server?token=body response-body=hidden"
    )
    close_secret = KeyboardInterrupt("/private/server.lock?token=cleanup lock-owner=hidden")
    events: list[str] = []

    class Server:
        server_port = 8787

        def __init__(self, address: tuple[str, int], _handler: object) -> None:
            assert address == ("127.0.0.1", 8787)
            events.append("construct")

        def serve_forever(self) -> NoReturn:
            events.append("serve")
            raise body_secret

        def server_close(self) -> None:
            events.append("close")
            raise close_secret

    monkeypatch.setattr(web_server, "HTTPServer", Server)

    with pytest.raises(RuntimeError, match=r"^DASHBOARD_SERVER_ERROR$") as captured:
        web_server.serve_dashboard(database_path, 8787)

    assert captured.value.__cause__ is body_secret
    assert str(body_secret) not in str(captured.value)
    assert str(close_secret) not in str(captured.value)
    assert events == ["construct", "serve", "close"]


def test_dashboard_server_active_cancellation_wins_over_cleanup_cancellation(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = asyncio.CancelledError("primary server cancellation token=primary")
    cleanup = asyncio.CancelledError("cleanup server cancellation token=cleanup")
    events: list[str] = []

    class Server:
        server_port = 8787

        def __init__(self, address: tuple[str, int], _handler: object) -> None:
            assert address == ("127.0.0.1", 8787)
            events.append("construct")

        def serve_forever(self) -> NoReturn:
            events.append("serve")
            raise primary

        def server_close(self) -> None:
            events.append("close")
            raise cleanup

    monkeypatch.setattr(web_server, "HTTPServer", Server)

    with pytest.raises(BaseException) as captured:
        web_server.serve_dashboard(database_path, 8787)

    assert captured.value is primary
    assert events == ["construct", "serve", "close"]


def test_dashboard_server_cleanup_keyboard_interrupt_is_stably_sanitized(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = KeyboardInterrupt(
        "server cleanup /private/server.lock?token=secret response-body=hidden"
    )
    events: list[str] = []

    class Server:
        server_port = 8787

        def __init__(self, _address: tuple[str, int], _handler: object) -> None:
            events.append("construct")

        def serve_forever(self) -> None:
            events.append("serve")

        def server_close(self) -> None:
            events.append("close")
            raise secret

    monkeypatch.setattr(web_server, "HTTPServer", Server)

    with pytest.raises(RuntimeError, match=r"^DASHBOARD_SERVER_ERROR$") as captured:
        web_server.serve_dashboard(database_path, 8787)

    assert captured.value.__cause__ is secret
    assert str(secret) not in str(captured.value)
    assert events == ["construct", "serve", "close"]


def test_dashboard_server_preserves_constructor_cancellation(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = asyncio.CancelledError("server acquisition cancellation token=primary")

    class Server:
        def __init__(self, _address: tuple[str, int], _handler: object) -> None:
            raise primary

    monkeypatch.setattr(web_server, "HTTPServer", Server)

    with pytest.raises(BaseException) as captured:
        web_server.serve_dashboard(database_path, 8787)

    assert captured.value is primary
