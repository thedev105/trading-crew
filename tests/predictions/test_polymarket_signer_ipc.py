from __future__ import annotations

import io
import json
import multiprocessing
import os
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from multiprocessing import reduction
from threading import Event, Lock
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from eth_account import Account

from polytrading.predictions.execution.authority import AuthorityDecision
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ExecutionOperation,
    canonical_execution_hash,
)
from polytrading.predictions.pilot.capabilities import (
    CapabilityGrant,
    SignerKillDirective,
)
from polytrading.predictions.pilot.models import AuthorizationMode
from polytrading.predictions.pilot.verifier import verified_capability_from_grant
from polytrading.predictions.polymarket_execution import signer as signer_module
from polytrading.predictions.polymarket_execution.auth import ClobAuthError
from polytrading.predictions.polymarket_execution.ipc import (
    MAX_FRAME_BYTES,
    CancelOrderPayload,
    DescribeIdentityPayload,
    HeartbeatPayload,
    ReadAccountPayload,
    ReadOrdersPayload,
    ReadTradesPayload,
    SanitizedOperationResult,
    SignedEnvelopeResult,
    SignerCapabilityProof,
    SignerKillPayload,
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
from polytrading.predictions.polymarket_execution.protocol import load_protocol_snapshot
from polytrading.predictions.polymarket_execution.routes import (
    AllowanceEntry,
    BalanceAllowancePayload,
    OrderAckPayload,
    RestCode,
    RouteKey,
)
from polytrading.predictions.polymarket_execution.secrets import SecretMaterial
from polytrading.predictions.polymarket_execution.signer import (
    SignerOperationHandlers,
    SignerService,
    run_signer_sidecar,
)
from tests.predictions.execution_helpers import execution_intent_fields
from tests.predictions.pilot_helpers import signer_capability_grant
from tests.predictions.test_execution_authority import (
    MANIFEST_HASH,
    authority_context,
)
from tests.predictions.test_polymarket_order_signing import (
    ACCOUNT_FINGERPRINT,
    PRIVATE_KEY,
)

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)
API_KEY = b"task-7-api-key"
API_SECRET = b"dGFzay03LXNlY3JldA=="
PASSPHRASE = b"task-7-passphrase"
_CHILD_SECRET_MATERIAL: SecretMaterial | None = None
_CANARY_ASSERTION_FAILED = "IPC_CANARY_DETECTED"


class _TestCapabilityIssuer:
    def __init__(self, private_key: bytes) -> None:
        self._private_key = Ed25519PrivateKey.from_private_bytes(private_key)

    @property
    def public_verification_key(self) -> bytes:
        return self._private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def proof(self, grant: CapabilityGrant) -> SignerCapabilityProof:
        signature = self._private_key.sign(grant.digest.encode("ascii"))
        return SignerCapabilityProof(grant=grant, signature=b64encode(signature))

    def kill_directive(self, capability_ids: tuple[UUID, ...]) -> SignerKillDirective:
        issued_at = NOW
        digest = canonical_execution_hash(
            {
                "kind": "signer-kill-v1",
                "capability_ids": capability_ids,
                "issued_at": issued_at,
            }
        )
        return SignerKillDirective(
            capability_ids=capability_ids,
            issued_at=issued_at,
            signature=b64encode(self._private_key.sign(digest.encode("ascii"))),
        )


ISSUER = _TestCapabilityIssuer(b"\x01" * 32)
OTHER_ISSUER = _TestCapabilityIssuer(b"\x02" * 32)


def _protocol_fixture_hash() -> str:
    snapshot = load_protocol_snapshot()
    return canonical_execution_hash(
        {
            "version": snapshot.version,
            "fixtures": [item.model_dump(mode="json") for item in snapshot.fixture_hashes],
        }
    )


def _capability_grant(**overrides: object) -> CapabilityGrant:
    base = signer_capability_grant(account_fingerprint=ACCOUNT_FINGERPRINT, now=NOW)
    binding = base.venue_binding.model_copy(
        update={
            "manifest_record_hash": MANIFEST_HASH,
            "manifest_source_hashes": ("3" * 64,),
            "eligibility_evidence_hashes": ("4" * 64,),
            "strategy_policy_hash": "5" * 64,
            "proof_policy_hash": "6" * 64,
            "economics_policy_hash": "7" * 64,
            "protocol_fixture_hash": _protocol_fixture_hash(),
            "route_set_version": "polymarket-mutations-v1",
            "route_set_hash": "d" * 64,
        }
    )
    return base.model_copy(
        update={
            "venue_binding": binding,
            "allowed_operations": (
                ExecutionOperation.CANCEL_ORDER,
                ExecutionOperation.HEARTBEAT,
                ExecutionOperation.SIGN_ORDER,
                ExecutionOperation.SUBMIT_ORDER,
            ),
            **overrides,
        }
    )


DEFAULT_GRANT = _capability_grant()


def _authority_proof() -> SignerCapabilityProof:
    return ISSUER.proof(DEFAULT_GRANT)


CAPABILITY_DIGEST = _authority_proof().grant.digest


class _ShortReader:
    def __init__(self, value: bytes, *, chunk_size: int) -> None:
        self._value = value
        self._chunk_size = chunk_size
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._value):
            return b""
        actual = min(size, self._chunk_size)
        chunk = self._value[self._offset : self._offset + actual]
        self._offset += len(chunk)
        return chunk


class _ShortWriter:
    def __init__(self, *, chunk_size: int) -> None:
        self.value = bytearray()
        self.flush_count = 0
        self._chunk_size = chunk_size

    def write(self, value: bytes) -> int:
        chunk = value[: self._chunk_size]
        self.value.extend(chunk)
        return len(chunk)

    def flush(self) -> None:
        self.flush_count += 1


class _FailingReader:
    def read(self, size: int = -1) -> bytes:
        del size
        raise OSError("frame-io-canary")


class _FailingWriter:
    def __init__(self, *, fail_flush: bool) -> None:
        self._fail_flush = fail_flush

    def write(self, value: bytes) -> int:
        if not self._fail_flush:
            raise OSError("frame-io-canary")
        return len(value)

    def flush(self) -> None:
        if self._fail_flush:
            raise OSError("frame-io-canary")


class _FdStream:
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor

    def read(self, size: int = -1) -> bytes:
        return os.read(self.descriptor, size)

    def write(self, value: bytes) -> int:
        return os.write(self.descriptor, value)

    def flush(self) -> None:
        return None


def _captured_protocol_error(operation: object) -> SignerProtocolError:
    captured: SignerProtocolError | None = None
    try:
        operation()  # type: ignore[operator]
    except SignerProtocolError as error:
        captured = error
    if captured is None:
        raise AssertionError("IPC_PROTOCOL_ERROR_NOT_RAISED") from None
    return captured


def _assert_canaries_absent(observed: str | bytes, *canaries: str | bytes) -> None:
    observed_bytes = observed if type(observed) is bytes else observed.encode("utf-8")
    for canary in canaries:
        canary_bytes = canary if type(canary) is bytes else canary.encode("utf-8")
        if canary_bytes in observed_bytes:
            raise AssertionError(_CANARY_ASSERTION_FAILED) from None


def test_frame_round_trip_uses_four_byte_big_endian_length() -> None:
    stream = io.BytesIO()

    write_frame(stream, b"public-payload")

    assert stream.getvalue() == b"\x00\x00\x00\x0epublic-payload"
    stream.seek(0)
    assert read_frame(stream) == b"public-payload"


def test_frame_reads_and_writes_exactly_across_short_io() -> None:
    encoded = b"\x00\x00\x00\x07payload"
    reader = _ShortReader(encoded, chunk_size=1)
    writer = _ShortWriter(chunk_size=2)

    assert read_frame(reader) == b"payload"  # type: ignore[arg-type]
    write_frame(writer, b"payload")  # type: ignore[arg-type]

    assert bytes(writer.value) == encoded
    assert writer.flush_count == 1


@pytest.mark.parametrize(
    "encoded",
    (
        b"\x00\x00\x00",
        b"\x00\x00\x00\x00",
        (MAX_FRAME_BYTES + 1).to_bytes(4, "big"),
        b"\x00\x00\x00\x05four",
    ),
)
def test_frame_rejects_invalid_size_or_truncation_with_stable_error(encoded: bytes) -> None:
    with pytest.raises(SignerProtocolError) as rejected:
        read_frame(io.BytesIO(encoded))

    assert str(rejected.value) in {"IPC_FRAME_SIZE_INVALID", "IPC_FRAME_TRUNCATED"}
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None


@pytest.mark.parametrize("size", (0, MAX_FRAME_BYTES + 1), ids=("empty", "oversized"))
def test_frame_write_rejects_invalid_size_before_io(size: int) -> None:
    stream = io.BytesIO()
    payload = b"x" * size

    with pytest.raises(SignerProtocolError, match=r"^IPC_FRAME_SIZE_INVALID$"):
        write_frame(stream, payload)

    assert stream.getvalue() == b""


@pytest.mark.parametrize(
    ("operation", "error_code"),
    (
        (lambda: read_frame(_FailingReader()), "IPC_FRAME_READ_FAILED"),
        (lambda: write_frame(_FailingWriter(fail_flush=False), b"x"), "IPC_FRAME_WRITE_FAILED"),
        (lambda: write_frame(_FailingWriter(fail_flush=True), b"x"), "IPC_FRAME_FLUSH_FAILED"),
    ),
)
def test_frame_io_errors_are_stable_context_free_and_do_not_reflect_exceptions(
    operation: object,
    error_code: str,
) -> None:
    error = _captured_protocol_error(operation)

    assert str(error) == error_code
    assert "frame-io-canary" not in str(error) + repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("payload", (bytearray(b"x"), memoryview(b"x"), "x"))
def test_frame_writer_accepts_exact_bytes_only(payload: object) -> None:
    error = _captured_protocol_error(lambda: write_frame(io.BytesIO(), payload))  # type: ignore[arg-type]

    assert str(error) == "IPC_FRAME_BYTES_REQUIRED"


def _intent(**overrides: object) -> ExecutionIntent:
    return ExecutionIntent(
        **execution_intent_fields(
            **{
                "account_fingerprint": ACCOUNT_FINGERPRINT,
                "capability_fingerprint": CAPABILITY_DIGEST,
                **overrides,
            }
        )
    )


def _payload(operation: ExecutionOperation) -> object:
    if operation is ExecutionOperation.DESCRIBE_IDENTITY:
        return DescribeIdentityPayload(operation=operation)
    if operation is ExecutionOperation.SIGNER_KILL:
        return SignerKillPayload(
            operation=operation,
            directive=SignerKillDirective(
                capability_ids=(UUID("11111111-1111-4111-8111-111111111112"),),
                issued_at=NOW,
                signature=b"cHVibGljLXNpZ25hdHVyZQ==",
            ),
        )
    if operation is ExecutionOperation.SIGN_ORDER:
        return SignOrderPayload(operation=operation, intent=_intent())
    if operation is ExecutionOperation.SUBMIT_ORDER:
        service = _service()
        signed = service.handle(_request(ExecutionOperation.SIGN_ORDER))
        assert isinstance(signed.result, SignedEnvelopeResult)
        service.close()
        return SubmitOrderPayload(
            operation=operation,
            intent=_intent(),
            envelope=signed.result.envelope,
        )
    if operation is ExecutionOperation.CANCEL_ORDER:
        return CancelOrderPayload(operation=operation, venue_order_id="order-1")
    if operation is ExecutionOperation.HEARTBEAT:
        return HeartbeatPayload(operation=operation, heartbeat_id="")
    if operation is ExecutionOperation.READ_ORDERS:
        return ReadOrdersPayload(operation=operation, venue_order_id=None)
    if operation is ExecutionOperation.READ_TRADES:
        return ReadTradesPayload(operation=operation)
    if operation is ExecutionOperation.READ_ACCOUNT:
        return ReadAccountPayload(
            operation=operation,
            signature_type=0,
            asset_type="COLLATERAL",
            token_id=None,
        )
    raise AssertionError("UNKNOWN_TEST_OPERATION") from None


def _request(
    operation: ExecutionOperation = ExecutionOperation.SIGN_ORDER,
    **overrides: object,
) -> SignerRequest:
    intent = _intent()
    payload = overrides.pop("payload", None)
    fields: dict[str, object] = {
        "schema_version": 1,
        "request_id": UUID("11111111-1111-4111-8111-111111111111"),
        "intent_id": intent.intent_id,
        "intent_fingerprint": intent.intent_fingerprint,
        "capability_digest": CAPABILITY_DIGEST,
        "plan_digest": DEFAULT_GRANT.plan_hash,
        "authority_proof": (
            None
            if operation
            in {
                ExecutionOperation.DESCRIBE_IDENTITY,
                ExecutionOperation.SIGNER_KILL,
                ExecutionOperation.READ_ORDERS,
                ExecutionOperation.READ_TRADES,
                ExecutionOperation.READ_ACCOUNT,
            }
            else _authority_proof()
        ),
        "manifest_digest": MANIFEST_HASH,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "protocol_version": "polymarket-clob-2026-08-25-v1",
        "operation": operation,
        "deadline": NOW + timedelta(seconds=5),
        "payload": _payload(operation) if payload is None else payload,
    }
    fields.update(overrides)
    return SignerRequest.model_validate(fields)


def _action_request(
    operation: ExecutionOperation,
    *,
    plan_id: UUID | None = None,
    request_id: UUID | None = None,
    authority_proof: SignerCapabilityProof | None = None,
) -> SignerRequest:
    proof = authority_proof or _authority_proof()
    intent_overrides: dict[str, object] = {"capability_fingerprint": proof.grant.digest}
    if plan_id is not None:
        intent_overrides["plan_id"] = plan_id
    intent = _intent(**intent_overrides)
    if operation is ExecutionOperation.SIGN_ORDER:
        payload: object = SignOrderPayload(operation=operation, intent=intent)
    elif operation is ExecutionOperation.SUBMIT_ORDER:
        payload = SubmitOrderPayload(
            operation=operation,
            intent=intent,
            envelope=sign_order(intent, PRIVATE_KEY, load_protocol_snapshot()),
        )
    else:
        raise AssertionError("ACTION_REQUEST_OPERATION_INVALID") from None
    return _request(
        operation,
        request_id=request_id or uuid4(),
        intent_id=intent.intent_id,
        intent_fingerprint=intent.intent_fingerprint,
        capability_digest=proof.grant.digest,
        plan_digest=proof.grant.plan_hash,
        authority_proof=proof,
        payload=payload,
    )


def _describe_identity_request() -> SignerRequest:
    return _request(
        ExecutionOperation.DESCRIBE_IDENTITY,
        account_fingerprint="0" * 64,
    )


def test_mutation_request_requires_a_capability_proof() -> None:
    with pytest.raises(SignerProtocolError, match=r"^IPC_MODEL_INVALID$"):
        _request(authority_proof=None)


def _handlers(**overrides: object) -> SignerOperationHandlers:
    def result(payload: object) -> SanitizedOperationResult:
        operation = payload.operation  # type: ignore[attr-defined]
        return SanitizedOperationResult(
            operation=operation,
            result_code={
                ExecutionOperation.SUBMIT_ORDER: "SUBMIT_ORDER_OK",
                ExecutionOperation.CANCEL_ORDER: "CANCEL_ORDER_OK",
                ExecutionOperation.HEARTBEAT: "HEARTBEAT_OK",
                ExecutionOperation.READ_ORDERS: "READ_ORDERS_OK",
                ExecutionOperation.READ_TRADES: "READ_TRADES_OK",
                ExecutionOperation.READ_ACCOUNT: "READ_ACCOUNT_OK",
            }[operation],
            evidence_hashes=(),
            venue_order_id="order-1"
            if operation
            in {
                ExecutionOperation.SUBMIT_ORDER,
                ExecutionOperation.CANCEL_ORDER,
                ExecutionOperation.READ_ORDERS,
            }
            else None,
            heartbeat_id="heartbeat-1" if operation is ExecutionOperation.HEARTBEAT else None,
        )

    fields: dict[str, object] = {
        "submit_order": result,
        "cancel_order": result,
        "heartbeat": result,
        "read_orders": result,
        "read_trades": result,
        "read_account": result,
    }
    fields.update(overrides)
    return SignerOperationHandlers(**fields)  # type: ignore[arg-type]


def _balance_operation_result(
    *,
    entry_count: int,
    amount: str,
) -> SanitizedOperationResult:
    return SanitizedOperationResult(
        operation=ExecutionOperation.READ_ACCOUNT,
        result_code=RestCode.READ_OK,
        evidence_hashes=("b" * 64,),
        route=RouteKey.READ_BALANCE_ALLOWANCE,
        observed_at=NOW,
        raw_body_hash="b" * 64,
        request_body_hash=None,
        attempts=1,
        recovery_required=False,
        kill_required=False,
        public_payload=BalanceAllowancePayload(
            kind="BALANCE_ALLOWANCE",
            balance="1",
            allowances=tuple(
                AllowanceEntry(address=f"0x{index:040x}", amount=amount)
                for index in range(entry_count)
            ),
        ),
    )


def _service(
    *,
    authority_calls: list[UUID] | None = None,
    read_calls: list[UUID] | None = None,
    handlers: SignerOperationHandlers | None = None,
    max_cache_entries: int = 64,
    now: datetime = NOW,
    context_now: datetime | None = None,
    private_key: bytes = PRIVATE_KEY,
    api_key: bytes = API_KEY,
    api_secret: bytes = API_SECRET,
    passphrase: bytes = PASSPHRASE,
) -> SignerService:
    def context_factory(request: SignerRequest, observed_at: datetime) -> object:
        if authority_calls is not None:
            authority_calls.append(request.request_id)
        assert request.authority_proof is not None
        grant = request.authority_proof.grant
        binding = grant.venue_binding
        capability = verified_capability_from_grant(grant, verified_at=observed_at)
        return authority_context(
            now=observed_at if context_now is None else context_now,
            account_fingerprint=request.account_fingerprint,
            account_scope_account_fingerprint=request.account_fingerprint,
            manifest_record_hash=request.manifest_digest,
            manifest_source_hashes=binding.manifest_source_hashes,
            strategy_policy_hash=binding.strategy_policy_hash,
            proof_policy_hash=binding.proof_policy_hash,
            economics_policy_hash=binding.economics_policy_hash,
            protocol_fixture_hash=binding.protocol_fixture_hash,
            route_set_version=binding.route_set_version,
            route_set_hash=binding.route_set_hash,
            requested_notional=Decimal("0"),
            capital_after=Decimal("0"),
            position_after=Decimal("0"),
            loss_after=Decimal("0"),
            activation_nonce=grant.nonce,
            expected_mode=grant.mode.value,
            expected_grant_kind=grant.grant_kind.value,
            action_id=grant.parent_action_id,
            session_id=grant.session_id,
            requested_limits_hash=grant.requested_limits_hash,
            ceiling_hash=grant.ceiling_hash,
            plan_hash=request.plan_digest,
            strategy_hash=grant.strategy_hash,
            proof_family_hash=grant.proof_family_hash,
            recovery_policy_hash=grant.recovery_policy_hash,
            verified_capability=capability,
        )

    def read_guard(request: SignerRequest, observed_at: datetime) -> AuthorityDecision:
        del observed_at
        if read_calls is not None:
            read_calls.append(request.request_id)
        return AuthorityDecision(True, None, ())

    return SignerService(
        secrets=SecretMaterial(
            bytearray(private_key),
            bytearray(api_key),
            bytearray(api_secret),
            bytearray(passphrase),
        ),
        authority_context_factory=context_factory,  # type: ignore[arg-type]
        read_guard=read_guard,
        handlers=handlers or _handlers(),
        clock=lambda: now,
        capability_public_key=ISSUER.public_verification_key,
        max_cache_entries=max_cache_entries,
    )


def test_describe_identity_returns_wallet_derived_fingerprints() -> None:
    service = _service()
    try:
        response = service.handle(_describe_identity_request())
    finally:
        service.close()

    expected = sha256(bytes.fromhex(Account.from_key(PRIVATE_KEY).address[2:])).hexdigest()
    assert response.ok, response.error_code
    assert response.result is not None
    assert response.result.account_fingerprint == expected
    assert response.result.wallet_fingerprint == expected


def test_describe_identity_never_reflects_secret_bytes() -> None:
    service = _service()
    try:
        response = service.handle(_describe_identity_request())
    finally:
        service.close()

    assert PRIVATE_KEY.hex() not in response.model_dump_json()


def test_describe_identity_is_the_only_account_gate_exempt_operation() -> None:
    service = _service()
    try:
        response = service.handle(
            _request(ExecutionOperation.READ_ACCOUNT, account_fingerprint="0" * 64)
        )
    finally:
        service.close()

    assert response.error_code == "ACCOUNT_FINGERPRINT_MISMATCH"


def _child_service_factory(
    secrets: SecretMaterial,
    *,
    crash_operation: ExecutionOperation | None,
    oversized_balance: bool = False,
) -> SignerService:
    global _CHILD_SECRET_MATERIAL
    _CHILD_SECRET_MATERIAL = secrets

    def context_factory(request: SignerRequest, observed_at: datetime) -> object:
        assert request.authority_proof is not None
        grant = request.authority_proof.grant
        binding = grant.venue_binding
        capability = verified_capability_from_grant(grant, verified_at=observed_at)
        return authority_context(
            now=observed_at,
            account_fingerprint=request.account_fingerprint,
            account_scope_account_fingerprint=request.account_fingerprint,
            manifest_record_hash=request.manifest_digest,
            manifest_source_hashes=binding.manifest_source_hashes,
            strategy_policy_hash=binding.strategy_policy_hash,
            proof_policy_hash=binding.proof_policy_hash,
            economics_policy_hash=binding.economics_policy_hash,
            protocol_fixture_hash=binding.protocol_fixture_hash,
            route_set_version=binding.route_set_version,
            route_set_hash=binding.route_set_hash,
            requested_notional=Decimal("0"),
            capital_after=Decimal("0"),
            position_after=Decimal("0"),
            loss_after=Decimal("0"),
            activation_nonce=grant.nonce,
            expected_mode=grant.mode.value,
            expected_grant_kind=grant.grant_kind.value,
            action_id=grant.parent_action_id,
            session_id=grant.session_id,
            requested_limits_hash=grant.requested_limits_hash,
            ceiling_hash=grant.ceiling_hash,
            plan_hash=request.plan_digest,
            strategy_hash=grant.strategy_hash,
            proof_family_hash=grant.proof_family_hash,
            recovery_policy_hash=grant.recovery_policy_hash,
            verified_capability=capability,
        )

    def result(payload: object) -> SanitizedOperationResult:
        operation = payload.operation  # type: ignore[attr-defined]
        if operation is crash_operation:
            raise SystemExit("SANITIZED_CHILD_CRASH")
        if oversized_balance and operation is ExecutionOperation.READ_ACCOUNT:
            return _balance_operation_result(entry_count=10_000, amount="1" * 56)
        return SanitizedOperationResult(
            operation=operation,
            result_code={
                ExecutionOperation.SUBMIT_ORDER: "SUBMIT_ORDER_OK",
                ExecutionOperation.CANCEL_ORDER: "CANCEL_ORDER_OK",
                ExecutionOperation.HEARTBEAT: "HEARTBEAT_OK",
                ExecutionOperation.READ_ORDERS: "READ_ORDERS_OK",
                ExecutionOperation.READ_TRADES: "READ_TRADES_OK",
                ExecutionOperation.READ_ACCOUNT: "READ_ACCOUNT_OK",
            }[operation],
            evidence_hashes=(),
            venue_order_id="order-1"
            if operation
            in {
                ExecutionOperation.SUBMIT_ORDER,
                ExecutionOperation.CANCEL_ORDER,
                ExecutionOperation.READ_ORDERS,
            }
            else None,
            heartbeat_id="heartbeat-1" if operation is ExecutionOperation.HEARTBEAT else None,
        )

    return SignerService(
        secrets=secrets,
        authority_context_factory=context_factory,  # type: ignore[arg-type]
        read_guard=lambda request, observed_at: AuthorityDecision(True, None, ()),
        handlers=SignerOperationHandlers(
            submit_order=result,
            cancel_order=result,
            heartbeat=result,
            read_orders=result,
            read_trades=result,
            read_account=result,
        ),
        clock=lambda: NOW,
        capability_public_key=ISSUER.public_verification_key,
        max_cache_entries=8,
    )


def _spawned_signer_target(
    request_handle: object,
    response_handle: object,
    secret_handles: tuple[object, object, object, object],
    audit_handle: object,
    crash_operation: ExecutionOperation | None,
    oversized_balance: bool,
    max_requests: int,
    max_lifetime_seconds: float,
) -> None:
    request_fd = request_handle.detach()  # type: ignore[attr-defined]
    response_fd = response_handle.detach()  # type: ignore[attr-defined]
    secret_fds = tuple(handle.detach() for handle in secret_handles)  # type: ignore[attr-defined]
    audit_fd = audit_handle.detach()  # type: ignore[attr-defined]
    try:
        run_signer_sidecar(
            request_fd=request_fd,
            response_fd=response_fd,
            secret_descriptors=secret_fds,
            service_factory=lambda secrets: _child_service_factory(
                secrets,
                crash_operation=crash_operation,
                oversized_balance=oversized_balance,
            ),
            max_requests=max_requests,
            max_lifetime_seconds=max_lifetime_seconds,
        )
    finally:
        material = _CHILD_SECRET_MATERIAL
        zeroized = material is not None and all(
            not any(value)
            for value in (
                material.private_key,
                material.api_key,
                material.api_secret,
                material.passphrase,
            )
        )
        os.write(audit_fd, b"1" if zeroized else b"0")
        os.close(audit_fd)


def _secret_descriptor(value: bytes) -> int:
    read_fd, write_fd = os.pipe()
    try:
        encoded = len(value).to_bytes(4, "big") + value
        assert os.write(write_fd, encoded) == len(encoded)
    finally:
        os.close(write_fd)
    return read_fd


def _spawn_signer(
    *,
    crash_operation: ExecutionOperation | None = None,
    oversized_balance: bool = False,
    max_requests: int = 1,
    max_lifetime_seconds: float = 5,
) -> tuple[multiprocessing.Process, int, int, int]:
    request_read, request_write = os.pipe()
    response_read, response_write = os.pipe()
    audit_read, audit_write = os.pipe()
    secret_fds = tuple(
        _secret_descriptor(value) for value in (PRIVATE_KEY, API_KEY, API_SECRET, PASSPHRASE)
    )
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_spawned_signer_target,
        args=(
            reduction.DupFd(request_read),
            reduction.DupFd(response_write),
            tuple(reduction.DupFd(descriptor) for descriptor in secret_fds),
            reduction.DupFd(audit_write),
            crash_operation,
            oversized_balance,
            max_requests,
            max_lifetime_seconds,
        ),
    )
    process.start()
    for descriptor in (request_read, response_write, audit_write, *secret_fds):
        os.close(descriptor)
    return process, request_write, response_read, audit_read


def _join_spawned(
    process: multiprocessing.Process,
    request_write: int,
    response_read: int,
    audit_read: int,
) -> tuple[int | None, bytes]:
    for descriptor in (request_write, response_read):
        with suppress(OSError):
            os.close(descriptor)
    process.join(timeout=5)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        raise AssertionError("SPAWNED_SIGNER_DID_NOT_EXIT") from None
    audit = os.read(audit_read, 1)
    os.close(audit_read)
    return process.exitcode, audit


def test_request_and_response_have_exact_strict_public_field_allowlists() -> None:
    assert tuple(SignerRequest.model_fields) == (
        "schema_version",
        "request_id",
        "intent_id",
        "intent_fingerprint",
        "capability_digest",
        "plan_digest",
        "authority_proof",
        "manifest_digest",
        "account_fingerprint",
        "protocol_version",
        "operation",
        "deadline",
        "payload",
    )
    assert tuple(SignerResponse.model_fields) == (
        "schema_version",
        "request_id",
        "ok",
        "result",
        "error_code",
    )
    raw = _request().model_dump(mode="json")
    raw["private_key"] = "private-key-canary"
    with pytest.raises(SignerProtocolError) as rejected:
        SignerRequest.model_validate(raw)
    assert str(rejected.value) == "IPC_MODEL_INVALID"
    assert "private-key-canary" not in str(rejected.value)
    assert "private-key-canary" not in repr(rejected.value)


@pytest.mark.parametrize("operation", tuple(ExecutionOperation))
def test_each_operation_requires_its_exact_discriminated_payload(
    operation: ExecutionOperation,
) -> None:
    request = _request(operation)
    assert request.payload.operation is operation
    different = (
        ExecutionOperation.READ_ACCOUNT
        if operation is not ExecutionOperation.READ_ACCOUNT
        else ExecutionOperation.READ_TRADES
    )
    with pytest.raises(SignerProtocolError, match=r"^IPC_MODEL_INVALID$"):
        _request(operation, payload=_payload(different))


def test_direct_nested_validation_and_bypass_surfaces_are_context_free() -> None:
    canary = "nested-direct-validation-canary"
    python_value = _request().model_dump(mode="python")
    python_value["payload"]["intent"]["private_key"] = canary
    json_value = _request().model_dump(mode="json")
    json_value["payload"]["intent"]["private_key"] = canary
    operations = (
        lambda: SignerRequest(**python_value),
        lambda: SignerRequest.model_validate(python_value),
        lambda: SignerRequest.model_validate_json(
            json.dumps(json_value, separators=(",", ":")),
            strict=True,
        ),
        lambda: SignerRequest.model_construct(payload={"private_key": canary}),
        lambda: _request().model_copy(update={"payload": {"private_key": canary}}),
    )

    for operation in operations:
        error = _captured_protocol_error(operation)
        assert str(error) == "IPC_MODEL_INVALID"
        _assert_canaries_absent(str(error) + repr(error), canary)
        assert error.__cause__ is None
        assert error.__context__ is None


def test_direct_model_display_redacts_even_valid_public_canary_fields() -> None:
    canary = API_KEY.decode()
    result = SanitizedOperationResult(
        operation=ExecutionOperation.SUBMIT_ORDER,
        result_code="SUBMIT_ORDER_OK",
        evidence_hashes=(),
        venue_order_id=canary,
    )

    _assert_canaries_absent(str(result) + repr(result), canary)


def test_arbitrary_result_and_error_codes_are_not_constructible() -> None:
    operations = (
        lambda: SanitizedOperationResult(
            operation=ExecutionOperation.READ_ACCOUNT,
            result_code="ARBITRARY_RESULT_CODE",
            evidence_hashes=(),
        ),
        lambda: SignerResponse.rejected(None, "ARBITRARY_ERROR_CODE"),
    )

    for operation in operations:
        error = _captured_protocol_error(operation)
        assert str(error) == "IPC_MODEL_INVALID"


@pytest.mark.parametrize(
    "fields",
    (
        {
            "operation": ExecutionOperation.SUBMIT_ORDER,
            "result_code": "SUBMIT_ORDER_OK",
            "evidence_hashes": (),
            "venue_order_id": None,
        },
        {
            "operation": ExecutionOperation.CANCEL_ORDER,
            "result_code": "SUBMIT_ORDER_OK",
            "evidence_hashes": (),
            "venue_order_id": "order-1",
        },
        {
            "operation": ExecutionOperation.HEARTBEAT,
            "result_code": "HEARTBEAT_OK",
            "evidence_hashes": (),
            "heartbeat_id": "",
        },
        {
            "operation": ExecutionOperation.READ_TRADES,
            "result_code": "READ_TRADES_OK",
            "evidence_hashes": (),
            "venue_order_id": "order-1",
        },
    ),
)
def test_operation_results_enforce_exact_success_invariants(
    fields: dict[str, object],
) -> None:
    error = _captured_protocol_error(lambda: SanitizedOperationResult(**fields))

    assert str(error) == "IPC_MODEL_INVALID"


def test_real_order_signer_returns_only_the_strict_public_envelope_result() -> None:
    service = _service()

    response = service.handle(_request())

    assert response.ok is True
    assert response.error_code is None
    assert isinstance(response.result, SignedEnvelopeResult)
    assert response.result.operation is ExecutionOperation.SIGN_ORDER
    assert response.result.envelope.intent_id == _intent().intent_id
    assert response.result.envelope.public_signature.startswith("0x")
    assert PRIVATE_KEY.hex() not in response.model_dump_json()
    service.close()


def test_signer_rejects_a_valid_grant_signed_by_another_issuer_before_authority_factory() -> None:
    authority_calls: list[UUID] = []
    service = _service(authority_calls=authority_calls)
    proof = OTHER_ISSUER.proof(DEFAULT_GRANT)

    response = service.handle(
        _action_request(
            ExecutionOperation.SUBMIT_ORDER,
            authority_proof=proof,
        )
    )

    assert response.error_code == "CAPABILITY_SIGNATURE_INVALID"
    assert authority_calls == []
    service.close()


def test_signer_consumes_a_primary_submission_before_handler_dispatch() -> None:
    handler_calls = 0

    def fail_submit(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload
        handler_calls += 1
        raise RuntimeError("HANDLER_FAILURE_MUST_NOT_RELEASE_AUTHORITY")

    service = _service(handlers=_handlers(submit_order=fail_submit))
    plan_id = UUID("11111111-1111-4111-8111-111111111119")
    first = _action_request(ExecutionOperation.SUBMIT_ORDER, plan_id=plan_id)
    replay = _action_request(
        ExecutionOperation.SUBMIT_ORDER,
        plan_id=plan_id,
    )

    assert service.handle(first).error_code == "HANDLER_FAILED"
    assert service.handle(replay).error_code == "CAPABILITY_REPLAYED"
    assert handler_calls == 1
    service.close()


def test_signed_kill_blocks_a_previously_valid_mutation_without_handler_dispatch() -> None:
    handler_calls = 0

    def submit(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload
        handler_calls += 1
        raise AssertionError("KILLED_SIGNER_MUST_NOT_DISPATCH")

    service = _service(handlers=_handlers(submit_order=submit))
    directive = ISSUER.kill_directive((DEFAULT_GRANT.capability_id,))
    kill = _request(
        ExecutionOperation.SIGNER_KILL,
        request_id=uuid4(),
        payload=SignerKillPayload(
            operation=ExecutionOperation.SIGNER_KILL,
            directive=directive,
        ),
    )

    killed = service.handle(kill)
    blocked = service.handle(_action_request(ExecutionOperation.SUBMIT_ORDER))

    assert killed.ok is True
    assert getattr(killed.result, "result_code", None) == "SIGNER_KILL_ENGAGED"
    assert blocked.error_code == "PILOT_KILL_ENGAGED"
    assert handler_calls == 0
    service.close()


def test_invalid_kill_directive_cannot_engage_or_clear_local_kill() -> None:
    service = _service()
    invalid = OTHER_ISSUER.kill_directive((DEFAULT_GRANT.capability_id,))
    rejected = service.handle(
        _request(
            ExecutionOperation.SIGNER_KILL,
            request_id=uuid4(),
            payload=SignerKillPayload(
                operation=ExecutionOperation.SIGNER_KILL,
                directive=invalid,
            ),
        )
    )

    allowed = service.handle(_action_request(ExecutionOperation.SIGN_ORDER))
    valid = ISSUER.kill_directive((DEFAULT_GRANT.capability_id,))
    engaged = service.handle(
        _request(
            ExecutionOperation.SIGNER_KILL,
            request_id=uuid4(),
            payload=SignerKillPayload(
                operation=ExecutionOperation.SIGNER_KILL,
                directive=valid,
            ),
        )
    )
    rejected_after_kill = service.handle(
        _request(
            ExecutionOperation.SIGNER_KILL,
            request_id=uuid4(),
            payload=SignerKillPayload(
                operation=ExecutionOperation.SIGNER_KILL,
                directive=invalid,
            ),
        )
    )
    still_blocked = service.handle(_action_request(ExecutionOperation.SUBMIT_ORDER))

    assert rejected.error_code == "CAPABILITY_SIGNATURE_INVALID"
    assert allowed.ok is True
    assert engaged.ok is True
    assert rejected_after_kill.error_code == "CAPABILITY_SIGNATURE_INVALID"
    assert still_blocked.error_code == "PILOT_KILL_ENGAGED"
    service.close()


def test_signer_binds_the_verified_grant_plan_hash_not_only_its_digest() -> None:
    service = _service()
    request = _action_request(ExecutionOperation.SUBMIT_ORDER)

    response = service.handle(request.model_copy(update={"plan_digest": "f" * 64}))

    assert response.error_code == "CAPABILITY_PLAN_MISMATCH"
    service.close()


@pytest.mark.parametrize(
    ("grant", "operation", "request_updates", "error_code"),
    (
        (
            _capability_grant(account_fingerprint="f" * 64),
            ExecutionOperation.SUBMIT_ORDER,
            {},
            "CAPABILITY_ACCOUNT_MISMATCH",
        ),
        (
            DEFAULT_GRANT,
            ExecutionOperation.SUBMIT_ORDER,
            {"manifest_digest": "f" * 64},
            "CAPABILITY_MANIFEST_MISMATCH",
        ),
        (
            _capability_grant(
                venue_binding=DEFAULT_GRANT.venue_binding.model_copy(
                    update={"protocol_fixture_hash": "f" * 64}
                )
            ),
            ExecutionOperation.SUBMIT_ORDER,
            {},
            "CAPABILITY_PROTOCOL_MISMATCH",
        ),
        (
            _capability_grant(allowed_operations=(ExecutionOperation.SIGN_ORDER,)),
            ExecutionOperation.SUBMIT_ORDER,
            {},
            "CAPABILITY_OPERATION_NOT_ALLOWED",
        ),
        (
            _capability_grant(not_before=NOW + timedelta(seconds=1)),
            ExecutionOperation.SUBMIT_ORDER,
            {},
            "CAPABILITY_NOT_YET_VALID",
        ),
        (
            _capability_grant(
                expires_at=NOW + timedelta(seconds=2),
                presence_deadline=NOW + timedelta(seconds=2),
            ),
            ExecutionOperation.SUBMIT_ORDER,
            {},
            "CAPABILITY_EXPIRED",
        ),
    ),
    ids=("account", "manifest", "protocol", "operation", "not-before", "request-deadline"),
)
def test_signer_compares_signed_grant_bindings_before_authority_factory(
    grant: CapabilityGrant,
    operation: ExecutionOperation,
    request_updates: dict[str, object],
    error_code: str,
) -> None:
    authority_calls: list[UUID] = []
    proof = ISSUER.proof(grant)
    service = _service(authority_calls=authority_calls)
    request = _action_request(operation, authority_proof=proof)

    response = service.handle(request.model_copy(update=request_updates))

    assert response.error_code == error_code
    assert authority_calls == []
    service.close()


def test_complete_strategy_allows_sign_then_submit_and_distinct_plan_intents() -> None:
    def submit(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        return SanitizedOperationResult(
            operation=ExecutionOperation.SUBMIT_ORDER,
            result_code="SUBMIT_ORDER_OK",
            evidence_hashes=(),
            venue_order_id=str(payload.intent.intent_id),
        )

    service = _service(handlers=_handlers(submit_order=submit))
    first_plan = UUID("22222222-2222-4222-8222-222222222221")
    second_plan = UUID("22222222-2222-4222-8222-222222222222")

    signed = service.handle(
        _action_request(ExecutionOperation.SIGN_ORDER, plan_id=first_plan)
    )
    first = service.handle(
        _action_request(ExecutionOperation.SUBMIT_ORDER, plan_id=first_plan)
    )
    replay = service.handle(
        _action_request(ExecutionOperation.SUBMIT_ORDER, plan_id=first_plan)
    )
    second = service.handle(
        _action_request(ExecutionOperation.SUBMIT_ORDER, plan_id=second_plan)
    )

    assert signed.ok is True
    assert first.ok is True
    assert replay.error_code == "CAPABILITY_REPLAYED"
    assert second.ok is True
    service.close()


def test_exact_order_capability_permits_only_one_intent() -> None:
    grant = _capability_grant(mode=AuthorizationMode.EXACT_ORDER)
    proof = ISSUER.proof(grant)
    service = _service()
    first_plan = UUID("33333333-3333-4333-8333-333333333331")
    second_plan = UUID("33333333-3333-4333-8333-333333333332")

    signed = service.handle(
        _action_request(
            ExecutionOperation.SIGN_ORDER,
            plan_id=first_plan,
            authority_proof=proof,
        )
    )
    submitted = service.handle(
        _action_request(
            ExecutionOperation.SUBMIT_ORDER,
            plan_id=first_plan,
            authority_proof=proof,
        )
    )
    denied = service.handle(
        _action_request(
            ExecutionOperation.SIGN_ORDER,
            plan_id=second_plan,
            authority_proof=proof,
        )
    )

    assert signed.ok is True
    assert submitted.ok is True
    assert denied.error_code == "CAPABILITY_REPLAYED"
    service.close()


def test_automation_capability_is_rejected_before_authority_factory() -> None:
    authority_calls: list[UUID] = []
    grant = _capability_grant(
        mode=AuthorizationMode.AUTOMATION_SESSION,
        single_use=False,
        session_id=UUID("44444444-4444-4444-8444-444444444444"),
    )
    service = _service(authority_calls=authority_calls)

    response = service.handle(
        _action_request(
            ExecutionOperation.SUBMIT_ORDER,
            authority_proof=ISSUER.proof(grant),
        )
    )

    assert response.error_code == "CAPABILITY_MODE_MISMATCH"
    assert authority_calls == []
    service.close()


def test_handler_result_requiring_kill_latches_local_signer_kill() -> None:
    request_hash = "a" * 64
    response_hash = "b" * 64

    def submit(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        del payload
        return SanitizedOperationResult(
            operation=ExecutionOperation.SUBMIT_ORDER,
            result_code=RestCode.ORDER_ACK_DELAYED,
            evidence_hashes=(request_hash, response_hash),
            venue_order_id="order-delayed",
            route=RouteKey.SUBMIT_ORDER,
            observed_at=NOW,
            raw_body_hash=response_hash,
            request_body_hash=request_hash,
            attempts=1,
            recovery_required=True,
            kill_required=True,
            public_payload=OrderAckPayload(
                kind="ORDER_ACK",
                order_id="order-delayed",
                status="delayed",
                making_amount="1",
                taking_amount="2",
                transaction_hashes=(),
                trade_ids=(),
            ),
        )

    service = _service(handlers=_handlers(submit_order=submit))

    result = service.handle(_action_request(ExecutionOperation.SUBMIT_ORDER))
    blocked = service.handle(
        _action_request(
            ExecutionOperation.SIGN_ORDER,
            plan_id=UUID("55555555-5555-4555-8555-555555555555"),
        )
    )

    assert result.ok is True
    assert blocked.error_code == "PILOT_KILL_ENGAGED"
    service.close()


def test_submit_payload_carries_the_source_intent_and_signed_envelope() -> None:
    intent = _intent()
    signed = _service().handle(_request(ExecutionOperation.SIGN_ORDER))
    assert isinstance(signed.result, SignedEnvelopeResult)

    payload = SubmitOrderPayload(
        operation=ExecutionOperation.SUBMIT_ORDER,
        intent=intent,
        envelope=signed.result.envelope,
    )

    assert payload.intent == intent
    assert payload.envelope == signed.result.envelope


@pytest.mark.parametrize(
    "tamper",
    ("foreign-relabelled", "signature", "fingerprint"),
)
def test_submit_rejects_foreign_relabelled_or_tampered_envelopes(tamper: str) -> None:
    intent = _intent()
    envelope = sign_order(intent, PRIVATE_KEY, load_protocol_snapshot())
    if tamper == "foreign-relabelled":
        foreign_intent = _intent(token_id="217427")
        foreign = sign_order(foreign_intent, PRIVATE_KEY, load_protocol_snapshot())
        envelope = foreign.model_copy(
            update={
                "intent_id": intent.intent_id,
                "intent_fingerprint": intent.intent_fingerprint,
            }
        )
    elif tamper == "signature":
        replacement = "0" if envelope.public_signature[-1] != "0" else "1"
        envelope = envelope.model_copy(
            update={"public_signature": envelope.public_signature[:-1] + replacement}
        )
    else:
        envelope = envelope.model_copy(update={"order_fingerprint": "f" * 64})
    payload = SubmitOrderPayload(
        operation=ExecutionOperation.SUBMIT_ORDER,
        intent=intent,
        envelope=envelope,
    )
    service = _service()

    response = service.handle(
        _request(
            ExecutionOperation.SUBMIT_ORDER,
            request_id=uuid4(),
            payload=payload,
        )
    )

    assert response.error_code == "ORDER_ENVELOPE_MISMATCH"
    service.close()


@pytest.mark.parametrize(
    "operation",
    (ExecutionOperation.SIGN_ORDER, ExecutionOperation.SUBMIT_ORDER),
)
def test_sign_and_submit_reject_an_expired_intent_even_with_a_fresh_outer_deadline(
    operation: ExecutionOperation,
) -> None:
    service = _service(now=NOW + timedelta(seconds=6))
    request = _request(
        operation,
        request_id=uuid4(),
        deadline=NOW + timedelta(seconds=10),
    )

    response = service.handle(request)

    assert response.error_code == "INTENT_DEADLINE_EXPIRED"
    service.close()


@pytest.mark.parametrize(
    "operation",
    (ExecutionOperation.SIGN_ORDER, ExecutionOperation.SUBMIT_ORDER),
)
def test_sign_and_submit_reject_an_outer_deadline_extension(
    operation: ExecutionOperation,
) -> None:
    service = _service()
    request = _request(
        operation,
        request_id=uuid4(),
        deadline=NOW + timedelta(seconds=6),
    )

    response = service.handle(request)

    assert response.error_code == "REQUEST_DEADLINE_EXCEEDS_INTENT"
    service.close()


@pytest.mark.parametrize(
    "operation",
    (ExecutionOperation.SIGN_ORDER, ExecutionOperation.SUBMIT_ORDER),
)
def test_sign_and_submit_require_authority_context_at_the_injected_now(
    operation: ExecutionOperation,
) -> None:
    service = _service(context_now=NOW - timedelta(microseconds=1))

    response = service.handle(_request(operation, request_id=uuid4()))

    assert response.error_code == "AUTHORITY_CONTEXT_TIME_MISMATCH"
    service.close()


def test_submit_binds_venue_order_id_before_known_intent_cancellation() -> None:
    cancel_calls = 0

    def cancel(payload: CancelOrderPayload) -> SanitizedOperationResult:
        nonlocal cancel_calls
        cancel_calls += 1
        return SanitizedOperationResult(
            operation=ExecutionOperation.CANCEL_ORDER,
            result_code="CANCEL_ORDER_OK",
            evidence_hashes=(),
            venue_order_id=payload.venue_order_id,
        )

    service = _service(handlers=_handlers(cancel_order=cancel))
    submitted = service.handle(_request(ExecutionOperation.SUBMIT_ORDER, request_id=uuid4()))
    cancelled = service.handle(_request(ExecutionOperation.CANCEL_ORDER, request_id=uuid4()))

    assert submitted.ok is True
    assert cancelled.ok is True
    assert cancel_calls == 1
    service.close()


@pytest.mark.parametrize(
    ("result_code", "status", "requires_halt"),
    (
        (RestCode.ORDER_ACK_MATCHED, "matched", False),
        (RestCode.ORDER_ACK_DELAYED, "delayed", True),
        (RestCode.ORDER_ACK_LIVE_UNEXPECTED, "live", True),
        (RestCode.ORDER_ACK_UNMATCHED, "unmatched", True),
    ),
)
def test_acknowledged_submit_binding_rejects_changed_intent_fingerprint(
    result_code: RestCode,
    status: str,
    requires_halt: bool,
) -> None:
    cancel_calls = 0
    request_hash = "a" * 64
    response_hash = "b" * 64

    def submit(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        del payload
        return SanitizedOperationResult(
            operation=ExecutionOperation.SUBMIT_ORDER,
            result_code=result_code,
            evidence_hashes=(request_hash, response_hash),
            venue_order_id="order-1",
            route=RouteKey.SUBMIT_ORDER,
            observed_at=NOW,
            raw_body_hash=response_hash,
            request_body_hash=request_hash,
            attempts=1,
            recovery_required=requires_halt,
            kill_required=requires_halt,
            public_payload=OrderAckPayload(
                kind="ORDER_ACK",
                order_id="order-1",
                status=status,
                making_amount="1",
                taking_amount="2",
                transaction_hashes=(),
                trade_ids=(),
            ),
        )

    def cancel(payload: CancelOrderPayload) -> SanitizedOperationResult:
        nonlocal cancel_calls
        cancel_calls += 1
        return SanitizedOperationResult(
            operation=ExecutionOperation.CANCEL_ORDER,
            result_code="CANCEL_ORDER_OK",
            evidence_hashes=(),
            venue_order_id=payload.venue_order_id,
        )

    service = _service(handlers=_handlers(submit_order=submit, cancel_order=cancel))
    submitted = service.handle(_request(ExecutionOperation.SUBMIT_ORDER, request_id=uuid4()))
    changed = service.handle(
        _request(
            ExecutionOperation.CANCEL_ORDER,
            request_id=uuid4(),
            intent_fingerprint="f" * 64,
        )
    )

    assert submitted.ok is True
    assert changed.error_code == (
        "PILOT_KILL_ENGAGED" if requires_halt else "CANCEL_ORDER_BINDING_MISMATCH"
    )
    assert cancel_calls == 0
    service.close()


@pytest.mark.parametrize(
    ("kind", "updates"),
    (
        ("submit", {"attempts": 2}),
        ("submit", {"request_body_hash": None, "evidence_hashes": ["b" * 64]}),
        (
            "read",
            {"request_body_hash": "a" * 64, "evidence_hashes": ["a" * 64, "b" * 64]},
        ),
    ),
    ids=("mutation-retry", "mutation-missing-hash", "get-extra-hash"),
)
def test_ipc_round_trip_rejects_impossible_route_evidence(
    kind: str,
    updates: dict[str, object],
) -> None:
    if kind == "submit":
        result = SanitizedOperationResult(
            operation=ExecutionOperation.SUBMIT_ORDER,
            result_code=RestCode.ORDER_ACK_MATCHED,
            evidence_hashes=("a" * 64, "b" * 64),
            venue_order_id="order-1",
            route=RouteKey.SUBMIT_ORDER,
            observed_at=NOW,
            raw_body_hash="b" * 64,
            request_body_hash="a" * 64,
            attempts=1,
            recovery_required=False,
            kill_required=False,
            public_payload=OrderAckPayload(
                kind="ORDER_ACK",
                order_id="order-1",
                status="matched",
                making_amount="1",
                taking_amount="2",
                transaction_hashes=(),
                trade_ids=(),
            ),
        )
    else:
        result = SanitizedOperationResult(
            operation=ExecutionOperation.READ_ORDERS,
            result_code=RestCode.READ_NOT_FOUND,
            evidence_hashes=("b" * 64,),
            venue_order_id="order-1",
            route=RouteKey.READ_ORDER,
            observed_at=NOW,
            raw_body_hash="b" * 64,
            request_body_hash=None,
            attempts=1,
            recovery_required=False,
            kill_required=False,
            public_payload=None,
        )
    response = SignerResponse.accepted(uuid4(), result)
    document = json.loads(canonical_response_bytes(response))
    document["result"].update(updates)

    with pytest.raises(SignerProtocolError, match="IPC_MODEL_INVALID"):
        SignerResponse.model_validate_json(
            json.dumps(document, separators=(",", ":"), sort_keys=True),
            strict=True,
        )


@pytest.mark.parametrize(
    ("operation", "route", "result_code", "venue_order_id", "recovery_required"),
    (
        (
            ExecutionOperation.READ_ORDERS,
            RouteKey.READ_OPEN_ORDERS,
            RestCode.READ_NOT_FOUND,
            None,
            False,
        ),
        (
            ExecutionOperation.READ_ORDERS,
            RouteKey.READ_ORDER,
            RestCode.TRANSPORT_UNAVAILABLE,
            "order-1",
            True,
        ),
        (
            ExecutionOperation.READ_TRADES,
            RouteKey.READ_TRADES,
            RestCode.RATE_LIMITED,
            None,
            True,
        ),
        (
            ExecutionOperation.READ_ACCOUNT,
            RouteKey.READ_BALANCE_ALLOWANCE,
            RestCode.PROTOCOL_RESPONSE_INVALID,
            None,
            True,
        ),
    ),
    ids=("non-order-404", "transport", "rate-limit", "malformed"),
)
def test_ipc_rejects_nonconservative_authenticated_read_results(
    operation: ExecutionOperation,
    route: RouteKey,
    result_code: RestCode,
    venue_order_id: str | None,
    recovery_required: bool,
) -> None:
    with pytest.raises(SignerProtocolError, match="IPC_MODEL_INVALID"):
        SanitizedOperationResult(
            operation=operation,
            result_code=result_code,
            evidence_hashes=("b" * 64,),
            venue_order_id=venue_order_id,
            route=route,
            observed_at=NOW,
            raw_body_hash="b" * 64,
            request_body_hash=None,
            attempts=1,
            recovery_required=recovery_required,
            kill_required=False,
            public_payload=None,
        )


def test_handler_result_is_freshly_revalidated_before_binding_and_cache() -> None:
    cancel_calls = 0
    forged = SanitizedOperationResult(
        operation=ExecutionOperation.SUBMIT_ORDER,
        result_code=RestCode.ORDER_ACK_MATCHED,
        evidence_hashes=("a" * 64, "b" * 64),
        venue_order_id="order-1",
        route=RouteKey.SUBMIT_ORDER,
        observed_at=NOW,
        raw_body_hash="b" * 64,
        request_body_hash="a" * 64,
        attempts=1,
        recovery_required=False,
        kill_required=False,
        public_payload=OrderAckPayload(
            kind="ORDER_ACK",
            order_id="order-1",
            status="matched",
            making_amount="1",
            taking_amount="2",
            transaction_hashes=(),
            trade_ids=(),
        ),
    )
    object.__setattr__(forged, "attempts", 2)

    def submit(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        del payload
        return forged

    def cancel(payload: CancelOrderPayload) -> SanitizedOperationResult:
        nonlocal cancel_calls
        cancel_calls += 1
        return SanitizedOperationResult(
            operation=ExecutionOperation.CANCEL_ORDER,
            result_code="CANCEL_ORDER_OK",
            evidence_hashes=(),
            venue_order_id=payload.venue_order_id,
        )

    service = _service(handlers=_handlers(submit_order=submit, cancel_order=cancel))
    submitted = service.handle(_request(ExecutionOperation.SUBMIT_ORDER, request_id=uuid4()))
    cancelled = service.handle(_request(ExecutionOperation.CANCEL_ORDER, request_id=uuid4()))

    assert submitted.error_code == "IPC_OPERATION_RESULT_INVALID"
    assert cancelled.error_code == "CANCEL_ORDER_UNKNOWN"
    assert cancel_calls == 0
    service.close()


def test_oversized_public_handler_result_becomes_a_stable_bounded_error() -> None:
    largest_minimal_result = _balance_operation_result(entry_count=10_000, amount="1")
    assert (
        len(canonical_response_bytes(SignerResponse.accepted(uuid4(), largest_minimal_result)))
        <= MAX_FRAME_BYTES
    )

    oversized_result = _balance_operation_result(entry_count=10_000, amount="1" * 56)
    candidate = SignerResponse.accepted(uuid4(), oversized_result)
    assert len(canonical_response_bytes(candidate)) > MAX_FRAME_BYTES

    def read_account(payload: ReadAccountPayload) -> SanitizedOperationResult:
        del payload
        return oversized_result

    service = _service(handlers=_handlers(read_account=read_account))
    response = service.handle(_request(ExecutionOperation.READ_ACCOUNT, request_id=uuid4()))
    bounded_bytes = canonical_response_bytes(response)

    assert response.error_code == "IPC_OPERATION_RESULT_INVALID"
    assert len(bounded_bytes) <= MAX_FRAME_BYTES
    write_frame(io.BytesIO(), bounded_bytes)
    service.close()


def test_unknown_or_foreign_intent_cancellation_never_dispatches() -> None:
    cancel_calls = 0

    def cancel(payload: CancelOrderPayload) -> SanitizedOperationResult:
        nonlocal cancel_calls
        cancel_calls += 1
        return SanitizedOperationResult(
            operation=ExecutionOperation.CANCEL_ORDER,
            result_code="CANCEL_ORDER_OK",
            evidence_hashes=(),
            venue_order_id=payload.venue_order_id,
        )

    service = _service(handlers=_handlers(cancel_order=cancel))
    unknown = service.handle(_request(ExecutionOperation.CANCEL_ORDER, request_id=uuid4()))
    submitted = service.handle(_request(ExecutionOperation.SUBMIT_ORDER, request_id=uuid4()))
    assert submitted.ok is True
    foreign = service.handle(
        _request(
            ExecutionOperation.CANCEL_ORDER,
            request_id=uuid4(),
            intent_id=uuid4(),
        )
    )

    assert unknown.error_code == "CANCEL_ORDER_UNKNOWN"
    assert foreign.error_code == "CANCEL_ORDER_BINDING_MISMATCH"
    assert cancel_calls == 0
    service.close()


def test_same_request_id_exact_retry_is_cached_but_changed_request_is_collision() -> None:
    authority_calls: list[UUID] = []
    service = _service(authority_calls=authority_calls)
    request = _request()

    first = service.handle(request)
    retry = service.handle(request)
    changed = _request(
        ExecutionOperation.READ_ACCOUNT,
        request_id=request.request_id,
    )
    collision = service.handle(changed)

    assert first.model_dump_json() == retry.model_dump_json()
    assert authority_calls == [request.request_id]
    assert collision.error_code == "IPC_REQUEST_COLLISION"
    service.close()


def test_concurrent_identical_requests_gate_and_dispatch_once() -> None:
    entered = Event()
    release = Event()
    second_started = Event()
    calls_lock = Lock()
    handler_calls = 0

    def read_account(payload: ReadAccountPayload) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload
        with calls_lock:
            handler_calls += 1
        entered.set()
        if not release.wait(timeout=5):
            raise AssertionError("CONCURRENT_HANDLER_NOT_RELEASED")
        return SanitizedOperationResult(
            operation=ExecutionOperation.READ_ACCOUNT,
            result_code="READ_ACCOUNT_OK",
            evidence_hashes=(),
        )

    service = _service(handlers=_handlers(read_account=read_account))
    request = _request(ExecutionOperation.READ_ACCOUNT, request_id=uuid4())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(service.handle, request)
        assert entered.wait(timeout=5)

        def second_call() -> SignerResponse:
            second_started.set()
            return service.handle(request)

        second_future = executor.submit(second_call)
        assert second_started.wait(timeout=5)
        release.set()
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)

    assert first.model_dump_json() == second.model_dump_json()
    assert handler_calls == 1
    service.close()


def test_concurrent_changed_payload_collides_without_second_dispatch() -> None:
    entered = Event()
    release = Event()
    second_started = Event()
    calls_lock = Lock()
    handler_calls = 0

    def read_account(payload: ReadAccountPayload) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload
        with calls_lock:
            handler_calls += 1
        entered.set()
        if not release.wait(timeout=5):
            raise AssertionError("CONCURRENT_HANDLER_NOT_RELEASED")
        return SanitizedOperationResult(
            operation=ExecutionOperation.READ_ACCOUNT,
            result_code="READ_ACCOUNT_OK",
            evidence_hashes=(),
        )

    service = _service(handlers=_handlers(read_account=read_account))
    first_request = _request(ExecutionOperation.READ_ACCOUNT, request_id=uuid4())
    changed_request = _request(
        ExecutionOperation.READ_ACCOUNT,
        request_id=first_request.request_id,
        deadline=NOW + timedelta(seconds=4),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(service.handle, first_request)
        assert entered.wait(timeout=5)

        def second_call() -> SignerResponse:
            second_started.set()
            return service.handle(changed_request)

        second_future = executor.submit(second_call)
        assert second_started.wait(timeout=5)
        release.set()
        first = first_future.result(timeout=5)
        collision = second_future.result(timeout=5)

    assert first.ok is True
    assert collision.error_code == "IPC_REQUEST_COLLISION"
    assert handler_calls == 1
    service.close()


def test_handler_base_exception_releases_service_lock() -> None:
    handler_calls = 0

    def read_account(payload: ReadAccountPayload) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload
        handler_calls += 1
        if handler_calls == 1:
            raise SystemExit("SANITIZED_HANDLER_EXIT")
        return SanitizedOperationResult(
            operation=ExecutionOperation.READ_ACCOUNT,
            result_code="READ_ACCOUNT_OK",
            evidence_hashes=(),
        )

    service = _service(handlers=_handlers(read_account=read_account))
    with pytest.raises(SystemExit, match=r"^SANITIZED_HANDLER_EXIT$"):
        service.handle(_request(ExecutionOperation.READ_ACCOUNT, request_id=uuid4()))

    response = service.handle(_request(ExecutionOperation.READ_ACCOUNT, request_id=uuid4()))

    assert response.ok is True
    assert handler_calls == 2
    service.close()


def test_cache_capacity_fails_closed_without_evicting_request_ids() -> None:
    service = _service(max_cache_entries=1)
    first = _request()
    second = _request(request_id=uuid4())

    accepted = service.handle(first)
    full = service.handle(second)
    replay = service.handle(first)

    assert accepted.model_dump_json() == replay.model_dump_json()
    assert full.error_code == "IPC_REPLAY_CACHE_FULL"
    service.close()


def test_deadline_is_half_open_on_first_request_and_cached_retry_is_identical() -> None:
    service = _service(now=NOW + timedelta(seconds=5))
    request = _request(deadline=NOW + timedelta(seconds=5))

    expired = service.handle(request)
    retry = service.handle(request)

    assert expired.error_code == "IPC_DEADLINE_EXPIRED"
    assert expired.model_dump_json() == retry.model_dump_json()
    service.close()


@pytest.mark.parametrize(
    "operation",
    (
        ExecutionOperation.SIGN_ORDER,
        ExecutionOperation.SUBMIT_ORDER,
        ExecutionOperation.CANCEL_ORDER,
        ExecutionOperation.HEARTBEAT,
    ),
)
def test_every_uncached_mutation_uses_fresh_independent_authority(
    operation: ExecutionOperation,
) -> None:
    authority_calls: list[UUID] = []
    service = _service(authority_calls=authority_calls)
    if operation is ExecutionOperation.CANCEL_ORDER:
        submitted = service.handle(_request(ExecutionOperation.SUBMIT_ORDER, request_id=uuid4()))
        assert submitted.ok is True
        authority_calls.clear()
    request = _request(operation, request_id=uuid4())

    response = service.handle(request)

    assert response.ok is True
    assert authority_calls == [request.request_id]
    service.close()


@pytest.mark.parametrize(
    "operation",
    (
        ExecutionOperation.READ_ORDERS,
        ExecutionOperation.READ_TRADES,
        ExecutionOperation.READ_ACCOUNT,
    ),
)
def test_each_uncached_read_uses_only_the_fresh_account_bound_read_guard(
    operation: ExecutionOperation,
) -> None:
    authority_calls: list[UUID] = []
    read_calls: list[UUID] = []
    service = _service(authority_calls=authority_calls, read_calls=read_calls)
    request = _request(operation, request_id=uuid4())

    response = service.handle(request)

    assert response.ok is True
    assert authority_calls == []
    assert read_calls == [request.request_id]
    service.close()


@pytest.mark.parametrize(
    "operation",
    (
        ExecutionOperation.SUBMIT_ORDER,
        ExecutionOperation.CANCEL_ORDER,
        ExecutionOperation.HEARTBEAT,
        ExecutionOperation.READ_ORDERS,
        ExecutionOperation.READ_TRADES,
        ExecutionOperation.READ_ACCOUNT,
    ),
)
def test_handlers_receive_only_their_exact_typed_payload(
    operation: ExecutionOperation,
) -> None:
    captured_arguments: list[tuple[object, ...]] = []

    def handler(*arguments: object) -> SanitizedOperationResult:
        captured_arguments.append(arguments)
        return SanitizedOperationResult(
            operation=operation,
            result_code={
                ExecutionOperation.SUBMIT_ORDER: "SUBMIT_ORDER_OK",
                ExecutionOperation.CANCEL_ORDER: "CANCEL_ORDER_OK",
                ExecutionOperation.HEARTBEAT: "HEARTBEAT_OK",
                ExecutionOperation.READ_ORDERS: "READ_ORDERS_OK",
                ExecutionOperation.READ_TRADES: "READ_TRADES_OK",
                ExecutionOperation.READ_ACCOUNT: "READ_ACCOUNT_OK",
            }[operation],
            evidence_hashes=(),
            venue_order_id="order-1"
            if operation
            in {
                ExecutionOperation.SUBMIT_ORDER,
                ExecutionOperation.CANCEL_ORDER,
                ExecutionOperation.READ_ORDERS,
            }
            else None,
            heartbeat_id="heartbeat-1" if operation is ExecutionOperation.HEARTBEAT else None,
        )

    handler_name = {
        ExecutionOperation.SUBMIT_ORDER: "submit_order",
        ExecutionOperation.CANCEL_ORDER: "cancel_order",
        ExecutionOperation.HEARTBEAT: "heartbeat",
        ExecutionOperation.READ_ORDERS: "read_orders",
        ExecutionOperation.READ_TRADES: "read_trades",
        ExecutionOperation.READ_ACCOUNT: "read_account",
    }[operation]
    service = _service(handlers=_handlers(**{handler_name: handler}))
    if operation is ExecutionOperation.CANCEL_ORDER:
        submitted = service.handle(_request(ExecutionOperation.SUBMIT_ORDER, request_id=uuid4()))
        assert submitted.ok is True

    response = service.handle(_request(operation, request_id=uuid4()))

    assert response.ok is True
    assert len(captured_arguments) == 1
    assert captured_arguments[0] == (captured_arguments[0][0],)
    assert captured_arguments[0][0].operation is operation  # type: ignore[attr-defined]
    service.close()


def test_unknown_operation_is_rejected_before_other_request_validation() -> None:
    service = _service()

    response = service.handle_raw(
        b'{"schema_version":1,"operation":"WITHDRAW","request_id":"not-a-uuid"}'
    )

    assert response.error_code == "IPC_OPERATION_NOT_ALLOWED"
    service.close()


def test_non_string_operation_is_rejected_without_leaking_a_parser_exception() -> None:
    service = _service()

    response = service.handle_raw(b'{"schema_version":1,"operation":[]}')

    assert response.error_code == "IPC_OPERATION_NOT_ALLOWED"
    service.close()


def test_unvalidated_model_copy_cannot_route_a_mutation_through_the_read_guard() -> None:
    authority_calls: list[UUID] = []
    read_calls: list[UUID] = []
    handler_calls = 0

    def cancel(payload: CancelOrderPayload) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload
        handler_calls += 1
        raise AssertionError("FORGED_REQUEST_DISPATCHED")

    service = _service(
        authority_calls=authority_calls,
        read_calls=read_calls,
        handlers=_handlers(cancel_order=cancel),
    )
    forged = _request(
        ExecutionOperation.CANCEL_ORDER,
        request_id=uuid4(),
    )
    object.__setattr__(forged, "operation", ExecutionOperation.READ_ACCOUNT)

    response = service.handle(forged)

    assert response.error_code == "IPC_REQUEST_INVALID"
    assert authority_calls == []
    assert read_calls == []
    assert handler_calls == 0
    service.close()


@pytest.mark.parametrize(
    "raw",
    (
        b"",
        b"{} trailing",
        b'{"operation":"SIGN_ORDER","operation":"READ_ACCOUNT"}',
        b"[" * 40 + b"]" * 40,
    ),
    ids=("empty", "trailing", "duplicate-key", "too-deep"),
)
def test_malformed_or_resource_hostile_json_has_one_sanitized_response(raw: bytes) -> None:
    service = _service()

    response = service.handle_raw(raw)

    assert response == SignerResponse.rejected(None, "IPC_REQUEST_INVALID")
    service.close()


def _legal_size_huge_json_integer() -> bytes:
    return b'{"schema_version":' + b"9" * 5_000 + b',"operation":"READ_ACCOUNT"}'


def test_parser_translates_huge_json_integer_conversion_failure() -> None:
    error = _captured_protocol_error(lambda: parse_signer_request(_legal_size_huge_json_integer()))

    assert str(error) == "IPC_REQUEST_INVALID"
    assert error.__cause__ is None
    assert error.__context__ is None


def test_handle_raw_translates_huge_json_integer_conversion_failure() -> None:
    service = _service()

    response = service.handle_raw(_legal_size_huge_json_integer())

    assert response == SignerResponse.rejected(None, "IPC_REQUEST_INVALID")
    service.close()


def test_spawned_sidecar_translates_huge_json_integer_and_zeroizes() -> None:
    process, request_write, response_read, audit_read = _spawn_signer()
    write_frame(_FdStream(request_write), _legal_size_huge_json_integer())  # type: ignore[arg-type]

    response_raw = read_frame(_FdStream(response_read))  # type: ignore[arg-type]
    response = SignerResponse.model_validate_json(response_raw, strict=True)
    exitcode, audit = _join_spawned(process, request_write, response_read, audit_read)

    assert response == SignerResponse.rejected(None, "IPC_REQUEST_INVALID")
    assert exitcode == 0
    assert audit == b"1"


def test_handler_and_authority_canaries_never_cross_response_or_log_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "signer-internal-canary"

    def fail(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        del payload
        raise ValueError(canary)

    service = _service(handlers=_handlers(submit_order=fail))
    response = service.handle(_request(ExecutionOperation.SUBMIT_ORDER))

    assert response.error_code == "HANDLER_FAILED"
    assert canary not in response.model_dump_json()
    assert canary not in caplog.text
    service.close()


@pytest.mark.parametrize(
    ("error", "error_code"),
    (
        (OrderSigningError("ORDER_ERROR_CANARY"), "ORDER_SIGNING_FAILED"),
        (ClobAuthError("AUTH_ERROR_CANARY"), "AUTH_HANDLER_FAILED"),
        (RuntimeError("HANDLER_ERROR_CANARY"), "HANDLER_FAILED"),
    ),
)
def test_handler_exception_classes_map_to_constant_codes_without_reflection(
    error: Exception,
    error_code: str,
) -> None:
    def fail(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        del payload
        raise error

    service = _service(handlers=_handlers(submit_order=fail))

    response = service.handle(_request(ExecutionOperation.SUBMIT_ORDER, request_id=uuid4()))

    assert response.error_code == error_code
    _assert_canaries_absent(
        response.model_dump_json(),
        "ORDER_ERROR_CANARY",
        "AUTH_ERROR_CANARY",
        "HANDLER_ERROR_CANARY",
    )
    service.close()


@pytest.mark.parametrize("secret_name", ("private_key", "api_key", "api_secret", "passphrase"))
def test_owned_secret_in_valid_handler_result_is_sanitized_before_binding(
    secret_name: str,
) -> None:
    configured = {
        "private_key": b"1" * 32,
        "api_key": API_KEY,
        "api_secret": API_SECRET,
        "passphrase": PASSPHRASE,
    }
    private_key = configured["private_key"] if secret_name == "private_key" else PRIVATE_KEY
    maker = Account.from_key(private_key).address
    account_fingerprint = sha256(bytes.fromhex(maker[2:])).hexdigest()
    grant = _capability_grant(account_fingerprint=account_fingerprint)
    proof = ISSUER.proof(grant)
    intent = _intent(
        account_fingerprint=account_fingerprint,
        capability_fingerprint=grant.digest,
    )
    envelope = sign_order(intent, private_key, load_protocol_snapshot())
    canary = configured[secret_name]

    def leak(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        del payload
        return SanitizedOperationResult(
            operation=ExecutionOperation.SUBMIT_ORDER,
            result_code="SUBMIT_ORDER_OK",
            evidence_hashes=(),
            venue_order_id=canary.decode(),
        )

    secret_overrides = {
        secret_name: configured[secret_name],
        "private_key": private_key,
    }
    service = _service(
        handlers=_handlers(submit_order=leak),
        **secret_overrides,  # type: ignore[arg-type]
    )
    submitted = service.handle(
        _request(
            ExecutionOperation.SUBMIT_ORDER,
            request_id=uuid4(),
            intent_id=intent.intent_id,
            intent_fingerprint=intent.intent_fingerprint,
            capability_digest=grant.digest,
            plan_digest=grant.plan_hash,
            authority_proof=proof,
            account_fingerprint=account_fingerprint,
            payload=SubmitOrderPayload(
                operation=ExecutionOperation.SUBMIT_ORDER,
                intent=intent,
                envelope=envelope,
            ),
        )
    )
    cancelled = service.handle(
        _request(
            ExecutionOperation.CANCEL_ORDER,
            request_id=uuid4(),
            intent_id=intent.intent_id,
            intent_fingerprint=intent.intent_fingerprint,
            capability_digest=grant.digest,
            plan_digest=grant.plan_hash,
            authority_proof=proof,
            account_fingerprint=account_fingerprint,
            payload=CancelOrderPayload(
                operation=ExecutionOperation.CANCEL_ORDER,
                venue_order_id=canary.decode(),
            ),
        )
    )

    assert submitted.error_code == "SECRET_OUTPUT_DETECTED"
    _assert_canaries_absent(submitted.model_dump_json(), canary)
    assert cancelled.error_code == "CANCEL_ORDER_UNKNOWN"
    service.close()


@pytest.mark.parametrize(
    ("secret_name", "escape_character"),
    (
        ("api_key", '"'),
        ("api_key", "\\"),
        ("passphrase", '"'),
        ("passphrase", "\\"),
    ),
    ids=("api-key-quote", "api-key-backslash", "passphrase-quote", "passphrase-backslash"),
)
def test_json_escaped_owned_secret_in_submit_result_is_rejected_before_binding(
    secret_name: str,
    escape_character: str,
) -> None:
    canary = f"task-7-{secret_name}-{escape_character}-canary".encode()
    handler_calls = 0
    cancel_calls = 0

    def leak(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload
        handler_calls += 1
        return SanitizedOperationResult(
            operation=ExecutionOperation.SUBMIT_ORDER,
            result_code="SUBMIT_ORDER_OK",
            evidence_hashes=(),
            venue_order_id=canary.decode(),
        )

    def cancel(payload: CancelOrderPayload) -> SanitizedOperationResult:
        nonlocal cancel_calls
        cancel_calls += 1
        return SanitizedOperationResult(
            operation=ExecutionOperation.CANCEL_ORDER,
            result_code="CANCEL_ORDER_OK",
            evidence_hashes=(),
            venue_order_id=payload.venue_order_id,
        )

    service = _service(
        handlers=_handlers(submit_order=leak, cancel_order=cancel),
        **{secret_name: canary},  # type: ignore[arg-type]
    )
    request = _request(ExecutionOperation.SUBMIT_ORDER, request_id=uuid4())

    first = service._handle_bytes(request)
    retry = service._handle_bytes(request)
    submitted = SignerResponse.model_validate_json(first, strict=True)
    cancelled = service.handle(
        _request(
            ExecutionOperation.CANCEL_ORDER,
            request_id=uuid4(),
            payload=CancelOrderPayload(
                operation=ExecutionOperation.CANCEL_ORDER,
                venue_order_id=canary.decode(),
            ),
        )
    )

    assert submitted.error_code == "SECRET_OUTPUT_DETECTED"
    assert submitted.result is None
    assert first == retry
    assert handler_calls == 1
    assert cancelled.error_code == "CANCEL_ORDER_UNKNOWN"
    assert cancel_calls == 0
    _assert_canaries_absent(first + retry + cancelled.model_dump_json().encode(), canary)
    service.close()


def test_authority_factory_failure_is_sanitized_without_dispatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "authority-factory-canary"
    handler_calls = 0

    def fail_authority(request: SignerRequest, observed_at: datetime) -> object:
        del request, observed_at
        raise ValueError(canary)

    def cancel(payload: CancelOrderPayload) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload
        handler_calls += 1
        raise AssertionError("HANDLER_MUST_NOT_RUN")

    service = SignerService(
        secrets=SecretMaterial(
            bytearray(PRIVATE_KEY),
            bytearray(API_KEY),
            bytearray(API_SECRET),
            bytearray(PASSPHRASE),
        ),
        authority_context_factory=fail_authority,  # type: ignore[arg-type]
        read_guard=lambda request, observed_at: AuthorityDecision(True, None, ()),
        handlers=_handlers(cancel_order=cancel),
        clock=lambda: NOW,
        capability_public_key=ISSUER.public_verification_key,
    )

    response = service.handle(_request(ExecutionOperation.CANCEL_ORDER))

    assert response.error_code == "AUTHORITY_GATE_FAILED"
    assert handler_calls == 0
    assert canary not in response.model_dump_json()
    assert canary not in caplog.text
    service.close()


def test_allowed_authority_decision_cannot_bypass_fresh_context_verification() -> None:
    handler_calls = 0

    def submit(payload: SubmitOrderPayload) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload
        handler_calls += 1
        raise AssertionError("ALLOWED_DECISION_MUST_NOT_DISPATCH")

    service = SignerService(
        secrets=SecretMaterial(
            bytearray(PRIVATE_KEY),
            bytearray(API_KEY),
            bytearray(API_SECRET),
            bytearray(PASSPHRASE),
        ),
        authority_context_factory=lambda request, observed_at: AuthorityDecision(True, None, ()),
        read_guard=lambda request, observed_at: AuthorityDecision(True, None, ()),
        handlers=_handlers(submit_order=submit),
        clock=lambda: NOW,
        capability_public_key=ISSUER.public_verification_key,
    )

    response = service.handle(_action_request(ExecutionOperation.SUBMIT_ORDER))

    assert response.error_code == "AUTHORITY_GATE_FAILED"
    assert handler_calls == 0
    service.close()


def test_malformed_authority_context_is_sanitized_without_dispatch() -> None:
    service = SignerService(
        secrets=SecretMaterial(
            bytearray(PRIVATE_KEY),
            bytearray(API_KEY),
            bytearray(API_SECRET),
            bytearray(PASSPHRASE),
        ),
        authority_context_factory=lambda request, observed_at: object(),  # type: ignore[arg-type]
        read_guard=lambda request, observed_at: AuthorityDecision(True, None, ()),
        handlers=_handlers(),
        clock=lambda: NOW,
        capability_public_key=ISSUER.public_verification_key,
    )

    response = service.handle(_request())

    assert response.error_code == "AUTHORITY_GATE_FAILED"
    service.close()


def test_read_guard_failure_uses_a_constant_code_without_dispatch() -> None:
    canary = "read-guard-canary"
    handler_calls = 0

    def fail_read_guard(request: SignerRequest, observed_at: datetime) -> AuthorityDecision:
        del request, observed_at
        raise ValueError(canary)

    def read_account(payload: ReadAccountPayload) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload
        handler_calls += 1
        raise AssertionError("READ_HANDLER_MUST_NOT_RUN")

    service = SignerService(
        secrets=SecretMaterial(
            bytearray(PRIVATE_KEY),
            bytearray(API_KEY),
            bytearray(API_SECRET),
            bytearray(PASSPHRASE),
        ),
        authority_context_factory=lambda request, observed_at: authority_context(),
        read_guard=fail_read_guard,
        handlers=_handlers(read_account=read_account),
        clock=lambda: NOW,
        capability_public_key=ISSUER.public_verification_key,
    )

    response = service.handle(_request(ExecutionOperation.READ_ACCOUNT, request_id=uuid4()))

    assert response.error_code == "READ_GUARD_FAILED"
    assert handler_calls == 0
    _assert_canaries_absent(response.model_dump_json(), canary)
    service.close()


def test_service_close_zeroizes_owned_secret_buffers_and_refuses_uncached_work() -> None:
    buffers = (
        bytearray(PRIVATE_KEY),
        bytearray(API_KEY),
        bytearray(API_SECRET),
        bytearray(PASSPHRASE),
    )
    material = SecretMaterial(*buffers)
    service = SignerService(
        secrets=material,
        authority_context_factory=lambda request, observed_at: authority_context(),
        read_guard=lambda request, observed_at: AuthorityDecision(True, None, ()),
        handlers=_handlers(),
        clock=lambda: NOW,
        capability_public_key=ISSUER.public_verification_key,
    )

    service.close()
    response = service.handle(_request(request_id=uuid4()))

    assert response.error_code == "IPC_SIGNER_CLOSED"
    assert all(not any(value) for value in buffers)


def test_service_close_closes_handler_owner_before_zeroizing_secrets() -> None:
    events: list[str] = []
    buffers = (
        bytearray(PRIVATE_KEY),
        bytearray(API_KEY),
        bytearray(API_SECRET),
        bytearray(PASSPHRASE),
    )

    def close_handlers() -> None:
        assert any(buffers[1])
        events.append("handlers")

    base = _handlers()
    handlers = SignerOperationHandlers(
        submit_order=base.submit_order,
        cancel_order=base.cancel_order,
        heartbeat=base.heartbeat,
        read_orders=base.read_orders,
        read_trades=base.read_trades,
        read_account=base.read_account,
        close=close_handlers,
    )
    service = SignerService(
        secrets=SecretMaterial(*buffers),
        authority_context_factory=lambda request, observed_at: authority_context(),
        read_guard=lambda request, observed_at: AuthorityDecision(True, None, ()),
        handlers=handlers,
        clock=lambda: NOW,
        capability_public_key=ISSUER.public_verification_key,
    )

    service.close()
    service.close()

    assert events == ["handlers"]
    assert all(not any(buffer) for buffer in buffers)


def test_service_close_contains_handler_close_failure_and_zeroizes_secrets() -> None:
    buffers = (
        bytearray(PRIVATE_KEY),
        bytearray(API_KEY),
        bytearray(API_SECRET),
        bytearray(PASSPHRASE),
    )

    def fail_close() -> None:
        raise RuntimeError("private handler close failure")

    base = _handlers()
    handlers = SignerOperationHandlers(
        submit_order=base.submit_order,
        cancel_order=base.cancel_order,
        heartbeat=base.heartbeat,
        read_orders=base.read_orders,
        read_trades=base.read_trades,
        read_account=base.read_account,
        close=fail_close,
    )
    service = SignerService(
        secrets=SecretMaterial(*buffers),
        authority_context_factory=lambda request, observed_at: authority_context(),
        read_guard=lambda request, observed_at: AuthorityDecision(True, None, ()),
        handlers=handlers,
        clock=lambda: NOW,
        capability_public_key=ISSUER.public_verification_key,
    )

    service.close()

    assert all(not any(buffer) for buffer in buffers)


def test_spawned_sidecar_returns_byte_identical_replay_and_rejects_collision() -> None:
    process, request_write, response_read, audit_read = _spawn_signer(max_requests=3)
    request_stream = _FdStream(request_write)
    response_stream = _FdStream(response_read)
    request = _request()
    changed = request.model_copy(update={"deadline": request.deadline + timedelta(seconds=1)})

    write_frame(request_stream, canonical_request_bytes(request))  # type: ignore[arg-type]
    first = read_frame(response_stream)  # type: ignore[arg-type]
    write_frame(request_stream, canonical_request_bytes(request))  # type: ignore[arg-type]
    retry = read_frame(response_stream)  # type: ignore[arg-type]
    write_frame(request_stream, canonical_request_bytes(changed))  # type: ignore[arg-type]
    collision_raw = read_frame(response_stream)  # type: ignore[arg-type]
    collision = SignerResponse.model_validate_json(collision_raw, strict=True)

    exitcode, audit = _join_spawned(
        process,
        request_write,
        response_read,
        audit_read,
    )
    assert first == retry
    assert collision.error_code == "IPC_REQUEST_COLLISION"
    assert exitcode == 0
    assert audit == b"1"
    rendered = first + retry + collision_raw
    for canary in (API_KEY, API_SECRET, PASSPHRASE):
        assert canary not in rendered


def test_spawned_sidecar_bounds_oversized_public_result_and_continues() -> None:
    process, request_write, response_read, audit_read = _spawn_signer(
        oversized_balance=True,
        max_requests=2,
    )
    request_stream = _FdStream(request_write)
    response_stream = _FdStream(response_read)

    write_frame(
        request_stream,
        canonical_request_bytes(_request(ExecutionOperation.READ_ACCOUNT, request_id=uuid4())),
    )  # type: ignore[arg-type]
    oversized_raw = read_frame(response_stream)  # type: ignore[arg-type]
    oversized = SignerResponse.model_validate_json(oversized_raw, strict=True)
    write_frame(
        request_stream,
        canonical_request_bytes(_request(ExecutionOperation.READ_TRADES, request_id=uuid4())),
    )  # type: ignore[arg-type]
    continued_raw = read_frame(response_stream)  # type: ignore[arg-type]
    continued = SignerResponse.model_validate_json(continued_raw, strict=True)

    exitcode, audit = _join_spawned(process, request_write, response_read, audit_read)

    assert oversized.error_code == "IPC_OPERATION_RESULT_INVALID"
    assert len(oversized_raw) <= MAX_FRAME_BYTES
    assert continued.ok is True
    assert exitcode == 0
    assert audit == b"1"


@pytest.mark.parametrize(
    ("wire", "error_code"),
    (
        (
            b'{"schema_version":1,"operation":"WITHDRAW","request_id":"x"}',
            "IPC_OPERATION_NOT_ALLOWED",
        ),
        (b"{} trailing", "IPC_REQUEST_INVALID"),
    ),
    ids=("prohibited-operation", "malformed-json"),
)
def test_spawned_sidecar_sanitizes_invalid_request_payloads(
    wire: bytes,
    error_code: str,
) -> None:
    process, request_write, response_read, audit_read = _spawn_signer()
    write_frame(_FdStream(request_write), wire)  # type: ignore[arg-type]

    response_raw = read_frame(_FdStream(response_read))  # type: ignore[arg-type]
    response = SignerResponse.model_validate_json(response_raw, strict=True)
    exitcode, audit = _join_spawned(process, request_write, response_read, audit_read)

    assert response.error_code == error_code
    assert exitcode == 0
    assert audit == b"1"


@pytest.mark.parametrize(
    ("wire", "error_code"),
    (
        ((MAX_FRAME_BYTES + 1).to_bytes(4, "big"), "IPC_FRAME_SIZE_INVALID"),
        (b"\x00\x00\x00\x08short", "IPC_FRAME_TRUNCATED"),
    ),
    ids=("oversized", "truncated"),
)
def test_spawned_sidecar_rejects_invalid_frames_and_zeroizes(
    wire: bytes,
    error_code: str,
) -> None:
    process, request_write, response_read, audit_read = _spawn_signer()
    assert os.write(request_write, wire) == len(wire)
    os.close(request_write)

    response_raw = read_frame(_FdStream(response_read))  # type: ignore[arg-type]
    response = SignerResponse.model_validate_json(response_raw, strict=True)
    exitcode, audit = _join_spawned(process, request_write, response_read, audit_read)

    assert response.error_code == error_code
    assert exitcode == 0
    assert audit == b"1"


def test_spawned_sidecar_deadline_is_deterministic_and_context_free() -> None:
    process, request_write, response_read, audit_read = _spawn_signer()
    expired = _request(deadline=NOW)
    write_frame(_FdStream(request_write), canonical_request_bytes(expired))  # type: ignore[arg-type]

    response_raw = read_frame(_FdStream(response_read))  # type: ignore[arg-type]
    response = SignerResponse.model_validate_json(response_raw, strict=True)
    exitcode, audit = _join_spawned(process, request_write, response_read, audit_read)

    assert response.error_code == "IPC_DEADLINE_EXPIRED"
    assert exitcode == 0
    assert audit == b"1"


def test_spawned_sidecar_zeroizes_after_handler_crash() -> None:
    process, request_write, response_read, audit_read = _spawn_signer(
        crash_operation=ExecutionOperation.READ_ACCOUNT
    )
    request = _request(ExecutionOperation.READ_ACCOUNT)
    write_frame(_FdStream(request_write), canonical_request_bytes(request))  # type: ignore[arg-type]
    os.close(request_write)
    process.join(timeout=5)
    response = os.read(response_read, 1)
    os.close(response_read)
    audit = os.read(audit_read, 1)
    os.close(audit_read)

    assert process.exitcode not in (None, 0)
    assert response == b""
    assert audit == b"1"


def test_spawned_sidecar_treats_parent_pipe_close_as_a_bounded_exit() -> None:
    process, request_write, response_read, audit_read = _spawn_signer()
    os.close(request_write)

    response_raw = read_frame(_FdStream(response_read))  # type: ignore[arg-type]
    response = SignerResponse.model_validate_json(response_raw, strict=True)
    exitcode, audit = _join_spawned(process, request_write, response_read, audit_read)

    assert response.error_code == "IPC_FRAME_TRUNCATED"
    assert exitcode == 0
    assert audit == b"1"


def test_spawned_sidecar_lifetime_is_bounded_while_parent_is_silent() -> None:
    process, request_write, response_read, audit_read = _spawn_signer(max_lifetime_seconds=0.1)

    exitcode, audit = _join_spawned(process, request_write, response_read, audit_read)

    assert exitcode == 0
    assert audit == b"1"


def test_sidecar_does_not_close_descriptor_numbers_reused_after_secret_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_read, request_write = os.pipe()
    response_read, response_write = os.pipe()
    secret_fds = tuple(_secret_descriptor(b"unused") for _ in range(4))
    reused: list[int] = []

    def load_and_reuse(*descriptors: int) -> SecretMaterial:
        for descriptor in descriptors:
            os.close(descriptor)
        for _ in range(2):
            pair = os.pipe()
            reused.extend(pair)
        return SecretMaterial(
            bytearray(PRIVATE_KEY),
            bytearray(API_KEY),
            bytearray(API_SECRET),
            bytearray(PASSPHRASE),
        )

    monkeypatch.setattr(signer_module, "read_secret_descriptors", load_and_reuse)
    write_frame(_FdStream(request_write), b"{} trailing")  # type: ignore[arg-type]
    os.close(request_write)

    run_signer_sidecar(
        request_fd=request_read,
        response_fd=response_write,
        secret_descriptors=secret_fds,
        service_factory=lambda secrets: _child_service_factory(
            secrets,
            crash_operation=None,
        ),
        max_requests=1,
        max_lifetime_seconds=1,
    )

    response = SignerResponse.model_validate_json(
        read_frame(_FdStream(response_read)),  # type: ignore[arg-type]
        strict=True,
    )
    assert response.error_code == "IPC_REQUEST_INVALID"
    for descriptor in reused:
        os.fstat(descriptor)
        os.close(descriptor)
    os.close(response_read)


def test_sidecar_zeroizes_loaded_secrets_when_factory_returns_wrong_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_read, request_write = os.pipe()
    response_read, response_write = os.pipe()
    secret_fds = tuple(_secret_descriptor(b"unused") for _ in range(4))
    buffers = (
        bytearray(PRIVATE_KEY),
        bytearray(API_KEY),
        bytearray(API_SECRET),
        bytearray(PASSPHRASE),
    )
    material = SecretMaterial(*buffers)

    def load(*descriptors: int) -> SecretMaterial:
        for descriptor in descriptors:
            os.close(descriptor)
        return material

    monkeypatch.setattr(signer_module, "read_secret_descriptors", load)
    os.close(request_write)

    run_signer_sidecar(
        request_fd=request_read,
        response_fd=response_write,
        secret_descriptors=secret_fds,
        service_factory=lambda secrets: object(),  # type: ignore[arg-type,return-value]
        max_requests=1,
        max_lifetime_seconds=1,
    )

    response = SignerResponse.model_validate_json(
        read_frame(_FdStream(response_read)),  # type: ignore[arg-type]
        strict=True,
    )
    assert response.error_code == "IPC_SERVICE_INITIALIZATION_FAILED"
    assert all(not any(value) for value in buffers)
    os.close(response_read)
