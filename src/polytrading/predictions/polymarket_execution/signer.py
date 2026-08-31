"""Capability-gated local signer service with no built-in network or persistence."""

from __future__ import annotations

import hmac
import os
import select
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock
from typing import Protocol
from uuid import UUID

from eth_account import Account

from polytrading.predictions.execution.authority import (
    AuthorityContext,
    AuthorityDecision,
    VerifiedExecutionCapability,
    verify_mutation_authority,
)
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ExecutionOperation,
    canonical_execution_hash,
)
from polytrading.predictions.pilot.capabilities import (
    SignedCapability,
    verify_capability_signature,
    verify_kill_directive,
)
from polytrading.predictions.pilot.verifier import verified_capability_from_grant
from polytrading.predictions.polymarket_execution.auth import ClobAuthError
from polytrading.predictions.polymarket_execution.ipc import (
    MAX_FRAME_BYTES,
    CancelOrderPayload,
    HeartbeatPayload,
    IdentityResult,
    ReadAccountPayload,
    ReadOrdersPayload,
    ReadTradesPayload,
    SanitizedOperationResult,
    SignedEnvelopeResult,
    SignerErrorCode,
    SignerKillPayload,
    SignerKillResult,
    SignerProtocolError,
    SignerRequest,
    SignerResponse,
    SignOrderPayload,
    SubmitOrderPayload,
    canonical_request_bytes,
    canonical_response_bytes,
    parse_signer_request,
    read_frame,
    write_frame,
)
from polytrading.predictions.polymarket_execution.order import OrderSigningError, sign_order
from polytrading.predictions.polymarket_execution.protocol import (
    PolymarketProtocolSnapshot,
    load_protocol_snapshot,
)
from polytrading.predictions.polymarket_execution.routes import RestCode
from polytrading.predictions.polymarket_execution.secrets import (
    SecretBoundaryError,
    SecretMaterial,
    read_secret_descriptors,
)

_MUTATING_OPERATIONS = frozenset(
    {
        ExecutionOperation.SIGN_ORDER,
        ExecutionOperation.SUBMIT_ORDER,
        ExecutionOperation.CANCEL_ORDER,
        ExecutionOperation.HEARTBEAT,
    }
)
_READ_OPERATIONS = frozenset(
    {
        ExecutionOperation.READ_ORDERS,
        ExecutionOperation.READ_TRADES,
        ExecutionOperation.READ_ACCOUNT,
    }
)


def _known_value_contains_secret(
    value: object,
    secret: bytes,
    secret_text: str | None,
) -> bool:
    if type(value) is str:
        return secret in value.encode("utf-8") or (secret_text is not None and secret_text in value)
    if type(value) is bytes:
        if secret in value:
            return True
        if secret_text is None:
            return False
        try:
            value_text = value.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return secret_text in value_text
    if type(value) is dict:
        return any(
            _known_value_contains_secret(item, secret, secret_text) for item in value.values()
        )
    if type(value) in (list, tuple):
        return any(_known_value_contains_secret(item, secret, secret_text) for item in value)
    return False


class SubmitOrderHandler(Protocol):
    def __call__(self, payload: SubmitOrderPayload) -> SanitizedOperationResult: ...


class CancelOrderHandler(Protocol):
    def __call__(self, payload: CancelOrderPayload) -> SanitizedOperationResult: ...


class HeartbeatHandler(Protocol):
    def __call__(self, payload: HeartbeatPayload) -> SanitizedOperationResult: ...


class ReadOrdersHandler(Protocol):
    def __call__(self, payload: ReadOrdersPayload) -> SanitizedOperationResult: ...


class ReadTradesHandler(Protocol):
    def __call__(self, payload: ReadTradesPayload) -> SanitizedOperationResult: ...


class ReadAccountHandler(Protocol):
    def __call__(self, payload: ReadAccountPayload) -> SanitizedOperationResult: ...


AuthorityContextFactory = Callable[[SignerRequest, datetime], AuthorityContext | AuthorityDecision]
ReadGuard = Callable[[SignerRequest, datetime], AuthorityDecision]
SignerServiceFactory = Callable[[SecretMaterial], "SignerService"]


@dataclass(frozen=True, slots=True)
class SignerOperationHandlers:
    """Typed handlers that must never re-enter their owning SignerService."""

    submit_order: SubmitOrderHandler
    cancel_order: CancelOrderHandler
    heartbeat: HeartbeatHandler
    read_orders: ReadOrdersHandler
    read_trades: ReadTradesHandler
    read_account: ReadAccountHandler
    close: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class _ReplayEntry:
    request_digest: bytes
    response_bytes: bytes


@dataclass(frozen=True, slots=True)
class _VenueOrderBinding:
    intent_id: UUID
    intent_fingerprint: str
    account_fingerprint: str
    envelope_digest: str


class SignerService:
    """Validate, gate, sign, and dispatch one bounded signer request at a time."""

    __slots__ = (
        "_authority_context_factory",
        "_cache",
        "_capability_public_key",
        "_clock",
        "_closed",
        "_consumed_primary_capabilities",
        "_consumed_primary_submissions",
        "_exact_order_primary_intents",
        "_handlers",
        "_kill_engaged",
        "_lock",
        "_max_cache_entries",
        "_protocol_fixture_hash",
        "_read_guard",
        "_secrets",
        "_snapshot",
        "_venue_order_bindings",
    )

    def __init__(
        self,
        *,
        secrets: SecretMaterial,
        authority_context_factory: AuthorityContextFactory,
        read_guard: ReadGuard,
        handlers: SignerOperationHandlers,
        clock: Callable[[], datetime],
        capability_public_key: bytes = b"",
        max_cache_entries: int = 1024,
        snapshot: PolymarketProtocolSnapshot | None = None,
    ) -> None:
        if type(secrets) is not SecretMaterial:
            raise TypeError("SECRET_MATERIAL_REQUIRED")
        if type(max_cache_entries) is not int or not 1 <= max_cache_entries <= 10_000:
            raise ValueError("IPC_REPLAY_CACHE_SIZE_INVALID")
        if type(capability_public_key) is not bytes or len(capability_public_key) not in (0, 32):
            raise ValueError("CAPABILITY_PUBLIC_KEY_INVALID")
        self._secrets = secrets
        self._authority_context_factory = authority_context_factory
        self._capability_public_key = capability_public_key
        self._read_guard = read_guard
        self._handlers = handlers
        self._lock = Lock()
        self._clock = clock
        self._max_cache_entries = max_cache_entries
        self._snapshot = snapshot or load_protocol_snapshot()
        self._protocol_fixture_hash = canonical_execution_hash(
            {
                "version": self._snapshot.version,
                "fixtures": [
                    item.model_dump(mode="json") for item in self._snapshot.fixture_hashes
                ],
            }
        )
        self._cache: dict[UUID, _ReplayEntry] = {}
        self._venue_order_bindings: dict[str, _VenueOrderBinding] = {}
        self._consumed_primary_capabilities: set[UUID] = set()
        self._consumed_primary_submissions: set[tuple[UUID, UUID]] = set()
        self._exact_order_primary_intents: dict[UUID, UUID] = {}
        self._kill_engaged = False
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._handlers.close is not None:
                    with suppress(Exception):
                        self._handlers.close()
            finally:
                self._secrets.close()

    def handle_raw(self, payload: bytes) -> SignerResponse:
        """Parse one bounded wire payload without reflecting invalid input."""
        try:
            request = parse_signer_request(payload)
        except SignerProtocolError as error:
            with self._lock:
                response, _ = self._sanitized_response_bytes(
                    SignerResponse.rejected(None, str(error))
                )
            return response
        return self.handle(request)

    def _handle_raw_bytes(self, payload: bytes) -> bytes:
        try:
            request = parse_signer_request(payload)
        except SignerProtocolError as error:
            with self._lock:
                return self._response_bytes(SignerResponse.rejected(None, str(error)))
        return self._handle_bytes(request)

    def handle(self, request: SignerRequest) -> SignerResponse:
        """Return a cached exact response or process one fresh validated request."""
        if type(request) is not SignerRequest:
            return SignerResponse.rejected(None, "IPC_REQUEST_INVALID")
        response_bytes = self._handle_bytes(request)
        return SignerResponse.model_validate_json(response_bytes, strict=True)

    def _handle_bytes(self, request: SignerRequest) -> bytes:
        """Atomically replay-check, gate, dispatch, scan, bind, and cache one request.

        Injected synchronous handlers must not call back into this service.
        """
        with self._lock:
            return self._handle_bytes_locked(request)

    def _handle_bytes_locked(self, request: SignerRequest) -> bytes:
        request_id = request.request_id
        if type(request_id) is not UUID:
            return self._response_bytes(SignerResponse.rejected(None, "IPC_REQUEST_INVALID"))
        cached = self._cache.get(request_id)
        try:
            request_bytes = canonical_request_bytes(request)
        except Exception:
            error_code = "IPC_REQUEST_COLLISION" if cached is not None else "IPC_REQUEST_INVALID"
            return self._response_bytes(SignerResponse.rejected(request_id, error_code))
        request_digest = sha256(request_bytes).digest()
        if cached is not None:
            if hmac.compare_digest(cached.request_digest, request_digest):
                return cached.response_bytes
            return self._response_bytes(
                SignerResponse.rejected(request_id, "IPC_REQUEST_COLLISION")
            )
        if len(self._cache) >= self._max_cache_entries:
            return self._response_bytes(
                SignerResponse.rejected(request_id, "IPC_REPLAY_CACHE_FULL")
            )

        try:
            request = parse_signer_request(request_bytes)
        except SignerProtocolError as error:
            response_bytes = self._response_bytes(SignerResponse.rejected(request_id, str(error)))
            self._cache[request_id] = _ReplayEntry(request_digest, response_bytes)
            return response_bytes

        response = self._handle_uncached(request)
        response, response_bytes = self._sanitized_response_bytes(response)
        binding = self._venue_order_binding(request, response)
        if binding is not None:
            venue_order_id, prospective = binding
            existing = self._venue_order_bindings.get(venue_order_id)
            if existing is not None and existing != prospective:
                response = SignerResponse.rejected(
                    request_id,
                    "VENUE_ORDER_BINDING_COLLISION",
                )
                response, response_bytes = self._sanitized_response_bytes(response)
            else:
                self._venue_order_bindings[venue_order_id] = prospective
        self._cache[request_id] = _ReplayEntry(request_digest, response_bytes)
        return response_bytes

    def _handle_uncached(self, request: SignerRequest) -> SignerResponse:
        if self._closed:
            return SignerResponse.rejected(request.request_id, "IPC_SIGNER_CLOSED")
        try:
            now = self._clock()
        except Exception:
            return SignerResponse.rejected(request.request_id, "IPC_CLOCK_INVALID")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return SignerResponse.rejected(request.request_id, "IPC_CLOCK_INVALID")
        now = now.astimezone(UTC)
        if now >= request.deadline:
            return SignerResponse.rejected(request.request_id, "IPC_DEADLINE_EXPIRED")
        intent = self._intent_for(request)
        if intent is not None:
            if now >= intent.deadline:
                return SignerResponse.rejected(request.request_id, "INTENT_DEADLINE_EXPIRED")
            if request.deadline > intent.deadline:
                return SignerResponse.rejected(
                    request.request_id,
                    "REQUEST_DEADLINE_EXCEEDS_INTENT",
                )
        if request.protocol_version != self._snapshot.version:
            return SignerResponse.rejected(request.request_id, "PROTOCOL_VERSION_MISMATCH")
        if request.operation is ExecutionOperation.DESCRIBE_IDENTITY:
            return self._describe_identity(request)
        if request.operation is ExecutionOperation.SIGNER_KILL:
            return self._engage_kill(request)
        if not self._secret_account_matches(request.account_fingerprint):
            return SignerResponse.rejected(request.request_id, "ACCOUNT_FINGERPRINT_MISMATCH")

        if request.operation in _MUTATING_OPERATIONS:
            if self._kill_engaged:
                return SignerResponse.rejected(request.request_id, "PILOT_KILL_ENGAGED")
            verified = self._verify_proof(request, now)
            if isinstance(verified, str):
                return SignerResponse.rejected(request.request_id, verified)
            decision = self._verify_mutation(request, now, verified)
        elif request.operation in _READ_OPERATIONS:
            decision = self._verify_read(request, now)
        else:
            return SignerResponse.rejected(request.request_id, "IPC_OPERATION_NOT_ALLOWED")
        if isinstance(decision, str):
            return SignerResponse.rejected(request.request_id, decision)
        if not decision.allowed:
            return SignerResponse.rejected(
                request.request_id,
                decision.reason or "AUTHORITY_GATE_FAILED",
            )
        if request.operation in _MUTATING_OPERATIONS:
            assert isinstance(verified, VerifiedExecutionCapability)
            replay = self._consume_primary_authority(request, verified)
            if replay is not None:
                return SignerResponse.rejected(request.request_id, replay)
        return self._dispatch(request)

    def _engage_kill(self, request: SignerRequest) -> SignerResponse:
        if not isinstance(request.payload, SignerKillPayload) or not verify_kill_directive(
            request.payload.directive,
            self._capability_public_key,
        ):
            return SignerResponse.rejected(
                request.request_id,
                "CAPABILITY_SIGNATURE_INVALID",
            )
        self._kill_engaged = True
        return SignerResponse.accepted(
            request.request_id,
            SignerKillResult(
                operation=ExecutionOperation.SIGNER_KILL,
                result_code="SIGNER_KILL_ENGAGED",
            ),
        )

    def _describe_identity(self, request: SignerRequest) -> SignerResponse:
        private_key: bytes | None = None
        try:
            private_key = bytes(self._secrets.private_key)
            address = Account.from_key(private_key).address
            fingerprint = sha256(bytes.fromhex(address[2:])).hexdigest()
        except Exception:
            return SignerResponse.rejected(request.request_id, "IPC_REQUEST_INVALID")
        finally:
            private_key = None
        return SignerResponse.accepted(
            request.request_id,
            IdentityResult(
                operation=ExecutionOperation.DESCRIBE_IDENTITY,
                account_fingerprint=fingerprint,
                wallet_fingerprint=fingerprint,
            ),
        )

    def _secret_account_matches(self, account_fingerprint: str) -> bool:
        private_key: bytes | None = None
        try:
            private_key = bytes(self._secrets.private_key)
            address = Account.from_key(private_key).address
            decoded = bytes.fromhex(address[2:])
        except Exception:
            return False
        finally:
            private_key = None
        return hmac.compare_digest(sha256(decoded).hexdigest(), account_fingerprint)

    def _verify_mutation(
        self,
        request: SignerRequest,
        now: datetime,
        verified_capability: VerifiedExecutionCapability,
    ) -> AuthorityDecision | SignerErrorCode:
        try:
            context = self._authority_context_factory(request, now)
            if type(context) is AuthorityDecision:
                return context
            if type(context) is not AuthorityContext:
                return "AUTHORITY_GATE_FAILED"
            context = context.model_copy(update={"verified_capability": verified_capability})
            capability = verified_capability
            manifest_hash = (
                canonical_execution_hash(context.manifest) if context.manifest is not None else None
            )
            if context.account_fingerprint != request.account_fingerprint:
                return AuthorityDecision(False, "CAPABILITY_ACCOUNT_MISMATCH", ())
            if context.now != now:
                return "AUTHORITY_CONTEXT_TIME_MISMATCH"
            if (
                context.manifest_record_hash != request.manifest_digest
                or manifest_hash != request.manifest_digest
            ):
                return AuthorityDecision(False, "CAPABILITY_MANIFEST_MISMATCH", ())
            if capability is None or capability.capability_digest != request.capability_digest:
                return AuthorityDecision(False, "CAPABILITY_CANONICAL_BYTES_INVALID", ())
            return verify_mutation_authority(context, request.operation)
        except Exception:
            return "AUTHORITY_GATE_FAILED"

    def _verify_proof(
        self,
        request: SignerRequest,
        now: datetime,
    ) -> VerifiedExecutionCapability | SignerErrorCode:
        proof = request.authority_proof
        if proof is None:
            return "CAPABILITY_MISSING"
        if not self._capability_public_key:
            return "EXECUTION_UNAVAILABLE"
        grant = proof.grant
        capability = SignedCapability(
            grant=grant,
            signature=bytes(proof.signature),
            public_verification_key=self._capability_public_key,
        )
        if not verify_capability_signature(capability, self._capability_public_key):
            return "CAPABILITY_SIGNATURE_INVALID"
        if not hmac.compare_digest(grant.digest, request.capability_digest):
            return "CAPABILITY_CANONICAL_BYTES_INVALID"
        if not hmac.compare_digest(grant.account_fingerprint, request.account_fingerprint):
            return "CAPABILITY_ACCOUNT_MISMATCH"
        if not hmac.compare_digest(
            grant.venue_binding.manifest_record_hash,
            request.manifest_digest,
        ):
            return "CAPABILITY_MANIFEST_MISMATCH"
        if not hmac.compare_digest(
            grant.venue_binding.protocol_fixture_hash,
            self._protocol_fixture_hash,
        ):
            return "CAPABILITY_PROTOCOL_MISMATCH"
        if not hmac.compare_digest(grant.plan_hash, request.plan_digest):
            return "CAPABILITY_PLAN_MISMATCH"
        if request.operation not in grant.allowed_operations:
            return "CAPABILITY_OPERATION_NOT_ALLOWED"
        if grant.mode.value == "AUTOMATION_SESSION":
            return "CAPABILITY_MODE_MISMATCH"
        if now < grant.not_before:
            return "CAPABILITY_NOT_YET_VALID"
        if now >= grant.expires_at or request.deadline > grant.expires_at:
            return "CAPABILITY_EXPIRED"
        if now > grant.presence_deadline or request.deadline > grant.presence_deadline:
            return "OPERATOR_PRESENCE_LOST"
        try:
            return verified_capability_from_grant(grant, verified_at=now)
        except Exception:
            return "CAPABILITY_CANONICAL_BYTES_INVALID"

    def _consume_primary_authority(
        self,
        request: SignerRequest,
        capability: VerifiedExecutionCapability,
    ) -> SignerErrorCode | None:
        if capability.grant_kind != "PRIMARY" or capability.single_use is not True:
            return None
        capability_id = capability.capability_id
        intent_id = request.intent_id
        if capability.mode == "EXACT_ORDER":
            existing_intent = self._exact_order_primary_intents.get(capability_id)
            if existing_intent is not None and existing_intent != intent_id:
                return "CAPABILITY_REPLAYED"
            self._exact_order_primary_intents.setdefault(capability_id, intent_id)
        if request.operation is ExecutionOperation.SUBMIT_ORDER:
            submission = (capability_id, intent_id)
            if submission in self._consumed_primary_submissions:
                return "CAPABILITY_REPLAYED"
            self._consumed_primary_submissions.add(submission)
        self._consumed_primary_capabilities.add(capability_id)
        return None

    def _verify_read(
        self,
        request: SignerRequest,
        now: datetime,
    ) -> AuthorityDecision | SignerErrorCode:
        try:
            decision = self._read_guard(request, now)
        except Exception:
            return "READ_GUARD_FAILED"
        if type(decision) is not AuthorityDecision:
            return "READ_GUARD_FAILED"
        return decision

    def _dispatch(self, request: SignerRequest) -> SignerResponse:
        try:
            if isinstance(request.payload, SignOrderPayload):
                private_key = bytes(self._secrets.private_key)
                try:
                    envelope = sign_order(request.payload.intent, private_key, self._snapshot)
                finally:
                    private_key = b""
                result = SignedEnvelopeResult(
                    operation=ExecutionOperation.SIGN_ORDER,
                    envelope=envelope,
                )
            else:
                if isinstance(request.payload, SubmitOrderPayload):
                    private_key = bytes(self._secrets.private_key)
                    try:
                        expected_envelope = sign_order(
                            request.payload.intent,
                            private_key,
                            self._snapshot,
                        )
                    finally:
                        private_key = b""
                    if (
                        expected_envelope != request.payload.envelope
                        or expected_envelope.model_dump_json()
                        != request.payload.envelope.model_dump_json()
                    ):
                        return SignerResponse.rejected(
                            request.request_id,
                            "ORDER_ENVELOPE_MISMATCH",
                        )
                if isinstance(request.payload, CancelOrderPayload):
                    binding = self._venue_order_bindings.get(request.payload.venue_order_id)
                    if binding is None:
                        return SignerResponse.rejected(
                            request.request_id,
                            "CANCEL_ORDER_UNKNOWN",
                        )
                    if (
                        binding.intent_id != request.intent_id
                        or binding.intent_fingerprint != request.intent_fingerprint
                        or binding.account_fingerprint != request.account_fingerprint
                    ):
                        return SignerResponse.rejected(
                            request.request_id,
                            "CANCEL_ORDER_BINDING_MISMATCH",
                        )
                handler = self._handler_for(request.payload)
                result = handler(request.payload)
                if type(result) is SanitizedOperationResult:
                    try:
                        result = SanitizedOperationResult.model_validate(
                            result.model_dump(mode="python"),
                            strict=True,
                        )
                    except Exception:
                        return SignerResponse.rejected(
                            request.request_id,
                            "IPC_OPERATION_RESULT_INVALID",
                        )
                if (
                    type(result) is not SanitizedOperationResult
                    or result.operation is not request.operation
                    or not self._result_matches_request(request, result)
                ):
                    return SignerResponse.rejected(
                        request.request_id,
                        "IPC_OPERATION_RESULT_INVALID",
                    )
                if result.kill_required is True:
                    self._kill_engaged = True
            return SignerResponse.accepted(request.request_id, result)
        except OrderSigningError:
            return SignerResponse.rejected(request.request_id, "ORDER_SIGNING_FAILED")
        except ClobAuthError:
            return SignerResponse.rejected(request.request_id, "AUTH_HANDLER_FAILED")
        except Exception:
            return SignerResponse.rejected(request.request_id, "HANDLER_FAILED")

    @staticmethod
    def _intent_for(request: SignerRequest) -> ExecutionIntent | None:
        if isinstance(request.payload, (SignOrderPayload, SubmitOrderPayload)):
            return request.payload.intent
        return None

    @staticmethod
    def _result_matches_request(
        request: SignerRequest,
        result: SanitizedOperationResult,
    ) -> bool:
        if isinstance(request.payload, CancelOrderPayload):
            return result.venue_order_id == request.payload.venue_order_id
        if (
            isinstance(request.payload, ReadOrdersPayload)
            and request.payload.venue_order_id is not None
        ):
            return result.venue_order_id == request.payload.venue_order_id
        return True

    def _sanitized_response_bytes(
        self,
        response: SignerResponse,
    ) -> tuple[SignerResponse, bytes]:
        if type(response) is not SignerResponse:
            sanitized = SignerResponse.rejected(None, "HANDLER_FAILED")
            return sanitized, canonical_response_bytes(sanitized)
        secrets: list[tuple[bytes, str | None]] = []
        for value in (
            self._secrets.private_key,
            self._secrets.api_key,
            self._secrets.api_secret,
            self._secrets.passphrase,
        ):
            secret = bytes(value)
            if not secret:
                continue
            try:
                secret_text = secret.decode("utf-8")
            except UnicodeDecodeError:
                secret_text = None
            secrets.append((secret, secret_text))
        try:
            response_value = response.model_dump(mode="python")
            if any(
                _known_value_contains_secret(response_value, secret, secret_text)
                for secret, secret_text in secrets
            ):
                sanitized = SignerResponse.rejected(
                    response.request_id,
                    "SECRET_OUTPUT_DETECTED",
                )
                return sanitized, canonical_response_bytes(sanitized)
            response_bytes = canonical_response_bytes(response)
        except Exception:
            sanitized = SignerResponse.rejected(response.request_id, "HANDLER_FAILED")
            return sanitized, canonical_response_bytes(sanitized)
        if len(response_bytes) > MAX_FRAME_BYTES:
            sanitized = SignerResponse.rejected(
                response.request_id,
                "IPC_OPERATION_RESULT_INVALID",
            )
            return sanitized, canonical_response_bytes(sanitized)
        for secret, _ in secrets:
            if secret in response_bytes:
                sanitized = SignerResponse.rejected(
                    response.request_id,
                    "SECRET_OUTPUT_DETECTED",
                )
                return sanitized, canonical_response_bytes(sanitized)
        return response, response_bytes

    def _response_bytes(self, response: SignerResponse) -> bytes:
        return self._sanitized_response_bytes(response)[1]

    @staticmethod
    def _venue_order_binding(
        request: SignerRequest,
        response: SignerResponse,
    ) -> tuple[str, _VenueOrderBinding] | None:
        if (
            not isinstance(request.payload, SubmitOrderPayload)
            or not response.ok
            or type(response.result) is not SanitizedOperationResult
            or response.result.result_code
            not in {
                "SUBMIT_ORDER_OK",
                RestCode.ORDER_ACK_MATCHED,
                RestCode.ORDER_ACK_DELAYED,
                RestCode.ORDER_ACK_LIVE_UNEXPECTED,
                RestCode.ORDER_ACK_UNMATCHED,
            }
            or response.result.venue_order_id is None
        ):
            return None
        return (
            response.result.venue_order_id,
            _VenueOrderBinding(
                intent_id=request.intent_id,
                intent_fingerprint=request.payload.envelope.intent_fingerprint,
                account_fingerprint=request.account_fingerprint,
                envelope_digest=canonical_execution_hash(request.payload.envelope),
            ),
        )

    def _handler_for(
        self,
        payload: SubmitOrderPayload
        | CancelOrderPayload
        | HeartbeatPayload
        | ReadOrdersPayload
        | ReadTradesPayload
        | ReadAccountPayload,
    ) -> (
        SubmitOrderHandler
        | CancelOrderHandler
        | HeartbeatHandler
        | ReadOrdersHandler
        | ReadTradesHandler
        | ReadAccountHandler
    ):
        if isinstance(payload, SubmitOrderPayload):
            return self._handlers.submit_order
        if isinstance(payload, CancelOrderPayload):
            return self._handlers.cancel_order
        if isinstance(payload, HeartbeatPayload):
            return self._handlers.heartbeat
        if isinstance(payload, ReadOrdersPayload):
            return self._handlers.read_orders
        if isinstance(payload, ReadTradesPayload):
            return self._handlers.read_trades
        return self._handlers.read_account


class _DeadlineFdStream:
    __slots__ = ("_deadline", "_descriptor", "_monotonic")

    def __init__(
        self,
        descriptor: int,
        *,
        deadline: float,
        monotonic: Callable[[], float],
    ) -> None:
        self._descriptor = descriptor
        self._deadline = deadline
        self._monotonic = monotonic
        os.set_blocking(descriptor, False)

    def _remaining(self) -> float:
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise OSError("IPC_DEADLINE_REACHED")
        return remaining

    def read(self, size: int = -1) -> bytes:
        readable, _, _ = select.select(
            (self._descriptor,),
            (),
            (),
            self._remaining(),
        )
        if not readable:
            raise OSError("IPC_DEADLINE_REACHED")
        return os.read(self._descriptor, size)

    def write(self, value: bytes) -> int:
        _, writable, _ = select.select(
            (),
            (self._descriptor,),
            (),
            self._remaining(),
        )
        if not writable:
            raise OSError("IPC_DEADLINE_REACHED")
        return os.write(self._descriptor, value)

    def flush(self) -> None:
        return None


def _close_descriptors(*descriptors: int) -> None:
    for descriptor in tuple(dict.fromkeys(descriptors)):
        try:
            os.close(descriptor)
        except OSError:
            continue


def _write_sidecar_response(stream: _DeadlineFdStream, response: SignerResponse) -> bool:
    try:
        write_frame(stream, canonical_response_bytes(response))  # type: ignore[arg-type]
    except SignerProtocolError:
        return False
    return True


def run_signer_sidecar(
    *,
    request_fd: int,
    response_fd: int,
    secret_descriptors: tuple[int, int, int, int],
    service_factory: SignerServiceFactory,
    max_requests: int = 1024,
    max_lifetime_seconds: float = 300,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Run the internal bounded local sidecar; never registered as a CLI entry point."""
    all_descriptors = (request_fd, response_fd, *secret_descriptors)
    secrets: SecretMaterial | None = None
    service: object | None = None
    secret_loader_called = False
    try:
        if (
            type(request_fd) is not int
            or type(response_fd) is not int
            or len(secret_descriptors) != 4
            or any(type(descriptor) is not int or descriptor < 0 for descriptor in all_descriptors)
            or len(set(all_descriptors)) != len(all_descriptors)
        ):
            return
        if type(max_requests) is not int or not 1 <= max_requests <= 10_000:
            return
        if type(max_lifetime_seconds) not in (int, float) or not 0 < max_lifetime_seconds <= 3600:
            return
        started = monotonic()
        deadline = started + max_lifetime_seconds
        request_stream = _DeadlineFdStream(
            request_fd,
            deadline=deadline,
            monotonic=monotonic,
        )
        response_stream = _DeadlineFdStream(
            response_fd,
            deadline=deadline,
            monotonic=monotonic,
        )
        try:
            for descriptor in secret_descriptors:
                os.set_blocking(descriptor, False)
        except OSError:
            _write_sidecar_response(
                response_stream,
                SignerResponse.rejected(None, "SECRET_DESCRIPTOR_READ_FAILED"),
            )
            return
        secret_loader_called = True
        try:
            secrets = read_secret_descriptors(*secret_descriptors)
        except SecretBoundaryError as error:
            _write_sidecar_response(
                response_stream,
                SignerResponse.rejected(None, str(error)),
            )
            return
        try:
            service = service_factory(secrets)
        except Exception:
            _write_sidecar_response(
                response_stream,
                SignerResponse.rejected(None, "IPC_SERVICE_INITIALIZATION_FAILED"),
            )
            return
        if type(service) is not SignerService:
            _write_sidecar_response(
                response_stream,
                SignerResponse.rejected(None, "IPC_SERVICE_INITIALIZATION_FAILED"),
            )
            return

        for _ in range(max_requests):
            try:
                payload = read_frame(request_stream)  # type: ignore[arg-type]
            except SignerProtocolError as error:
                _write_sidecar_response(
                    response_stream,
                    SignerResponse.rejected(None, str(error)),
                )
                return
            response_bytes = service._handle_raw_bytes(payload)
            try:
                write_frame(response_stream, response_bytes)  # type: ignore[arg-type]
            except SignerProtocolError:
                return
    finally:
        if type(service) is SignerService:
            service.close()
        if secrets is not None:
            secrets.close()
        descriptors_to_close = (request_fd, response_fd)
        if not secret_loader_called:
            descriptors_to_close += secret_descriptors
        _close_descriptors(*descriptors_to_close)


__all__ = ["SignerOperationHandlers", "SignerService"]
