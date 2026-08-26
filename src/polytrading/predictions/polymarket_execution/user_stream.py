"""Strict, sanitized Polymarket authenticated-user-stream observations."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from threading import Lock, get_ident
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from polytrading.predictions.domain import PredictionVenue, Sha256, normalize_utc_timestamp
from polytrading.predictions.execution.authority import AuthorityDecision
from polytrading.predictions.execution.models import (
    VenueOrderEvent,
    VenueOrderState,
    VenueTradeEvent,
    VenueTradeState,
)
from polytrading.predictions.polymarket_execution.protocol import load_protocol_snapshot
from polytrading.predictions.polymarket_execution.routes import RouteKey
from polytrading.predictions.polymarket_execution.secrets import SecretMaterial

MAX_USER_STREAM_MESSAGE_BYTES = 1_048_576
_EVENT_ID_NAMESPACE = UUID("9ff728db-76f5-481f-bc3e-91e4eb2dad82")
_PUBLIC_IDENTIFIER = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$"),
]
_SECRET_IDENTIFIER = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4096, pattern=r"^[\x20-\x7e]+$"),
]
_ASCII_INTEGER = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]*)$", max_length=256)]
_ASCII_DECIMAL = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9][0-9]*)(?:\.[0-9]+)?$", max_length=256),
]
_EVM_ADDRESS = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{40}$")]
_TRANSACTION_HASH = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{64}$")]


class UserStreamProtocolError(ValueError):
    """A context-free rejection of one user-stream protocol observation."""

    def __init__(self) -> None:
        super().__init__("USER_STREAM_PROTOCOL_ERROR")


def _normalize_task9_datetime(value: object, *, error_code: str) -> datetime:
    invalid = type(value) is not datetime
    normalized: datetime | None = None
    if not invalid:
        try:
            normalized = normalize_utc_timestamp(value)
        except Exception:
            invalid = True
    if invalid or normalized is None:
        raise ValueError(error_code) from None
    return normalized


@dataclass(frozen=True, slots=True, init=False)
class _UserSubscriptionEvidence:
    frame_hash: Sha256
    protocol_version: str
    observed_at: datetime
    _initialized: bool = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        frame_hash: Sha256,
        protocol_version: str,
        observed_at: datetime,
    ) -> None:
        try:
            object.__getattribute__(self, "_initialized")
        except AttributeError:
            pass
        else:
            raise ValueError("USER_SUBSCRIPTION_EVIDENCE_INVALID") from None
        normalized_observed_at = _normalize_task9_datetime(
            observed_at,
            error_code="USER_SUBSCRIPTION_EVIDENCE_INVALID",
        )
        if (
            type(frame_hash) is not str
            or len(frame_hash) != 64
            or any(character not in "0123456789abcdef" for character in frame_hash)
            or type(protocol_version) is not str
            or protocol_version != load_protocol_snapshot().version
        ):
            raise ValueError("USER_SUBSCRIPTION_EVIDENCE_INVALID") from None
        object.__setattr__(self, "_initialized", True)
        object.__setattr__(self, "frame_hash", frame_hash)
        object.__setattr__(self, "protocol_version", protocol_version)
        object.__setattr__(self, "observed_at", normalized_observed_at)

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("USER_SUBSCRIPTION_EVIDENCE_NOT_SUBCLASSABLE") from None

    def __copy__(self) -> object:
        raise ValueError("USER_SUBSCRIPTION_EVIDENCE_INVALID") from None

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        del memo
        raise ValueError("USER_SUBSCRIPTION_EVIDENCE_INVALID") from None

    def __reduce__(self) -> object:
        raise ValueError("USER_SUBSCRIPTION_EVIDENCE_INVALID") from None

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise ValueError("USER_SUBSCRIPTION_EVIDENCE_INVALID") from None

    def __getstate__(self) -> object:
        raise ValueError("USER_SUBSCRIPTION_EVIDENCE_INVALID") from None


class _UserSubscriptionTransport(Protocol):
    def send_user_subscription(self, frame: bytes) -> None: ...


class _SignerUserStreamSession:
    """Signer-owned one-way authenticated subscription with sanitized evidence only."""

    __slots__ = (
        "_evidence",
        "_lock",
        "_owner_thread_id",
        "_read_guard",
        "_sealed",
        "_secrets",
        "_state",
        "_transport",
    )

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("USER_SUBSCRIPTION_SESSION_NOT_SUBCLASSABLE") from None

    def __init__(
        self,
        *,
        secrets: SecretMaterial,
        transport: _UserSubscriptionTransport,
        read_guard: Callable[[datetime], AuthorityDecision],
    ) -> None:
        initialized = False
        try:
            object.__getattribute__(self, "_sealed")
        except AttributeError:
            initialized = False
        else:
            initialized = True
        if initialized:
            raise UserStreamProtocolError() from None
        object.__setattr__(self, "_sealed", True)
        if type(secrets) is not SecretMaterial:
            raise TypeError("SECRET_MATERIAL_REQUIRED") from None
        sender = getattr(transport, "send_user_subscription", None)
        if not callable(sender):
            raise TypeError("USER_SUBSCRIPTION_TRANSPORT_REQUIRED") from None
        if not callable(read_guard):
            raise TypeError("USER_SUBSCRIPTION_READ_GUARD_REQUIRED") from None
        object.__setattr__(self, "_secrets", secrets)
        object.__setattr__(self, "_transport", transport)
        object.__setattr__(self, "_read_guard", read_guard)
        object.__setattr__(self, "_evidence", None)
        object.__setattr__(self, "_lock", Lock())
        object.__setattr__(self, "_owner_thread_id", get_ident())
        object.__setattr__(self, "_state", "READY")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("USER_SUBSCRIPTION_SESSION_IMMUTABLE") from None

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("USER_SUBSCRIPTION_SESSION_IMMUTABLE") from None

    def __repr__(self) -> str:
        return "_SignerUserStreamSession(<redacted>)"

    def __copy__(self) -> object:
        raise UserStreamProtocolError() from None

    def __deepcopy__(self, memo: dict[int, object]) -> object:
        del memo
        raise UserStreamProtocolError() from None

    def __reduce__(self) -> object:
        raise UserStreamProtocolError() from None

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise UserStreamProtocolError() from None

    def __getstate__(self) -> object:
        raise UserStreamProtocolError() from None

    def open(self, *, observed_at: datetime) -> _UserSubscriptionEvidence:
        if get_ident() != self._owner_thread_id:
            raise UserStreamProtocolError() from None
        with self._lock:
            if self._evidence is not None:
                return self._evidence
            if self._state != "READY":
                raise UserStreamProtocolError() from None
            object.__setattr__(self, "_state", "OPENING")
        invalid = False
        normalized: datetime | None = None
        decision: AuthorityDecision | None = None
        try:
            normalized = _normalize_task9_datetime(
                observed_at,
                error_code="USER_STREAM_PROTOCOL_ERROR",
            )
            decision = self._read_guard(normalized)
        except Exception:
            invalid = True
        if invalid or type(decision) is not AuthorityDecision or not decision.allowed:
            with self._lock:
                object.__setattr__(self, "_state", "FAILED")
            raise UserStreamProtocolError() from None

        frame: bytes | None = None
        try:
            frame = json.dumps(
                {
                    "auth": {
                        "apiKey": _subscription_secret_text(self._secrets.api_key),
                        "passphrase": _subscription_secret_text(self._secrets.passphrase),
                        "secret": _subscription_secret_text(self._secrets.api_secret),
                    },
                    "type": load_protocol_snapshot().websocket.subscription_type,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            self._transport.send_user_subscription(frame)
        except Exception:
            invalid = True
        if invalid or frame is None or normalized is None:
            with self._lock:
                object.__setattr__(self, "_state", "FAILED")
            raise UserStreamProtocolError() from None
        evidence: _UserSubscriptionEvidence | None = None
        try:
            evidence = _UserSubscriptionEvidence(
                frame_hash=sha256(frame).hexdigest(),
                protocol_version=load_protocol_snapshot().version,
                observed_at=normalized,
            )
        except Exception:
            invalid = True
        frame = None
        if invalid or evidence is None:
            with self._lock:
                object.__setattr__(self, "_state", "FAILED")
            raise UserStreamProtocolError() from None
        with self._lock:
            object.__setattr__(self, "_evidence", evidence)
            object.__setattr__(self, "_state", "SUCCEEDED")
        return evidence


def _subscription_secret_text(value: bytearray) -> str:
    if type(value) is not bytearray:
        raise ValueError("invalid subscription secret")
    encoded = bytes(value)
    text = encoded.decode("ascii")
    if not text or any(ord(character) < 0x20 or ord(character) > 0x7E for character in text):
        raise ValueError("invalid subscription secret")
    return text


_WIRE_MODELS_SEALED = False


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)

    def __init_subclass__(cls, **kwargs: object) -> None:
        if _WIRE_MODELS_SEALED:
            raise TypeError("USER_STREAM_WIRE_NOT_SUBCLASSABLE") from None
        super().__init_subclass__(**kwargs)

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: object) -> Self:
        del _fields_set, values
        raise ValueError("USER_STREAM_WIRE_CONSTRUCTION_INVALID") from None

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


class _MakerOrderWire(_WireModel):
    order_id: _PUBLIC_IDENTIFIER
    owner: _SECRET_IDENTIFIER
    maker_address: _EVM_ADDRESS
    matched_amount: _ASCII_DECIMAL
    price: _ASCII_DECIMAL
    fee_rate_bps: _ASCII_INTEGER
    asset_id: _ASCII_INTEGER
    outcome: _PUBLIC_IDENTIFIER
    outcome_index: Annotated[int, Field(ge=0)]
    side: Literal["BUY", "SELL"]


class _OrderWire(_WireModel):
    event_type: Literal["order"]
    id: _PUBLIC_IDENTIFIER
    owner: _SECRET_IDENTIFIER
    market: _PUBLIC_IDENTIFIER
    asset_id: _ASCII_INTEGER
    side: Literal["BUY", "SELL"]
    order_owner: _SECRET_IDENTIFIER
    original_size: _ASCII_DECIMAL
    size_matched: _ASCII_DECIMAL
    price: _ASCII_DECIMAL
    associate_trades: tuple[_PUBLIC_IDENTIFIER, ...] | None
    outcome: _PUBLIC_IDENTIFIER
    type: Literal["PLACEMENT", "UPDATE", "CANCELLATION"]
    created_at: _ASCII_INTEGER
    expiration: _ASCII_INTEGER
    order_type: _PUBLIC_IDENTIFIER
    status: Literal["LIVE", "MATCHED", "CANCELED"]
    maker_address: _EVM_ADDRESS
    timestamp: _ASCII_INTEGER


class _TradeWire(_WireModel):
    event_type: Literal["trade"]
    type: Literal["TRADE"]
    id: _PUBLIC_IDENTIFIER
    taker_order_id: _PUBLIC_IDENTIFIER
    market: _PUBLIC_IDENTIFIER
    asset_id: _ASCII_INTEGER
    side: Literal["BUY", "SELL"]
    size: _ASCII_DECIMAL
    fee_rate_bps: _ASCII_INTEGER
    price: _ASCII_DECIMAL
    status: Literal[
        "MATCHED_NOT_BROADCASTED",
        "MATCHED",
        "MINED",
        "CONFIRMED",
        "RETRYING",
        "FAILED",
    ]
    match_time: _ASCII_INTEGER
    last_update: _ASCII_INTEGER
    outcome: _PUBLIC_IDENTIFIER
    owner: _SECRET_IDENTIFIER
    trade_owner: _SECRET_IDENTIFIER
    maker_address: _EVM_ADDRESS
    transaction_hash: _TRANSACTION_HASH | None
    bucket_index: Annotated[int, Field(ge=0)]
    maker_orders: tuple[_MakerOrderWire, ...]
    trader_side: Literal["MAKER", "TAKER"]
    timestamp: _ASCII_INTEGER


_WIRE_MODELS_SEALED = True
_EVENT_ADAPTER = TypeAdapter(Annotated[_OrderWire | _TradeWire, Field(discriminator="event_type")])


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> object:
    raise ValueError("nonfinite JSON constant")


def _validated_wire(frame: bytes) -> _OrderWire | _TradeWire:
    invalid = False
    wire: _OrderWire | _TradeWire | None = None
    try:
        json.loads(
            frame,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
        wire = _EVENT_ADAPTER.validate_json(frame, strict=True)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError, OverflowError):
        invalid = True
    if invalid or wire is None:
        raise UserStreamProtocolError() from None
    return wire


@dataclass(frozen=True, slots=True, init=False)
class UserStreamHealth:
    status: Literal["CONNECTED", "RECOVERY_REQUIRED"]
    observed_at: datetime
    monotonic_at: float
    kill_reason: (
        Literal[
            "USER_STREAM_DISCONNECTED",
            "USER_STREAM_PROTOCOL_ERROR",
            "USER_STREAM_PING_MISSED",
            "USER_STREAM_PONG_MISSED",
        ]
        | None
    )
    required_reads: tuple[RouteKey, ...]
    next_ping_at: float
    pong_deadline_at: float | None
    recovery_phase: Literal[
        "NONE",
        "READS_REQUIRED",
        "DISCONNECTED_AWAITING_BOTH",
        "DISCONNECTED_AWAITING_RECONNECT",
        "DISCONNECTED_AWAITING_READS",
    ] = "NONE"
    _initialized: bool = field(init=False, repr=False, compare=False)
    _pending_ping_sent_at: float | None = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        status: Literal["CONNECTED", "RECOVERY_REQUIRED"],
        observed_at: datetime,
        monotonic_at: float,
        kill_reason: (
            Literal[
                "USER_STREAM_DISCONNECTED",
                "USER_STREAM_PROTOCOL_ERROR",
                "USER_STREAM_PING_MISSED",
                "USER_STREAM_PONG_MISSED",
            ]
            | None
        ),
        required_reads: tuple[RouteKey, ...],
        next_ping_at: float,
        pong_deadline_at: float | None,
        recovery_phase: Literal[
            "NONE",
            "READS_REQUIRED",
            "DISCONNECTED_AWAITING_BOTH",
            "DISCONNECTED_AWAITING_RECONNECT",
            "DISCONNECTED_AWAITING_READS",
        ] = "NONE",
    ) -> None:
        initialized = False
        try:
            object.__getattribute__(self, "_initialized")
        except AttributeError:
            initialized = False
        else:
            initialized = True
        if initialized:
            raise ValueError("USER_STREAM_HEALTH_INVALID") from None
        normalized_observed_at = _normalize_task9_datetime(
            observed_at,
            error_code="USER_STREAM_HEALTH_INVALID",
        )
        object.__setattr__(self, "_initialized", True)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "observed_at", normalized_observed_at)
        object.__setattr__(self, "monotonic_at", monotonic_at)
        object.__setattr__(self, "kill_reason", kill_reason)
        object.__setattr__(self, "required_reads", required_reads)
        object.__setattr__(self, "next_ping_at", next_ping_at)
        object.__setattr__(self, "pong_deadline_at", pong_deadline_at)
        object.__setattr__(self, "recovery_phase", recovery_phase)
        object.__setattr__(self, "_pending_ping_sent_at", None)
        self.__post_init__()

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("USER_STREAM_HEALTH_NOT_SUBCLASSABLE") from None

    def __post_init__(self) -> None:
        invalid = (
            type(self) is not UserStreamHealth
            or type(self.status) is not str
            or type(self.observed_at) is not datetime
            or type(self.monotonic_at) is not float
            or type(self.kill_reason) not in {str, type(None)}
            or type(self.required_reads) is not tuple
            or any(type(route) is not RouteKey for route in self.required_reads)
            or type(self.next_ping_at) is not float
            or type(self.pong_deadline_at) not in {float, type(None)}
            or type(self.recovery_phase) is not str
            or type(self._pending_ping_sent_at) not in {float, type(None)}
        )
        interval = float(load_protocol_snapshot().websocket.ping_interval_seconds)
        if not invalid:
            invalid = (
                not math.isfinite(self.monotonic_at)
                or self.monotonic_at < 0
                or not math.isfinite(self.next_ping_at)
                or self.next_ping_at < self.monotonic_at
                or self.next_ping_at - self.monotonic_at > interval
                or self.status not in {"CONNECTED", "RECOVERY_REQUIRED"}
                or self.kill_reason
                not in {
                    None,
                    "USER_STREAM_DISCONNECTED",
                    "USER_STREAM_PROTOCOL_ERROR",
                    "USER_STREAM_PING_MISSED",
                    "USER_STREAM_PONG_MISSED",
                }
                or self.recovery_phase
                not in {
                    "NONE",
                    "READS_REQUIRED",
                    "DISCONNECTED_AWAITING_BOTH",
                    "DISCONNECTED_AWAITING_RECONNECT",
                    "DISCONNECTED_AWAITING_READS",
                }
            )
        if not invalid and self.pong_deadline_at is not None:
            invalid = (
                self._pending_ping_sent_at is None
                or not math.isfinite(self.pong_deadline_at)
                or self.pong_deadline_at <= self.monotonic_at
                or self.pong_deadline_at != self.next_ping_at
            )
            if not invalid and self._pending_ping_sent_at is not None:
                invalid = (
                    not math.isfinite(self._pending_ping_sent_at)
                    or self._pending_ping_sent_at < 0
                    or self._pending_ping_sent_at > self.monotonic_at
                    or self.pong_deadline_at - self._pending_ping_sent_at != interval
                    or self._pending_ping_sent_at + interval != self.pong_deadline_at
                )
        elif not invalid:
            invalid = self._pending_ping_sent_at is not None
        if not invalid and self.status == "CONNECTED":
            invalid = (
                self.kill_reason is not None
                or self.required_reads != ()
                or self.recovery_phase != "NONE"
                or not _has_representable_cadence_origin(self.next_ping_at)
            )
        elif not invalid:
            invalid = (
                self.kill_reason is None
                or self.required_reads != _RECOVERY_READS
                or self.pong_deadline_at is not None
            )
            if not invalid and self.kill_reason == "USER_STREAM_DISCONNECTED":
                invalid = self.recovery_phase not in {
                    "DISCONNECTED_AWAITING_BOTH",
                    "DISCONNECTED_AWAITING_RECONNECT",
                    "DISCONNECTED_AWAITING_READS",
                }
            elif not invalid:
                invalid = self.recovery_phase != "READS_REQUIRED"
        if invalid:
            raise ValueError("USER_STREAM_HEALTH_INVALID") from None

    @classmethod
    def connected(
        cls,
        observed_at: datetime,
        *,
        monotonic_at: float = 0.0,
    ) -> UserStreamHealth:
        if cls is not UserStreamHealth:
            raise TypeError("USER_STREAM_HEALTH_NOT_SUBCLASSABLE") from None
        monotonic = _finite_monotonic(monotonic_at)
        return cls(
            status="CONNECTED",
            observed_at=_normalize_task9_datetime(
                observed_at,
                error_code="USER_STREAM_HEALTH_INVALID",
            ),
            monotonic_at=monotonic,
            kill_reason=None,
            required_reads=(),
            next_ping_at=_representable_cadence_after(monotonic),
            pong_deadline_at=None,
            recovery_phase="NONE",
        )

    def _kill(
        self,
        reason: Literal[
            "USER_STREAM_DISCONNECTED",
            "USER_STREAM_PROTOCOL_ERROR",
            "USER_STREAM_PING_MISSED",
            "USER_STREAM_PONG_MISSED",
        ],
        observed_at: datetime,
        monotonic_at: float,
    ) -> UserStreamHealth:
        if self.status == "RECOVERY_REQUIRED":
            return self
        monotonic = self._checked_observation(monotonic_at)
        if monotonic is None:
            monotonic = self.monotonic_at
        return type(self)(
            status="RECOVERY_REQUIRED",
            observed_at=_normalize_task9_datetime(
                observed_at,
                error_code="USER_STREAM_HEALTH_INVALID",
            ),
            monotonic_at=monotonic,
            kill_reason=reason,
            required_reads=_RECOVERY_READS,
            next_ping_at=max(self.next_ping_at, monotonic),
            pong_deadline_at=None,
            recovery_phase=(
                "DISCONNECTED_AWAITING_BOTH"
                if reason == "USER_STREAM_DISCONNECTED"
                else "READS_REQUIRED"
            ),
        )

    def on_disconnect(
        self,
        observed_at: datetime,
        *,
        monotonic_at: float | None = None,
    ) -> UserStreamHealth:
        return self._kill(
            "USER_STREAM_DISCONNECTED",
            observed_at,
            self.monotonic_at if monotonic_at is None else monotonic_at,
        )

    def on_protocol_error(
        self,
        observed_at: datetime,
        *,
        monotonic_at: float,
    ) -> UserStreamHealth:
        return self._kill(
            "USER_STREAM_PROTOCOL_ERROR",
            observed_at,
            monotonic_at,
        )

    def on_reconnect(
        self,
        observed_at: datetime,
        *,
        monotonic_at: float,
    ) -> UserStreamHealth:
        monotonic = self._checked_observation(monotonic_at)
        if monotonic is None:
            return self._kill(
                "USER_STREAM_PROTOCOL_ERROR",
                observed_at,
                self.monotonic_at,
            )
        if self.status == "CONNECTED":
            return self
        if self.status != "RECOVERY_REQUIRED":
            raise ValueError("USER_STREAM_RECOVERY_NOT_REQUIRED") from None
        if self.recovery_phase == "DISCONNECTED_AWAITING_RECONNECT":
            return type(self).connected(observed_at, monotonic_at=monotonic)
        next_phase = (
            "DISCONNECTED_AWAITING_READS"
            if self.recovery_phase == "DISCONNECTED_AWAITING_BOTH"
            else self.recovery_phase
        )
        return type(self)(
            status=self.status,
            observed_at=_normalize_task9_datetime(
                observed_at,
                error_code="USER_STREAM_HEALTH_INVALID",
            ),
            monotonic_at=monotonic,
            kill_reason=self.kill_reason,
            required_reads=self.required_reads,
            next_ping_at=max(self.next_ping_at, monotonic),
            pong_deadline_at=None,
            recovery_phase=next_phase,
        )

    def on_authoritative_reads_completed(
        self,
        reads: tuple[RouteKey, ...],
        *,
        observed_at: datetime,
        monotonic_at: float,
    ) -> UserStreamHealth:
        if reads != _RECOVERY_READS:
            raise ValueError("USER_STREAM_RECOVERY_READS_INCOMPLETE") from None
        monotonic = self._checked_observation(monotonic_at)
        if monotonic is None:
            return self._kill(
                "USER_STREAM_PROTOCOL_ERROR",
                observed_at,
                self.monotonic_at,
            )
        if self.status == "CONNECTED":
            return self
        if self.status != "RECOVERY_REQUIRED":
            raise ValueError("USER_STREAM_RECOVERY_READS_INCOMPLETE") from None
        if self.recovery_phase in {"READS_REQUIRED", "DISCONNECTED_AWAITING_READS"}:
            return type(self).connected(observed_at, monotonic_at=monotonic)
        next_phase = (
            "DISCONNECTED_AWAITING_RECONNECT"
            if self.recovery_phase == "DISCONNECTED_AWAITING_BOTH"
            else self.recovery_phase
        )
        return type(self)(
            status=self.status,
            observed_at=_normalize_task9_datetime(
                observed_at,
                error_code="USER_STREAM_HEALTH_INVALID",
            ),
            monotonic_at=monotonic,
            kill_reason=self.kill_reason,
            required_reads=self.required_reads,
            next_ping_at=max(self.next_ping_at, monotonic),
            pong_deadline_at=None,
            recovery_phase=next_phase,
        )

    def _checked_observation(self, monotonic_at: float) -> float | None:
        try:
            monotonic = _finite_monotonic(monotonic_at)
        except ValueError:
            return None
        return monotonic if monotonic >= self.monotonic_at else None

    def _advance_connected(
        self,
        *,
        observed_at: datetime,
        monotonic_at: float,
        next_ping_at: float | None = None,
        pong_deadline_at: float | None | Literal[False] = False,
        pending_ping_sent_at: float | None | Literal[False] = False,
    ) -> UserStreamHealth:
        deadline = self.pong_deadline_at if pong_deadline_at is False else pong_deadline_at
        ping_sent_at = (
            self._pending_ping_sent_at if pending_ping_sent_at is False else pending_ping_sent_at
        )
        health = type(self)(
            status="CONNECTED",
            observed_at=_normalize_task9_datetime(
                observed_at,
                error_code="USER_STREAM_HEALTH_INVALID",
            ),
            monotonic_at=monotonic_at,
            kill_reason=None,
            required_reads=(),
            next_ping_at=self.next_ping_at if next_ping_at is None else next_ping_at,
            pong_deadline_at=None,
            recovery_phase="NONE",
        )
        if deadline is None:
            if ping_sent_at is not None:
                raise ValueError("USER_STREAM_HEALTH_INVALID") from None
            return health
        if ping_sent_at is None:
            raise ValueError("USER_STREAM_HEALTH_INVALID") from None
        object.__setattr__(health, "_pending_ping_sent_at", ping_sent_at)
        object.__setattr__(health, "pong_deadline_at", deadline)
        health.__post_init__()
        return health

    def ping_due(self, monotonic_at: float) -> bool:
        monotonic = _finite_monotonic(monotonic_at)
        if monotonic < self.monotonic_at:
            raise ValueError("USER_STREAM_MONOTONIC_REGRESSION") from None
        return (
            self.status == "CONNECTED"
            and self.pong_deadline_at is None
            and _ping_due(monotonic, self.next_ping_at)
        )

    def on_ping_sent(
        self,
        frame: bytes,
        *,
        observed_at: datetime,
        monotonic_at: float,
    ) -> UserStreamHealth:
        monotonic = self._checked_observation(monotonic_at)
        if monotonic is None or type(frame) is not bytes or frame != b"PING":
            return self._kill(
                "USER_STREAM_PROTOCOL_ERROR",
                observed_at,
                self.monotonic_at,
            )
        if self.status != "CONNECTED" or self.pong_deadline_at is not None:
            return self._kill("USER_STREAM_PROTOCOL_ERROR", observed_at, monotonic)
        if _ping_missed(monotonic, self.next_ping_at):
            return self._kill("USER_STREAM_PING_MISSED", observed_at, monotonic)
        if not _ping_due(monotonic, self.next_ping_at):
            return self._kill("USER_STREAM_PROTOCOL_ERROR", observed_at, monotonic)
        cadence_invalid = False
        next_ping_at = 0.0
        pong_deadline_at = 0.0
        try:
            next_ping_at = _representable_cadence_after(self.next_ping_at)
            pong_deadline_at = _representable_cadence_after(monotonic)
        except ValueError:
            cadence_invalid = True
        if cadence_invalid:
            return self._kill("USER_STREAM_PROTOCOL_ERROR", observed_at, monotonic)
        return self._advance_connected(
            observed_at=observed_at,
            monotonic_at=monotonic,
            next_ping_at=next_ping_at,
            pong_deadline_at=pong_deadline_at,
            pending_ping_sent_at=monotonic,
        )

    def on_pong(
        self,
        frame: bytes,
        *,
        observed_at: datetime,
        monotonic_at: float,
    ) -> UserStreamHealth:
        monotonic = self._checked_observation(monotonic_at)
        if monotonic is None or type(frame) is not bytes or frame != b"PONG":
            return self._kill(
                "USER_STREAM_PROTOCOL_ERROR",
                observed_at,
                self.monotonic_at,
            )
        if self.status != "CONNECTED" or self.pong_deadline_at is None:
            return self._kill("USER_STREAM_PROTOCOL_ERROR", observed_at, monotonic)
        if _pong_missed(monotonic, self.pong_deadline_at):
            return self._kill("USER_STREAM_PONG_MISSED", observed_at, monotonic)
        return self._advance_connected(
            observed_at=observed_at,
            monotonic_at=monotonic,
            pong_deadline_at=None,
            pending_ping_sent_at=None,
        )

    def on_event_observed(
        self,
        *,
        observed_at: datetime,
        monotonic_at: float,
    ) -> UserStreamHealth:
        monotonic = self._checked_observation(monotonic_at)
        if monotonic is None:
            return self._kill(
                "USER_STREAM_PROTOCOL_ERROR",
                observed_at,
                self.monotonic_at,
            )
        return self.check_deadlines(
            observed_at=observed_at,
            monotonic_at=monotonic,
        )

    def check_deadlines(
        self,
        *,
        observed_at: datetime,
        monotonic_at: float,
    ) -> UserStreamHealth:
        monotonic = self._checked_observation(monotonic_at)
        if monotonic is None:
            return self._kill(
                "USER_STREAM_PROTOCOL_ERROR",
                observed_at,
                self.monotonic_at,
            )
        if self.status != "CONNECTED":
            return self
        if self.pong_deadline_at is not None and _pong_missed(monotonic, self.pong_deadline_at):
            return self._kill("USER_STREAM_PONG_MISSED", observed_at, monotonic)
        if self.pong_deadline_at is None and _ping_missed(monotonic, self.next_ping_at):
            return self._kill("USER_STREAM_PING_MISSED", observed_at, monotonic)
        return self._advance_connected(
            observed_at=observed_at,
            monotonic_at=monotonic,
        )


_RECOVERY_READS = (
    RouteKey.READ_OPEN_ORDERS,
    RouteKey.READ_TRADES,
    RouteKey.READ_BALANCE_ALLOWANCE,
)


def _finite_monotonic(value: float) -> float:
    invalid = type(value) not in {int, float} or isinstance(value, bool)
    monotonic = 0.0
    if not invalid:
        try:
            monotonic = float(value)
        except (OverflowError, ValueError):
            invalid = True
    if invalid or not math.isfinite(monotonic) or monotonic < 0:
        raise ValueError("USER_STREAM_MONOTONIC_INVALID") from None
    return monotonic


def _representable_cadence_after(monotonic_at: float) -> float:
    interval = float(load_protocol_snapshot().websocket.ping_interval_seconds)
    deadline = monotonic_at + interval
    if (
        not math.isfinite(deadline)
        or deadline <= monotonic_at
        or deadline - monotonic_at != interval
    ):
        raise ValueError("USER_STREAM_HEALTH_INVALID") from None
    return deadline


def _has_representable_cadence_origin(deadline: float) -> bool:
    interval = float(load_protocol_snapshot().websocket.ping_interval_seconds)
    origin = deadline - interval
    return (
        math.isfinite(origin)
        and origin >= 0
        and origin < deadline
        and origin + interval == deadline
        and deadline - origin == interval
    )


def _ping_due(monotonic_at: float, deadline: float) -> bool:
    return monotonic_at >= deadline


def _ping_missed(monotonic_at: float, deadline: float) -> bool:
    return monotonic_at > deadline


def _pong_missed(monotonic_at: float, deadline: float) -> bool:
    return monotonic_at >= deadline


def _read_injected_monotonic(
    clock: Callable[[], float],
    *,
    previous: float | None = None,
) -> tuple[float, bool]:
    invalid = False
    observed = 0.0
    try:
        observed = _finite_monotonic(clock())
    except Exception:
        invalid = True
    if invalid:
        if previous is not None:
            return previous, False
        raise UserStreamProtocolError() from None
    if previous is not None and observed < previous:
        return previous, False
    return observed, True


def recovery_reads_after_stream_gap(health: UserStreamHealth) -> tuple[RouteKey, ...]:
    return health.required_reads


class UserStreamParser:
    """Bounded stateful evidence parser with no raw-frame retention."""

    __slots__ = (
        "_health",
        "_latest_timestamps",
        "_max_event_identities",
        "_monotonic",
        "_seen_evidence",
    )

    def __init__(
        self,
        *,
        connected_at: datetime,
        monotonic: Callable[[], float],
        max_event_identities: int = 10_000,
    ) -> None:
        if not callable(monotonic):
            raise TypeError("USER_STREAM_MONOTONIC_CLOCK_REQUIRED") from None
        if type(max_event_identities) is not int or not 1 <= max_event_identities <= 10_000:
            raise ValueError("USER_STREAM_IDENTITY_LIMIT_INVALID") from None
        initial_monotonic, _ = _read_injected_monotonic(monotonic)
        health: UserStreamHealth | None = None
        invalid = False
        try:
            health = UserStreamHealth.connected(
                connected_at,
                monotonic_at=initial_monotonic,
            )
        except ValueError:
            invalid = True
        if invalid or health is None:
            raise UserStreamProtocolError() from None
        self._health = health
        self._monotonic = monotonic
        self._max_event_identities = max_event_identities
        self._seen_evidence: dict[tuple[str, str, str], str] = {}
        self._latest_timestamps: dict[tuple[str, str], int] = {}

    @property
    def health(self) -> UserStreamHealth:
        return self._health

    def __repr__(self) -> str:
        return (
            "UserStreamParser("
            f"status={self._health.status!r}, identities={len(self._seen_evidence)})"
        )

    def parse(
        self,
        frame: bytes,
        *,
        receipt_time: datetime,
    ) -> VenueOrderEvent | VenueTradeEvent | None:
        if self._health.kill_reason is not None:
            raise UserStreamProtocolError() from None
        invalid = False
        event: VenueOrderEvent | VenueTradeEvent | None = None
        wire: _OrderWire | _TradeWire | None = None
        try:
            event = parse_user_event(frame, receipt_time=receipt_time)
            wire = _validated_wire(frame)
        except UserStreamProtocolError:
            invalid = True
        if invalid or event is None or wire is None:
            self._kill_protocol(receipt_time)
            raise UserStreamProtocolError() from None

        monotonic_at, clock_valid = _read_injected_monotonic(
            self._monotonic,
            previous=self._health.monotonic_at,
        )
        if not clock_valid:
            self._install_protocol_kill(receipt_time, monotonic_at=monotonic_at)
            raise UserStreamProtocolError() from None
        self._health = self._health.on_event_observed(
            observed_at=receipt_time,
            monotonic_at=monotonic_at,
        )
        if self._health.kill_reason is not None:
            raise UserStreamProtocolError() from None

        identity = (wire.event_type, wire.id, wire.timestamp)
        existing_hash = self._seen_evidence.get(identity)
        if existing_hash is not None:
            if existing_hash == event.raw_event_hash:
                return None
            self._kill_protocol(receipt_time)
            raise UserStreamProtocolError() from None
        occurrence = (wire.event_type, wire.id)
        timestamp = int(wire.timestamp)
        latest = self._latest_timestamps.get(occurrence)
        if latest is not None and timestamp < latest:
            self._kill_protocol(receipt_time)
            raise UserStreamProtocolError() from None
        if len(self._seen_evidence) >= self._max_event_identities:
            self._kill_protocol(receipt_time)
            raise UserStreamProtocolError() from None
        self._seen_evidence[identity] = event.raw_event_hash
        self._latest_timestamps[occurrence] = timestamp
        return event

    def _kill_protocol(self, observed_at: datetime) -> None:
        monotonic_at, _ = _read_injected_monotonic(
            self._monotonic,
            previous=self._health.monotonic_at,
        )
        self._install_protocol_kill(observed_at, monotonic_at=monotonic_at)

    def _install_protocol_kill(self, observed_at: datetime, *, monotonic_at: float) -> None:
        try:
            normalized = _normalize_task9_datetime(
                observed_at,
                error_code="USER_STREAM_HEALTH_INVALID",
            )
        except ValueError:
            normalized = self._health.observed_at
        self._health = self._health.on_protocol_error(
            normalized,
            monotonic_at=monotonic_at,
        )

    def on_gap(self, *, observed_at: datetime) -> None:
        self._kill_protocol(observed_at)


def _millisecond_timestamp(value: str) -> datetime:
    milliseconds = int(value)
    seconds, remainder = divmod(milliseconds, 1000)
    return datetime.fromtimestamp(seconds, UTC) + timedelta(milliseconds=remainder)


def _event_uuid(*, kind: str, venue_id: str, timestamp: str, raw_event_hash: str) -> UUID:
    identity = json.dumps(
        {
            "kind": kind,
            "raw_event_hash": raw_event_hash,
            "timestamp": timestamp,
            "venue_id": venue_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_EVENT_ID_NAMESPACE, identity)


def _order_state(wire: _OrderWire) -> VenueOrderState:
    original_size = Decimal(wire.original_size)
    size_matched = Decimal(wire.size_matched)
    if wire.type == "PLACEMENT" and wire.status == "LIVE" and size_matched == 0:
        return VenueOrderState.ACK_LIVE_UNEXPECTED
    if (
        wire.type == "UPDATE"
        and wire.status in {"LIVE", "MATCHED"}
        and 0 < size_matched <= original_size
    ):
        return (
            VenueOrderState.PARTIALLY_FILLED
            if size_matched < original_size
            else VenueOrderState.FILLED
        )
    if (
        wire.type == "CANCELLATION"
        and wire.status == "CANCELED"
        and 0 <= size_matched <= original_size
    ):
        return VenueOrderState.CANCELLED
    raise ValueError("unsupported order transition")


def parse_user_event(
    frame: bytes,
    *,
    receipt_time: datetime,
) -> VenueOrderEvent | VenueTradeEvent:
    """Parse one exact frozen user event into a venue-neutral lifecycle fact."""
    if type(frame) is not bytes or not frame or len(frame) > MAX_USER_STREAM_MESSAGE_BYTES:
        raise UserStreamProtocolError() from None
    wire = _validated_wire(frame)
    invalid_metadata = False
    try:
        received_at = _normalize_task9_datetime(
            receipt_time,
            error_code="USER_STREAM_PROTOCOL_ERROR",
        )
        venue_timestamp = _millisecond_timestamp(wire.timestamp)
    except (ValueError, TypeError, OverflowError, OSError):
        invalid_metadata = True
        received_at = None
        venue_timestamp = None
    if invalid_metadata or received_at is None or venue_timestamp is None:
        raise UserStreamProtocolError() from None
    raw_event_hash = sha256(frame).hexdigest()
    event_id = _event_uuid(
        kind=wire.event_type,
        venue_id=wire.id,
        timestamp=wire.timestamp,
        raw_event_hash=raw_event_hash,
    )
    protocol_version = load_protocol_snapshot().version
    if isinstance(wire, _OrderWire):
        state: VenueOrderState | None = None
        with suppress(ValueError, TypeError):
            state = _order_state(wire)
        if state is None:
            raise UserStreamProtocolError() from None
        return VenueOrderEvent(
            schema_version=1,
            event_id=event_id,
            venue=PredictionVenue.POLYMARKET,
            raw_event_hash=raw_event_hash,
            source_channel="polymarket-user",
            venue_order_id=wire.id,
            intent_id=None,
            original_venue_state=wire.status,
            normalized_state=state,
            terminal=state in {VenueOrderState.FILLED, VenueOrderState.CANCELLED},
            venue_timestamp=venue_timestamp,
            received_at=received_at,
            sequence_number=None,
            protocol_version=protocol_version,
        )
    state = VenueTradeState(wire.status)
    return VenueTradeEvent(
        schema_version=1,
        trade_event_id=event_id,
        venue=PredictionVenue.POLYMARKET,
        raw_event_hash=raw_event_hash,
        source_channel="polymarket-user",
        venue_trade_id=wire.id,
        venue_order_id=wire.taker_order_id if wire.trader_side == "TAKER" else None,
        intent_id=None,
        original_venue_state=wire.status,
        normalized_state=state,
        terminal=state in {VenueTradeState.CONFIRMED, VenueTradeState.FAILED},
        venue_timestamp=venue_timestamp,
        received_at=received_at,
        sequence_number=None,
        protocol_version=protocol_version,
    )


__all__ = [
    "MAX_USER_STREAM_MESSAGE_BYTES",
    "UserStreamHealth",
    "UserStreamParser",
    "UserStreamProtocolError",
    "parse_user_event",
    "recovery_reads_after_stream_gap",
]
