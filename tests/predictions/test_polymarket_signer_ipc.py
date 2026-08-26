from __future__ import annotations

import io
import multiprocessing
import os
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from multiprocessing import reduction
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from polytrading.predictions.execution.authority import AuthorityDecision
from polytrading.predictions.execution.models import ExecutionIntent, ExecutionOperation
from polytrading.predictions.polymarket_execution import signer as signer_module
from polytrading.predictions.polymarket_execution.ipc import (
    MAX_FRAME_BYTES,
    CancelOrderPayload,
    HeartbeatPayload,
    ReadAccountPayload,
    ReadOrdersPayload,
    ReadTradesPayload,
    SanitizedOperationResult,
    SignedEnvelopeResult,
    SignerProtocolError,
    SignerRequest,
    SignerResponse,
    SignOrderPayload,
    SubmitOrderPayload,
    canonical_request_bytes,
    read_frame,
    write_frame,
)
from polytrading.predictions.polymarket_execution.secrets import SecretMaterial
from polytrading.predictions.polymarket_execution.signer import (
    SignerOperationHandlers,
    SignerService,
    run_signer_sidecar,
)
from tests.predictions.execution_helpers import execution_intent_fields
from tests.predictions.test_execution_authority import (
    MANIFEST_HASH,
    authority_context,
    verified_capability,
)
from tests.predictions.test_polymarket_auth import L2_SIGNATURE
from tests.predictions.test_polymarket_order_signing import (
    ACCOUNT_FINGERPRINT,
    PRIVATE_KEY,
)

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)
CAPABILITY_DIGEST = "9" * 64
API_KEY = b"task-7-api-key"
API_SECRET = b"dGFzay03LXNlY3JldA=="
PASSPHRASE = b"task-7-passphrase"
_CHILD_SECRET_MATERIAL: SecretMaterial | None = None


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
    if operation is ExecutionOperation.SIGN_ORDER:
        return SignOrderPayload(operation=operation, intent=_intent())
    if operation is ExecutionOperation.SUBMIT_ORDER:
        service = _service()
        signed = service.handle(_request(ExecutionOperation.SIGN_ORDER))
        assert isinstance(signed.result, SignedEnvelopeResult)
        return SubmitOrderPayload(operation=operation, envelope=signed.result.envelope)
    if operation is ExecutionOperation.CANCEL_ORDER:
        return CancelOrderPayload(operation=operation, venue_order_id="order-1")
    if operation is ExecutionOperation.HEARTBEAT:
        return HeartbeatPayload(operation=operation, heartbeat_id="")
    if operation is ExecutionOperation.READ_ORDERS:
        return ReadOrdersPayload(operation=operation, venue_order_id=None)
    if operation is ExecutionOperation.READ_TRADES:
        return ReadTradesPayload(operation=operation)
    if operation is ExecutionOperation.READ_ACCOUNT:
        return ReadAccountPayload(operation=operation)
    raise AssertionError("UNKNOWN_TEST_OPERATION") from None


def _request(
    operation: ExecutionOperation = ExecutionOperation.SIGN_ORDER,
    **overrides: object,
) -> SignerRequest:
    intent = _intent()
    fields: dict[str, object] = {
        "schema_version": 1,
        "request_id": UUID("11111111-1111-4111-8111-111111111111"),
        "intent_id": intent.intent_id,
        "intent_fingerprint": intent.intent_fingerprint,
        "capability_digest": CAPABILITY_DIGEST,
        "manifest_digest": MANIFEST_HASH,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "protocol_version": "polymarket-clob-2026-08-25-v1",
        "operation": operation,
        "deadline": NOW + timedelta(seconds=5),
        "payload": _payload(operation),
    }
    fields.update(overrides)
    return SignerRequest.model_validate(fields)


def _handlers(**overrides: object) -> SignerOperationHandlers:
    def result(payload: object, authenticate: object) -> SanitizedOperationResult:
        del authenticate
        operation = payload.operation  # type: ignore[attr-defined]
        return SanitizedOperationResult(
            operation=operation,
            result_code="FIXTURE_OK",
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


def _service(
    *,
    authority_calls: list[UUID] | None = None,
    read_calls: list[UUID] | None = None,
    handlers: SignerOperationHandlers | None = None,
    max_cache_entries: int = 64,
    now: datetime = NOW,
) -> SignerService:
    def context_factory(request: SignerRequest, observed_at: datetime) -> object:
        if authority_calls is not None:
            authority_calls.append(request.request_id)
        capability = verified_capability(
            account_fingerprint=request.account_fingerprint,
            capability_digest=request.capability_digest,
        )
        return authority_context(
            now=observed_at,
            account_fingerprint=request.account_fingerprint,
            account_scope_account_fingerprint=request.account_fingerprint,
            manifest_record_hash=request.manifest_digest,
            verified_capability=capability,
        )

    def read_guard(request: SignerRequest, observed_at: datetime) -> AuthorityDecision:
        del observed_at
        if read_calls is not None:
            read_calls.append(request.request_id)
        return AuthorityDecision(True, None, ())

    return SignerService(
        secrets=SecretMaterial(
            bytearray(PRIVATE_KEY),
            bytearray(API_KEY),
            bytearray(API_SECRET),
            bytearray(PASSPHRASE),
        ),
        authority_context_factory=context_factory,  # type: ignore[arg-type]
        read_guard=read_guard,
        handlers=handlers or _handlers(),
        clock=lambda: now,
        max_cache_entries=max_cache_entries,
    )


def _child_service_factory(
    secrets: SecretMaterial,
    *,
    crash_operation: ExecutionOperation | None,
) -> SignerService:
    global _CHILD_SECRET_MATERIAL
    _CHILD_SECRET_MATERIAL = secrets

    def context_factory(request: SignerRequest, observed_at: datetime) -> object:
        capability = verified_capability(
            account_fingerprint=request.account_fingerprint,
            capability_digest=request.capability_digest,
        )
        return authority_context(
            now=observed_at,
            account_fingerprint=request.account_fingerprint,
            account_scope_account_fingerprint=request.account_fingerprint,
            manifest_record_hash=request.manifest_digest,
            verified_capability=capability,
        )

    def result(payload: object, authenticate: object) -> SanitizedOperationResult:
        del authenticate
        operation = payload.operation  # type: ignore[attr-defined]
        if operation is crash_operation:
            raise SystemExit("SANITIZED_CHILD_CRASH")
        return SanitizedOperationResult(
            operation=operation,
            result_code="CHILD_FIXTURE_OK",
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
        max_cache_entries=8,
    )


def _spawned_signer_target(
    request_handle: object,
    response_handle: object,
    secret_handles: tuple[object, object, object, object],
    audit_handle: object,
    crash_operation: ExecutionOperation | None,
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
    with pytest.raises(ValidationError) as rejected:
        SignerRequest.model_validate(raw)
    assert "private-key-canary" not in str(rejected.value)
    assert "private-key-canary" not in repr(rejected.value)
    assert "private-key-canary" not in str(rejected.value.errors())
    assert "private-key-canary" not in rejected.value.json()


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
    with pytest.raises(ValidationError, match="operation must match payload"):
        _request(operation, payload=_payload(different))


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


def test_same_request_id_exact_retry_is_cached_but_changed_request_is_collision() -> None:
    authority_calls: list[UUID] = []
    service = _service(authority_calls=authority_calls)
    request = _request()

    first = service.handle(request)
    retry = service.handle(request)
    changed = request.model_copy(update={"intent_fingerprint": "f" * 64})
    collision = service.handle(changed)

    assert first.model_dump_json() == retry.model_dump_json()
    assert authority_calls == [request.request_id]
    assert collision.error_code == "IPC_REQUEST_COLLISION"
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


def test_heartbeat_handler_uses_real_l2_signer_without_headers_crossing_ipc() -> None:
    captured_signatures: list[str] = []

    def heartbeat(payload: HeartbeatPayload, authenticate: object) -> SanitizedOperationResult:
        headers = authenticate(  # type: ignore[operator]
            timestamp="1787673600",
            method="POST",
            route="/v1/heartbeats",
            body=b'{"heartbeat_id":""}',
        )
        captured_signatures.append(headers.signature)
        return SanitizedOperationResult(
            operation=payload.operation,
            result_code="HEARTBEAT_OK",
            evidence_hashes=(),
            heartbeat_id="heartbeat-1",
        )

    service = _service(handlers=_handlers(heartbeat=heartbeat))

    response = service.handle(_request(ExecutionOperation.HEARTBEAT))

    assert response.ok is True
    assert captured_signatures and captured_signatures != [L2_SIGNATURE]
    rendered = response.model_dump_json()
    for canary in (API_KEY, API_SECRET, PASSPHRASE):
        assert canary.decode() not in rendered
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

    def cancel(payload: CancelOrderPayload, authenticate: object) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload, authenticate
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
    ).model_copy(update={"operation": ExecutionOperation.READ_ACCOUNT})

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


def test_handler_and_authority_canaries_never_cross_response_or_log_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "signer-internal-canary"

    def fail(payload: SubmitOrderPayload, authenticate: object) -> SanitizedOperationResult:
        del payload, authenticate
        raise ValueError(canary)

    service = _service(handlers=_handlers(submit_order=fail))
    response = service.handle(_request(ExecutionOperation.SUBMIT_ORDER))

    assert response.error_code == "IPC_OPERATION_FAILED"
    assert canary not in response.model_dump_json()
    assert canary not in caplog.text
    service.close()


def test_authority_factory_failure_is_sanitized_without_dispatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "authority-factory-canary"
    handler_calls = 0

    def fail_authority(request: SignerRequest, observed_at: datetime) -> object:
        del request, observed_at
        raise ValueError(canary)

    def cancel(payload: CancelOrderPayload, authenticate: object) -> SanitizedOperationResult:
        nonlocal handler_calls
        del payload, authenticate
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
    )

    response = service.handle(_request(ExecutionOperation.CANCEL_ORDER))

    assert response.error_code == "CAPABILITY_MISSING"
    assert handler_calls == 0
    assert canary not in response.model_dump_json()
    assert canary not in caplog.text
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
    )

    response = service.handle(_request())

    assert response.error_code == "CAPABILITY_MISSING"
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
    )

    service.close()
    response = service.handle(_request(request_id=uuid4()))

    assert response.error_code == "IPC_SIGNER_CLOSED"
    assert all(not any(value) for value in buffers)


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
        crash_operation=ExecutionOperation.CANCEL_ORDER
    )
    request = _request(ExecutionOperation.CANCEL_ORDER)
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
