"""Bounded, length-prefixed local signer messages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, BinaryIO, Final, Literal, Self, get_args
from uuid import UUID

from pydantic import (
    Base64Bytes,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.config import ExtraValues

from polytrading.predictions.domain import PredictionRecord, Sha256, normalize_utc_timestamp
from polytrading.predictions.execution.authority import AuthorityReason
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ExecutionOperation,
    SignedOrderEnvelope,
)
from polytrading.predictions.pilot.capabilities import CapabilityGrant, SignerKillDirective
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    POLYMARKET_PROTOCOL_VERSION,
)
from polytrading.predictions.polymarket_execution.routes import (
    BalanceAllowancePayload,
    CancellationPayload,
    HeartbeatAckPayload,
    OrderAckPayload,
    OrderReadPayload,
    OrdersReadPayload,
    RestCode,
    RouteKey,
    RoutePublicPayload,
    TradesReadPayload,
    allowed_route_result_codes,
    expected_route_result_flags,
    validate_route_result_evidence,
)

MAX_FRAME_BYTES: Final = 1_048_576
_FRAME_HEADER_BYTES: Final = 4
_MAX_JSON_DEPTH: Final = 32
_PublicIdentifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$"),
]
_HeartbeatIdentifier = Annotated[
    str,
    StringConstraints(max_length=256, pattern=r"^[\x20-\x7e]*$"),
]
_AsciiInteger = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9][0-9]*)$", max_length=256),
]
_EvmAddress = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{40}$")]
LegacySignerResultCode = Literal[
    "SUBMIT_ORDER_OK",
    "CANCEL_ORDER_OK",
    "HEARTBEAT_OK",
    "READ_ORDERS_OK",
    "READ_TRADES_OK",
    "READ_ACCOUNT_OK",
]
SignerResultCode = LegacySignerResultCode | RestCode
SignerErrorCode = Literal[
    "IPC_MODEL_INVALID",
    "IPC_FRAME_BYTES_REQUIRED",
    "IPC_FRAME_FLUSH_FAILED",
    "IPC_FRAME_READ_FAILED",
    "IPC_FRAME_SIZE_INVALID",
    "IPC_FRAME_TRUNCATED",
    "IPC_FRAME_WRITE_FAILED",
    "IPC_REQUEST_INVALID",
    "IPC_OPERATION_NOT_ALLOWED",
    "IPC_SCHEMA_UNSUPPORTED",
    "IPC_REQUEST_COLLISION",
    "IPC_REPLAY_CACHE_FULL",
    "IPC_SIGNER_CLOSED",
    "IPC_CLOCK_INVALID",
    "IPC_DEADLINE_EXPIRED",
    "INTENT_DEADLINE_EXPIRED",
    "REQUEST_DEADLINE_EXCEEDS_INTENT",
    "PROTOCOL_VERSION_MISMATCH",
    "ACCOUNT_FINGERPRINT_MISMATCH",
    "AUTHORITY_CONTEXT_TIME_MISMATCH",
    "AUTHORITY_GATE_FAILED",
    "READ_GUARD_FAILED",
    "ORDER_ENVELOPE_MISMATCH",
    "CANCEL_ORDER_UNKNOWN",
    "CANCEL_ORDER_BINDING_MISMATCH",
    "VENUE_ORDER_BINDING_COLLISION",
    "IPC_OPERATION_RESULT_INVALID",
    "ORDER_SIGNING_FAILED",
    "AUTH_HANDLER_FAILED",
    "HANDLER_FAILED",
    "SECRET_OUTPUT_DETECTED",
    "IPC_SERVICE_INITIALIZATION_FAILED",
    "SECRET_DESCRIPTOR_INVALID",
    "SECRET_DESCRIPTOR_READ_FAILED",
    "SECRET_DESCRIPTOR_SIZE_INVALID",
    "SECRET_DESCRIPTOR_TRAILING_BYTES",
    "SECRET_DESCRIPTOR_TRUNCATED",
    "SECRET_PRIVATE_KEY_SIZE_INVALID",
]
_SIGNER_ERROR_CODES = frozenset(get_args(SignerErrorCode))


class SignerProtocolError(ValueError):
    """A context-free signer protocol rejection identified by a closed stable code."""

    def __init__(self, error_code: object) -> None:
        closed_code = (
            error_code
            if type(error_code) is str and error_code in _SIGNER_ERROR_CODES
            else "IPC_REQUEST_INVALID"
        )
        super().__init__(closed_code)


class _SafeModelMetaclass(type(PredictionRecord)):
    def __call__(cls, *args: object, **kwargs: object) -> object:
        invalid = False
        result: object | None = None
        try:
            result = super().__call__(*args, **kwargs)
        except (ValidationError, ValueError, TypeError, OverflowError):
            invalid = True
        if invalid:
            raise SignerProtocolError("IPC_MODEL_INVALID") from None
        assert result is not None
        return result


class _SignerRecord(PredictionRecord, metaclass=_SafeModelMetaclass):
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
        """Translate every direct Python validation failure to one constant error."""
        invalid = False
        result: Self | None = None
        try:
            result = super().model_validate(
                obj,
                strict=strict,
                extra=extra,
                from_attributes=from_attributes,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except (ValidationError, ValueError, TypeError, OverflowError):
            invalid = True
        if invalid:
            raise SignerProtocolError("IPC_MODEL_INVALID") from None
        assert result is not None
        return result

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Keep strict JSON semantics while sealing Pydantic's input-bearing errors."""
        invalid = False
        result: Self | None = None
        try:
            result = super().model_validate_json(
                json_data,
                strict=strict,
                extra=extra,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except (ValidationError, ValueError, TypeError, OverflowError):
            invalid = True
        if invalid:
            raise SignerProtocolError("IPC_MODEL_INVALID") from None
        assert result is not None
        return result

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: object) -> Self:
        del _fields_set, values
        raise SignerProtocolError("IPC_MODEL_INVALID") from None

    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        if update is None:
            return super().model_copy(deep=deep)
        values = self.model_dump(mode="python")
        values.update(update)
        return type(self).model_validate(values)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    __str__ = __repr__

    def __repr_args__(self) -> list[tuple[str | None, object]]:
        return []


class SignOrderPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.SIGN_ORDER]
    intent: ExecutionIntent


class SubmitOrderPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.SUBMIT_ORDER]
    intent: ExecutionIntent
    envelope: SignedOrderEnvelope


class CancelOrderPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.CANCEL_ORDER]
    venue_order_id: _PublicIdentifier


class HeartbeatPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.HEARTBEAT]
    heartbeat_id: _HeartbeatIdentifier


class ReadOrdersPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.READ_ORDERS]
    venue_order_id: _PublicIdentifier | None = None
    id: _PublicIdentifier | None = None
    market: _PublicIdentifier | None = None
    asset_id: _AsciiInteger | None = None

    @model_validator(mode="after")
    def _single_order_or_list_filters(self) -> ReadOrdersPayload:
        if self.venue_order_id is not None and any(
            value is not None for value in (self.id, self.market, self.asset_id)
        ):
            raise ValueError("single-order read cannot carry list filters")
        return self


class ReadTradesPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.READ_TRADES]
    id: _PublicIdentifier | None = None
    market: _PublicIdentifier | None = None
    asset_id: _AsciiInteger | None = None
    maker_address: _EvmAddress | None = None
    after: Annotated[int, Field(ge=0)] | None = None
    before: Annotated[int, Field(ge=0)] | None = None


class ReadAccountPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.READ_ACCOUNT]
    signature_type: Literal[0]
    asset_type: Literal["COLLATERAL", "CONDITIONAL"]
    token_id: _AsciiInteger | None

    @model_validator(mode="after")
    def _token_matches_asset_type(self) -> ReadAccountPayload:
        if (self.asset_type == "CONDITIONAL") != (self.token_id is not None):
            raise ValueError("token_id is required only for CONDITIONAL assets")
        return self


class DescribeIdentityPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.DESCRIBE_IDENTITY]


class SignerKillPayload(_SignerRecord):
    operation: Literal[ExecutionOperation.SIGNER_KILL]
    directive: SignerKillDirective


class SignerCapabilityProof(_SignerRecord):
    grant: CapabilityGrant
    signature: Base64Bytes


SignerPayload = Annotated[
    DescribeIdentityPayload
    | SignerKillPayload
    | SignOrderPayload
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
    authority_proof: SignerCapabilityProof | None = None
    manifest_digest: Sha256
    account_fingerprint: Sha256
    # A request may run under either reviewed checkpoint; the signer still compares this against
    # the snapshot it loaded itself, so an unreviewed version never reaches an order.
    protocol_version: Literal[POLYMARKET_PROTOCOL_VERSION, POLYMARKET_PILOT_PROTOCOL_VERSION]
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
        proof_free_operations = frozenset(
            {
                ExecutionOperation.DESCRIBE_IDENTITY,
                ExecutionOperation.SIGNER_KILL,
                ExecutionOperation.READ_ORDERS,
                ExecutionOperation.READ_TRADES,
                ExecutionOperation.READ_ACCOUNT,
            }
        )
        if self.operation in proof_free_operations:
            if self.authority_proof is not None:
                raise ValueError("this operation must not carry an authority proof")
        elif self.authority_proof is None:
            raise ValueError("mutating operations require an authority proof")
        elif self.authority_proof.grant.digest != self.capability_digest:
            raise ValueError("authority proof does not match capability digest")
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
            intent = self.payload.intent
            envelope = self.payload.envelope
            if (
                intent.intent_id != self.intent_id
                or intent.intent_fingerprint != self.intent_fingerprint
                or intent.capability_fingerprint != self.capability_digest
                or intent.account_fingerprint != self.account_fingerprint
                or intent.protocol_version != self.protocol_version
                or envelope.intent_id != self.intent_id
                or envelope.intent_fingerprint != self.intent_fingerprint
                or envelope.protocol_version != self.protocol_version
            ):
                raise ValueError("request does not match submitted envelope")
        return self


class SignedEnvelopeResult(_SignerRecord):
    operation: Literal[ExecutionOperation.SIGN_ORDER]
    envelope: SignedOrderEnvelope


class IdentityResult(_SignerRecord):
    operation: Literal[ExecutionOperation.DESCRIBE_IDENTITY]
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256


class SanitizedOperationResult(_SignerRecord):
    operation: Literal[
        ExecutionOperation.SUBMIT_ORDER,
        ExecutionOperation.CANCEL_ORDER,
        ExecutionOperation.HEARTBEAT,
        ExecutionOperation.READ_ORDERS,
        ExecutionOperation.READ_TRADES,
        ExecutionOperation.READ_ACCOUNT,
    ]
    result_code: SignerResultCode
    evidence_hashes: tuple[Sha256, ...]
    venue_order_id: _PublicIdentifier | None = None
    heartbeat_id: _HeartbeatIdentifier | None = None
    route: RouteKey | None = None
    observed_at: datetime | None = None
    raw_body_hash: Sha256 | None = None
    request_body_hash: Sha256 | None = None
    attempts: Annotated[int, Field(ge=0, le=2)] | None = None
    recovery_required: bool | None = None
    kill_required: bool | None = None
    public_payload: RoutePublicPayload | None = None

    @field_validator("evidence_hashes")
    @classmethod
    def _sorted_unique_hashes(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("evidence hashes must be sorted and unique")
        return value

    @field_validator("observed_at")
    @classmethod
    def _observed_at_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def _operation_valid_identifiers(self) -> SanitizedOperationResult:
        expected_codes: dict[ExecutionOperation, LegacySignerResultCode] = {
            ExecutionOperation.SUBMIT_ORDER: "SUBMIT_ORDER_OK",
            ExecutionOperation.CANCEL_ORDER: "CANCEL_ORDER_OK",
            ExecutionOperation.HEARTBEAT: "HEARTBEAT_OK",
            ExecutionOperation.READ_ORDERS: "READ_ORDERS_OK",
            ExecutionOperation.READ_TRADES: "READ_TRADES_OK",
            ExecutionOperation.READ_ACCOUNT: "READ_ACCOUNT_OK",
        }
        if isinstance(self.result_code, str) and not isinstance(self.result_code, RestCode):
            if self.result_code != expected_codes[self.operation]:
                raise ValueError("result code does not match operation")
            if any(
                value is not None
                for value in (
                    self.route,
                    self.observed_at,
                    self.raw_body_hash,
                    self.request_body_hash,
                    self.attempts,
                    self.recovery_required,
                    self.kill_required,
                    self.public_payload,
                )
            ):
                raise ValueError("legacy result cannot carry REST fields")
            if self.operation in {
                ExecutionOperation.SUBMIT_ORDER,
                ExecutionOperation.CANCEL_ORDER,
            }:
                if self.venue_order_id is None or self.heartbeat_id is not None:
                    raise ValueError("operation result identifiers are invalid")
            elif self.operation is ExecutionOperation.HEARTBEAT:
                if not self.heartbeat_id or self.venue_order_id is not None:
                    raise ValueError("operation result identifiers are invalid")
            elif self.operation is ExecutionOperation.READ_ORDERS:
                if self.heartbeat_id is not None:
                    raise ValueError("operation result identifiers are invalid")
            elif self.venue_order_id is not None or self.heartbeat_id is not None:
                raise ValueError("operation result identifiers are invalid")
            return self

        if not isinstance(self.result_code, RestCode):
            raise ValueError("result code is invalid")
        expected_routes = {
            ExecutionOperation.SUBMIT_ORDER: {RouteKey.SUBMIT_ORDER},
            ExecutionOperation.CANCEL_ORDER: {RouteKey.CANCEL_ORDER},
            ExecutionOperation.HEARTBEAT: {RouteKey.HEARTBEAT},
            ExecutionOperation.READ_ORDERS: {
                RouteKey.READ_ORDER,
                RouteKey.READ_OPEN_ORDERS,
            },
            ExecutionOperation.READ_TRADES: {RouteKey.READ_TRADES},
            ExecutionOperation.READ_ACCOUNT: {RouteKey.READ_BALANCE_ALLOWANCE},
        }
        if (
            self.route not in expected_routes[self.operation]
            or self.observed_at is None
            or self.attempts is None
            or self.recovery_required is None
            or self.kill_required is None
        ):
            raise ValueError("REST result does not match operation")
        assert self.route is not None
        if self.result_code not in allowed_route_result_codes(self.route):
            raise ValueError("REST code does not match route")
        expected_payload_type: type[object] | None = None
        if self.result_code in {
            RestCode.ORDER_ACK_MATCHED,
            RestCode.ORDER_ACK_DELAYED,
            RestCode.ORDER_ACK_LIVE_UNEXPECTED,
            RestCode.ORDER_ACK_UNMATCHED,
        }:
            expected_payload_type = OrderAckPayload
        elif self.result_code is RestCode.CANCEL_ACKNOWLEDGED:
            expected_payload_type = CancellationPayload
        elif self.result_code in {
            RestCode.HEARTBEAT_ACCEPTED,
            RestCode.HEARTBEAT_ID_MISMATCH,
        }:
            expected_payload_type = HeartbeatAckPayload
        elif self.result_code is RestCode.READ_OK:
            expected_payload_type = {
                RouteKey.READ_ORDER: OrderReadPayload,
                RouteKey.READ_OPEN_ORDERS: OrdersReadPayload,
                RouteKey.READ_TRADES: TradesReadPayload,
                RouteKey.READ_BALANCE_ALLOWANCE: BalanceAllowancePayload,
            }[self.route]
        if (expected_payload_type is None) != (self.public_payload is None) or (
            expected_payload_type is not None
            and type(self.public_payload) is not expected_payload_type
        ):
            raise ValueError("REST payload does not match code")
        expected_hashes = tuple(
            sorted(
                {
                    value
                    for value in (self.raw_body_hash, self.request_body_hash)
                    if value is not None
                }
            )
        )
        if self.evidence_hashes != expected_hashes:
            raise ValueError("REST evidence hashes do not match")
        assert self.attempts is not None
        validate_route_result_evidence(
            route=self.route,
            code=self.result_code,
            attempts=self.attempts,
            request_body_hash=self.request_body_hash,
        )
        expected_recovery, expected_kill = expected_route_result_flags(
            route=self.route,
            code=self.result_code,
            payload=self.public_payload,
        )
        if (self.recovery_required, self.kill_required) != (
            expected_recovery,
            expected_kill,
        ):
            raise ValueError("REST flags do not match code")
        if self.operation is ExecutionOperation.SUBMIT_ORDER:
            if self.heartbeat_id is not None:
                raise ValueError("operation result identifiers are invalid")
            if isinstance(self.public_payload, OrderAckPayload):
                if self.venue_order_id != self.public_payload.order_id:
                    raise ValueError("order acknowledgement identifier mismatch")
            elif self.venue_order_id is not None:
                raise ValueError("operation result identifiers are invalid")
        elif self.operation is ExecutionOperation.CANCEL_ORDER:
            if self.venue_order_id is None or self.heartbeat_id is not None:
                raise ValueError("operation result identifiers are invalid")
            if isinstance(self.public_payload, CancellationPayload) and (
                self.public_payload.order_id != self.venue_order_id
            ):
                raise ValueError("cancellation identifier mismatch")
        elif self.operation is ExecutionOperation.HEARTBEAT:
            if (
                self.venue_order_id is not None
                or (
                    isinstance(self.public_payload, HeartbeatAckPayload)
                    and self.public_payload.heartbeat_id != self.heartbeat_id
                )
                or (
                    not isinstance(self.public_payload, HeartbeatAckPayload)
                    and self.heartbeat_id is not None
                )
            ):
                raise ValueError("operation result identifiers are invalid")
        elif self.operation is ExecutionOperation.READ_ORDERS:
            if self.heartbeat_id is not None:
                raise ValueError("operation result identifiers are invalid")
            if isinstance(self.public_payload, OrderReadPayload) and (
                self.venue_order_id != self.public_payload.id
            ):
                raise ValueError("read order identifier mismatch")
        elif self.venue_order_id is not None or self.heartbeat_id is not None:
            raise ValueError("operation result identifiers are invalid")
        return self


SignerResult = Annotated[
    IdentityResult | SignedEnvelopeResult | SanitizedOperationResult,
    Field(discriminator="operation"),
]


class SignerResponse(_SignerRecord):
    schema_version: Literal[1]
    request_id: UUID | None
    ok: bool
    result: SignerResult | None
    error_code: SignerErrorCode | AuthorityReason | None

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
    def rejected(
        cls,
        request_id: UUID | None,
        error_code: SignerErrorCode | AuthorityReason,
    ) -> SignerResponse:
        return cls(
            schema_version=1,
            request_id=request_id,
            ok=False,
            result=None,
            error_code=error_code,
        )


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
    except SignerProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError, OverflowError):
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
    except SignerProtocolError:
        invalid_request = True
    if invalid_request:
        raise SignerProtocolError("IPC_REQUEST_INVALID") from None
    raise AssertionError("unreachable")


__all__ = [
    "MAX_FRAME_BYTES",
    "CancelOrderPayload",
    "DescribeIdentityPayload",
    "HeartbeatPayload",
    "IdentityResult",
    "ReadAccountPayload",
    "ReadOrdersPayload",
    "ReadTradesPayload",
    "SanitizedOperationResult",
    "SignOrderPayload",
    "SignedEnvelopeResult",
    "SignerCapabilityProof",
    "SignerErrorCode",
    "SignerKillPayload",
    "SignerProtocolError",
    "SignerRequest",
    "SignerResponse",
    "SignerResultCode",
    "SubmitOrderPayload",
    "canonical_request_bytes",
    "canonical_response_bytes",
    "parse_signer_request",
    "read_frame",
    "write_frame",
]
