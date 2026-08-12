import re
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

Decimal38x18 = Annotated[
    Decimal,
    Field(max_digits=38, decimal_places=18, allow_inf_nan=False),
]
LatencyDecimal18x6 = Annotated[
    Decimal,
    Field(max_digits=18, decimal_places=6, ge=Decimal(0), allow_inf_nan=False),
]


def normalize_utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


class StrictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator(
        "observed_at", "effective_at", "effective_from", "venue_timestamp", check_fields=False
    )
    @classmethod
    def require_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        return normalize_utc_timestamp(value)

    @field_validator("source_hash", check_fields=False)
    @classmethod
    def require_sha256_source_hash(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("source hash must contain 64 lowercase hexadecimal characters")
        return value


class Venue(StrEnum):
    HYPERLIQUID = "hyperliquid"
    BYBIT = "bybit"


class Asset(StrEnum):
    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"


class InstrumentKind(StrEnum):
    LINEAR_PERPETUAL = "linear_perpetual"


class RawEnvelope(StrictRecord):
    schema_version: Literal[1]
    event_id: UUID
    venue: Venue
    endpoint: str
    venue_timestamp: datetime | None
    observed_at: datetime
    received_monotonic_ns: int
    request_latency_ms: LatencyDecimal18x6
    source_version: str
    payload_json: str
    source_hash: str


class InstrumentSpec(StrictRecord):
    schema_version: Literal[1]
    instrument_id: str
    venue: Venue
    symbol: str
    asset: Asset
    kind: InstrumentKind
    contract_multiplier: Decimal38x18
    index_family: str | None
    oracle_family: str | None
    mark_method: str | None
    liquidation_method: str | None
    collateral_asset: str | None
    pnl_asset: str | None
    funding_formula_id: str | None
    funding_cap: Decimal38x18 | None
    funding_interval_hours: Decimal38x18
    funding_payment_offset_minutes: int | None
    min_notional: Decimal38x18 | None
    quantity_step: Decimal38x18 | None
    price_tick: Decimal38x18 | None
    is_inverse: bool
    is_prelaunch: bool
    observed_at: datetime
    source_hash: str

    @field_validator("contract_multiplier", "funding_interval_hours")
    @classmethod
    def require_positive_values(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("value must be positive")
        return value


class FundingObservation(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    symbol: str
    asset: Asset
    rate: Decimal38x18
    interval_hours: Decimal38x18
    effective_at: datetime
    observed_at: datetime
    source_hash: str

    @field_validator("interval_hours")
    @classmethod
    def require_positive_interval(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("interval must be positive")
        return value

    @computed_field
    @property
    def hourly_rate(self) -> Decimal:
        return self.rate / self.interval_hours


class MarketSnapshot(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    symbol: str
    asset: Asset
    bid: Decimal38x18
    ask: Decimal38x18
    mark: Decimal38x18
    index: Decimal38x18
    open_interest: Decimal38x18 | None
    effective_at: datetime
    observed_at: datetime
    source_hash: str

    @field_validator("bid", "ask", "mark", "index")
    @classmethod
    def require_positive_prices(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("price must be positive")
        return value

    @model_validator(mode="after")
    def require_non_crossed_quote(self) -> "MarketSnapshot":
        if self.ask < self.bid:
            raise ValueError("ask must be greater than or equal to bid")
        return self


class BookLevel(StrictRecord):
    price: Decimal38x18
    quantity: Decimal38x18
    order_count: int | None

    @field_validator("price", "quantity")
    @classmethod
    def require_positive_values(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("value must be positive")
        return value


class Level2BookSnapshot(StrictRecord):
    schema_version: Literal[1]
    cycle_id: UUID
    venue: Venue
    symbol: str
    asset: Asset
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    depth_limit: Literal[20]
    sequence: str | None
    effective_at: datetime
    observed_at: datetime
    source_hash: str

    @model_validator(mode="after")
    def require_valid_book(self) -> "Level2BookSnapshot":
        if not self.bids or not self.asks:
            raise ValueError("book must contain bids and asks")
        if any(
            left.price <= right.price
            for left, right in zip(self.bids[:-1], self.bids[1:], strict=True)
        ):
            raise ValueError("book bids must be in strictly descending price order")
        if any(
            left.price >= right.price
            for left, right in zip(self.asks[:-1], self.asks[1:], strict=True)
        ):
            raise ValueError("book asks must be in strictly ascending price order")
        if self.bids[0].price >= self.asks[0].price:
            raise ValueError("book top of book must not cross")
        return self


class FeeSchedule(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    tier_name: str
    maker_rate: Decimal38x18
    taker_rate: Decimal38x18
    effective_from: datetime
    observed_at: datetime
    source_url: str
    source_hash: str
