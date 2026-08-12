from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from polytrading.domain.models import (
    Asset,
    BookLevel,
    FundingObservation,
    InstrumentKind,
    InstrumentSpec,
    Level2BookSnapshot,
    Venue,
)
from polytrading.venues.synchronized import BookCollectionCycle

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
SOURCE_HASH = "a" * 64


def instrument_spec(**overrides: Any) -> InstrumentSpec:
    values: dict[str, Any] = {
        "schema_version": 1,
        "instrument_id": "bybit:BTCUSDT:linear_perpetual",
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "kind": InstrumentKind.LINEAR_PERPETUAL,
        "contract_multiplier": Decimal("1"),
        "index_family": "BTC",
        "oracle_family": "BTC",
        "mark_method": "venue_mark",
        "liquidation_method": "venue_rulebook",
        "collateral_asset": "USDT",
        "pnl_asset": "USDT",
        "funding_formula_id": "bybit-linear-v1",
        "funding_cap": Decimal("0.00375"),
        "funding_interval_hours": Decimal("8"),
        "funding_payment_offset_minutes": 0,
        "min_notional": Decimal("5"),
        "quantity_step": Decimal("0.001"),
        "price_tick": Decimal("0.1"),
        "is_inverse": False,
        "is_prelaunch": False,
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return InstrumentSpec(**values)


def funding_observation(**overrides: Any) -> FundingObservation:
    values: dict[str, Any] = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "rate": Decimal("0.0008"),
        "interval_hours": Decimal("8"),
        "effective_at": NOW,
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return FundingObservation(**values)


def book_snapshot(**overrides: Any) -> Level2BookSnapshot:
    values: dict[str, Any] = {
        "schema_version": 1,
        "cycle_id": UUID("00000000-0000-0000-0000-000000000901"),
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "bids": (
            BookLevel(price=Decimal("100"), quantity=Decimal("2"), order_count=2),
            BookLevel(price=Decimal("99"), quantity=Decimal("3"), order_count=1),
        ),
        "asks": (
            BookLevel(price=Decimal("101"), quantity=Decimal("4"), order_count=3),
            BookLevel(price=Decimal("102"), quantity=Decimal("5"), order_count=1),
        ),
        "depth_limit": 20,
        "sequence": "factory-sequence",
        "effective_at": NOW,
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return Level2BookSnapshot(**values)


def book_collection_cycle(**overrides: Any) -> BookCollectionCycle:
    values: dict[str, Any] = {
        "schema_version": 1,
        "cycle_id": UUID("00000000-0000-0000-0000-000000000901"),
        "assets": (Asset.BTC, Asset.ETH, Asset.SOL),
        "venues": (Venue.BYBIT, Venue.HYPERLIQUID),
        "request_started_at": NOW,
        "request_completed_at": NOW,
        "effective_timestamps": (NOW,),
        "max_effective_skew_ms": Decimal("0"),
        "status": "complete",
        "failure_codes": (),
        "source_hashes": (SOURCE_HASH,),
    }
    values.update(overrides)
    return BookCollectionCycle(**values)
