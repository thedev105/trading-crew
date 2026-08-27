"""Closed Polymarket REST route metadata derived from the frozen protocol snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from polytrading.predictions.execution.models import (
    ExecutionIntent,
    SignedOrderEnvelope,
    canonical_execution_hash,
)
from polytrading.predictions.polymarket_execution.protocol import load_protocol_snapshot

ROUTE_SET_VERSION = "polymarket-mutations-v1"


class RouteKey(StrEnum):
    SUBMIT_ORDER = "SUBMIT_ORDER"
    CANCEL_ORDER = "CANCEL_ORDER"
    READ_ORDER = "READ_ORDER"
    READ_OPEN_ORDERS = "READ_OPEN_ORDERS"
    READ_TRADES = "READ_TRADES"
    READ_BALANCE_ALLOWANCE = "READ_BALANCE_ALLOWANCE"
    HEARTBEAT = "HEARTBEAT"
    GEOBLOCK = "GEOBLOCK"


class RestCode(StrEnum):
    ORDER_ACK_MATCHED = "ORDER_ACK_MATCHED"
    ORDER_ACK_DELAYED = "ORDER_ACK_DELAYED"
    ORDER_ACK_LIVE_UNEXPECTED = "ORDER_ACK_LIVE_UNEXPECTED"
    ORDER_ACK_UNMATCHED = "ORDER_ACK_UNMATCHED"
    ORDER_OUTCOME_UNKNOWN = "ORDER_OUTCOME_UNKNOWN"
    CANCEL_ACKNOWLEDGED = "CANCEL_ACKNOWLEDGED"
    CANCEL_NOT_CONFIRMED = "CANCEL_NOT_CONFIRMED"
    CANCEL_OUTCOME_UNKNOWN = "CANCEL_OUTCOME_UNKNOWN"
    HEARTBEAT_ACCEPTED = "HEARTBEAT_ACCEPTED"
    HEARTBEAT_ID_MISMATCH = "HEARTBEAT_ID_MISMATCH"
    HEARTBEAT_OUTCOME_UNKNOWN = "HEARTBEAT_OUTCOME_UNKNOWN"
    READ_OK = "READ_OK"
    READ_NOT_FOUND = "READ_NOT_FOUND"
    READ_FAILED = "READ_FAILED"
    GEOBLOCK_OK = "GEOBLOCK_OK"
    GEOBLOCK_FAILED = "GEOBLOCK_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REJECTED = "AUTH_REJECTED"
    PROTOCOL_RESPONSE_INVALID = "PROTOCOL_RESPONSE_INVALID"
    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    AUTH_REQUEST_BUILD_FAILED = "AUTH_REQUEST_BUILD_FAILED"


@dataclass(frozen=True, slots=True)
class RouteSpec:
    key: RouteKey
    host: str
    method: Literal["GET", "POST", "DELETE"]
    path_template: str
    auth_level: Literal["PUBLIC", "L2", "L2_AND_ORDER_SIGNATURE"]
    mutation: bool
    query_fields: tuple[str, ...]
    request_fields: tuple[str, ...]

    @property
    def path(self) -> str:
        return self.path_template


_PublicIdentifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$"),
]
_OptionalPublicIdentifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$"),
]
_AsciiInteger = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]*)$", max_length=256)]
_AsciiDecimal = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9][0-9]*)(?:\.[0-9]+)?$", max_length=256),
]
_EvmAddress = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{40}$")]
_TransactionHash = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{64}$")]
_PrintableText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4096, pattern=r"^[\x20-\x7e]+$"),
]
_OptionalLocation = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[\x20-\x7e]+$"),
]
_HeartbeatIdentifier = Annotated[
    str,
    StringConstraints(max_length=256, pattern=r"^[\x20-\x7e]*$"),
]


class _RouteRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: object) -> Self:
        del _fields_set, values
        raise ValueError("ROUTE_MODEL_CONSTRUCTION_INVALID") from None

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


class SubmitOrderRequest(_RouteRecord):
    route: Literal[RouteKey.SUBMIT_ORDER]
    intent: ExecutionIntent
    envelope: SignedOrderEnvelope

    @model_validator(mode="after")
    def _bind_intent_and_envelope(self) -> SubmitOrderRequest:
        if (
            self.envelope.intent_id != self.intent.intent_id
            or self.envelope.intent_fingerprint != self.intent.intent_fingerprint
            or self.envelope.protocol_version != self.intent.protocol_version
        ):
            raise ValueError("submitted envelope does not match intent")
        return self


class CancelOrderRequest(_RouteRecord):
    route: Literal[RouteKey.CANCEL_ORDER]
    order_id: _PublicIdentifier


class ReadOrderRequest(_RouteRecord):
    route: Literal[RouteKey.READ_ORDER]
    order_id: _PublicIdentifier

    @field_validator("order_id")
    @classmethod
    def _unambiguous_path_segment(cls, value: str) -> str:
        if "/" in value or "\\" in value:
            raise ValueError("order_id must be one path segment")
        return value


class ReadOpenOrdersRequest(_RouteRecord):
    route: Literal[RouteKey.READ_OPEN_ORDERS]
    id: _OptionalPublicIdentifier | None = None
    market: _OptionalPublicIdentifier | None = None
    asset_id: _AsciiInteger | None = None


class ReadTradesRequest(_RouteRecord):
    route: Literal[RouteKey.READ_TRADES]
    id: _OptionalPublicIdentifier | None = None
    market: _OptionalPublicIdentifier | None = None
    asset_id: _AsciiInteger | None = None
    maker_address: _EvmAddress | None = None
    after: Annotated[int, Field(ge=0)] | None = None
    before: Annotated[int, Field(ge=0)] | None = None


class ReadBalanceAllowanceRequest(_RouteRecord):
    route: Literal[RouteKey.READ_BALANCE_ALLOWANCE]
    signature_type: Literal[0]
    asset_type: Literal["COLLATERAL", "CONDITIONAL"]
    token_id: _AsciiInteger | None

    @model_validator(mode="after")
    def _token_matches_asset_type(self) -> ReadBalanceAllowanceRequest:
        if (self.asset_type == "CONDITIONAL") != (self.token_id is not None):
            raise ValueError("token_id is required only for CONDITIONAL assets")
        return self


class HeartbeatRequest(_RouteRecord):
    route: Literal[RouteKey.HEARTBEAT]
    heartbeat_id: _HeartbeatIdentifier


class GeoblockRequest(_RouteRecord):
    route: Literal[RouteKey.GEOBLOCK]


RouteRequest = Annotated[
    SubmitOrderRequest
    | CancelOrderRequest
    | ReadOrderRequest
    | ReadOpenOrdersRequest
    | ReadTradesRequest
    | ReadBalanceAllowanceRequest
    | HeartbeatRequest
    | GeoblockRequest,
    Field(discriminator="route"),
]


class OrderAckPayload(_RouteRecord):
    kind: Literal["ORDER_ACK"]
    order_id: _PublicIdentifier
    status: Literal["matched", "delayed", "live", "unmatched"]
    making_amount: _AsciiDecimal
    taking_amount: _AsciiDecimal
    transaction_hashes: Annotated[tuple[_TransactionHash, ...], Field(max_length=10_000)]
    trade_ids: Annotated[tuple[_PublicIdentifier, ...], Field(max_length=10_000)]


class CancellationPayload(_RouteRecord):
    kind: Literal["CANCELLATION"]
    order_id: _PublicIdentifier
    confirmation_required: Literal[True]


class HeartbeatAckPayload(_RouteRecord):
    kind: Literal["HEARTBEAT_ACK"]
    heartbeat_id: _HeartbeatIdentifier

    @field_validator("heartbeat_id")
    @classmethod
    def _nonempty_heartbeat_id(cls, value: str) -> str:
        if not value:
            raise ValueError("heartbeat acknowledgement requires an identifier")
        return value


class OrderReadPayload(_RouteRecord):
    kind: Literal["ORDER_READ"]
    id: _PublicIdentifier
    market: _PublicIdentifier
    asset_id: _AsciiInteger
    maker_address: _EvmAddress
    side: Literal["BUY", "SELL"]
    price: _AsciiDecimal
    original_size: _AsciiDecimal
    size_matched: _AsciiDecimal
    outcome: _PrintableText
    order_type: _PrintableText
    status: _PrintableText
    associate_trades: Annotated[tuple[_PublicIdentifier, ...], Field(max_length=10_000)] | None
    created_at: _AsciiInteger
    expiration: _AsciiInteger


class OrdersReadPayload(_RouteRecord):
    kind: Literal["ORDERS_READ"]
    items: Annotated[tuple[OrderReadPayload, ...], Field(max_length=10_000)]


class MakerOrderReadPayload(_RouteRecord):
    order_id: _PublicIdentifier
    maker_address: _EvmAddress
    matched_amount: _AsciiDecimal
    price: _AsciiDecimal
    fee_rate_bps: _AsciiInteger
    asset_id: _AsciiInteger
    outcome: _PrintableText
    outcome_index: Annotated[int, Field(ge=0)]
    side: Literal["BUY", "SELL"]


class TradeReadPayload(_RouteRecord):
    kind: Literal["TRADE_READ"]
    id: _PublicIdentifier
    market: _PublicIdentifier
    asset_id: _AsciiInteger
    maker_address: _EvmAddress
    taker_order_id: _PublicIdentifier
    side: Literal["BUY", "SELL"]
    trader_side: Literal["MAKER", "TAKER"]
    price: _AsciiDecimal
    size: _AsciiDecimal
    outcome: _PrintableText
    status: Literal[
        "MATCHED_NOT_BROADCASTED",
        "MATCHED",
        "MINED",
        "CONFIRMED",
        "RETRYING",
        "FAILED",
    ]
    fee_rate_bps: _AsciiInteger
    bucket_index: Annotated[int, Field(ge=0)]
    transaction_hash: _TransactionHash | None
    maker_orders: Annotated[tuple[MakerOrderReadPayload, ...], Field(max_length=10_000)]
    match_time: _AsciiInteger
    last_update: _AsciiInteger


class TradesReadPayload(_RouteRecord):
    kind: Literal["TRADES_READ"]
    items: Annotated[tuple[TradeReadPayload, ...], Field(max_length=10_000)]


class AllowanceEntry(_RouteRecord):
    address: _EvmAddress
    amount: _AsciiInteger


class BalanceAllowancePayload(_RouteRecord):
    kind: Literal["BALANCE_ALLOWANCE"]
    balance: _AsciiInteger
    allowances: Annotated[tuple[AllowanceEntry, ...], Field(max_length=10_000)]

    @field_validator("allowances")
    @classmethod
    def _sorted_unique_allowances(
        cls,
        value: tuple[AllowanceEntry, ...],
    ) -> tuple[AllowanceEntry, ...]:
        addresses = tuple(item.address for item in value)
        if addresses != tuple(sorted(addresses)) or len(addresses) != len(set(addresses)):
            raise ValueError("allowances must be sorted and unique")
        return value


class GeoblockResult(_RouteRecord):
    kind: Literal["GEOBLOCK"]
    blocked: bool
    country: _OptionalLocation | None
    region: _OptionalLocation | None


RoutePublicPayload = Annotated[
    OrderAckPayload
    | CancellationPayload
    | HeartbeatAckPayload
    | OrderReadPayload
    | OrdersReadPayload
    | TradesReadPayload
    | BalanceAllowancePayload
    | GeoblockResult,
    Field(discriminator="kind"),
]


def _route_specs() -> Mapping[RouteKey, RouteSpec]:
    routes = load_protocol_snapshot().routes
    selected = (
        (RouteKey.SUBMIT_ORDER, routes.place_order, True),
        (RouteKey.CANCEL_ORDER, routes.cancel_order, True),
        (RouteKey.READ_ORDER, routes.get_order, False),
        (RouteKey.READ_OPEN_ORDERS, routes.list_orders, False),
        (RouteKey.READ_TRADES, routes.list_trades, False),
        (RouteKey.READ_BALANCE_ALLOWANCE, routes.balance_allowance, False),
        (RouteKey.HEARTBEAT, routes.heartbeat, True),
        (RouteKey.GEOBLOCK, routes.geoblock, False),
    )
    return MappingProxyType(
        {
            key: RouteSpec(
                key=key,
                host=route.host,
                method=route.method,
                path_template=route.path,
                auth_level=route.auth_level,  # type: ignore[arg-type]
                mutation=mutation,
                query_fields=route.query_fields,
                request_fields=route.request_fields,
            )
            for key, route, mutation in selected
        }
    )


ROUTE_SPECS = _route_specs()
ROUTE_SET_HASH = canonical_execution_hash(
    {
        "schema_version": 1,
        "route_set_version": ROUTE_SET_VERSION,
        "routes": [
            {
                "key": route_key.value,
                "host": spec.host,
                "method": spec.method,
                "path_template": spec.path_template,
                "auth_level": spec.auth_level,
                "mutation": spec.mutation,
                "query_fields": list(spec.query_fields),
                "request_fields": list(spec.request_fields),
            }
            for route_key, spec in sorted(ROUTE_SPECS.items(), key=lambda item: item[0].value)
        ],
    }
)


def validate_route_result_evidence(
    *,
    route: RouteKey,
    code: RestCode,
    attempts: int,
    request_body_hash: str | None,
) -> None:
    """Reject attempt/body evidence impossible for the closed route specification."""
    build_failed = code is RestCode.AUTH_REQUEST_BUILD_FAILED
    if ROUTE_SPECS[route].mutation:
        expected_attempts = 0 if build_failed else 1
        expected_body_hash = not build_failed
        if attempts != expected_attempts or (request_body_hash is not None) != expected_body_hash:
            raise ValueError("REST_RESULT_EVIDENCE_INVALID") from None
        return
    if request_body_hash is not None:
        raise ValueError("REST_RESULT_EVIDENCE_INVALID") from None
    if build_failed:
        if attempts != 0:
            raise ValueError("REST_RESULT_EVIDENCE_INVALID") from None
    elif attempts not in {1, 2}:
        raise ValueError("REST_RESULT_EVIDENCE_INVALID") from None


def allowed_route_result_codes(route: RouteKey) -> frozenset[RestCode]:
    """Return the exact closed result-code set for one route."""
    read_codes = {
        RestCode.READ_OK,
        RestCode.READ_FAILED,
        RestCode.RATE_LIMITED,
        RestCode.AUTH_REJECTED,
        RestCode.PROTOCOL_RESPONSE_INVALID,
        RestCode.TRANSPORT_UNAVAILABLE,
        RestCode.AUTH_REQUEST_BUILD_FAILED,
    }
    return {
        RouteKey.SUBMIT_ORDER: frozenset(
            {
                RestCode.ORDER_ACK_MATCHED,
                RestCode.ORDER_ACK_DELAYED,
                RestCode.ORDER_ACK_LIVE_UNEXPECTED,
                RestCode.ORDER_ACK_UNMATCHED,
                RestCode.ORDER_OUTCOME_UNKNOWN,
                RestCode.AUTH_REJECTED,
                RestCode.AUTH_REQUEST_BUILD_FAILED,
            }
        ),
        RouteKey.CANCEL_ORDER: frozenset(
            {
                RestCode.CANCEL_ACKNOWLEDGED,
                RestCode.CANCEL_NOT_CONFIRMED,
                RestCode.CANCEL_OUTCOME_UNKNOWN,
                RestCode.AUTH_REJECTED,
                RestCode.AUTH_REQUEST_BUILD_FAILED,
            }
        ),
        RouteKey.HEARTBEAT: frozenset(
            {
                RestCode.HEARTBEAT_ACCEPTED,
                RestCode.HEARTBEAT_ID_MISMATCH,
                RestCode.HEARTBEAT_OUTCOME_UNKNOWN,
                RestCode.AUTH_REJECTED,
                RestCode.AUTH_REQUEST_BUILD_FAILED,
            }
        ),
        RouteKey.READ_ORDER: frozenset(read_codes | {RestCode.READ_NOT_FOUND}),
        RouteKey.READ_OPEN_ORDERS: frozenset(read_codes),
        RouteKey.READ_TRADES: frozenset(read_codes),
        RouteKey.READ_BALANCE_ALLOWANCE: frozenset(read_codes),
        RouteKey.GEOBLOCK: frozenset(
            {
                RestCode.GEOBLOCK_OK,
                RestCode.GEOBLOCK_FAILED,
                RestCode.AUTH_REJECTED,
                RestCode.PROTOCOL_RESPONSE_INVALID,
                RestCode.AUTH_REQUEST_BUILD_FAILED,
            }
        ),
    }[route]


def expected_route_result_flags(
    *,
    route: RouteKey,
    code: RestCode,
    payload: RoutePublicPayload | None,
) -> tuple[bool, bool]:
    """Return the route-aware recovery and kill requirements for one result."""
    authenticated_reads = {
        RouteKey.READ_ORDER,
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    }
    if route in authenticated_reads:
        if code in {RestCode.READ_OK, RestCode.READ_NOT_FOUND}:
            return False, False
        return True, True
    if route is RouteKey.GEOBLOCK:
        safe = (
            code is RestCode.GEOBLOCK_OK
            and isinstance(payload, GeoblockResult)
            and not payload.blocked
        )
        return (False, False) if safe else (True, True)
    if code in {RestCode.ORDER_ACK_MATCHED, RestCode.HEARTBEAT_ACCEPTED}:
        return False, False
    if code in {
        RestCode.CANCEL_ACKNOWLEDGED,
        RestCode.CANCEL_NOT_CONFIRMED,
        RestCode.HEARTBEAT_ID_MISMATCH,
    }:
        return True, False
    return True, True


__all__ = [
    "ROUTE_SET_HASH",
    "ROUTE_SET_VERSION",
    "ROUTE_SPECS",
    "AllowanceEntry",
    "BalanceAllowancePayload",
    "CancellationPayload",
    "GeoblockRequest",
    "GeoblockResult",
    "HeartbeatAckPayload",
    "MakerOrderReadPayload",
    "OrderAckPayload",
    "OrderReadPayload",
    "OrdersReadPayload",
    "ReadBalanceAllowanceRequest",
    "ReadOpenOrdersRequest",
    "ReadOrderRequest",
    "ReadTradesRequest",
    "RestCode",
    "RouteKey",
    "RoutePublicPayload",
    "RouteRequest",
    "RouteSpec",
    "TradeReadPayload",
    "TradesReadPayload",
    "allowed_route_result_codes",
    "expected_route_result_flags",
    "validate_route_result_evidence",
]
