from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from polytrading.domain.models import FeeSchedule, Venue
from polytrading.registry import MissingPointInTimeRecordError
from polytrading.registry.fees import FeeRegistry
from polytrading.storage.store import DuckDBStore
from tests.domain.factories import NOW, SOURCE_HASH


def fee_schedule(**overrides: object) -> FeeSchedule:
    values: dict[str, object] = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "tier_name": "VIP 0",
        "maker_rate": Decimal("-0.00010"),
        "taker_rate": Decimal("0.00055"),
        "effective_from": NOW - timedelta(days=1),
        "observed_at": NOW - timedelta(minutes=1),
        "source_url": "https://example.test/fees",
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return FeeSchedule(**values)


def test_as_of_uses_effective_and_observed_time_and_never_returns_future_record(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    registry = FeeRegistry(store)
    known = fee_schedule()
    future = fee_schedule(
        effective_from=NOW + timedelta(minutes=1),
        observed_at=NOW + timedelta(minutes=2),
        maker_rate=Decimal("-0.00020"),
        source_hash="b" * 64,
    )
    registry.record(known)
    registry.record(future)

    assert registry.as_of(Venue.BYBIT, "VIP 0", NOW - timedelta(days=2)) is None
    assert registry.as_of(Venue.BYBIT, "VIP 0", NOW) == known
    store.close()


def test_calculate_reports_exact_maker_rebate_and_taker_fee(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    registry = FeeRegistry(store)
    registry.record(fee_schedule())

    assert registry.calculate(Venue.BYBIT, "VIP 0", "maker", Decimal("123.45"), NOW) == Decimal(
        "-0.0123450"
    )
    assert registry.calculate(Venue.BYBIT, "VIP 0", "taker", Decimal("123.45"), NOW) == Decimal(
        "0.0678975"
    )
    assert registry.calculate(Venue.BYBIT, "VIP 0", "maker", Decimal("0"), NOW) == Decimal(0)
    store.close()


def test_calculate_rejects_negative_notional_without_sizing_it(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    registry = FeeRegistry(store)
    registry.record(fee_schedule())

    with pytest.raises(ValueError, match="notional must be non-negative"):
        registry.calculate(Venue.BYBIT, "VIP 0", "maker", Decimal("-1"), NOW)

    store.close()


def test_calculate_requires_point_in_time_fee_evidence(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    registry = FeeRegistry(store)

    with pytest.raises(MissingPointInTimeRecordError) as captured:
        registry.calculate(Venue.BYBIT, "VIP 0", "taker", Decimal("100"), NOW)

    assert captured.value.lookup_key == ("fee", "bybit", "VIP 0")
    assert captured.value.as_of == NOW
    store.close()


def test_fee_as_of_rejects_a_naive_timestamp(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    registry = FeeRegistry(store)

    with pytest.raises(ValueError, match="timezone-aware"):
        registry.as_of(Venue.BYBIT, "VIP 0", datetime(2026, 8, 12, 12))

    store.close()
