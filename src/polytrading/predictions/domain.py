from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
ProbabilityDecimal = Annotated[Decimal, Field(gt=0, lt=1, allow_inf_nan=False)]

_SHA256 = re.compile(r"[0-9a-f]{64}")

_TIMESTAMP_FIELDS = (
    "venue_timestamp",
    "observed_at",
    "start_at",
    "end_at",
    "information_cutoff",
    "retrieved_at",
    "effective_at",
    "reviewed_at",
    "latest_market_retrieved_at",
    "latest_book_observed_at",
    "as_of",
)


def normalize_utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


class PredictionRecord(BaseModel):
    """Strict base for the prediction-market domain.

    Deliberately independent of ``polytrading.domain.models``: this package must never share a
    type with the perpetual-futures domain, so the two systems cannot accidentally interoperate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator(*_TIMESTAMP_FIELDS, check_fields=False)
    @classmethod
    def _require_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        return normalize_utc_timestamp(value)

    @field_validator("source_hash", "raw_hash", "normalized_hash", check_fields=False)
    @classmethod
    def _require_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("source hash must contain 64 lowercase hexadecimal characters")
        return value


class PredictionVenue(StrEnum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"
    LIMITLESS = "limitless"


class PredictionSource(StrEnum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"
    LIMITLESS = "limitless"


class PredictionRawEnvelope(PredictionRecord):
    schema_version: Literal[1]
    event_id: UUID
    venue: PredictionVenue
    endpoint: str
    venue_timestamp: datetime | None
    observed_at: datetime
    received_monotonic_ns: int
    request_latency_ms: NonNegativeDecimal
    source_version: str
    payload_json: str
    source_hash: Sha256


class MarketRecord(PredictionRecord):
    schema_version: Literal[1]
    market_id: str
    venue: PredictionVenue
    underlying_exchange: str | None
    event_id: str | None
    question: str
    slug: str | None
    outcomes: tuple[str, ...]
    outcome_token_ids: tuple[str, ...] | None
    negative_risk: bool | None
    active: bool
    closed: bool
    restricted: bool
    order_book_enabled: bool
    start_at: datetime | None
    end_at: datetime | None
    resolution_source: str | None
    rule_version_id: UUID
    information_cutoff: datetime
    source_url: str
    retrieved_at: datetime
    raw_hash: Sha256
    normalized_hash: Sha256

    @field_validator("outcomes")
    @classmethod
    def _require_nonempty_outcomes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("market must declare at least one outcome")
        return value

    @model_validator(mode="after")
    def _require_matching_token_count(self) -> MarketRecord:
        """Validate outcome/token alignment and the venue-specific negative_risk contract.

        ``negative_risk`` is Polymarket/Limitless-specific (both use conditional-token
        markets); Kalshi has no such concept, so a Kalshi record must leave it unset.
        """
        if self.outcome_token_ids is not None and len(self.outcome_token_ids) != len(self.outcomes):
            raise ValueError("outcome token IDs must align one-to-one with outcomes")
        if self.venue is PredictionVenue.KALSHI and self.negative_risk is not None:
            raise ValueError("negative_risk is Polymarket/Limitless-specific; unknown for Kalshi")
        return self


class RuleVersion(PredictionRecord):
    schema_version: Literal[1]
    rule_version_id: UUID
    market_id: str
    venue: PredictionVenue
    question: str
    description: str
    resolution_source: str | None
    outcomes: tuple[str, ...]
    superseded_rule_version_id: UUID | None
    effective_at: datetime
    source_hash: Sha256

    @field_validator("outcomes")
    @classmethod
    def _require_nonempty_outcomes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("rule version must declare at least one outcome")
        return value


class TradeRecord(PredictionRecord):
    schema_version: Literal[1]
    venue: PredictionVenue
    market_id: str
    outcome_token_id: str | None
    trade_id: str
    price: ProbabilityDecimal
    size: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    side: Literal["buy", "sell"] | None
    effective_at: datetime
    observed_at: datetime
    source_hash: Sha256


class PredictionBookLevel(PredictionRecord):
    price: ProbabilityDecimal
    size: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class PredictionBookSnapshot(PredictionRecord):
    schema_version: Literal[1]
    cycle_id: UUID
    venue: PredictionVenue
    market_id: str
    outcome_token_id: str | None
    bids: tuple[PredictionBookLevel, ...]
    asks: tuple[PredictionBookLevel, ...]
    sequence: str | None
    effective_at: datetime
    observed_at: datetime
    source_hash: Sha256

    @model_validator(mode="after")
    def _require_valid_book(self) -> PredictionBookSnapshot:
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


class PredictionFeeRate(PredictionRecord):
    schema_version: Literal[1]
    venue: PredictionVenue
    market_id: str | None
    maker_rate: NonNegativeDecimal
    taker_rate: NonNegativeDecimal
    observed_at: datetime
    source_hash: Sha256


type PredictionNormalizedRecord = (
    MarketRecord | RuleVersion | TradeRecord | PredictionBookSnapshot | PredictionFeeRate
)
