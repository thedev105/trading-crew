from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any

import pytest

from polytrading.predictions.pilot.server import (
    CSRF_HEADER,
    MAXIMUM_BODY_BYTES,
    PILOT_ROUTES,
    SECURITY_HEADERS,
    SESSION_COOKIE,
    PilotApplication,
    PilotRequest,
    PilotRequestError,
    PilotResponse,
)

PORT = 8788
ORIGIN = f"http://localhost:{PORT}"
HOST = f"localhost:{PORT}"
NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


class RecordingServices:
    """Records what the control plane asked for and never inspects transport details."""

    def __init__(self, *, failure: PilotRequestError | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.failure = failure

    def _record(self, name: str, payload: Any = None) -> dict[str, object]:
        self.calls.append((name, payload))
        if self.failure is not None:
            raise self.failure
        return {"route": name}

    def readiness(self) -> dict[str, object]:
        return self._record("readiness")

    def policy(self) -> dict[str, object]:
        return self._record("policy")

    def opportunities(self) -> dict[str, object]:
        return self._record("opportunities")

    def live_session(self) -> dict[str, object]:
        return self._record("live_session")

    def audit(self) -> dict[str, object]:
        return self._record("audit")

    def update_policy(self, payload: dict[str, object]) -> dict[str, object]:
        return self._record("update_policy", payload)

    def register_options(self, payload: dict[str, object]) -> dict[str, object]:
        return self._record("register_options", payload)

    def register_verify(self, payload: dict[str, object]) -> dict[str, object]:
        return self._record("register_verify", payload)

    def auth_options(self, payload: dict[str, object]) -> dict[str, object]:
        return self._record("auth_options", payload)

    def provision_credentials(self, payload: dict[str, object]) -> dict[str, object]:
        return self._record("provision_credentials", payload)

    def activate(self, payload: dict[str, object]) -> dict[str, object]:
        return self._record("activate", payload)

    def authorize(self, payload: dict[str, object]) -> dict[str, object]:
        return self._record("authorize", payload)

    def presence(self, payload: dict[str, object]) -> dict[str, object]:
        return self._record("presence", payload)

    def stop(self, payload: dict[str, object]) -> dict[str, object]:
        return self._record("stop", payload)

    def clear_kill(self, payload: dict[str, object]) -> dict[str, object]:
        return self._record("clear_kill", payload)


def application(**overrides: Any) -> PilotApplication:
    return PilotApplication(overrides.pop("services", RecordingServices()), port=PORT, **overrides)


def request(method: str, target: str, **overrides: Any) -> PilotRequest:
    fields: dict[str, Any] = {
        "method": method,
        "target": target,
        "host": HOST,
        "received_at": NOW,
        "origin": ORIGIN if method == "POST" else None,
        "headers": {},
        "cookies": {},
        "body": b"",
    }
    fields.update(overrides)
    return PilotRequest(**fields)


def opened(app: PilotApplication) -> tuple[str, str]:
    response = app.respond(request("POST", "/api/v1/pilot/session"))
    assert response.status is HTTPStatus.CREATED
    assert response.set_cookie is not None
    token = response.set_cookie.split(";", 1)[0].split("=", 1)[1]
    csrf = _body(response)["csrf_token"]
    return token, str(csrf)


def _body(response: PilotResponse) -> dict[str, Any]:
    import json

    return json.loads(response.body.decode("utf-8"))


def authenticated(
    app: PilotApplication, method: str, target: str, **overrides: Any
) -> PilotResponse:
    token, csrf = overrides.pop("credentials", None) or opened(app)
    headers = dict(overrides.pop("headers", {}))
    if method == "POST":
        headers.setdefault(CSRF_HEADER, (csrf,))
    return app.respond(
        request(
            method,
            target,
            headers=headers,
            cookies={SESSION_COOKIE: (token,)},
            **overrides,
        )
    )


def test_session_creation_sets_a_strict_httponly_cookie() -> None:
    app = application()
    response = app.respond(request("POST", "/api/v1/pilot/session"))

    assert response.status is HTTPStatus.CREATED
    assert "HttpOnly" in response.set_cookie
    assert "SameSite=Strict" in response.set_cookie
    assert "Path=/" in response.set_cookie
    assert "Secure" not in response.set_cookie  # loopback http origin
    assert _body(response)["csrf_token"]


def test_every_response_carries_the_security_headers_and_no_cors() -> None:
    app = application()
    response = authenticated(app, "GET", "/api/v1/pilot/readiness")

    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert not any(name.lower().startswith("access-control") for name in response.headers)
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("host", ["localhost:9999", "127.0.0.1:8788", "example.test", ""])
def test_only_the_configured_host_is_served(host: str) -> None:
    app = application()
    response = app.respond(request("GET", "/api/v1/pilot/readiness", host=host))

    assert response.status is HTTPStatus.BAD_REQUEST
    assert _body(response)["error"] == "HOST_NOT_ALLOWED"


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:8788", "http://localhost:8789", "https://localhost:8788", None]
)
def test_a_state_changing_request_requires_the_exact_origin(origin: str | None) -> None:
    app = application()
    token, csrf = opened(app)
    response = app.respond(
        request(
            "POST",
            "/api/v1/pilot/presence",
            origin=origin,
            headers={CSRF_HEADER: (csrf,)},
            cookies={SESSION_COOKIE: (token,)},
        )
    )

    assert response.status is HTTPStatus.FORBIDDEN
    assert _body(response)["error"] == "ORIGIN_NOT_ALLOWED"


def test_a_read_from_a_foreign_origin_is_refused() -> None:
    app = application()
    token, _ = opened(app)
    response = app.respond(
        request(
            "GET",
            "/api/v1/pilot/readiness",
            origin="http://evil.test",
            cookies={SESSION_COOKIE: (token,)},
        )
    )

    assert response.status is HTTPStatus.FORBIDDEN


@pytest.mark.parametrize("method", ["OPTIONS", "TRACE", "CONNECT", "PUT", "DELETE", "PATCH"])
def test_no_method_outside_get_and_post_is_served(method: str) -> None:
    app = application()
    response = app.respond(request(method, "/api/v1/pilot/readiness"))

    assert response.status is HTTPStatus.METHOD_NOT_ALLOWED
    assert response.headers["Allow"] == "GET, POST"
    assert not any(name.lower().startswith("access-control") for name in response.headers)


@pytest.mark.parametrize(
    "target",
    [
        "http://localhost:8788/api/v1/pilot/readiness",
        "//evil.test/api/v1/pilot/readiness",
        "/api/v1/pilot/readiness?force=1",
        "/api/v1/pilot/readiness#fragment",
    ],
)
def test_absolute_or_decorated_targets_are_refused(target: str) -> None:
    app = application()
    response = app.respond(request("GET", target))

    assert response.status is HTTPStatus.BAD_REQUEST
    assert _body(response)["error"] == "REQUEST_TARGET_INVALID"


def test_an_oversized_target_is_refused() -> None:
    app = application()
    response = app.respond(request("GET", "/" + "a" * 1024))

    assert response.status is HTTPStatus.REQUEST_URI_TOO_LONG


def test_an_oversized_body_is_refused_before_any_handler() -> None:
    app = application()
    services = RecordingServices()
    app = PilotApplication(services, port=PORT)
    token, csrf = opened(app)
    response = app.respond(
        request(
            "POST",
            "/api/v1/pilot/presence",
            headers={CSRF_HEADER: (csrf,)},
            cookies={SESSION_COOKIE: (token,)},
            body=b"x" * (MAXIMUM_BODY_BYTES + 1),
        )
    )

    assert response.status is HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert services.calls == []


def test_duplicate_headers_or_cookies_are_refused() -> None:
    app = application()
    token, csrf = opened(app)
    duplicated_header = app.respond(
        request(
            "POST",
            "/api/v1/pilot/presence",
            headers={CSRF_HEADER: (csrf, csrf)},
            cookies={SESSION_COOKIE: (token,)},
        )
    )
    duplicated_cookie = app.respond(
        request(
            "GET",
            "/api/v1/pilot/readiness",
            cookies={SESSION_COOKIE: (token, token)},
        )
    )

    assert duplicated_header.status is HTTPStatus.BAD_REQUEST
    assert duplicated_cookie.status is HTTPStatus.BAD_REQUEST


@pytest.mark.parametrize(
    "target",
    ["/api/v1/pilot", "/api/v1/pilot/unknown", "/api/v1/predictions-dashboard", "/healthz"],
)
def test_only_the_fixed_route_table_is_reachable(target: str) -> None:
    app = application()
    response = authenticated(app, "GET", target)

    assert response.status is HTTPStatus.NOT_FOUND


def test_a_read_route_is_not_reachable_by_post_and_the_reverse() -> None:
    app = application()
    credentials = opened(app)

    posted_read = authenticated(app, "POST", "/api/v1/pilot/readiness", credentials=credentials)
    fetched_write = authenticated(app, "GET", "/api/v1/pilot/presence", credentials=credentials)

    assert posted_read.status is HTTPStatus.NOT_FOUND
    assert fetched_write.status is HTTPStatus.NOT_FOUND


def test_a_request_without_a_session_is_refused() -> None:
    app = application()
    response = app.respond(request("GET", "/api/v1/pilot/readiness"))

    assert response.status is HTTPStatus.UNAUTHORIZED
    assert _body(response)["error"] == "SESSION_REQUIRED"


def test_an_unknown_or_expired_session_is_refused() -> None:
    app = application()
    token, _ = opened(app)
    unknown = app.respond(
        request("GET", "/api/v1/pilot/readiness", cookies={SESSION_COOKIE: ("forged",)})
    )
    expired = app.respond(
        request(
            "GET",
            "/api/v1/pilot/readiness",
            cookies={SESSION_COOKIE: (token,)},
            received_at=NOW + timedelta(hours=2),
        )
    )

    assert unknown.status is HTTPStatus.UNAUTHORIZED
    assert expired.status is HTTPStatus.UNAUTHORIZED


def test_a_second_session_replaces_the_first() -> None:
    app = application()
    first_token, _ = opened(app)
    opened(app)

    response = app.respond(
        request("GET", "/api/v1/pilot/readiness", cookies={SESSION_COOKIE: (first_token,)})
    )

    assert response.status is HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize("csrf", [None, "forged-token", ""])
def test_a_state_changing_request_requires_its_csrf_token(csrf: str | None) -> None:
    app = application()
    token, _ = opened(app)
    headers = {} if csrf is None else {CSRF_HEADER: (csrf,)}
    response = app.respond(
        request(
            "POST",
            "/api/v1/pilot/presence",
            headers=headers,
            cookies={SESSION_COOKIE: (token,)},
        )
    )

    assert response.status is HTTPStatus.FORBIDDEN
    assert _body(response)["error"] == "CSRF_TOKEN_INVALID"


@pytest.mark.parametrize("body", [b"not json", b"[]", b'"text"', b'{"nan": NaN}', b"{1: 2}"])
def test_a_non_object_or_invalid_json_body_is_refused(body: bytes) -> None:
    app = application()
    response = authenticated(app, "POST", "/api/v1/pilot/presence", body=body)

    assert response.status is HTTPStatus.BAD_REQUEST
    assert _body(response)["error"] == "REQUEST_BODY_INVALID"


def test_a_read_route_may_not_carry_a_body() -> None:
    app = application()
    response = authenticated(app, "GET", "/api/v1/pilot/readiness", body=b"{}")

    assert response.status is HTTPStatus.BAD_REQUEST
    assert _body(response)["error"] == "REQUEST_BODY_NOT_ALLOWED"


def test_handlers_receive_the_session_hash_rather_than_the_session_token() -> None:
    services = RecordingServices()
    app = PilotApplication(services, port=PORT)
    token, csrf = opened(app)
    app.respond(
        request(
            "POST",
            "/api/v1/pilot/presence",
            headers={CSRF_HEADER: (csrf,)},
            cookies={SESSION_COOKIE: (token,)},
            body=b'{"opportunity_id":"abc"}',
        )
    )

    name, payload = services.calls[-1]
    assert name == "presence"
    assert payload["opportunity_id"] == "abc"
    assert payload["browser_session_hash"] != token
    assert token not in str(payload)


def test_rate_limits_apply_per_session() -> None:
    app = application()
    credentials = opened(app)
    statuses = {
        authenticated(app, "GET", "/api/v1/pilot/readiness", credentials=credentials).status
        for _ in range(70)
    }

    assert HTTPStatus.TOO_MANY_REQUESTS in statuses


def test_a_handler_failure_never_reaches_the_browser() -> None:
    services = RecordingServices()

    class ExplodingServices(RecordingServices):
        def readiness(self) -> dict[str, object]:
            raise RuntimeError("database at /Users/operator/private.duckdb is locked")

    app = PilotApplication(ExplodingServices(), port=PORT)
    response = authenticated(app, "GET", "/api/v1/pilot/readiness")

    assert response.status is HTTPStatus.INTERNAL_SERVER_ERROR
    assert _body(response) == {"error": "PILOT_REQUEST_FAILED"}
    assert b"private.duckdb" not in response.body
    assert services.calls == []


def test_terminal_diagnostics_record_only_safe_unexpected_failure_details() -> None:
    class ExplodingServices(RecordingServices):
        def readiness(self) -> dict[str, object]:
            raise RuntimeError("wallet-private-key=super-secret")

    diagnostics: list[str] = []
    app = PilotApplication(ExplodingServices(), port=PORT, error_reporter=diagnostics.append)
    response = authenticated(app, "GET", "/api/v1/pilot/readiness")

    assert response.status is HTTPStatus.INTERNAL_SERVER_ERROR
    assert _body(response) == {"error": "PILOT_REQUEST_FAILED"}
    assert diagnostics == [
        '{"error":"PILOT_REQUEST_FAILED","exception":"RuntimeError",'
        '"method":"GET","route":"readiness","status":500,'
        '"timestamp":"2026-08-29T12:00:00+00:00"}'
    ]
    assert "super-secret" not in diagnostics[0]
    assert "wallet-private-key" not in diagnostics[0]


def test_terminal_diagnostics_never_record_an_invalid_request_target() -> None:
    diagnostics: list[str] = []
    app = application(error_reporter=diagnostics.append)
    response = app.respond(request("GET", "/api/v1/pilot/readiness?token=super-secret"))

    assert response.status is HTTPStatus.BAD_REQUEST
    assert _body(response) == {"error": "REQUEST_TARGET_INVALID"}
    assert diagnostics == [
        '{"error":"REQUEST_TARGET_INVALID","method":"GET","route":null,'
        '"status":400,"timestamp":"2026-08-29T12:00:00+00:00"}'
    ]
    assert "super-secret" not in diagnostics[0]


def test_a_typed_handler_rejection_keeps_its_code() -> None:
    services = RecordingServices(
        failure=PilotRequestError(HTTPStatus.CONFLICT, "PILOT_KILL_ENGAGED")
    )
    app = PilotApplication(services, port=PORT)
    response = authenticated(app, "GET", "/api/v1/pilot/readiness")

    assert response.status is HTTPStatus.CONFLICT
    assert _body(response)["error"] == "PILOT_KILL_ENGAGED"


def test_the_route_table_is_exactly_the_reviewed_surface() -> None:
    assert set(PILOT_ROUTES.values()) == {
        "create_browser_session",
        "readiness",
        "policy",
        "opportunities",
        "live_session",
        "audit",
        "update_policy",
        "register_options",
        "register_verify",
        "auth_options",
        "provision_credentials",
        "activate",
        "authorize",
        "presence",
        "stop",
        "clear_kill",
    }
    assert all(method in {"GET", "POST"} for method, _ in PILOT_ROUTES)
    assert all(path.startswith("/api/v1/pilot") for _, path in PILOT_ROUTES)


# -- cockpit assets ---------------------------------------------------------------------------


def test_the_cockpit_assets_are_served_from_the_exact_asset_map() -> None:
    from polytrading.predictions.pilot.server import PILOT_ASSETS

    app = application()
    for path, (_name, content_type) in PILOT_ASSETS.items():
        response = app.respond(request("GET", path))
        assert response.status is HTTPStatus.OK, path
        assert response.content_type == content_type
        assert response.body
        assert response.headers["Content-Security-Policy"].startswith("default-src 'none'")


def test_assets_need_no_session_but_still_refuse_a_foreign_origin() -> None:
    app = application()

    anonymous = app.respond(request("GET", "/"))
    foreign = app.respond(
        PilotRequest(
            method="GET",
            target="/",
            host=HOST,
            received_at=NOW,
            origin="http://evil.test",
        )
    )

    assert anonymous.status is HTTPStatus.OK
    assert foreign.status is HTTPStatus.FORBIDDEN


@pytest.mark.parametrize(
    "target", ["/assets/", "/assets/secret.js", "/assets/../app.js", "/index.html"]
)
def test_no_path_outside_the_asset_map_is_served(target: str) -> None:
    response = application().respond(request("GET", target))

    assert response.status in {HTTPStatus.NOT_FOUND, HTTPStatus.BAD_REQUEST}


def test_assets_are_not_writable() -> None:
    app = application()
    credentials = opened(app)

    response = authenticated(app, "POST", "/", credentials=credentials)

    assert response.status is HTTPStatus.NOT_FOUND
