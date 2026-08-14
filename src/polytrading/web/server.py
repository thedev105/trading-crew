from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from polytrading.domain.models import normalize_utc_timestamp
from polytrading.lifecycle import cleanup_error_cause, owned_resource_cleanup
from polytrading.storage.store import DuckDBStore
from polytrading.web.dashboard import DashboardBuilder, render_dashboard_json

_LOGGER = logging.getLogger(__name__)
_HOST = re.compile(r"(?P<name>localhost|127\.0\.0\.1)(?::(?P<port>[0-9]+))?")
_STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; connect-src 'self'; img-src 'self' data:"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Connection": "close",
}


@dataclass(frozen=True)
class WebResponse:
    status: HTTPStatus
    content_type: str
    body: bytes
    headers: Mapping[str, str]


class DashboardLifecycleError(RuntimeError):
    """A dashboard lifecycle failed without exposing machine-local details."""


class DashboardApplication:
    def __init__(self, database_path: Path, clock: Callable[[], datetime]) -> None:
        self._database_path = database_path
        self._clock = clock

    def respond(self, method: str, target: str, host: str) -> WebResponse:
        if method != "GET":
            return _error_response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "METHOD_NOT_ALLOWED",
                headers={"Allow": "GET"},
            )
        if not _valid_host(host):
            return _error_response(HTTPStatus.BAD_REQUEST, "INVALID_HOST")

        parsed = urlsplit(target)
        if parsed.path == "/api/v1/dashboard" and (parsed.query or parsed.fragment):
            return _error_response(HTTPStatus.BAD_REQUEST, "QUERY_NOT_ALLOWED")
        if parsed.path == "/api/v1/dashboard":
            return self._dashboard_response()
        if parsed.path == "/healthz" and not parsed.query and not parsed.fragment:
            return _json_response(HTTPStatus.OK, {"status": "ok"})
        asset = _STATIC_ASSETS.get(parsed.path)
        if asset is not None and not parsed.query and not parsed.fragment:
            name, media_type = asset
            body = resources.files("polytrading.web.assets").joinpath(name).read_bytes()
            return WebResponse(HTTPStatus.OK, media_type, body, dict(_SECURITY_HEADERS))
        return _error_response(HTTPStatus.NOT_FOUND, "NOT_FOUND")

    def _dashboard_response(self) -> WebResponse:
        try:
            with owned_resource_cleanup() as cleanup:
                as_of = normalize_utc_timestamp(self._clock())
                store = DuckDBStore(self._database_path, read_only=True)
                cleanup.add(store.close)
                snapshot = DashboardBuilder(store, self._database_path).build(as_of)
                return WebResponse(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    render_dashboard_json(snapshot),
                    dict(_SECURITY_HEADERS),
                )
        except Exception as error:
            status, code = _classify_dashboard_failure(error)
            _LOGGER.error("dashboard snapshot failure: %s", code)
            return _error_response(status, code)


def validate_dashboard_database(path: Path) -> None:
    if not path.is_file():
        raise ValueError("dashboard requires an existing database file")
    try:
        with owned_resource_cleanup() as cleanup:
            store = DuckDBStore(path, read_only=True)
            cleanup.add(store.close)
    except Exception as error:
        _status, code = _classify_dashboard_failure(error)
        raise DashboardLifecycleError(code) from cleanup_error_cause(error)


def serve_dashboard(
    database_path: Path,
    port: int,
    *,
    clock: Callable[[], datetime] | None = None,
) -> None:
    application = DashboardApplication(database_path, clock=clock or _utc_now)
    try:
        with owned_resource_cleanup() as cleanup:
            server = HTTPServer(("127.0.0.1", port), _handler_for(application))
            cleanup.add(server.server_close)
            print(f"polytrading dashboard: http://127.0.0.1:{server.server_port}")
            with suppress(KeyboardInterrupt):
                server.serve_forever()
    except Exception as error:
        raise DashboardLifecycleError("DASHBOARD_SERVER_ERROR") from cleanup_error_cause(error)


def _handler_for(application: DashboardApplication) -> type[BaseHTTPRequestHandler]:
    class DashboardRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._write_response("GET")

        def do_HEAD(self) -> None:
            self._write_response("HEAD")

        def do_POST(self) -> None:
            self._write_response("POST")

        def do_PUT(self) -> None:
            self._write_response("PUT")

        def do_PATCH(self) -> None:
            self._write_response("PATCH")

        def do_DELETE(self) -> None:
            self._write_response("DELETE")

        def do_OPTIONS(self) -> None:
            self._write_response("OPTIONS")

        def do_TRACE(self) -> None:
            self._write_response("TRACE")

        def do_CONNECT(self) -> None:
            self._write_response("CONNECT")

        def _write_response(self, method: str) -> None:
            response = application.respond(method, self.path, self.headers.get("Host", ""))
            self.send_response(response.status.value)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, format: str, *args: object) -> None:
            _LOGGER.info("dashboard request: " + format, *args)

    return DashboardRequestHandler


def _valid_host(host: str) -> bool:
    match = _HOST.fullmatch(host)
    if match is None:
        return False
    port = match.group("port")
    return port is None or 1 <= int(port) <= 65_535


def _is_database_busy(error: BaseException) -> bool:
    return isinstance(error, duckdb.IOException) and "Could not set lock on file" in str(error)


def _classify_dashboard_failure(error: BaseException) -> tuple[HTTPStatus, str]:
    classified_error = cleanup_error_cause(error)
    if _is_database_busy(classified_error):
        return HTTPStatus.SERVICE_UNAVAILABLE, "DATABASE_BUSY"
    if isinstance(classified_error, (duckdb.Error, OSError, RuntimeError)):
        return HTTPStatus.SERVICE_UNAVAILABLE, "DATABASE_UNAVAILABLE"
    return HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR"


def _json_response(status: HTTPStatus, document: object) -> WebResponse:
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return WebResponse(
        status,
        "application/json; charset=utf-8",
        body,
        dict(_SECURITY_HEADERS),
    )


def _error_response(
    status: HTTPStatus,
    code: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> WebResponse:
    response = _json_response(status, {"error": {"code": code}})
    return WebResponse(
        response.status,
        response.content_type,
        response.body,
        {**response.headers, **(headers or {})},
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
