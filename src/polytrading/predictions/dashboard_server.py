from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from polytrading.lifecycle import cleanup_error_cause, owned_resource_cleanup
from polytrading.predictions.dashboard import (
    build_prediction_dashboard_snapshot,
    render_prediction_dashboard_json,
)
from polytrading.predictions.dashboard_live import (
    DashboardCursorError,
    DashboardReset,
    DashboardRevision,
    DashboardRevisionBuffer,
    DashboardRevisionPublisher,
    DashboardSnapshotUnavailable,
)
from polytrading.predictions.storage.store import PredictionMarketStore

_LOGGER = logging.getLogger(__name__)
_HOST = re.compile(r"(?P<name>localhost|127\.0\.0\.1)(?::(?P<port>[0-9]+))?")
_MAX_HOST_BYTES = 255
_MAX_PORT_DIGITS = 5
_MAX_TARGET_BYTES = 2048
_STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/api.js": ("api.js", "text/javascript; charset=utf-8"),
    "/assets/stream.js": ("stream.js", "text/javascript; charset=utf-8"),
    "/assets/store.js": ("store.js", "text/javascript; charset=utf-8"),
    "/assets/charts.js": ("charts.js", "text/javascript; charset=utf-8"),
    "/assets/views.js": ("views.js", "text/javascript; charset=utf-8"),
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


class PredictionDashboardLifecycleError(RuntimeError):
    """A prediction-market dashboard lifecycle failed without exposing local details."""


class PredictionDashboardApplication:
    def __init__(
        self,
        database_path: Path,
        clock: Callable[[], datetime],
        *,
        revision_buffer: DashboardRevisionBuffer | None = None,
    ) -> None:
        self._database_path = database_path
        self._clock = clock
        self._revision_buffer = revision_buffer or DashboardRevisionBuffer(clock=clock)

    def respond(
        self,
        method: str,
        target: str,
        host: str,
        *,
        last_event_id: str | None = None,
        event_timeout: float = 0,
    ) -> WebResponse:
        if not _valid_host(host):
            return _error_response(HTTPStatus.BAD_REQUEST, "INVALID_HOST")

        if not _bounded_utf8(target, _MAX_TARGET_BYTES):
            return _error_response(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST_TARGET")
        try:
            parsed = urlsplit(target)
        except (TypeError, ValueError):
            return _error_response(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST_TARGET")
        if parsed.scheme or parsed.netloc:
            return _error_response(HTTPStatus.BAD_REQUEST, "INVALID_REQUEST_TARGET")
        if parsed.path == "/api/v1/predictions-events":
            if method != "GET":
                return _error_response(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "METHOD_NOT_ALLOWED",
                    headers={"Allow": "GET"},
                )
            if parsed.query or parsed.fragment:
                return _error_response(HTTPStatus.BAD_REQUEST, "QUERY_NOT_ALLOWED")
            return self._events_response(last_event_id, timeout=event_timeout)

        if method not in {"GET", "HEAD"}:
            return _error_response(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "METHOD_NOT_ALLOWED",
                headers={"Allow": "GET, HEAD"},
            )
        if parsed.path == "/api/v1/predictions-dashboard" and (parsed.query or parsed.fragment):
            return _error_response(HTTPStatus.BAD_REQUEST, "QUERY_NOT_ALLOWED")
        if parsed.path == "/api/v1/predictions-dashboard":
            response = self._dashboard_response()
            return _without_body(response) if method == "HEAD" else response
        if parsed.path == "/healthz" and not parsed.query and not parsed.fragment:
            response = _json_response(HTTPStatus.OK, {"status": "ok"})
            return _without_body(response) if method == "HEAD" else response
        asset = _STATIC_ASSETS.get(parsed.path)
        if asset is not None and not parsed.query and not parsed.fragment:
            name, media_type = asset
            body = resources.files("polytrading.predictions.web_assets").joinpath(name).read_bytes()
            response = WebResponse(HTTPStatus.OK, media_type, body, dict(_SECURITY_HEADERS))
            return _without_body(response) if method == "HEAD" else response
        return _error_response(HTTPStatus.NOT_FOUND, "NOT_FOUND")

    @property
    def revision_buffer(self) -> DashboardRevisionBuffer:
        return self._revision_buffer

    def _events_response(self, last_event_id: str | None, *, timeout: float) -> WebResponse:
        try:
            event = self._revision_buffer.wait_for_event(last_event_id, timeout=timeout)
            body = b": keepalive\n\n" if event is None else _sse_event_frame(event)
        except DashboardCursorError:
            return _error_response(HTTPStatus.BAD_REQUEST, "INVALID_EVENT_CURSOR")
        except DashboardSnapshotUnavailable:
            return _error_response(HTTPStatus.SERVICE_UNAVAILABLE, "SNAPSHOT_UNAVAILABLE")
        except Exception:
            _LOGGER.error("prediction_dashboard_failure failure=EVENT_STREAM_UNAVAILABLE")
            return _error_response(HTTPStatus.SERVICE_UNAVAILABLE, "EVENT_STREAM_UNAVAILABLE")
        return WebResponse(
            HTTPStatus.OK,
            "text/event-stream; charset=utf-8",
            body,
            {
                **_SECURITY_HEADERS,
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    def _dashboard_response(self) -> WebResponse:
        try:
            snapshot = build_prediction_dashboard_snapshot(self._database_path, now=self._clock())
            return WebResponse(
                HTTPStatus.OK,
                "application/json; charset=utf-8",
                render_prediction_dashboard_json(snapshot),
                dict(_SECURITY_HEADERS),
            )
        except Exception as error:
            status, code = _classify_dashboard_failure(error)
            _LOGGER.error("predictions dashboard snapshot failure: %s", code)
            return _error_response(status, code)


def validate_prediction_dashboard_database(path: Path) -> None:
    if not path.is_file():
        raise ValueError("predictions dashboard requires an existing database file")
    failure_code: str | None = None
    try:
        with owned_resource_cleanup() as cleanup:
            store = PredictionMarketStore(path, read_only=True)
            cleanup.add(store.close)
    except Exception as error:
        _status, failure_code = _classify_dashboard_failure(error)
    if failure_code is not None:
        raise PredictionDashboardLifecycleError(failure_code) from None


def serve_prediction_dashboard(
    database_path: Path,
    port: int,
    *,
    clock: Callable[[], datetime] | None = None,
) -> None:
    server_clock = clock or _utc_now
    revision_buffer = DashboardRevisionBuffer(clock=server_clock)
    application = PredictionDashboardApplication(
        database_path,
        clock=server_clock,
        revision_buffer=revision_buffer,
    )
    publisher = DashboardRevisionPublisher(
        snapshot_factory=lambda: build_prediction_dashboard_snapshot(
            database_path, now=server_clock()
        ),
        revision_buffer=revision_buffer,
        interval_seconds=1,
        clock=server_clock,
    )
    failed = False
    try:
        with owned_resource_cleanup() as cleanup:
            cleanup.add(publisher.close)
            publisher.poll_once()
            publisher.start()
            server = ThreadingHTTPServer(("127.0.0.1", port), _handler_for(application))
            server.daemon_threads = True
            cleanup.add(server.server_close)
            print(f"polytrading predictions dashboard: http://127.0.0.1:{server.server_port}")
            with suppress(KeyboardInterrupt):
                server.serve_forever()
    except Exception:
        failed = True
    if failed:
        raise PredictionDashboardLifecycleError("DASHBOARD_SERVER_ERROR") from None


def _handler_for(
    application: PredictionDashboardApplication,
) -> type[BaseHTTPRequestHandler]:
    class PredictionDashboardRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30

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
            last_event_id = self.headers.get("Last-Event-ID")
            response = application.respond(
                method,
                self.path,
                self.headers.get("Host", ""),
                last_event_id=last_event_id,
            )
            _LOGGER.info(
                "prediction_dashboard_request method=%s route=%s status=%d",
                method,
                _route_code(self.path),
                response.status.value,
            )
            self.send_response(response.status.value)
            self.send_header("Content-Type", response.content_type)
            is_event_stream = response.content_type == "text/event-stream; charset=utf-8"
            if not is_event_stream:
                self.send_header(
                    "Content-Length",
                    response.headers.get("Content-Length", str(len(response.body))),
                )
            for name, value in response.headers.items():
                if name == "Content-Length":
                    continue
                self.send_header(name, value)
            self.end_headers()
            if method == "HEAD":
                return
            if not _write_sse_frame(self.wfile, response.body):
                return
            if not is_event_stream:
                return

            cursor = _event_id_from_frame(response.body) or last_event_id
            while not application.revision_buffer.closed:
                followup = application.respond(
                    "GET",
                    "/api/v1/predictions-events",
                    self.headers.get("Host", ""),
                    last_event_id=cursor,
                    event_timeout=15,
                )
                if followup.status != HTTPStatus.OK:
                    return
                if not _write_sse_frame(self.wfile, followup.body):
                    return
                cursor = _event_id_from_frame(followup.body) or cursor

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return PredictionDashboardRequestHandler


def _without_body(response: WebResponse) -> WebResponse:
    return WebResponse(
        response.status,
        response.content_type,
        b"",
        {**response.headers, "Content-Length": str(len(response.body))},
    )


def _sse_event_frame(event: DashboardRevision | DashboardReset) -> bytes:
    event_name = "revision" if isinstance(event, DashboardRevision) else "reset"
    document = event.model_dump(mode="json", exclude={"event_id"})
    data = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {event.event_id}\nevent: {event_name}\ndata: {data}\n\n".encode()


def _event_id_from_frame(frame: bytes) -> str | None:
    first_line, _separator, _rest = frame.partition(b"\n")
    if not first_line.startswith(b"id: "):
        return None
    try:
        return first_line.removeprefix(b"id: ").decode("ascii")
    except UnicodeDecodeError:
        return None


def _write_sse_frame(stream: object, frame: bytes) -> bool:
    try:
        stream.write(frame)  # type: ignore[attr-defined]
        stream.flush()  # type: ignore[attr-defined]
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False
    return True


def _valid_host(host: str) -> bool:
    if not _bounded_utf8(host, _MAX_HOST_BYTES):
        return False
    match = _HOST.fullmatch(host)
    if match is None:
        return False
    port = match.group("port")
    return port is None or (len(port) <= _MAX_PORT_DIGITS and 1 <= int(port) <= 65_535)


def _bounded_utf8(value: object, maximum_bytes: int) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8")) <= maximum_bytes
    except UnicodeEncodeError:
        return False


def _route_code(target: str) -> str:
    if target.startswith("/api/v1/predictions-dashboard"):
        return "DASHBOARD"
    if target.startswith("/api/v1/predictions-events"):
        return "EVENTS"
    if target.startswith("/assets/") or target == "/":
        return "STATIC"
    if target.startswith("/healthz"):
        return "HEALTH"
    return "UNKNOWN"


def _is_database_busy(error: BaseException) -> bool:
    return isinstance(error, duckdb.IOException) and "Could not set lock on file" in str(error)


def _classify_dashboard_failure(error: BaseException) -> tuple[HTTPStatus, str]:
    classified_error = cleanup_error_cause(error)
    if _is_database_busy(classified_error):
        return HTTPStatus.SERVICE_UNAVAILABLE, "DATABASE_BUSY"
    if isinstance(classified_error, duckdb.Error | OSError | RuntimeError | ValueError):
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
