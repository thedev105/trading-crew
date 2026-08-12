from datetime import datetime, timedelta
from pathlib import Path

import pytest

from polytrading.domain.models import Venue
from polytrading.registry import MissingPointInTimeRecordError
from polytrading.registry.instruments import InstrumentRegistry
from polytrading.storage.store import DuckDBStore
from tests.domain.factories import NOW, instrument_spec


def test_as_of_returns_latest_known_instrument_without_future_information(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    registry = InstrumentRegistry(store)
    early = instrument_spec(observed_at=NOW - timedelta(minutes=2))
    future = instrument_spec(
        observed_at=NOW + timedelta(minutes=2),
        price_tick=early.price_tick * 2,
        source_hash="b" * 64,
    )
    registry.record(early)
    registry.record(future)

    assert registry.as_of(Venue.BYBIT, "BTCUSDT", NOW - timedelta(minutes=3)) is None
    assert registry.as_of(Venue.BYBIT, "BTCUSDT", NOW) == early
    store.close()


def test_require_as_of_fails_closed_with_only_lookup_context(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    registry = InstrumentRegistry(store)

    with pytest.raises(MissingPointInTimeRecordError) as captured:
        registry.require_as_of(Venue.BYBIT, "BTCUSDT", NOW)

    assert captured.value.lookup_key == ("instrument", "bybit", "BTCUSDT")
    assert captured.value.as_of == NOW
    assert captured.value.args == (("instrument", "bybit", "BTCUSDT"), NOW)
    store.close()


def test_as_of_rejects_a_naive_timestamp(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    registry = InstrumentRegistry(store)

    with pytest.raises(ValueError, match="timezone-aware"):
        registry.as_of(Venue.BYBIT, "BTCUSDT", datetime(2026, 8, 12, 12))

    store.close()
