"""The exact-origin loopback control plane for the local pilot.

Every request must arrive at one configured ``http://localhost:<port>`` origin, carry the browser
session cookie this server issued, and — when it changes anything — its CSRF token. The route
table is fixed: an unregistered path or method is a flat rejection, never a fallback. Requests
carry stable identifiers only; the server resolves and recomputes all action material itself.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any, Final, Literal, Protocol
from urllib.parse import urlsplit

MAXIMUM_TARGET_BYTES: Final = 1024
MAXIMUM_BODY_BYTES: Final = 16_384
MAXIMUM_HEADER_BYTES: Final = 4096
SESSION_COOKIE: Final = "pilot_session"
CSRF_HEADER: Final = "x-pilot-csrf"
SESSION_LIFETIME: Final = timedelta(hours=1)
RATE_LIMIT_WINDOW: Final = timedelta(seconds=10)
RATE_LIMIT_REQUESTS: Final = 60

# The complete surface. Nothing else is routable, and no route takes a query or a fragment.
PILOT_ROUTES: Final[Mapping[tuple[str, str], str]] = {
    ("POST", "/api/v1/pilot/session"): "create_browser_session",
    ("GET", "/api/v1/pilot/readiness"): "readiness",
    ("GET", "/api/v1/pilot/policy"): "policy",
    ("GET", "/api/v1/pilot/opportunities"): "opportunities",
    ("GET", "/api/v1/pilot/live-session"): "live_session",
    ("GET", "/api/v1/pilot/audit"): "audit",
    ("POST", "/api/v1/pilot/policy"): "update_policy",
    ("POST", "/api/v1/pilot/passkeys/register/options"): "register_options",
    ("POST", "/api/v1/pilot/passkeys/register/verify"): "register_verify",
    ("POST", "/api/v1/pilot/passkeys/authenticate/options"): "auth_options",
    ("POST", "/api/v1/pilot/credentials/provision"): "provision_credentials",
    ("POST", "/api/v1/pilot/activation"): "activate",
    ("POST", "/api/v1/pilot/authorizations"): "authorize",
    ("POST", "/api/v1/pilot/presence"): "presence",
    ("POST", "/api/v1/pilot/stop"): "stop",
    ("POST", "/api/v1/pilot/kill/clear"): "clear_kill",
}
_PUBLIC_ROUTES: Final = frozenset({"create_browser_session"})
_ALLOWED_METHODS: Final = ("GET", "POST")

# No CORS header appears anywhere: a cross-origin reader must get nothing, not a filtered answer.
SECURITY_HEADERS: Final[Mapping[str, str]] = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "font-src 'self'; connect-src 'self'; form-action 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
        "microphone=(), payment=(), usb=()"
    ),
    "Cache-Control": "no-store",
    "Connection": "close",
}


@dataclass(frozen=True, slots=True)
class PilotRequest:
    method: str
    target: str
    host: str
    received_at: datetime
    origin: str | None = None
    headers: Mapping[str, Sequence[str]] = field(default_factory=dict)
    cookies: Mapping[str, Sequence[str]] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class PilotResponse:
    status: HTTPStatus
    content_type: str
    body: bytes
    headers: Mapping[str, str]
    set_cookie: str | None = None


class PilotServices(Protocol):
    """Everything the control plane may ask for. Each method resolves its own evidence."""

    def readiness(self) -> Mapping[str, object]: ...

    def policy(self) -> Mapping[str, object]: ...

    def opportunities(self) -> Mapping[str, object]: ...

    def live_session(self) -> Mapping[str, object]: ...

    def audit(self) -> Mapping[str, object]: ...

    def update_policy(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def register_options(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def register_verify(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def auth_options(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def provision_credentials(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def activate(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def authorize(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def presence(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def stop(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...

    def clear_kill(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class _BrowserSession:
    session_token: str
    csrf_token: str
    session_hash: str
    created_at: datetime


class PilotApplication:
    """One local operator's control plane, bound to one origin and one browser session at a time."""

    def __init__(
        self,
        services: PilotServices,
        *,
        port: int,
        clock: Any = None,
    ) -> None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("PILOT_PORT_INVALID")
        self._services = services
        self._port = port
        self._origin = f"http://localhost:{port}"
        self._host = f"localhost:{port}"
        self._clock = clock
        self._sessions: dict[str, _BrowserSession] = {}
        self._requests: dict[str, list[datetime]] = {}

    @property
    def origin(self) -> str:
        return self._origin

    def respond(self, request: PilotRequest) -> PilotResponse:
        rejection = self._reject_transport(request)
        if rejection is not None:
            return rejection
        path = urlsplit(request.target).path
        handler_name = PILOT_ROUTES[(request.method, path)]
        if handler_name == "create_browser_session":
            return self._create_browser_session(request)
        session = self._authenticated_session(request)
        if session is None:
            return _error(HTTPStatus.UNAUTHORIZED, "SESSION_REQUIRED")
        if not self._within_rate_limit(session, request.received_at):
            return _error(HTTPStatus.TOO_MANY_REQUESTS, "RATE_LIMITED")
        if request.method == "POST":
            csrf = _single_header(request.headers, CSRF_HEADER)
            if csrf is None or not secrets.compare_digest(csrf, session.csrf_token):
                return _error(HTTPStatus.FORBIDDEN, "CSRF_TOKEN_INVALID")
            payload = _parse_json_object(request.body)
            if payload is None:
                return _error(HTTPStatus.BAD_REQUEST, "REQUEST_BODY_INVALID")
            payload = {**payload, "browser_session_hash": session.session_hash}
            return self._dispatch(handler_name, payload)
        if request.body:
            return _error(HTTPStatus.BAD_REQUEST, "REQUEST_BODY_NOT_ALLOWED")
        return self._dispatch(handler_name, None)

    def _reject_transport(self, request: PilotRequest) -> PilotResponse | None:
        if request.host != self._host:
            return _error(HTTPStatus.BAD_REQUEST, "HOST_NOT_ALLOWED")
        if not _bounded_headers(request.headers) or not _bounded_headers(request.cookies):
            return _error(HTTPStatus.BAD_REQUEST, "HEADER_INVALID")
        if len(request.target.encode("utf-8")) > MAXIMUM_TARGET_BYTES:
            return _error(HTTPStatus.REQUEST_URI_TOO_LONG, "REQUEST_TARGET_INVALID")
        try:
            parsed = urlsplit(request.target)
        except ValueError:
            return _error(HTTPStatus.BAD_REQUEST, "REQUEST_TARGET_INVALID")
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            return _error(HTTPStatus.BAD_REQUEST, "REQUEST_TARGET_INVALID")
        if request.method not in _ALLOWED_METHODS:
            return _error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "METHOD_NOT_ALLOWED",
                headers={"Allow": ", ".join(_ALLOWED_METHODS)},
            )
        if (request.method, parsed.path) not in PILOT_ROUTES:
            if any(method == request.method for method, _ in PILOT_ROUTES):
                return _error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
            return _error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
        if len(request.body) > MAXIMUM_BODY_BYTES:
            return _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "REQUEST_BODY_TOO_LARGE")
        if request.method == "POST" and request.origin != self._origin:
            return _error(HTTPStatus.FORBIDDEN, "ORIGIN_NOT_ALLOWED")
        if request.method == "GET" and request.origin not in (None, self._origin):
            return _error(HTTPStatus.FORBIDDEN, "ORIGIN_NOT_ALLOWED")
        return None

    def _create_browser_session(self, request: PilotRequest) -> PilotResponse:
        from hashlib import sha256

        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session = _BrowserSession(
            session_token=session_token,
            csrf_token=csrf_token,
            session_hash=sha256(session_token.encode("ascii")).hexdigest(),
            created_at=request.received_at,
        )
        self._sessions = {session_token: session}
        self._requests = {session_token: []}
        response = _json(HTTPStatus.CREATED, {"csrf_token": csrf_token})
        return PilotResponse(
            status=response.status,
            content_type=response.content_type,
            body=response.body,
            headers=response.headers,
            set_cookie=(f"{SESSION_COOKIE}={session_token}; Path=/; HttpOnly; SameSite=Strict"),
        )

    def _authenticated_session(self, request: PilotRequest) -> _BrowserSession | None:
        token = _single_header(request.cookies, SESSION_COOKIE)
        if token is None:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        if request.received_at - session.created_at > SESSION_LIFETIME:
            self._sessions.pop(token, None)
            return None
        return session

    def _within_rate_limit(self, session: _BrowserSession, now: datetime) -> bool:
        recent = [
            moment
            for moment in self._requests.get(session.session_token, ())
            if now - moment < RATE_LIMIT_WINDOW
        ]
        recent.append(now)
        self._requests[session.session_token] = recent
        return len(recent) <= RATE_LIMIT_REQUESTS

    def _dispatch(self, handler_name: str, payload: Mapping[str, object] | None) -> PilotResponse:
        handler = getattr(self._services, handler_name, None)
        if handler is None:
            return _error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
        try:
            result = handler() if payload is None else handler(payload)
        except PilotRequestError as error:
            return _error(error.status, error.code)
        except Exception:
            # Nothing about the failure reaches the browser: only a stable code.
            return _error(HTTPStatus.INTERNAL_SERVER_ERROR, "PILOT_REQUEST_FAILED")
        return _json(HTTPStatus.OK, result)


class PilotRequestError(Exception):
    """A handler rejection the control plane may report with a stable code."""

    def __init__(self, status: HTTPStatus, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _bounded_headers(headers: Mapping[str, Sequence[str]]) -> bool:
    for name, values in headers.items():
        if type(name) is not str or len(values) != 1:
            return False
        value = values[0]
        if type(value) is not str or len(value.encode("utf-8")) > MAXIMUM_HEADER_BYTES:
            return False
    return True


def _single_header(headers: Mapping[str, Sequence[str]], name: str) -> str | None:
    for key, values in headers.items():
        if key.casefold() == name and len(values) == 1:
            return values[0]
    return None


def _parse_json_object(body: bytes) -> dict[str, object] | None:
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        return None
    if type(parsed) is not dict or any(type(key) is not str for key in parsed):
        return None
    return parsed


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _json(status: HTTPStatus, payload: Mapping[str, object] | Any) -> PilotResponse:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return PilotResponse(
        status=status,
        content_type="application/json; charset=utf-8",
        body=body.encode("utf-8"),
        headers=dict(SECURITY_HEADERS),
    )


def _error(
    status: HTTPStatus, code: str, *, headers: Mapping[str, str] | None = None
) -> PilotResponse:
    response = _json(status, {"error": code})
    return PilotResponse(
        status=response.status,
        content_type=response.content_type,
        body=response.body,
        headers={**response.headers, **(headers or {})},
    )


PilotRouteName = Literal[
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
]

__all__ = [
    "CSRF_HEADER",
    "MAXIMUM_BODY_BYTES",
    "PILOT_ROUTES",
    "SECURITY_HEADERS",
    "SESSION_COOKIE",
    "PilotApplication",
    "PilotRequest",
    "PilotRequestError",
    "PilotResponse",
    "PilotRouteName",
    "PilotServices",
]
