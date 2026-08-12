from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from polytrading.domain.models import Asset, InstrumentKind, InstrumentSpec, Venue

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
