from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID

from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookLevel,
    PredictionBookSnapshot,
    PredictionFeeRate,
    PredictionRawEnvelope,
    PredictionVenue,
    RuleVersion,
    TradeRecord,
)

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
SOURCE_HASH = "a" * 64
RAW_PAYLOAD_JSON = "{}"
RAW_PAYLOAD_HASH = sha256(RAW_PAYLOAD_JSON.encode()).hexdigest()
RULE_VERSION_ID = UUID("00000000-0000-0000-0000-000000000a01")
CYCLE_ID = UUID("00000000-0000-0000-0000-000000000b01")


def raw_envelope(**overrides: Any) -> PredictionRawEnvelope:
    values: dict[str, Any] = {
        "schema_version": 1,
        "event_id": UUID("00000000-0000-0000-0000-000000000c01"),
        "venue": PredictionVenue.POLYMARKET,
        "endpoint": "/markets",
        "venue_timestamp": None,
        "observed_at": NOW,
        "received_monotonic_ns": 1,
        "request_latency_ms": Decimal("10"),
        "source_version": "gamma-v1",
        "payload_json": RAW_PAYLOAD_JSON,
        "source_hash": RAW_PAYLOAD_HASH,
    }
    values.update(overrides)
    return PredictionRawEnvelope(**values)


def market_record(**overrides: Any) -> MarketRecord:
    values: dict[str, Any] = {
        "schema_version": 1,
        "market_id": "0xcondition",
        "venue": PredictionVenue.POLYMARKET,
        "underlying_exchange": None,
        "event_id": None,
        "question": "Will BTC close above $100k?",
        "slug": "btc-100k",
        "outcomes": ("Yes", "No"),
        "outcome_token_ids": ("111", "222"),
        "negative_risk": False,
        "active": True,
        "closed": False,
        "restricted": False,
        "order_book_enabled": True,
        "start_at": NOW,
        "end_at": None,
        "resolution_source": "https://example.test/rules",
        "rule_version_id": RULE_VERSION_ID,
        "information_cutoff": NOW,
        "source_url": "https://gamma-api.polymarket.com/markets",
        "retrieved_at": NOW,
        "raw_hash": SOURCE_HASH,
        "normalized_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return MarketRecord(**values)


def rule_version(**overrides: Any) -> RuleVersion:
    values: dict[str, Any] = {
        "schema_version": 1,
        "rule_version_id": RULE_VERSION_ID,
        "market_id": "0xcondition",
        "venue": PredictionVenue.POLYMARKET,
        "question": "Will BTC close above $100k?",
        "description": "Resolves YES if BTC closes above $100k on the resolution date.",
        "resolution_source": "https://example.test/rules",
        "outcomes": ("Yes", "No"),
        "superseded_rule_version_id": None,
        "effective_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return RuleVersion(**values)


def trade_record(**overrides: Any) -> TradeRecord:
    values: dict[str, Any] = {
        "schema_version": 1,
        "venue": PredictionVenue.POLYMARKET,
        "market_id": "0xcondition",
        "outcome_token_id": "111",
        "trade_id": "trade-1",
        "price": Decimal("0.62"),
        "size": Decimal("100"),
        "side": "buy",
        "effective_at": NOW,
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return TradeRecord(**values)


def level(price: str, size: str) -> PredictionBookLevel:
    return PredictionBookLevel(price=Decimal(price), size=Decimal(size))


def prediction_book_snapshot(**overrides: Any) -> PredictionBookSnapshot:
    values: dict[str, Any] = {
        "schema_version": 1,
        "cycle_id": CYCLE_ID,
        "venue": PredictionVenue.POLYMARKET,
        "market_id": "0xcondition",
        "outcome_token_id": "111",
        "bids": (level("0.60", "100"), level("0.55", "50")),
        "asks": (level("0.65", "80"), level("0.70", "40")),
        "sequence": "1",
        "effective_at": NOW,
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return PredictionBookSnapshot(**values)


def fee_rate(**overrides: Any) -> PredictionFeeRate:
    values: dict[str, Any] = {
        "schema_version": 1,
        "venue": PredictionVenue.POLYMARKET,
        "market_id": None,
        "maker_rate": Decimal("0"),
        "taker_rate": Decimal("0"),
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return PredictionFeeRate(**values)
