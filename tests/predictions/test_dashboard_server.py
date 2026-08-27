import json
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from io import BytesIO
from pathlib import Path

import pytest

import polytrading.predictions.dashboard_live as prediction_dashboard_live
import polytrading.predictions.dashboard_server as prediction_dashboard_server
from polytrading.predictions.dashboard import PredictionDashboardBuilder
from polytrading.predictions.dashboard_live import (
    DashboardRevisionBuffer,
    DashboardRevisionPublisher,
)
from polytrading.predictions.dashboard_models import PredictionDashboardSnapshot
from polytrading.predictions.dashboard_server import (
    PredictionDashboardApplication,
    PredictionDashboardLifecycleError,
    serve_prediction_dashboard,
    validate_prediction_dashboard_database,
)
from polytrading.predictions.storage.store import PredictionMarketStore
from polytrading.storage.store import DuckDBStore

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
MARKET_ATLAS_MODULE_PATHS = (
    "/assets/api.js",
    "/assets/stream.js",
    "/assets/store.js",
    "/assets/charts.js",
    "/assets/views.js",
)


def _seeded_revision_buffer(database: Path, *, capacity: int = 3) -> DashboardRevisionBuffer:
    store = PredictionMarketStore(database)
    try:
        snapshot = PredictionDashboardBuilder(store, database).build(NOW)
    finally:
        store.close()
    buffer = DashboardRevisionBuffer(capacity=capacity, clock=lambda: NOW)
    buffer.publish(snapshot)
    return buffer


def _changed_snapshot(
    snapshot: PredictionDashboardSnapshot, seed: int
) -> PredictionDashboardSnapshot:
    values = snapshot.model_dump(mode="python", exclude={"revision_id"})
    values["recipes"] = snapshot.recipes.model_copy(
        update={"recipes": (*snapshot.recipes.recipes, f"observer-status-{seed}")}
    )
    return PredictionDashboardSnapshot.finalize(**values)


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


def test_dashboard_response_is_unavailable_before_first_publisher_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    buffer = DashboardRevisionBuffer(capacity=2, clock=lambda: NOW)
    publisher = DashboardRevisionPublisher(
        snapshot_factory=lambda: (_ for _ in ()).throw(AssertionError("factory must not run")),
        revision_buffer=buffer,
        interval_seconds=1,
        clock=lambda: NOW,
    )
    application = PredictionDashboardApplication(
        database,
        clock=lambda: (_ for _ in ()).throw(AssertionError("GET must not rebuild")),
        revision_buffer=buffer,
        snapshot_provider=publisher.latest_snapshot,
    )

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1")

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(response.body) == {"error": {"code": "SNAPSHOT_UNAVAILABLE"}}
    publisher.close()


def test_published_event_and_advancing_clock_gets_share_one_exact_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    build_times = iter((NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)))
    build_calls: list[datetime] = []

    def build_once() -> PredictionDashboardSnapshot:
        cutoff = next(build_times)
        build_calls.append(cutoff)
        return prediction_dashboard_server.build_prediction_dashboard_snapshot(database, now=cutoff)

    buffer = DashboardRevisionBuffer(capacity=2, clock=lambda: NOW)
    publisher = DashboardRevisionPublisher(
        snapshot_factory=build_once,
        revision_buffer=buffer,
        interval_seconds=1,
        clock=lambda: NOW + timedelta(seconds=10),
    )
    application = PredictionDashboardApplication(
        database,
        clock=lambda: (_ for _ in ()).throw(AssertionError("GET must not rebuild")),
        revision_buffer=buffer,
        snapshot_provider=publisher.latest_snapshot,
    )

    event = publisher.poll_once()
    assert event is not None
    event_response = application.respond("GET", "/api/v1/predictions-events", "127.0.0.1")
    first = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1")
    second = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1")

    event_document = json.loads(event_response.body.split(b"data: ", 1)[1])
    first_document = json.loads(first.body)
    assert first.status == second.status == HTTPStatus.OK
    assert first.body == second.body
    assert event_document["revision_id"] == first_document["revision_id"] == event.revision_id
    assert build_calls == [NOW]
    publisher.close()


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

    monkeypatch.setattr(prediction_dashboard_server, "ThreadingHTTPServer", StubServer)
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

    monkeypatch.setattr(prediction_dashboard_server, "ThreadingHTTPServer", FailingServer)

    with pytest.raises(PredictionDashboardLifecycleError, match="DASHBOARD_SERVER_ERROR") as caught:
        serve_prediction_dashboard(database, 8787, clock=lambda: NOW)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert str(secret) not in str(caught.value)


@pytest.mark.parametrize(
    "path",
    ["/api/v1/predictions-dashboard", "/", "/assets/app.css", "/assets/app.js"],
)
def test_head_has_get_parity_and_no_body(tmp_path: Path, path: str) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    snapshot = prediction_dashboard_server.build_prediction_dashboard_snapshot(database, now=NOW)
    application = PredictionDashboardApplication(
        database, clock=lambda: NOW, snapshot_provider=lambda: snapshot
    )

    get_response = application.respond("GET", path, "127.0.0.1")
    head_response = application.respond("HEAD", path, "127.0.0.1")

    assert head_response.status == get_response.status == HTTPStatus.OK
    assert head_response.content_type == get_response.content_type
    assert head_response.headers == {
        **get_response.headers,
        "Content-Length": str(len(get_response.body)),
    }
    assert head_response.body == b""


@pytest.mark.parametrize("path", MARKET_ATLAS_MODULE_PATHS)
def test_market_atlas_module_assets_are_fixed_no_store_javascript(
    tmp_path: Path, path: str
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", path, "127.0.0.1")
    head = application.respond("HEAD", path, "127.0.0.1")

    assert response.status == HTTPStatus.OK
    assert response.content_type == "text/javascript; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.body
    assert head.status == HTTPStatus.OK
    assert head.content_type == response.content_type
    assert head.headers["Content-Length"] == str(len(response.body))
    assert head.body == b""


def test_static_asset_allowlist_does_not_become_a_wildcard(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    assert (
        application.respond("GET", "/assets/not-allowlisted.js", "127.0.0.1").status
        == HTTPStatus.NOT_FOUND
    )
    for path in MARKET_ATLAS_MODULE_PATHS:
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"):
            response = application.respond(method, path, "127.0.0.1")
            assert response.status == HTTPStatus.METHOD_NOT_ALLOWED
            assert response.headers["Allow"] == "GET, HEAD"


def test_sse_emits_exact_compact_revision_metadata_only(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    buffer = _seeded_revision_buffer(database)
    application = PredictionDashboardApplication(
        database, clock=lambda: NOW, revision_buffer=buffer
    )

    response = application.respond(
        "GET", "/api/v1/predictions-events", "127.0.0.1", last_event_id=None
    )

    assert response.status == HTTPStatus.OK
    assert response.content_type == "text/event-stream; charset=utf-8"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Accel-Buffering"] == "no"
    lines = response.body.decode("utf-8").splitlines()
    assert lines[0] == "retry: 3000"
    assert lines[1] == "id: 1"
    assert lines[2] == "event: revision"
    assert lines[3].startswith("data: {")
    document = json.loads(lines[3].removeprefix("data: "))
    assert set(document) == {
        "schema_version",
        "revision_id",
        "as_of",
        "emitted_at",
        "changed_domains",
    }
    forbidden = {
        "event_id",
        "posting_count",
        "markets",
        "account_fingerprint",
        "canonical_order_json",
        "raw_payload",
        "command",
    }
    assert forbidden.isdisjoint(document)
    assert response.body.endswith(b"\n\n")


def test_sse_resume_reset_keepalive_and_coalescing(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    buffer = _seeded_revision_buffer(database, capacity=2)
    store = PredictionMarketStore(database)
    try:
        base = PredictionDashboardBuilder(store, database).build(NOW)
    finally:
        store.close()
    second = buffer.publish(_changed_snapshot(base, 2))
    latest = buffer.publish(_changed_snapshot(base, 3))
    assert second is not None and latest is not None
    application = PredictionDashboardApplication(
        database, clock=lambda: NOW, revision_buffer=buffer
    )

    reset = application.respond("GET", "/api/v1/predictions-events", "127.0.0.1", last_event_id="1")
    assert reset.body.startswith(b"retry: 3000\nid: 3\nevent: reset\ndata: ")
    reset_data = json.loads(reset.body.split(b"data: ", 1)[1])
    assert set(reset_data) == {
        "schema_version",
        "latest_revision_id",
        "emitted_at",
        "reason",
    }

    keepalive = application.respond(
        "GET", "/api/v1/predictions-events", "127.0.0.1", last_event_id="3"
    )
    assert keepalive.body == b": keepalive\n\n"


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "HEAD"]
)
def test_events_route_is_get_only(tmp_path: Path, method: str) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)
    response = application.respond(method, "/api/v1/predictions-events", "127.0.0.1")
    assert response.status == HTTPStatus.METHOD_NOT_ALLOWED
    assert response.headers["Allow"] == "GET"


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"]
)
@pytest.mark.parametrize("path", ["/api/v1/predictions-dashboard", "/", "/assets/app.js"])
def test_snapshot_and_static_routes_have_no_mutating_or_bidirectional_method(
    tmp_path: Path, method: str, path: str
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)
    response = application.respond(method, path, "127.0.0.1")
    assert response.status == HTTPStatus.METHOD_NOT_ALLOWED
    assert response.headers["Allow"] == "GET, HEAD"


def test_events_reject_query_non_loopback_and_invalid_cursor(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(
        database,
        clock=lambda: NOW,
        revision_buffer=_seeded_revision_buffer(database),
    )
    assert (
        application.respond("GET", "/api/v1/predictions-events?x=1", "127.0.0.1").status
        == HTTPStatus.BAD_REQUEST
    )
    assert (
        application.respond("GET", "/api/v1/predictions-events", "example.com").status
        == HTTPStatus.BAD_REQUEST
    )
    response = application.respond(
        "GET", "/api/v1/predictions-events", "127.0.0.1", last_event_id="hostile"
    )
    assert response.status == HTTPStatus.BAD_REQUEST
    assert b"INVALID_EVENT_CURSOR" in response.body


def test_broken_sse_pipe_is_a_clean_disconnect() -> None:
    class BrokenPipe:
        def write(self, _body: bytes) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise AssertionError("flush cannot follow a broken write")

    assert prediction_dashboard_server._write_sse_frame(BrokenPipe(), b"frame") is False
    healthy = BytesIO()
    assert prediction_dashboard_server._write_sse_frame(healthy, b"frame") is True
    assert healthy.getvalue() == b"frame"


def test_empty_revision_buffer_with_supplied_cursor_is_snapshot_unavailable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(
        database,
        clock=lambda: NOW,
        revision_buffer=DashboardRevisionBuffer(capacity=2, clock=lambda: NOW),
    )

    response = application.respond(
        "GET", "/api/v1/predictions-events", "127.0.0.1", last_event_id="1"
    )

    assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(response.body) == {"error": {"code": "SNAPSHOT_UNAVAILABLE"}}


def test_host_and_event_cursor_are_bounded_before_regex_or_integer_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    class GuardedPattern:
        def fullmatch(self, _value: str) -> object:
            raise AssertionError("unbounded input reached parser")

    original_host_pattern = prediction_dashboard_server._HOST
    monkeypatch.setattr(prediction_dashboard_server, "_HOST", GuardedPattern())
    huge_host = "127.0.0.1:" + "9" * 10_000
    host_response = application.respond("GET", "/healthz", huge_host)
    assert host_response.status == HTTPStatus.BAD_REQUEST
    assert b"INVALID_HOST" in host_response.body

    monkeypatch.setattr(prediction_dashboard_server, "_HOST", original_host_pattern)
    monkeypatch.setattr(prediction_dashboard_live, "_EVENT_ID", GuardedPattern())
    huge_cursor = "9" * 10_000
    cursor_response = application.respond(
        "GET", "/api/v1/predictions-events", "127.0.0.1", last_event_id=huge_cursor
    )
    assert cursor_response.status == HTTPStatus.BAD_REQUEST
    assert b"INVALID_EVENT_CURSOR" in cursor_response.body


def test_request_parsing_wait_and_serialization_failures_are_fixed_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    canary = "private-key authorization signed-body account geographic-ip raw-target"
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    malformed = application.respond("GET", "http://[", "127.0.0.1")
    assert malformed.status == HTTPStatus.BAD_REQUEST
    assert json.loads(malformed.body) == {"error": {"code": "INVALID_REQUEST_TARGET"}}

    monkeypatch.setattr(
        application.revision_buffer,
        "wait_for_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(canary)),
    )
    failed_wait = application.respond("GET", "/api/v1/predictions-events", "127.0.0.1")
    assert failed_wait.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(failed_wait.body) == {"error": {"code": "EVENT_STREAM_UNAVAILABLE"}}

    seeded = _seeded_revision_buffer(database)
    serializing = PredictionDashboardApplication(
        database, clock=lambda: NOW, revision_buffer=seeded
    )
    monkeypatch.setattr(
        prediction_dashboard_server,
        "_sse_event_frame",
        lambda _event: (_ for _ in ()).throw(RuntimeError(canary)),
    )
    failed_serialization = serializing.respond("GET", "/api/v1/predictions-events", "127.0.0.1")
    assert failed_serialization.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert json.loads(failed_serialization.body) == {"error": {"code": "EVENT_STREAM_UNAVAILABLE"}}
    combined = failed_wait.body + failed_serialization.body + caplog.text.encode()
    assert canary.encode() not in combined


def test_handler_default_logging_never_renders_raw_request_values(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)
    handler = object.__new__(prediction_dashboard_server._handler_for(application))
    raw_target_canary = "GET /?authorization=private-key-account-ip HTTP/1.1"
    caplog.set_level("INFO", logger=prediction_dashboard_server.__name__)

    handler.log_message("%s", raw_target_canary)

    assert raw_target_canary not in caplog.text


def test_server_publishes_synchronously_before_starting_or_listening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    calls: list[str] = []
    publisher_instance = None

    class StubPublisher:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal publisher_instance
            publisher_instance = self

        def poll_once(self) -> None:
            calls.append("poll")

        def latest_snapshot(self) -> PredictionDashboardSnapshot:
            raise AssertionError("server should not request during construction")

        def start(self) -> None:
            calls.append("start")

        def close(self) -> None:
            calls.append("publisher-close")

    class StubApplication:
        def __init__(
            self,
            _database_path: Path,
            _clock: object = None,
            *,
            clock: object = None,
            revision_buffer: object,
            snapshot_provider: object,
        ) -> None:
            del self, _clock, clock, revision_buffer
            assert publisher_instance is not None
            assert snapshot_provider == publisher_instance.latest_snapshot

    class StubServer:
        server_port = 8787

        def __init__(self, _address: tuple[str, int], _handler: object) -> None:
            calls.append("listen")

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            calls.append("server-close")

    monkeypatch.setattr(prediction_dashboard_server, "DashboardRevisionPublisher", StubPublisher)
    monkeypatch.setattr(
        prediction_dashboard_server, "PredictionDashboardApplication", StubApplication
    )
    monkeypatch.setattr(prediction_dashboard_server, "ThreadingHTTPServer", StubServer)

    serve_prediction_dashboard(database, 8787, clock=lambda: NOW)

    assert calls[:3] == ["poll", "start", "listen"]
