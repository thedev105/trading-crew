"""Bounded, length-prefixed local signer messages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, BinaryIO, Final, Literal, Self
from uuid import UUID

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.config import ExtraValues

from polytrading.predictions.domain import PredictionRecord, Sha256, normalize_utc_timestamp
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ExecutionOperation,
    SignedOrderEnvelope,
)
from polytrading.predictions.polymarket_execution.protocol import POLYMARKET_PROTOCOL_VERSION

MAX_FRAME_BYTES: Final = 1_048_576
_FRAME_HEADER_BYTES: Final = 4
_MAX_JSON_DEPTH: Final = 32
_StableCode = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
_PublicIdentifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$"),
]
_HeartbeatIdentifier = Annotated[
    str,
    StringConstraints(max_length=256, pattern=r"^[\x20-\x7e]*$"),
]


class _SignerRecord(PredictionRecord):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    @classmethod
    def model_validate(
        cls,
        obj: object,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        from_attributes: bool | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Redact forbidden direct-call inputs before Pydantic captures them."""
        sanitized = cls._redact_forbidden_input(obj)
        return super().model_validate(
            sanitized,
            strict=strict,
            extra=extra,
            from_attributes=from_attributes,
            context=context,
            by_alias=by_alias,
            by_name=by_name,
        )

    @classmethod
    def _redact_forbidden_input(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        if all(key in cls.model_fields for key in value):
            return value
        return {
            key: item if key in cls.model_fields else "<redacted>" for key, item in value.items()
        }


class SignOrderPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.SIGN_ORDER]
    intent: ExecutionIntent


class SubmitOrderPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.SUBMIT_ORDER]
    envelope: SignedOrderEnvelope


class CancelOrderPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.CANCEL_ORDER]
    venue_order_id: _PublicIdentifier


class HeartbeatPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.HEARTBEAT]
    heartbeat_id: _HeartbeatIdentifier


class ReadOrdersPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.READ_ORDERS]
    venue_order_id: _PublicIdentifier | None


class ReadTradesPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.READ_TRADES]


class ReadAccountPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.READ_ACCOUNT]


SignerPayload = Annotated[
    SignOrderPayload
    | SubmitOrderPayload
    | CancelOrderPayload
    | HeartbeatPayload
    | ReadOrdersPayload
    | ReadTradesPayload
    | ReadAccountPayload,
    Field(discriminator="operation"),
]


class SignerRequest(_SignerRecord):
    schema_version: Literal[1]
    request_id: UUID
    intent_id: UUID
    intent_fingerprint: Sha256
    capability_digest: Sha256
    manifest_digest: Sha256
    account_fingerprint: Sha256
    protocol_version: Literal[POLYMARKET_PROTOCOL_VERSION]
    operation: ExecutionOperation
    deadline: datetime
    payload: SignerPayload

    @field_validator("deadline")
    @classmethod
    def _deadline_utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def _bind_payload(self) -> SignerRequest:
        if self.payload.operation is not self.operation:
            raise ValueError("operation must match payload")
        if isinstance(self.payload, SignOrderPayload):
            intent = self.payload.intent
            if (
                intent.intent_id != self.intent_id
                or intent.intent_fingerprint != self.intent_fingerprint
                or intent.capability_fingerprint != self.capability_digest
                or intent.account_fingerprint != self.account_fingerprint
                or intent.protocol_version != self.protocol_version
            ):
                raise ValueError("request does not match signed intent")
        if isinstance(self.payload, SubmitOrderPayload):
            envelope = self.payload.envelope
            if (
                envelope.intent_id != self.intent_id
                or envelope.intent_fingerprint != self.intent_fingerprint
                or envelope.protocol_version != self.protocol_version
            ):
                raise ValueError("request does not match submitted envelope")
        return self


class SignedEnvelopeResult(_SignerRecord):
    operation: Literal[ExecutionOperation.SIGN_ORDER]
    envelope: SignedOrderEnvelope


class SanitizedOperationResult(_SignerRecord):
    operation: Literal[
        ExecutionOperation.SUBMIT_ORDER,
        ExecutionOperation.CANCEL_ORDER,
        ExecutionOperation.HEARTBEAT,
        ExecutionOperation.READ_ORDERS,
        ExecutionOperation.READ_TRADES,
        ExecutionOperation.READ_ACCOUNT,
    ]
    result_code: _StableCode
    evidence_hashes: tuple[Sha256, ...]
    venue_order_id: _PublicIdentifier | None = None
    heartbeat_id: _HeartbeatIdentifier | None = None

    @field_validator("evidence_hashes")
    @classmethod
    def _sorted_unique_hashes(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("evidence hashes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _operation_valid_identifiers(self) -> SanitizedOperationResult:
        venue_id_operations = {
            ExecutionOperation.SUBMIT_ORDER,
            ExecutionOperation.CANCEL_ORDER,
            ExecutionOperation.READ_ORDERS,
        }
        if self.operation not in venue_id_operations and self.venue_order_id is not None:
            raise ValueError("venue_order_id is not valid for operation")
        if self.operation is not ExecutionOperation.HEARTBEAT and self.heartbeat_id is not None:
            raise ValueError("heartbeat_id is not valid for operation")
        if self.operation is ExecutionOperation.CANCEL_ORDER and self.venue_order_id is None:
            raise ValueError("cancel result requires venue_order_id")
        return self


SignerResult = Annotated[
    SignedEnvelopeResult | SanitizedOperationResult,
    Field(discriminator="operation"),
]


class SignerResponse(_SignerRecord):
    schema_version: Literal[1]
    request_id: UUID | None
    ok: bool
    result: SignerResult | None
    error_code: _StableCode | None

    @model_validator(mode="after")
    def _result_matches_status(self) -> SignerResponse:
        if self.ok:
            if self.result is None or self.error_code is not None:
                raise ValueError("successful response requires only a result")
        elif self.result is not None or self.error_code is None:
            raise ValueError("rejected response requires only an error code")
        return self

    @classmethod
    def accepted(cls, request_id: UUID, result: SignerResult) -> SignerResponse:
        return cls(
            schema_version=1,
            request_id=request_id,
            ok=True,
            result=result,
            error_code=None,
        )

    @classmethod
    def rejected(cls, request_id: UUID | None, error_code: str) -> SignerResponse:
        return cls(
            schema_version=1,
            request_id=request_id,
            ok=False,
            result=None,
            error_code=error_code,
        )


class SignerProtocolError(ValueError):
    """A context-free signer protocol rejection identified by a stable code."""


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        read_failed = False
        try:
            chunk = stream.read(size - len(value))
        except OSError:
            read_failed = True
            chunk = b""
        if read_failed:
            raise SignerProtocolError("IPC_FRAME_READ_FAILED") from None
        if type(chunk) is not bytes:
            raise SignerProtocolError("IPC_FRAME_READ_FAILED") from None
        if not chunk:
            raise SignerProtocolError("IPC_FRAME_TRUNCATED") from None
        if len(chunk) > size - len(value):
            raise SignerProtocolError("IPC_FRAME_READ_FAILED") from None
        value.extend(chunk)
    return bytes(value)


def read_frame(stream: BinaryIO) -> bytes:
    """Read one exact 4-byte-big-endian frame without unbounded allocation."""
    length_raw = _read_exact(stream, _FRAME_HEADER_BYTES)
    length = int.from_bytes(length_raw, "big")
    if length <= 0 or length > MAX_FRAME_BYTES:
        raise SignerProtocolError("IPC_FRAME_SIZE_INVALID") from None
    return _read_exact(stream, length)


def _write_exact(stream: BinaryIO, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        write_failed = False
        try:
            written = stream.write(value[offset:])
        except OSError:
            write_failed = True
            written = 0
        if write_failed:
            raise SignerProtocolError("IPC_FRAME_WRITE_FAILED") from None
        if type(written) is not int or written <= 0 or written > len(value) - offset:
            raise SignerProtocolError("IPC_FRAME_WRITE_FAILED") from None
        offset += written


def write_frame(stream: BinaryIO, payload: bytes) -> None:
    """Write one exact bounded bytes frame and flush it once."""
    if type(payload) is not bytes:
        raise SignerProtocolError("IPC_FRAME_BYTES_REQUIRED") from None
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise SignerProtocolError("IPC_FRAME_SIZE_INVALID") from None
    _write_exact(stream, len(payload).to_bytes(_FRAME_HEADER_BYTES, "big"))
    _write_exact(stream, payload)
    flush_failed = False
    try:
        stream.flush()
    except OSError:
        flush_failed = True
    if flush_failed:
        raise SignerProtocolError("IPC_FRAME_FLUSH_FAILED") from None


def canonical_request_bytes(request: SignerRequest) -> bytes:
    return json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_response_bytes(response: SignerResponse) -> bytes:
    return json.dumps(
        response.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_bounded_json_depth(payload: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in payload:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise SignerProtocolError("IPC_REQUEST_INVALID") from None
        elif byte in (0x5D, 0x7D):
            depth -= 1
            if depth < 0:
                raise SignerProtocolError("IPC_REQUEST_INVALID") from None
    if in_string or depth != 0:
        raise SignerProtocolError("IPC_REQUEST_INVALID") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SignerProtocolError("IPC_REQUEST_INVALID") from None
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> object:
    raise SignerProtocolError("IPC_REQUEST_INVALID") from None


def parse_signer_request(payload: bytes) -> SignerRequest:
    if type(payload) is not bytes or not payload or len(payload) > MAX_FRAME_BYTES:
        raise SignerProtocolError("IPC_REQUEST_INVALID") from None
    _require_bounded_json_depth(payload)
    invalid_json = False
    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        invalid_json = True
        decoded = None
    if invalid_json:
        raise SignerProtocolError("IPC_REQUEST_INVALID") from None
    if not isinstance(decoded, Mapping):
        raise SignerProtocolError("IPC_REQUEST_INVALID") from None
    operation = decoded.get("operation")
    if operation is not None and (
        type(operation) is not str or operation not in {item.value for item in ExecutionOperation}
    ):
        raise SignerProtocolError("IPC_OPERATION_NOT_ALLOWED") from None
    if decoded.get("schema_version") != 1:
        raise SignerProtocolError("IPC_SCHEMA_UNSUPPORTED") from None
    invalid_request = False
    try:
        return SignerRequest.model_validate_json(payload, strict=True)
    except ValidationError:
        invalid_request = True
    if invalid_request:
        raise SignerProtocolError("IPC_REQUEST_INVALID") from None
    raise AssertionError("unreachable")


__all__ = [
    "MAX_FRAME_BYTES",
    "CancelOrderPayload",
    "HeartbeatPayload",
    "ReadAccountPayload",
    "ReadOrdersPayload",
    "ReadTradesPayload",
    "SanitizedOperationResult",
    "SignOrderPayload",
    "SignedEnvelopeResult",
    "SignerProtocolError",
    "SignerRequest",
    "SignerResponse",
    "SubmitOrderPayload",
    "canonical_request_bytes",
    "canonical_response_bytes",
    "parse_signer_request",
    "read_frame",
    "write_frame",
]
