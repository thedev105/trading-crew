"""Fail-closed composition for the local pilot process.

Startup proves the database schema, the origin and RP ID, the pilot protocol checkpoint, and the
secret-store posture before anything is served, and the process always starts killed. No wallet
key, credential, or authenticated transport is constructed here: those exist only inside the
signer, and only after an operator ceremony that later increments gate.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, Literal

from polytrading.lifecycle import owned_resource_cleanup
from polytrading.predictions.pilot.capabilities import PilotCapabilityIssuer
from polytrading.predictions.pilot.launch import compose_pilot_environment
from polytrading.predictions.pilot.passkeys import (
    RP_ID,
    PasskeyError,
    PasskeyService,
    PyWebAuthnPasskeyService,
    pilot_origin,
)
from polytrading.predictions.pilot.policy import COMPILED_PILOT_CEILINGS
from polytrading.predictions.pilot.presence import NativePresenceSource, PresenceMonitor
from polytrading.predictions.pilot.presence_macos import MacOSPresenceSource
from polytrading.predictions.pilot.server import (
    MAXIMUM_BODY_BYTES,
    PilotApplication,
    PilotRequest,
    PilotRequestError,
    PilotResponse,
)
from polytrading.predictions.pilot.services import LivePilotServices, PilotEnvironment
from polytrading.predictions.pilot.signer_bootstrap import (
    SignerBootstrapError,
    SignerChannel,
    launch_signer_sidecar,
)
from polytrading.predictions.pilot.signer_link import describe_identity
from polytrading.predictions.pilot.signer_services import offline_pilot_signer_service
from polytrading.predictions.pilot.verifier import PilotCapabilityVerifier
from polytrading.predictions.polymarket_execution.keychain_macos import (
    MacOSKeychainSecretStore,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    verify_protocol_sources,
)
from polytrading.predictions.polymarket_execution.secrets import SecretStoreError
from polytrading.predictions.storage.store import PredictionMarketStore

PilotRuntimeCode = Literal[
    "PILOT_DATABASE_MISSING",
    "PILOT_DATABASE_SCHEMA_STALE",
    "PILOT_ORIGIN_INVALID",
    "PILOT_PROTOCOL_REVIEW_REQUIRED",
    "PILOT_SECRET_STORE_UNAVAILABLE",
]
_LOOPBACK_ADDRESS: Final = "127.0.0.1"


class PilotRuntimeError(RuntimeError):
    """A startup gate the pilot refuses to pass, named by a stable code."""

    def __init__(self, code: PilotRuntimeCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PilotPosture:
    """The sanitized posture the control plane may report before any authority exists."""

    kill_engaged: bool
    protocol_version: str
    rp_id: str
    origin: str
    secret_store_available: bool


class KilledPilotServices:
    """The services a freshly launched pilot exposes: read-only posture, no authority.

    Every launch starts killed (spec section 3.2), so each mutating route refuses until the
    operator ceremonies that later increments implement have run.
    """

    def __init__(self, posture: PilotPosture, *, ceilings: Mapping[str, object]) -> None:
        self._posture = posture
        self._ceilings = ceilings

    def readiness(self) -> dict[str, object]:
        return {
            "kill_engaged": self._posture.kill_engaged,
            "protocol_version": self._posture.protocol_version,
            "rp_id": self._posture.rp_id,
            "origin": self._posture.origin,
            "secret_store_available": self._posture.secret_store_available,
            "live_authority": False,
        }

    def policy(self) -> dict[str, object]:
        return {"ceilings": dict(self._ceilings), "requested_limits": None}

    def opportunities(self) -> dict[str, object]:
        return {"opportunities": [], "reason": "PILOT_KILL_ENGAGED"}

    def live_session(self) -> dict[str, object]:
        return {"session": None, "reason": "PILOT_KILL_ENGAGED"}

    def audit(self) -> dict[str, object]:
        return {"events": []}

    def _refuse(self, payload: Mapping[str, object]) -> dict[str, object]:
        del payload
        raise PilotRequestError(HTTPStatus.CONFLICT, "PILOT_KILL_ENGAGED")

    update_policy = _refuse
    register_options = _refuse
    register_verify = _refuse
    auth_options = _refuse
    provision_credentials = _refuse
    activate = _refuse
    authorize = _refuse
    presence = _refuse
    stop = _refuse
    clear_kill = _refuse


@dataclass(frozen=True, slots=True)
class PilotRuntime:
    application: PilotApplication
    posture: PilotPosture
    store: PredictionMarketStore
    issuer: PilotCapabilityIssuer | None = None
    verifier: PilotCapabilityVerifier | None = None
    signer_channel: SignerChannel | None = None

    def close(self) -> None:
        """Drop this launch's signing key first, then the database handle."""
        if self.issuer is not None:
            self.issuer.close()
        if self.signer_channel is not None:
            self.signer_channel.close()
        self.store.close()


def build_pilot_runtime(
    database_path: Path,
    port: int,
    *,
    platform: str = "darwin",
    now: Callable[[], datetime] | None = None,
    environment: PilotEnvironment | None = None,
    passkeys: PasskeyService | None = None,
    presence_source: NativePresenceSource | None = None,
    key_id: str = "pilot-launch",
) -> PilotRuntime:
    """Validate every launch gate, then compose a control plane that starts killed.

    Without an ``environment`` the pilot serves posture only. With one, the operator ceremonies
    are reachable — and each still refuses until its own gates pass, because the launch begins
    killed and every grant needs its own passkey assertion.
    """

    clock = now or (lambda: datetime.now(UTC))
    if not Path(database_path).is_file():
        raise PilotRuntimeError("PILOT_DATABASE_MISSING")
    try:
        origin = pilot_origin(port)
    except PasskeyError as error:
        raise PilotRuntimeError("PILOT_ORIGIN_INVALID") from error
    readiness = verify_protocol_sources(version=POLYMARKET_PILOT_PROTOCOL_VERSION)
    if readiness.state != "CURRENT":
        raise PilotRuntimeError("PILOT_PROTOCOL_REVIEW_REQUIRED")
    secret_store_available = _secret_store_available(platform)
    if not secret_store_available:
        raise PilotRuntimeError("PILOT_SECRET_STORE_UNAVAILABLE")
    try:
        store = PredictionMarketStore(Path(database_path), read_only=environment is None)
    except Exception as error:
        raise PilotRuntimeError("PILOT_DATABASE_SCHEMA_STALE") from error
    posture = PilotPosture(
        kill_engaged=True,
        protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
        rp_id=RP_ID,
        origin=origin,
        secret_store_available=secret_store_available,
    )
    if environment is None:
        services: object = KilledPilotServices(
            posture, ceilings=COMPILED_PILOT_CEILINGS.model_dump(mode="json")
        )
        return PilotRuntime(
            application=PilotApplication(services, port=port), posture=posture, store=store
        )

    issuer = PilotCapabilityIssuer(key_id=key_id)
    verifier = PilotCapabilityVerifier(issuer.public_verification_key)
    monitor = PresenceMonitor(
        source=presence_source or MacOSPresenceSource(platform=platform),
        started_at=clock(),
    )
    services = LivePilotServices(
        store=store,
        environment=environment,
        passkeys=passkeys or PyWebAuthnPasskeyService(port=port),
        issuer=issuer,
        verifier=verifier,
        presence=monitor,
        clock=clock,
    )
    return PilotRuntime(
        application=PilotApplication(services, port=port),
        posture=posture,
        store=store,
        issuer=issuer,
        verifier=verifier,
    )


def build_launch_runtime(
    database_path: Path,
    port: int,
    *,
    platform: str = "darwin",
    bootstrap: Callable[[], SignerChannel] | None = None,
    now: Callable[[], datetime] | None = None,
) -> PilotRuntime:
    """Launch the sidecar and compose live-but-killed services, or serve posture only."""
    clock = now or (lambda: datetime.now(UTC))
    try:
        channel = (bootstrap or _bootstrap_signer(platform))()
        account_fingerprint, wallet_fingerprint = describe_identity(
            channel.request_stream, channel.response_stream, clock=clock
        )
    except (SignerBootstrapError, SecretStoreError) as error:
        print(
            f"pilot: signer unavailable ({error}); serving posture only",
            file=sys.stderr,
        )
        return build_pilot_runtime(database_path, port, platform=platform, now=clock)
    store = PredictionMarketStore(Path(database_path))
    try:
        environment = compose_pilot_environment(
            store,
            account_fingerprint=account_fingerprint,
            wallet_fingerprint=wallet_fingerprint,
            credentials_present=channel.credentials_present,
            now=clock,
        )
    except BaseException:
        channel.close()
        raise
    finally:
        store.close()
    runtime = build_pilot_runtime(
        database_path, port, platform=platform, now=clock, environment=environment
    )
    object.__setattr__(runtime, "signer_channel", channel)
    return runtime


def _bootstrap_signer(platform: str) -> Callable[[], SignerChannel]:
    return lambda: launch_signer_sidecar(
        store=MacOSKeychainSecretStore(platform=platform),
        service_factory=offline_pilot_signer_service,
    )


def _secret_store_available(platform: str) -> bool:
    """Probe the secret-store boundary without reading, unlocking, or creating any item."""
    try:
        MacOSKeychainSecretStore(library=_UnavailableKeychainLibrary(), platform=platform)
    except SecretStoreError:
        return False
    return True


class _UnavailableKeychainLibrary:
    """A construction-time placeholder: startup proves the platform, never touches an item."""

    def find_generic_password(self, service: bytes, account: bytes) -> tuple[int, bytes]:
        raise SecretStoreError("SECRET_STORE_UNAVAILABLE")

    def add_generic_password(self, service: bytes, account: bytes, value: bytes) -> int:
        raise SecretStoreError("SECRET_STORE_UNAVAILABLE")

    def update_generic_password(self, service: bytes, account: bytes, value: bytes) -> int:
        raise SecretStoreError("SECRET_STORE_UNAVAILABLE")

    def delete_generic_password(self, service: bytes, account: bytes) -> int:
        raise SecretStoreError("SECRET_STORE_UNAVAILABLE")


def _handler_for(application: PilotApplication) -> type[BaseHTTPRequestHandler]:
    class _PilotHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "pilot"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            del format, args  # request lines never reach a log

        def _respond(self, method: str) -> None:
            length = self.headers.get("Content-Length", "0")
            try:
                size = int(length)
            except ValueError:
                size = MAXIMUM_BODY_BYTES + 1
            body = self.rfile.read(size) if 0 < size <= MAXIMUM_BODY_BYTES else b""
            request = PilotRequest(
                method=method,
                target=self.path,
                host=self.headers.get("Host", ""),
                received_at=datetime.now(UTC),
                origin=self.headers.get("Origin"),
                headers={
                    name.casefold(): (value,)
                    for name, value in self.headers.items()
                    if name.casefold() not in {"cookie", "host", "origin"}
                },
                cookies=_parse_cookies(self.headers.get("Cookie")),
                body=body if size <= MAXIMUM_BODY_BYTES else b"x" * (MAXIMUM_BODY_BYTES + 1),
            )
            self._write(application.respond(request))

        def do_GET(self) -> None:
            self._respond("GET")

        def do_POST(self) -> None:
            self._respond("POST")

        def _write(self, response: PilotResponse) -> None:
            self.send_response(int(response.status))
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in response.headers.items():
                self.send_header(name, value)
            if response.set_cookie is not None:
                self.send_header("Set-Cookie", response.set_cookie)
            self.end_headers()
            self.wfile.write(response.body)

    return _PilotHandler


def _parse_cookies(header: str | None) -> dict[str, tuple[str, ...]]:
    if not header:
        return {}
    cookies: dict[str, list[str]] = {}
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            cookies.setdefault(name, []).append(value)
    return {name: tuple(values) for name, values in cookies.items()}


def serve_polymarket_pilot(database_path: Path, port: int, *, platform: str = "darwin") -> None:
    """Serve the pilot on loopback only, refusing to start if any launch gate fails."""

    runtime = build_launch_runtime(database_path, port, platform=platform)
    with owned_resource_cleanup() as cleanup:
        cleanup.add(runtime.close)
        server = ThreadingHTTPServer((_LOOPBACK_ADDRESS, port), _handler_for(runtime.application))
        server.daemon_threads = True
        cleanup.add(server.server_close)
        server.serve_forever()


__all__ = [
    "KilledPilotServices",
    "PilotPosture",
    "PilotRuntime",
    "PilotRuntimeError",
    "build_launch_runtime",
    "build_pilot_runtime",
    "serve_polymarket_pilot",
]
