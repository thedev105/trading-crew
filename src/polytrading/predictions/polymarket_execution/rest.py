"""Closed authenticated REST execution with sanitized, no-retry mutation outcomes."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import ip_address
from threading import get_ident
from typing import TYPE_CHECKING, Annotated, Never, Protocol, Self
from urllib.parse import quote, urlencode

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from polytrading.predictions.domain import Sha256
from polytrading.predictions.polymarket_execution.auth import (
    ClobAuthError,
    ClobCredentials,
    sign_l2_request,
)
from polytrading.predictions.polymarket_execution.routes import (
    ROUTE_SPECS,
    BalanceAllowancePayload,
    CancellationPayload,
    CancelOrderRequest,
    GeoblockRequest,
    GeoblockResult,
    HeartbeatAckPayload,
    HeartbeatRequest,
    MakerOrderReadPayload,
    OrderAckPayload,
    OrderReadPayload,
    OrdersReadPayload,
    ReadBalanceAllowanceRequest,
    ReadOpenOrdersRequest,
    ReadOrderRequest,
    ReadTradesRequest,
    RestCode,
    RouteKey,
    RoutePublicPayload,
    RouteRequest,
    SubmitOrderRequest,
    TradeReadPayload,
    TradesReadPayload,
)

if TYPE_CHECKING:
    from polytrading.predictions.execution.models import ExecutionOperation
    from polytrading.predictions.polymarket_execution.ipc import (
        CancelOrderPayload,
        HeartbeatPayload,
        ReadAccountPayload,
        ReadOrdersPayload,
        ReadTradesPayload,
        SanitizedOperationResult,
        SubmitOrderPayload,
    )
    from polytrading.predictions.polymarket_execution.signer import (
        SignerOperationHandlers,
    )

MAX_RESPONSE_BYTES = 1_048_576
MAX_ARRAY_ITEMS = 10_000
_AsciiDecimal = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9][0-9]*)(?:\.[0-9]+)?$", max_length=256),
]
_PublicIdentifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$"),
]
_TransactionHash = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{64}$")]
_EvmAddress = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{40}$")]
_AsciiInteger = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]*)$", max_length=256)]
_PrintableText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4096, pattern=r"^[\x20-\x7e]+$"),
]
_RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)
_READ_RETRY_ROUTES = frozenset(
    {
        RouteKey.READ_ORDER,
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
        RouteKey.GEOBLOCK,
    }
)
_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


def _read_result_codes() -> frozenset[RestCode]:
    return frozenset(
        {
            RestCode.READ_OK,
            RestCode.READ_NOT_FOUND,
            RestCode.READ_FAILED,
            RestCode.RATE_LIMITED,
            RestCode.AUTH_REJECTED,
            RestCode.PROTOCOL_RESPONSE_INVALID,
            RestCode.TRANSPORT_UNAVAILABLE,
            RestCode.AUTH_REQUEST_BUILD_FAILED,
        }
    )


def _outcome_flags(
    route: RouteKey,
    code: RestCode,
    payload: RoutePublicPayload | None,
) -> tuple[bool, bool]:
    no_recovery = {
        RestCode.ORDER_ACK_MATCHED,
        RestCode.HEARTBEAT_ACCEPTED,
        RestCode.READ_OK,
        RestCode.READ_NOT_FOUND,
        RestCode.GEOBLOCK_OK,
    }
    no_kill = no_recovery | {
        RestCode.CANCEL_ACKNOWLEDGED,
        RestCode.CANCEL_NOT_CONFIRMED,
        RestCode.HEARTBEAT_ID_MISMATCH,
        RestCode.PROTOCOL_RESPONSE_INVALID,
        RestCode.READ_FAILED,
        RestCode.RATE_LIMITED,
        RestCode.TRANSPORT_UNAVAILABLE,
    }
    recovery = code not in no_recovery
    kill = code not in no_kill
    if code is RestCode.CANCEL_ACKNOWLEDGED:
        recovery = True
    if route is RouteKey.GEOBLOCK and (
        code is not RestCode.GEOBLOCK_OK
        or (isinstance(payload, GeoblockResult) and payload.blocked)
    ):
        recovery = True
        kill = True
    return recovery, kill


@dataclass(frozen=True, slots=True)
class ReadRetryPolicy:
    """At most one local retry for explicitly safe read routes."""

    max_attempts: int = 1
    delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts not in {1, 2}:
            raise ValueError("READ_RETRY_ATTEMPTS_INVALID") from None
        if (
            type(self.delay_seconds) not in {int, float}
            or isinstance(self.delay_seconds, bool)
            or not math.isfinite(self.delay_seconds)
            or not 0 <= self.delay_seconds <= 1
        ):
            raise ValueError("READ_RETRY_DELAY_INVALID") from None
        object.__setattr__(self, "delay_seconds", float(self.delay_seconds))


@dataclass(frozen=True, slots=True)
class RestTimeouts:
    """Finite HTTPX phase timeouts bounded to thirty seconds."""

    connect: float = 2.0
    read: float = 3.0
    write: float = 2.0
    pool: float = 1.0

    def __post_init__(self) -> None:
        for name in ("connect", "read", "write", "pool"):
            value = getattr(self, name)
            if (
                type(value) not in {int, float}
                or isinstance(value, bool)
                or not math.isfinite(value)
                or not 0 < value <= 30
            ):
                raise ValueError("REST_TIMEOUT_INVALID") from None
            object.__setattr__(self, name, float(value))

    def as_httpx(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect,
            read=self.read,
            write=self.write,
            pool=self.pool,
        )


class RestResult(BaseModel):
    """A closed public result that cannot retain request/response objects or arbitrary text."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    route: RouteKey
    code: RestCode
    observed_at: datetime
    raw_body_hash: Sha256 | None
    request_body_hash: Sha256 | None
    attempts: Annotated[int, Field(ge=0, le=2)]
    recovery_required: bool
    kill_required: bool
    payload: RoutePublicPayload | None

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: object) -> Self:
        del _fields_set, values
        raise ValueError("REST_RESULT_CONSTRUCTION_INVALID") from None

    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        del deep
        values = self.model_dump(mode="python")
        if update is not None:
            values.update(update)
        return type(self).model_validate(values, strict=True)

    @field_validator("observed_at")
    @classmethod
    def _observed_at_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _closed_route_variant(self) -> RestResult:
        route_codes = {
            RouteKey.SUBMIT_ORDER: {
                RestCode.ORDER_ACK_MATCHED,
                RestCode.ORDER_ACK_DELAYED,
                RestCode.ORDER_ACK_LIVE_UNEXPECTED,
                RestCode.ORDER_ACK_UNMATCHED,
                RestCode.ORDER_OUTCOME_UNKNOWN,
                RestCode.AUTH_REJECTED,
                RestCode.AUTH_REQUEST_BUILD_FAILED,
            },
            RouteKey.CANCEL_ORDER: {
                RestCode.CANCEL_ACKNOWLEDGED,
                RestCode.CANCEL_NOT_CONFIRMED,
                RestCode.CANCEL_OUTCOME_UNKNOWN,
                RestCode.AUTH_REJECTED,
                RestCode.AUTH_REQUEST_BUILD_FAILED,
            },
            RouteKey.HEARTBEAT: {
                RestCode.HEARTBEAT_ACCEPTED,
                RestCode.HEARTBEAT_ID_MISMATCH,
                RestCode.HEARTBEAT_OUTCOME_UNKNOWN,
                RestCode.AUTH_REJECTED,
                RestCode.AUTH_REQUEST_BUILD_FAILED,
            },
            RouteKey.READ_ORDER: _read_result_codes(),
            RouteKey.READ_OPEN_ORDERS: _read_result_codes(),
            RouteKey.READ_TRADES: _read_result_codes(),
            RouteKey.READ_BALANCE_ALLOWANCE: _read_result_codes(),
            RouteKey.GEOBLOCK: {
                RestCode.GEOBLOCK_OK,
                RestCode.GEOBLOCK_FAILED,
                RestCode.AUTH_REJECTED,
                RestCode.PROTOCOL_RESPONSE_INVALID,
                RestCode.AUTH_REQUEST_BUILD_FAILED,
            },
        }
        if self.code not in route_codes[self.route]:
            raise ValueError("REST_CODE_ROUTE_MISMATCH")
        expected_payload_type: type[BaseModel] | None = None
        if self.code in {
            RestCode.ORDER_ACK_MATCHED,
            RestCode.ORDER_ACK_DELAYED,
            RestCode.ORDER_ACK_LIVE_UNEXPECTED,
            RestCode.ORDER_ACK_UNMATCHED,
        }:
            expected_payload_type = OrderAckPayload
        elif self.code is RestCode.CANCEL_ACKNOWLEDGED:
            expected_payload_type = CancellationPayload
        elif self.code in {RestCode.HEARTBEAT_ACCEPTED, RestCode.HEARTBEAT_ID_MISMATCH}:
            expected_payload_type = HeartbeatAckPayload
        elif self.code is RestCode.READ_OK:
            expected_payload_type = {
                RouteKey.READ_ORDER: OrderReadPayload,
                RouteKey.READ_OPEN_ORDERS: OrdersReadPayload,
                RouteKey.READ_TRADES: TradesReadPayload,
                RouteKey.READ_BALANCE_ALLOWANCE: BalanceAllowancePayload,
            }[self.route]
        elif self.code is RestCode.GEOBLOCK_OK:
            expected_payload_type = GeoblockResult
        if (expected_payload_type is None) != (self.payload is None) or (
            expected_payload_type is not None and type(self.payload) is not expected_payload_type
        ):
            raise ValueError("REST_PAYLOAD_CODE_MISMATCH")
        if (self.code is RestCode.AUTH_REQUEST_BUILD_FAILED) != (self.attempts == 0):
            raise ValueError("REST_ATTEMPTS_CODE_MISMATCH")
        expected_recovery, expected_kill = _outcome_flags(
            self.route,
            self.code,
            self.payload,
        )
        if (self.recovery_required, self.kill_required) != (
            expected_recovery,
            expected_kill,
        ):
            raise ValueError("REST_FLAGS_CODE_MISMATCH")
        return self


class PolymarketRestTransport(Protocol):
    async def execute(
        self,
        request: RouteRequest,
        *,
        credentials: ClobCredentials | None = None,
    ) -> RestResult: ...

    async def execute_geoblock_restricted(
        self,
        request: GeoblockRequest,
    ) -> RestrictedGeoblockResponse: ...

    async def aclose(self) -> None: ...


class RestrictedGeoblockEvidence:
    """Raw geolocation evidence with no generic display or serialization surface."""

    __slots__ = ("_exact_bytes", "_raw_ip", "_sealed")

    def __init__(self, *, raw_ip: str, exact_bytes: bytes) -> None:
        if type(raw_ip) is not str:
            raise ValueError("GEOBLOCK_EVIDENCE_INVALID") from None
        try:
            ip_address(raw_ip)
        except ValueError:
            raise ValueError("GEOBLOCK_EVIDENCE_INVALID") from None
        if type(exact_bytes) is not bytes or not 0 < len(exact_bytes) <= MAX_RESPONSE_BYTES:
            raise ValueError("GEOBLOCK_EVIDENCE_INVALID") from None
        object.__setattr__(self, "_raw_ip", raw_ip)
        object.__setattr__(self, "_exact_bytes", exact_bytes)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        if object.__getattribute__(self, "_sealed"):
            raise AttributeError("GEOBLOCK_EVIDENCE_IMMUTABLE") from None

    def __repr__(self) -> str:
        return "RestrictedGeoblockEvidence(<restricted>)"

    __str__ = __repr__

    @property
    def raw_evidence_hash(self) -> str:
        return sha256(object.__getattribute__(self, "_exact_bytes")).hexdigest()

    @staticmethod
    def _restricted() -> Never:
        raise ValueError("GEOBLOCK_EVIDENCE_RESTRICTED") from None

    def __copy__(self) -> Never:
        return self._restricted()

    def __deepcopy__(self, memo: object) -> Never:
        del memo
        return self._restricted()

    def __reduce__(self) -> Never:
        return self._restricted()

    def __reduce_ex__(self, protocol: int) -> Never:
        del protocol
        return self._restricted()

    def __getstate__(self) -> Never:
        return self._restricted()

    def __eq__(self, other: object) -> Never:
        del other
        return self._restricted()

    def __hash__(self) -> Never:
        return self._restricted()


class RestrictedGeoblockResponse:
    """One restricted geoblock exchange, sealed from generic object surfaces."""

    __slots__ = ("_evidence", "_result", "_sealed")

    def __init__(
        self,
        *,
        result: RestResult,
        evidence: RestrictedGeoblockEvidence | None,
    ) -> None:
        if type(result) is not RestResult or result.route is not RouteKey.GEOBLOCK:
            raise ValueError("GEOBLOCK_RESTRICTED_RESPONSE_INVALID") from None
        if evidence is not None and type(evidence) is not RestrictedGeoblockEvidence:
            raise ValueError("GEOBLOCK_RESTRICTED_RESPONSE_INVALID") from None
        if (result.code is RestCode.GEOBLOCK_OK) != (evidence is not None):
            raise ValueError("GEOBLOCK_RESTRICTED_RESPONSE_INVALID") from None
        if evidence is not None and evidence.raw_evidence_hash != result.raw_body_hash:
            raise ValueError("GEOBLOCK_RESTRICTED_RESPONSE_INVALID") from None
        object.__setattr__(self, "_result", result)
        object.__setattr__(self, "_evidence", evidence)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        if object.__getattribute__(self, "_sealed"):
            raise AttributeError("GEOBLOCK_RESTRICTED_RESPONSE_IMMUTABLE") from None

    @property
    def result(self) -> RestResult:
        return object.__getattribute__(self, "_result")

    @property
    def evidence(self) -> RestrictedGeoblockEvidence | None:
        return object.__getattribute__(self, "_evidence")

    def __repr__(self) -> str:
        return "RestrictedGeoblockResponse(<restricted>)"

    __str__ = __repr__

    @staticmethod
    def _restricted() -> Never:
        raise ValueError("GEOBLOCK_RESTRICTED_RESPONSE_RESTRICTED") from None

    def __copy__(self) -> Never:
        return self._restricted()

    def __deepcopy__(self, memo: object) -> Never:
        del memo
        return self._restricted()

    def __reduce__(self) -> Never:
        return self._restricted()

    def __reduce_ex__(self, protocol: int) -> Never:
        del protocol
        return self._restricted()

    def __getstate__(self) -> Never:
        return self._restricted()

    def __eq__(self, other: object) -> Never:
        del other
        return self._restricted()

    def __hash__(self) -> Never:
        return self._restricted()


class _OrderAckWire(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
    )

    success: bool
    error_msg: Annotated[str, Field(alias="errorMsg", max_length=4096)]
    order_id: _PublicIdentifier = Field(alias="orderID")
    status: str
    making_amount: _AsciiDecimal = Field(alias="makingAmount")
    taking_amount: _AsciiDecimal = Field(alias="takingAmount")
    transaction_hashes: Annotated[
        tuple[_TransactionHash, ...], Field(max_length=MAX_ARRAY_ITEMS)
    ] = Field(alias="transactionsHashes")
    trade_ids: Annotated[tuple[_PublicIdentifier, ...], Field(max_length=MAX_ARRAY_ITEMS)] = Field(
        alias="tradeIDs"
    )


class _CancelWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    canceled: Annotated[tuple[_PublicIdentifier, ...], Field(max_length=MAX_ARRAY_ITEMS)]
    not_canceled: dict[_PublicIdentifier, _PrintableText]


class _HeartbeatWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    heartbeat_id: _PublicIdentifier


class _HeartbeatMismatchWire(_HeartbeatWire):
    error_msg: _PrintableText


class _OrderReadWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: _PublicIdentifier
    market: _PublicIdentifier
    asset_id: _AsciiInteger
    owner: _PublicIdentifier
    maker_address: _EvmAddress
    side: Annotated[str, StringConstraints(pattern=r"^(BUY|SELL)$")]
    price: _AsciiDecimal
    original_size: _AsciiDecimal
    size_matched: _AsciiDecimal
    outcome: _PrintableText
    order_type: _PrintableText
    status: _PrintableText
    associate_trades: (
        Annotated[tuple[_PublicIdentifier, ...], Field(max_length=MAX_ARRAY_ITEMS)] | None
    )
    created_at: _AsciiInteger
    expiration: _AsciiInteger


class _MakerOrderReadWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    order_id: _PublicIdentifier
    owner: _PublicIdentifier
    maker_address: _EvmAddress
    matched_amount: _AsciiDecimal
    price: _AsciiDecimal
    fee_rate_bps: _AsciiInteger
    asset_id: _AsciiInteger
    outcome: _PrintableText
    outcome_index: Annotated[int, Field(ge=0)]
    side: Annotated[str, StringConstraints(pattern=r"^(BUY|SELL)$")]


class _TradeReadWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: _PublicIdentifier
    market: _PublicIdentifier
    asset_id: _AsciiInteger
    owner: _PublicIdentifier
    maker_address: _EvmAddress
    taker_order_id: _PublicIdentifier
    side: Annotated[str, StringConstraints(pattern=r"^(BUY|SELL)$")]
    trader_side: Annotated[str, StringConstraints(pattern=r"^(MAKER|TAKER)$")]
    price: _AsciiDecimal
    size: _AsciiDecimal
    outcome: _PrintableText
    status: Annotated[
        str,
        StringConstraints(
            pattern=r"^(MATCHED_NOT_BROADCASTED|MATCHED|MINED|CONFIRMED|RETRYING|FAILED)$"
        ),
    ]
    fee_rate_bps: _AsciiInteger
    bucket_index: Annotated[int, Field(ge=0)]
    transaction_hash: _TransactionHash | None
    maker_orders: Annotated[tuple[_MakerOrderReadWire, ...], Field(max_length=MAX_ARRAY_ITEMS)]
    match_time: _AsciiInteger
    last_update: _AsciiInteger


class _BalanceAllowanceWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    balance: _AsciiInteger
    allowances: dict[_EvmAddress, _AsciiInteger]


class _GeoblockWire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    blocked: bool
    ip: str
    country: (
        Annotated[
            str,
            StringConstraints(min_length=1, max_length=64, pattern=r"^[\x20-\x7e]+$"),
        ]
        | None
    )
    region: (
        Annotated[
            str,
            StringConstraints(min_length=1, max_length=64, pattern=r"^[\x20-\x7e]+$"),
        ]
        | None
    )

    @field_validator("ip")
    @classmethod
    def _valid_ip(cls, value: str) -> str:
        try:
            ip_address(value)
        except ValueError:
            raise ValueError("invalid IP address") from None
        return value


_ORDERS_ADAPTER = TypeAdapter(
    Annotated[tuple[_OrderReadWire, ...], Field(max_length=MAX_ARRAY_ITEMS)]
)
_TRADES_ADAPTER = TypeAdapter(
    Annotated[tuple[_TradeReadWire, ...], Field(max_length=MAX_ARRAY_ITEMS)]
)


@dataclass(frozen=True, slots=True)
class _BuiltRequest:
    route: RouteKey
    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    request_body_hash: str | None

    def httpx_request(self) -> httpx.Request:
        return httpx.Request(
            self.method,
            self.url,
            headers=self.headers,
            content=self.body,
        )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _reject_ambiguous_json(body: bytes) -> None:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def invalid_constant(value: str) -> Never:
        del value
        raise ValueError("invalid JSON constant")

    json.loads(
        body,
        object_pairs_hook=unique_object,
        parse_constant=invalid_constant,
    )


def _result(
    *,
    route: RouteKey,
    code: RestCode,
    observed_at: datetime,
    attempts: int,
    raw_body_hash: str | None = None,
    request_body_hash: str | None = None,
    payload: RoutePublicPayload | None = None,
) -> RestResult:
    recovery, kill = _outcome_flags(route, code, payload)
    return RestResult(
        route=route,
        code=code,
        observed_at=observed_at,
        raw_body_hash=raw_body_hash,
        request_body_hash=request_body_hash,
        attempts=attempts,
        recovery_required=recovery,
        kill_required=kill,
        payload=payload,
    )


def classify_order_ack(
    body: bytes,
    *,
    observed_at: datetime,
    attempts: int,
    request_body_hash: str,
) -> RestResult:
    """Classify one strict successful HTTP order body without retaining raw bytes."""
    raw_body_hash = sha256(body).hexdigest() if body else None
    try:
        _reject_ambiguous_json(body)
        wire = _OrderAckWire.model_validate_json(body, strict=True)
    except ValueError:
        return _result(
            route=RouteKey.SUBMIT_ORDER,
            code=RestCode.ORDER_OUTCOME_UNKNOWN,
            observed_at=observed_at,
            attempts=attempts,
            raw_body_hash=raw_body_hash,
            request_body_hash=request_body_hash,
        )
    codes = {
        "matched": RestCode.ORDER_ACK_MATCHED,
        "delayed": RestCode.ORDER_ACK_DELAYED,
        "live": RestCode.ORDER_ACK_LIVE_UNEXPECTED,
        "unmatched": RestCode.ORDER_ACK_UNMATCHED,
    }
    code = codes.get(wire.status)
    if not wire.success or wire.error_msg or code is None:
        return _result(
            route=RouteKey.SUBMIT_ORDER,
            code=RestCode.ORDER_OUTCOME_UNKNOWN,
            observed_at=observed_at,
            attempts=attempts,
            raw_body_hash=raw_body_hash,
            request_body_hash=request_body_hash,
        )
    payload = OrderAckPayload(
        kind="ORDER_ACK",
        order_id=wire.order_id,
        status=wire.status,
        making_amount=wire.making_amount,
        taking_amount=wire.taking_amount,
        transaction_hashes=wire.transaction_hashes,
        trade_ids=wire.trade_ids,
    )
    return _result(
        route=RouteKey.SUBMIT_ORDER,
        code=code,
        observed_at=observed_at,
        attempts=attempts,
        raw_body_hash=raw_body_hash,
        request_body_hash=request_body_hash,
        payload=payload,
    )


def sanitize_venue_error(
    *,
    route: RouteKey,
    observed_at: datetime,
    attempts: int,
    request_body_hash: str | None,
    status_code: int | None = None,
    raw_body_hash: str | None = None,
    protocol_invalid: bool = False,
) -> RestResult:
    """Map public status/failure categories to a stable code without venue text."""
    read_routes = {
        RouteKey.READ_ORDER,
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    }
    if status_code in {401, 403}:
        code = RestCode.AUTH_REJECTED
    elif route in read_routes and status_code == 404:
        code = RestCode.READ_NOT_FOUND
    elif route in read_routes and status_code == 429:
        code = RestCode.RATE_LIMITED
    elif route in read_routes and status_code is not None and status_code >= 500:
        code = RestCode.READ_FAILED
    elif protocol_invalid and route in read_routes | {RouteKey.GEOBLOCK}:
        code = RestCode.PROTOCOL_RESPONSE_INVALID
    elif route is RouteKey.SUBMIT_ORDER:
        code = RestCode.ORDER_OUTCOME_UNKNOWN
    elif route is RouteKey.CANCEL_ORDER:
        code = RestCode.CANCEL_OUTCOME_UNKNOWN
    elif route is RouteKey.HEARTBEAT:
        code = RestCode.HEARTBEAT_OUTCOME_UNKNOWN
    elif route is RouteKey.GEOBLOCK:
        code = RestCode.GEOBLOCK_FAILED
    else:
        code = RestCode.TRANSPORT_UNAVAILABLE
    return _result(
        route=route,
        code=code,
        observed_at=observed_at,
        attempts=attempts,
        raw_body_hash=raw_body_hash,
        request_body_hash=request_body_hash,
    )


def _order_payload(wire: _OrderReadWire) -> OrderReadPayload:
    values = wire.model_dump(mode="python", exclude={"owner"})
    return OrderReadPayload(kind="ORDER_READ", **values)


def _maker_order_payload(wire: _MakerOrderReadWire) -> MakerOrderReadPayload:
    values = wire.model_dump(mode="python", exclude={"owner"})
    return MakerOrderReadPayload(**values)


def _trade_payload(wire: _TradeReadWire) -> TradeReadPayload:
    values = wire.model_dump(mode="python", exclude={"owner", "maker_orders"})
    return TradeReadPayload(
        kind="TRADE_READ",
        maker_orders=tuple(_maker_order_payload(item) for item in wire.maker_orders),
        **values,
    )


def _strict_success_result(
    *,
    request: RouteRequest,
    body: bytes,
    status_code: int,
    observed_at: datetime,
    attempts: int,
    request_body_hash: str | None,
    api_key: str | None,
    geoblock_evidence: list[RestrictedGeoblockEvidence] | None = None,
) -> RestResult:
    raw_body_hash = sha256(body).hexdigest() if body else None
    restricted_evidence: RestrictedGeoblockEvidence | None = None
    try:
        _reject_ambiguous_json(body)
        if isinstance(request, SubmitOrderRequest):
            return classify_order_ack(
                body,
                observed_at=observed_at,
                attempts=attempts,
                request_body_hash=request_body_hash or sha256(b"").hexdigest(),
            )
        if isinstance(request, CancelOrderRequest):
            wire = _CancelWire.model_validate_json(body, strict=True)
            target_count = wire.canceled.count(request.order_id)
            contradicted = request.order_id in wire.not_canceled
            if target_count == 1 and not contradicted:
                return _result(
                    route=request.route,
                    code=RestCode.CANCEL_ACKNOWLEDGED,
                    observed_at=observed_at,
                    attempts=attempts,
                    raw_body_hash=raw_body_hash,
                    request_body_hash=request_body_hash,
                    payload=CancellationPayload(
                        kind="CANCELLATION",
                        order_id=request.order_id,
                        confirmation_required=True,
                    ),
                )
            return _result(
                route=request.route,
                code=RestCode.CANCEL_NOT_CONFIRMED,
                observed_at=observed_at,
                attempts=attempts,
                raw_body_hash=raw_body_hash,
                request_body_hash=request_body_hash,
            )
        if isinstance(request, HeartbeatRequest):
            if status_code == 400:
                wire_mismatch = _HeartbeatMismatchWire.model_validate_json(body, strict=True)
                return _result(
                    route=request.route,
                    code=RestCode.HEARTBEAT_ID_MISMATCH,
                    observed_at=observed_at,
                    attempts=attempts,
                    raw_body_hash=raw_body_hash,
                    request_body_hash=request_body_hash,
                    payload=HeartbeatAckPayload(
                        kind="HEARTBEAT_ACK",
                        heartbeat_id=wire_mismatch.heartbeat_id,
                    ),
                )
            wire_heartbeat = _HeartbeatWire.model_validate_json(body, strict=True)
            return _result(
                route=request.route,
                code=RestCode.HEARTBEAT_ACCEPTED,
                observed_at=observed_at,
                attempts=attempts,
                raw_body_hash=raw_body_hash,
                request_body_hash=request_body_hash,
                payload=HeartbeatAckPayload(
                    kind="HEARTBEAT_ACK",
                    heartbeat_id=wire_heartbeat.heartbeat_id,
                ),
            )
        if isinstance(request, ReadOrderRequest):
            wire_order = _OrderReadWire.model_validate_json(body, strict=True)
            if wire_order.owner != api_key:
                raise ValueError("response account owner mismatch")
            payload: RoutePublicPayload = _order_payload(wire_order)
        elif isinstance(request, ReadOpenOrdersRequest):
            wire_orders = _ORDERS_ADAPTER.validate_json(body, strict=True)
            if any(item.owner != api_key for item in wire_orders):
                raise ValueError("response account owner mismatch")
            payload = OrdersReadPayload(
                kind="ORDERS_READ",
                items=tuple(_order_payload(item) for item in wire_orders),
            )
        elif isinstance(request, ReadTradesRequest):
            wire_trades = _TRADES_ADAPTER.validate_json(body, strict=True)
            if any(item.owner != api_key for item in wire_trades):
                raise ValueError("response account owner mismatch")
            payload = TradesReadPayload(
                kind="TRADES_READ",
                items=tuple(_trade_payload(item) for item in wire_trades),
            )
        elif isinstance(request, ReadBalanceAllowanceRequest):
            wire_balance = _BalanceAllowanceWire.model_validate_json(body, strict=True)
            payload = BalanceAllowancePayload(
                kind="BALANCE_ALLOWANCE",
                balance=wire_balance.balance,
                allowances=wire_balance.allowances,
            )
        elif isinstance(request, GeoblockRequest):
            wire_geo = _GeoblockWire.model_validate_json(body, strict=True)
            restricted_evidence = RestrictedGeoblockEvidence(
                raw_ip=wire_geo.ip,
                exact_bytes=body,
            )
            payload = GeoblockResult(
                kind="GEOBLOCK",
                blocked=wire_geo.blocked,
                country=wire_geo.country,
                region=wire_geo.region,
            )
        else:
            raise ValueError("unsupported response route")
    except ValueError:
        return sanitize_venue_error(
            route=request.route,
            observed_at=observed_at,
            attempts=attempts,
            request_body_hash=request_body_hash,
            raw_body_hash=raw_body_hash,
            protocol_invalid=True,
        )
    result = _result(
        route=request.route,
        code=RestCode.GEOBLOCK_OK if request.route is RouteKey.GEOBLOCK else RestCode.READ_OK,
        observed_at=observed_at,
        attempts=attempts,
        raw_body_hash=raw_body_hash,
        request_body_hash=request_body_hash,
        payload=payload,
    )
    if restricted_evidence is not None and geoblock_evidence is not None:
        geoblock_evidence.append(restricted_evidence)
    return result


def _build_submit_request(
    request: SubmitOrderRequest,
    credentials: ClobCredentials,
    timestamp: str,
) -> _BuiltRequest:
    try:
        public_order = json.loads(request.envelope.canonical_order_json)
    except (TypeError, json.JSONDecodeError):
        raise ClobAuthError("AUTH_REQUEST_BUILD_FAILED") from None
    signed_order = {**public_order, "signature": request.envelope.public_signature}
    if sha256(_canonical_json_bytes(signed_order)).hexdigest() != request.envelope.exact_body_hash:
        raise ClobAuthError("AUTH_REQUEST_BUILD_FAILED") from None
    body = _canonical_json_bytes(
        {
            "deferExec": False,
            "order": signed_order,
            "orderType": request.intent.order_type.value,
            "owner": credentials.api_key.decode("ascii"),
        }
    )
    spec = ROUTE_SPECS[RouteKey.SUBMIT_ORDER]
    auth_headers = sign_l2_request(
        credentials,
        timestamp=timestamp,
        method=spec.method,
        route=spec.path_template,
        body=body,
    )
    headers = (*auth_headers.items(), ("Content-Type", "application/json"))
    return _BuiltRequest(
        route=RouteKey.SUBMIT_ORDER,
        method=spec.method,
        url=spec.host + spec.path_template,
        headers=headers,
        body=body,
        request_body_hash=sha256(body).hexdigest(),
    )


def _build_request(
    request: RouteRequest,
    credentials: ClobCredentials | None,
    timestamp: Callable[[], str],
) -> _BuiltRequest:
    if isinstance(request, GeoblockRequest):
        spec = ROUTE_SPECS[request.route]
        return _BuiltRequest(
            route=request.route,
            method=spec.method,
            url=spec.host + spec.path_template,
            headers=(),
            body=b"",
            request_body_hash=None,
        )
    if type(credentials) is not ClobCredentials:
        raise ClobAuthError("AUTH_REQUEST_BUILD_FAILED") from None
    signed_at = timestamp()
    if (
        type(signed_at) is not str
        or not signed_at
        or not signed_at.isascii()
        or not signed_at.isdigit()
    ):
        raise ClobAuthError("AUTH_REQUEST_BUILD_FAILED") from None
    if isinstance(request, SubmitOrderRequest):
        return _build_submit_request(request, credentials, signed_at)

    spec = ROUTE_SPECS[request.route]
    path = spec.path_template
    body = b""
    query: list[tuple[str, str | int]] = []
    content_type = False
    if isinstance(request, CancelOrderRequest):
        body = _canonical_json_bytes({"orderID": request.order_id})
        content_type = True
    elif isinstance(request, HeartbeatRequest):
        body = _canonical_json_bytes({"heartbeat_id": request.heartbeat_id})
        content_type = True
    elif isinstance(request, ReadOrderRequest):
        path = path.format(order_id=quote(request.order_id, safe=""))
    elif isinstance(
        request,
        (ReadOpenOrdersRequest, ReadTradesRequest, ReadBalanceAllowanceRequest),
    ):
        query = [
            (name, value)
            for name in spec.query_fields
            if (value := getattr(request, name)) is not None
        ]
    else:
        raise ClobAuthError("AUTH_REQUEST_BUILD_FAILED") from None

    auth_headers = sign_l2_request(
        credentials,
        timestamp=signed_at,
        method=spec.method,
        route=path,
        body=body,
    )
    headers = tuple(auth_headers.items())
    if content_type:
        headers += (("Content-Type", "application/json"),)
    url = spec.host + path
    if query:
        url += "?" + urlencode(query)
    return _BuiltRequest(
        route=request.route,
        method=spec.method,
        url=url,
        headers=headers,
        body=body,
        request_body_hash=sha256(body).hexdigest() if body else None,
    )


class HttpxPolymarketRestTransport:
    """Execute only closed typed requests using an injected non-retrying HTTP transport."""

    __slots__ = ("_client", "_clock", "_retry_policy", "_sleeper", "_timestamp")

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timestamp: Callable[[], str],
        clock: Callable[[], datetime],
        retry_policy: ReadRetryPolicy | None = None,
        sleeper: AsyncSleeper | None = None,
        timeouts: RestTimeouts | None = None,
    ) -> None:
        if (transport is None) == (client is None):
            raise TypeError("ONE_HTTPX_CLIENT_OR_TRANSPORT_REQUIRED")
        checked_timeouts = timeouts or RestTimeouts()
        if not isinstance(checked_timeouts, RestTimeouts):
            raise TypeError("REST_TIMEOUTS_REQUIRED")
        if transport is not None:
            if not isinstance(transport, httpx.AsyncBaseTransport):
                raise TypeError("HTTPX_ASYNC_TRANSPORT_REQUIRED")
            self._client = httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                trust_env=False,
                timeout=checked_timeouts.as_httpx(),
                headers={},
                cookies={},
            )
        else:
            if not isinstance(client, httpx.AsyncClient):
                raise TypeError("HTTPX_ASYNC_CLIENT_REQUIRED")
            if client.follow_redirects:
                raise ValueError("HTTPX_REDIRECTS_MUST_BE_DISABLED") from None
            client.headers.clear()
            client.cookies.clear()
            client.auth = None
            client.event_hooks["request"].clear()
            client.event_hooks["response"].clear()
            client.timeout = checked_timeouts.as_httpx()
            self._client = client
        self._timestamp = timestamp
        self._clock = clock
        self._retry_policy = retry_policy or ReadRetryPolicy()
        if not isinstance(self._retry_policy, ReadRetryPolicy):
            raise TypeError("READ_RETRY_POLICY_REQUIRED")
        if sleeper is not None and not callable(sleeper):
            raise TypeError("ASYNC_SLEEPER_REQUIRED")
        if self._retry_policy.max_attempts == 2 and sleeper is None:
            raise TypeError("ASYNC_SLEEPER_REQUIRED")
        self._sleeper = sleeper

    async def execute(
        self,
        request: RouteRequest,
        *,
        credentials: ClobCredentials | None = None,
    ) -> RestResult:
        return await self._execute(
            request,
            credentials=credentials,
            geoblock_evidence=None,
        )

    async def execute_geoblock_restricted(
        self,
        request: GeoblockRequest,
    ) -> RestrictedGeoblockResponse:
        """Return one sanitized result and its same-response restricted evidence."""
        if type(request) is not GeoblockRequest:
            raise TypeError("GEOBLOCK_REQUEST_REQUIRED")
        retained: list[RestrictedGeoblockEvidence] = []
        result = await self._execute(
            request,
            credentials=None,
            geoblock_evidence=retained,
        )
        evidence = retained[0] if retained else None
        return RestrictedGeoblockResponse(result=result, evidence=evidence)

    async def _execute(
        self,
        request: RouteRequest,
        *,
        credentials: ClobCredentials | None,
        geoblock_evidence: list[RestrictedGeoblockEvidence] | None,
    ) -> RestResult:
        try:
            observed_at = self._clock()
            if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
                raise ValueError("REST_CLOCK_INVALID")
            observed_at = observed_at.astimezone(UTC)
        except Exception:
            return _result(
                route=request.route,
                code=RestCode.AUTH_REQUEST_BUILD_FAILED,
                observed_at=datetime.fromtimestamp(0, UTC),
                attempts=0,
            )
        try:
            built = _build_request(request, credentials, self._timestamp)
            api_key = (
                credentials.api_key.decode("ascii")
                if type(credentials) is ClobCredentials
                else None
            )
        except Exception:
            return _result(
                route=request.route,
                code=RestCode.AUTH_REQUEST_BUILD_FAILED,
                observed_at=observed_at,
                attempts=0,
            )

        max_attempts = self._retry_policy.max_attempts if built.route in _READ_RETRY_ROUTES else 1
        for attempt in range(1, max_attempts + 1):
            self._client.cookies.clear()
            try:
                response = await self._client.send(built.httpx_request(), stream=True)
            except _RETRYABLE_TRANSPORT_ERRORS:
                if attempt < max_attempts and await self._sleep_before_retry():
                    continue
                return sanitize_venue_error(
                    route=built.route,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                )
            except httpx.TransportError:
                return sanitize_venue_error(
                    route=built.route,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                )
            except Exception:
                return sanitize_venue_error(
                    route=built.route,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                )
            body = bytearray()
            oversized = False
            stream_retryable = False
            response_close_failed = False
            try:
                if response.is_stream_consumed:
                    cached = response.content
                    if len(cached) > MAX_RESPONSE_BYTES:
                        oversized = True
                    else:
                        body.extend(cached)
                else:
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            oversized = True
                            break
                        body.extend(chunk)
            except _RETRYABLE_TRANSPORT_ERRORS:
                stream_retryable = True
            except httpx.TransportError:
                return sanitize_venue_error(
                    route=built.route,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                )
            except Exception:
                return sanitize_venue_error(
                    route=built.route,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                )
            finally:
                try:
                    await response.aclose()
                except Exception:
                    response_close_failed = True
                finally:
                    self._client.cookies.clear()
            if response_close_failed:
                return sanitize_venue_error(
                    route=built.route,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                )
            if stream_retryable:
                if attempt < max_attempts and await self._sleep_before_retry():
                    continue
                return sanitize_venue_error(
                    route=built.route,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                )
            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < max_attempts:
                if await self._sleep_before_retry():
                    continue
                return sanitize_venue_error(
                    route=built.route,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                    status_code=response.status_code,
                )
            body_bytes = bytes(body)
            raw_body_hash = sha256(body_bytes).hexdigest() if body_bytes and not oversized else None
            if oversized or not _json_content_type(response):
                return sanitize_venue_error(
                    route=built.route,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                    status_code=response.status_code,
                    raw_body_hash=raw_body_hash,
                    protocol_invalid=True,
                )
            if isinstance(request, HeartbeatRequest) and response.status_code == 400:
                return _strict_success_result(
                    request=request,
                    body=body_bytes,
                    status_code=response.status_code,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                    api_key=api_key,
                )
            if not 200 <= response.status_code < 300:
                return sanitize_venue_error(
                    route=built.route,
                    observed_at=observed_at,
                    attempts=attempt,
                    request_body_hash=built.request_body_hash,
                    status_code=response.status_code,
                    raw_body_hash=raw_body_hash,
                    protocol_invalid=300 <= response.status_code < 400,
                )
            return _strict_success_result(
                request=request,
                body=body_bytes,
                status_code=response.status_code,
                observed_at=observed_at,
                attempts=attempt,
                request_body_hash=built.request_body_hash,
                api_key=api_key,
                geoblock_evidence=geoblock_evidence,
            )
        raise RuntimeError("REST_ATTEMPT_LOOP_UNREACHABLE")

    async def _sleep_before_retry(self) -> bool:
        if self._sleeper is None:
            return False
        try:
            await self._sleeper(self._retry_policy.delay_seconds)
        except Exception:
            return False
        return True

    async def aclose(self) -> None:
        self._client.cookies.clear()
        with suppress(Exception):
            await self._client.aclose()


def _json_content_type(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return content_type.split(";", 1)[0].strip().casefold() == "application/json"


AsyncSleeper = Callable[[float], Awaitable[None]]


class SignerRestHandlers:
    """Thread-affine owner of fixed Task 7-to-REST handler closures."""

    __slots__ = (
        "_active",
        "_closed",
        "_credentials",
        "_runner",
        "_thread_id",
        "_transport",
    )

    def __init__(
        self,
        *,
        credentials: ClobCredentials,
        transport: HttpxPolymarketRestTransport,
    ) -> None:
        if type(credentials) is not ClobCredentials:
            raise TypeError("CLOB_CREDENTIALS_REQUIRED")
        if type(transport) is not HttpxPolymarketRestTransport:
            raise TypeError("POLYMARKET_REST_TRANSPORT_REQUIRED")
        self._credentials = credentials
        self._transport = transport
        self._runner = asyncio.Runner()
        self._thread_id = get_ident()
        self._active = False
        self._closed = False

    def as_operation_handlers(self) -> SignerOperationHandlers:
        """Return only fixed, typed operation closures; no generic REST or signing hook."""
        from polytrading.predictions.polymarket_execution.signer import (
            SignerOperationHandlers,
        )

        return SignerOperationHandlers(
            submit_order=self._submit_order,
            cancel_order=self._cancel_order,
            heartbeat=self._heartbeat,
            read_orders=self._read_orders,
            read_trades=self._read_trades,
            read_account=self._read_account,
            close=self.close,
        )

    def _run(
        self,
        request: RouteRequest,
        *,
        venue_order_id: str | None = None,
    ) -> SanitizedOperationResult:
        from polytrading.predictions.polymarket_execution.ipc import (
            SanitizedOperationResult,
        )

        self._require_available()
        self._active = True
        try:
            result = self._runner.run(
                self._transport.execute(request, credentials=self._credentials)
            )
        finally:
            self._active = False
        public_order_id = (
            result.payload.order_id
            if isinstance(result.payload, OrderAckPayload)
            else venue_order_id
        )
        heartbeat_id = (
            result.payload.heartbeat_id if isinstance(result.payload, HeartbeatAckPayload) else None
        )
        evidence_hashes = tuple(
            sorted(
                {
                    value
                    for value in (result.raw_body_hash, result.request_body_hash)
                    if value is not None
                }
            )
        )
        return SanitizedOperationResult(
            operation=self._operation_for_route(result.route),
            result_code=result.code,
            evidence_hashes=evidence_hashes,
            venue_order_id=public_order_id,
            heartbeat_id=heartbeat_id,
            route=result.route,
            observed_at=result.observed_at,
            raw_body_hash=result.raw_body_hash,
            request_body_hash=result.request_body_hash,
            attempts=result.attempts,
            recovery_required=result.recovery_required,
            kill_required=result.kill_required,
            public_payload=result.payload,
        )

    @staticmethod
    def _operation_for_route(route: RouteKey) -> ExecutionOperation:
        from polytrading.predictions.execution.models import ExecutionOperation

        return {
            RouteKey.SUBMIT_ORDER: ExecutionOperation.SUBMIT_ORDER,
            RouteKey.CANCEL_ORDER: ExecutionOperation.CANCEL_ORDER,
            RouteKey.HEARTBEAT: ExecutionOperation.HEARTBEAT,
            RouteKey.READ_ORDER: ExecutionOperation.READ_ORDERS,
            RouteKey.READ_OPEN_ORDERS: ExecutionOperation.READ_ORDERS,
            RouteKey.READ_TRADES: ExecutionOperation.READ_TRADES,
            RouteKey.READ_BALANCE_ALLOWANCE: ExecutionOperation.READ_ACCOUNT,
        }[route]

    def _submit_order(self, payload: SubmitOrderPayload) -> SanitizedOperationResult:
        return self._run(
            SubmitOrderRequest(
                route=RouteKey.SUBMIT_ORDER,
                intent=payload.intent,
                envelope=payload.envelope,
            )
        )

    def _cancel_order(self, payload: CancelOrderPayload) -> SanitizedOperationResult:
        return self._run(
            CancelOrderRequest(
                route=RouteKey.CANCEL_ORDER,
                order_id=payload.venue_order_id,
            ),
            venue_order_id=payload.venue_order_id,
        )

    def _heartbeat(self, payload: HeartbeatPayload) -> SanitizedOperationResult:
        return self._run(
            HeartbeatRequest(
                route=RouteKey.HEARTBEAT,
                heartbeat_id=payload.heartbeat_id,
            )
        )

    def _read_orders(self, payload: ReadOrdersPayload) -> SanitizedOperationResult:
        if payload.venue_order_id is not None:
            return self._run(
                ReadOrderRequest(
                    route=RouteKey.READ_ORDER,
                    order_id=payload.venue_order_id,
                ),
                venue_order_id=payload.venue_order_id,
            )
        return self._run(
            ReadOpenOrdersRequest(
                route=RouteKey.READ_OPEN_ORDERS,
                id=payload.id,
                market=payload.market,
                asset_id=payload.asset_id,
            )
        )

    def _read_trades(self, payload: ReadTradesPayload) -> SanitizedOperationResult:
        return self._run(
            ReadTradesRequest(
                route=RouteKey.READ_TRADES,
                id=payload.id,
                market=payload.market,
                asset_id=payload.asset_id,
                maker_address=payload.maker_address,
                after=payload.after,
                before=payload.before,
            )
        )

    def _read_account(self, payload: ReadAccountPayload) -> SanitizedOperationResult:
        return self._run(
            ReadBalanceAllowanceRequest(
                route=RouteKey.READ_BALANCE_ALLOWANCE,
                signature_type=payload.signature_type,
                asset_type=payload.asset_type,
                token_id=payload.token_id,
            )
        )

    def _require_available(self) -> None:
        if get_ident() != self._thread_id:
            raise ClobAuthError("SIGNER_REST_HANDLERS_THREAD_INVALID") from None
        if self._closed:
            raise ClobAuthError("SIGNER_REST_HANDLERS_CLOSED") from None
        if self._active:
            raise ClobAuthError("SIGNER_REST_HANDLERS_REENTRANT") from None

    def close(self) -> None:
        if self._closed:
            return
        self._require_available()
        self._active = True
        try:
            self._runner.run(self._transport.aclose())
        finally:
            self._runner.close()
            self._active = False
            self._closed = True


__all__ = [
    "MAX_ARRAY_ITEMS",
    "MAX_RESPONSE_BYTES",
    "HttpxPolymarketRestTransport",
    "PolymarketRestTransport",
    "ReadRetryPolicy",
    "RestCode",
    "RestResult",
    "RestTimeouts",
    "RestrictedGeoblockEvidence",
    "RestrictedGeoblockResponse",
    "SignerRestHandlers",
    "classify_order_ack",
    "sanitize_venue_error",
]
