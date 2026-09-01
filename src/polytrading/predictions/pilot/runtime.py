"""Fail-closed composition for the local pilot process.

Startup proves the database schema, the origin and RP ID, the pilot protocol checkpoint, and the
secret-store posture before anything is served, and the process always starts killed. No wallet
key, credential, or authenticated transport is constructed here: those exist only inside the
signer, and only after an operator ceremony that later increments gate.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

from polytrading.lifecycle import owned_resource_cleanup
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.authority import VerifiedExecutionCapability
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ExecutionOperation,
    canonical_execution_hash,
)
from polytrading.predictions.pilot.capabilities import (
    MutationEvidence,
    PilotCapabilityIssuer,
    SignedMutationEvidence,
)
from polytrading.predictions.pilot.execution_port import (
    CoordinatorExecutionPort,
    ExecutionEvidence,
    VenueSubmissionPort,
)
from polytrading.predictions.pilot.launch import (
    compose_pilot_environment,
    signer_account_reader,
    signer_position_reader,
)
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
from polytrading.predictions.pilot.reconciliation import reconcile_startup
from polytrading.predictions.pilot.server import (
    MAXIMUM_BODY_BYTES,
    PilotApplication,
    PilotRequest,
    PilotRequestError,
    PilotResponse,
)
from polytrading.predictions.pilot.services import LivePilotServices, PilotEnvironment
from polytrading.predictions.pilot.sessions import PilotExecutor
from polytrading.predictions.pilot.signer_bootstrap import (
    SignerChannel,
    SignerServiceFactory,
    launch_signer_sidecar,
)
from polytrading.predictions.pilot.signer_link import (
    SignerLinkError,
    SignerLinkVenuePort,
    describe_identity,
)
from polytrading.predictions.pilot.signer_services import live_pilot_signer_service
from polytrading.predictions.pilot.verifier import PilotCapabilityVerifier
from polytrading.predictions.polymarket_execution.ipc import SignerCapabilityProof
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
    signer_port: VenueSubmissionPort | None = None

    def close(self) -> None:
        """Kill and close the signer before destroying this launch's issuing key."""
        if self.signer_port is not None and self.issuer is not None:
            with suppress(Exception):
                self.signer_port.engage_kill(self.issuer.issued_capability_ids)
        if self.signer_channel is not None:
            self.signer_channel.close()
        if self.issuer is not None:
            self.issuer.close()
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
    launch_issuer: PilotCapabilityIssuer | None = None,
    signer_channel: SignerChannel | None = None,
    signer_port: VenueSubmissionPort | None = None,
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

    issuer = launch_issuer or PilotCapabilityIssuer(key_id=key_id)
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
        signer_channel=signer_channel,
        signer_port=signer_port,
    )


def build_launch_runtime(
    database_path: Path,
    port: int,
    *,
    platform: str = "darwin",
    bootstrap: Callable[[SignerServiceFactory], SignerChannel] | None = None,
    now: Callable[[], datetime] | None = None,
) -> PilotRuntime:
    """Launch the sidecar and compose live-but-killed services, or serve posture only."""
    clock = now or (lambda: datetime.now(UTC))
    issuer = PilotCapabilityIssuer(key_id="pilot-launch")
    channel: SignerChannel | None = None
    venue_port: SignerLinkVenuePort | None = None
    composition_store: PredictionMarketStore | None = None
    try:
        service_factory = live_pilot_signer_service(
            capability_public_key=issuer.public_verification_key,
            clock=clock,
        )
        channel = (bootstrap or _bootstrap_signer(platform))(service_factory)
        account_fingerprint, wallet_fingerprint = describe_identity(
            channel.request_stream, channel.response_stream, clock=clock
        )
        composition_store = PredictionMarketStore(Path(database_path))
        observed_at = clock()
        manifest = composition_store.verified_latest_venue_manifest_as_of(
            PredictionVenue.POLYMARKET,
            observed_at,
        )
        manifest_digest = "0" * 64 if manifest is None else canonical_execution_hash(manifest)

        proofs: dict[UUID, SignerCapabilityProof] = {}
        evidence_providers: dict[UUID, Callable[[], ExecutionEvidence]] = {}
        runtime_reference: dict[str, PilotRuntime] = {}
        venue_port = SignerLinkVenuePort(
            request_stream=channel.request_stream,
            response_stream=channel.response_stream,
            account_fingerprint=account_fingerprint,
            manifest_digest=manifest_digest,
            clock=clock,
            account_reader=signer_account_reader(
                account_fingerprint=account_fingerprint,
                wallet_fingerprint=wallet_fingerprint,
            ),
            signed_envelope=None,
            proof_for=lambda capability_id: proofs[capability_id],
            mutation_evidence=lambda intent, operation, proof: _mutation_evidence_for(
                issuer=issuer,
                intent=intent,
                operation=operation,
                proof=proof,
                current_evidence=evidence_providers.get(proof.grant.capability_id),
                clock=clock,
            ),
            kill_directive=lambda capability_ids: issuer.issue_kill_directive(
                capability_ids,
                issued_at=clock(),
            ),
            position_reader=signer_position_reader,
        )

        def current_manifest():
            runtime = runtime_reference["runtime"]
            return runtime.store.verified_latest_venue_manifest_as_of(
                PredictionVenue.POLYMARKET,
                clock(),
            )

        def current_reconciliation():
            return reconcile_startup(
                venue_port,
                account_fingerprint=account_fingerprint,
                now=clock,
            )

        def executor_factory(
            grants: Mapping[UUID, VerifiedExecutionCapability],
            signer_proofs: Mapping[UUID, SignerCapabilityProof],
            current_evidence: Callable[[], ExecutionEvidence],
        ) -> PilotExecutor:
            runtime = runtime_reference["runtime"]
            if runtime.verifier is None:
                raise SignerLinkError("MUTATION_EVIDENCE_UNAVAILABLE")
            for capability_id, proof in signer_proofs.items():
                proofs[capability_id] = proof
                evidence_providers[capability_id] = current_evidence
            coordinator = CoordinatorExecutionPort(
                store=runtime.store,
                signer=venue_port,
                verifier=runtime.verifier,
                grants=grants,
                evidence=current_evidence,
                clock=clock,
            )
            return PilotExecutor(coordinator, clock=clock)

        environment = compose_pilot_environment(
            composition_store,
            account_fingerprint=account_fingerprint,
            wallet_fingerprint=wallet_fingerprint,
            credentials_present=channel.credentials_present,
            now=clock,
            venue_port=venue_port,
            executor_factory=executor_factory,
            manifest_provider=current_manifest,
            reconciliation_provider=current_reconciliation,
        )
        runtime = build_pilot_runtime(
            database_path,
            port,
            platform=platform,
            now=clock,
            environment=environment,
            launch_issuer=issuer,
            signer_channel=channel,
            signer_port=venue_port,
        )
        runtime_reference["runtime"] = runtime
        return runtime
    except Exception:
        if venue_port is not None:
            with suppress(Exception):
                venue_port.engage_kill(issuer.issued_capability_ids)
        if channel is not None:
            channel.close()
        issuer.close()
        print("pilot: signer unavailable; serving posture only", file=sys.stderr)
        return build_pilot_runtime(database_path, port, platform=platform, now=clock)
    finally:
        if composition_store is not None:
            composition_store.close()


def _bootstrap_signer(
    platform: str,
) -> Callable[[SignerServiceFactory], SignerChannel]:
    return lambda service_factory: launch_signer_sidecar(
        store=MacOSKeychainSecretStore(platform=platform),
        service_factory=service_factory,
    )


def _mutation_evidence_for(
    *,
    issuer: PilotCapabilityIssuer,
    intent: ExecutionIntent,
    operation: ExecutionOperation,
    proof: SignerCapabilityProof,
    current_evidence: Callable[[], ExecutionEvidence] | None,
    clock: Callable[[], datetime],
) -> SignedMutationEvidence:
    """Sign one action-local public snapshot or refuse before writing IPC."""
    del operation
    if current_evidence is None:
        raise SignerLinkError("MUTATION_EVIDENCE_UNAVAILABLE")
    evidence = current_evidence()
    account = evidence.account
    if (
        evidence.manifest is None
        or account is None
        or account.account_fingerprint != proof.grant.account_fingerprint
        or evidence.reconciliation_hash is None
        or evidence.reconciliation_observed_at is None
        or type(evidence.geoblock_allowed) is not bool
        or evidence.geoblock_evidence_hash is None
        or evidence.geoblock_expires_at is None
        or evidence.account_scope_evidence_hash is None
        or evidence.account_scope_expires_at is None
    ):
        raise SignerLinkError("MUTATION_EVIDENCE_UNAVAILABLE")
    issued_at = clock()
    expires_at = min(
        issued_at + timedelta(seconds=5),
        proof.grant.expires_at,
        intent.deadline,
    )
    if expires_at <= issued_at:
        raise SignerLinkError("MUTATION_EVIDENCE_STALE")
    limits = proof.grant.effective_limits
    snapshot = MutationEvidence(
        schema_version=1,
        manifest=evidence.manifest,
        manifest_record_hash=canonical_execution_hash(evidence.manifest),
        account_fingerprint=account.account_fingerprint,
        reconciliation_hash=evidence.reconciliation_hash,
        reconciliation_observed_at=evidence.reconciliation_observed_at,
        geoblock_allowed=evidence.geoblock_allowed,
        geoblock_evidence_hash=evidence.geoblock_evidence_hash,
        geoblock_expires_at=evidence.geoblock_expires_at,
        account_scope_evidence_hash=evidence.account_scope_evidence_hash,
        account_scope_expires_at=evidence.account_scope_expires_at,
        kill_engaged=evidence.kill_engaged or account.kill_engaged,
        operator_present=evidence.operator_present,
        plan_digest=intent.capability_fingerprint,
        authority_digest=proof.grant.digest,
        requested_notional=intent.maximum_spend or Decimal("0"),
        capital_after=limits.session_deployed_capital,
        position_after=limits.strategy_gross_notional,
        loss_after=Decimal("0"),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    return issuer.issue_mutation_evidence(snapshot)


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
