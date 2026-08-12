import importlib.resources
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from polytrading.domain.models import (
    Asset,
    BookLevel,
    FeeSchedule,
    FundingObservation,
    Level2BookSnapshot,
    MarketSnapshot,
    RawEnvelope,
    Venue,
)
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from tests.domain.factories import NOW, SOURCE_HASH, instrument_spec

OTHER_SOURCE_HASH = "b" * 64


def raw_envelope(**overrides: object) -> RawEnvelope:
    values: dict[str, object] = {
        "schema_version": 1,
        "event_id": UUID("00000000-0000-0000-0000-000000000001"),
        "venue": Venue.BYBIT,
        "endpoint": "/v5/market/tickers",
        "venue_timestamp": NOW - timedelta(milliseconds=25),
        "observed_at": NOW,
        "received_monotonic_ns": 12_345_678_901,
        "request_latency_ms": Decimal("1.234567"),
        "source_version": "v5",
        "payload_json": '{"result":{"price":"65000.123456789123456"}}',
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return RawEnvelope(**values)


def funding_observation(**overrides: object) -> FundingObservation:
    values: dict[str, object] = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "rate": Decimal("0.000123456789123456"),
        "interval_hours": Decimal("8.000000000000000000"),
        "effective_at": NOW - timedelta(hours=1),
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return FundingObservation(**values)


def market_snapshot(**overrides: object) -> MarketSnapshot:
    values: dict[str, object] = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "bid": Decimal("65000.123456789123456000"),
        "ask": Decimal("65000.223456789123456000"),
        "mark": Decimal("65000.173456789123456000"),
        "index": Decimal("65000.163456789123456000"),
        "open_interest": Decimal("1234.567890123456789000"),
        "effective_at": NOW - timedelta(seconds=1),
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return MarketSnapshot(**values)


def book_snapshot(**overrides: object) -> Level2BookSnapshot:
    values: dict[str, object] = {
        "schema_version": 1,
        "cycle_id": UUID("00000000-0000-0000-0000-000000000002"),
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "bids": (
            BookLevel(
                price=Decimal("65000.123456789123456000"),
                quantity=Decimal("1.234567890123456789"),
                order_count=2,
            ),
            BookLevel(
                price=Decimal("64999.900000000000000000"),
                quantity=Decimal("2.000000000000000001"),
                order_count=None,
            ),
        ),
        "asks": (
            BookLevel(
                price=Decimal("65000.223456789123456000"),
                quantity=Decimal("3.234567890123456789"),
                order_count=1,
            ),
            BookLevel(
                price=Decimal("65001.000000000000000000"),
                quantity=Decimal("4.000000000000000001"),
                order_count=3,
            ),
        ),
        "depth_limit": 20,
        "sequence": "12345",
        "effective_at": NOW - timedelta(milliseconds=50),
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return Level2BookSnapshot(**values)


def fee_schedule(**overrides: object) -> FeeSchedule:
    values: dict[str, object] = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "tier_name": "VIP 0",
        "maker_rate": Decimal("0.000100000000000001"),
        "taker_rate": Decimal("0.000600000000000001"),
        "effective_from": NOW - timedelta(days=1),
        "observed_at": NOW,
        "source_url": "https://example.test/fees",
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return FeeSchedule(**values)


def open_store(path: Path) -> DuckDBStore:
    return DuckDBStore(path)


def test_migration_version_is_recorded_exactly_once_across_reopens(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"

    open_store(path).close()
    open_store(path).close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute(
            "SELECT version, count(*) FROM schema_migrations GROUP BY version"
        ).fetchall() == [(1, 1)]


def test_unknown_applied_migration_gap_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    open_store(path).close()
    with duckdb.connect(str(path)) as connection:
        connection.execute("INSERT INTO schema_migrations VALUES (?, ?)", [3, NOW])

    with pytest.raises(RuntimeError, match="not a known prefix"):
        open_store(path)


def test_migration_sql_is_available_as_packaged_data() -> None:
    migration = importlib.resources.files("polytrading.storage.schema").joinpath(
        "001_initial.sql"
    )

    assert migration.is_file()
    assert "CREATE TABLE schema_migrations" in migration.read_text(encoding="utf-8")


def test_all_record_types_round_trip_without_float_conversion(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    raw = raw_envelope()
    instrument = instrument_spec(contract_multiplier=Decimal("1.000000000000000001"))
    funding = funding_observation()
    market = market_snapshot()
    book = book_snapshot()
    fee = fee_schedule()
    store = open_store(path)

    assert store.append_raw(raw) is True
    assert store.append_instrument(instrument) is True
    assert store.append_funding(funding) is True
    assert store.append_market_snapshot(market) is True
    assert store.append_book_snapshot(book) is True
    assert store.append_fee_schedule(fee) is True

    assert store.latest_instrument_as_of(Venue.BYBIT, "BTCUSDT", NOW) == instrument
    assert store.funding_between(
        Venue.BYBIT, "BTCUSDT", NOW - timedelta(hours=2), NOW + timedelta(seconds=1)
    ) == (funding,)
    assert store.latest_book_as_of(Venue.BYBIT, "BTCUSDT", NOW) == book
    assert store.latest_fee_as_of(Venue.BYBIT, "VIP 0", NOW) == fee
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        stored_raw = connection.execute(
            "SELECT request_latency_ms FROM raw_envelopes"
        ).fetchone()
        stored_market = connection.execute(
            "SELECT bid, open_interest FROM market_snapshots"
        ).fetchone()
        stored_levels = connection.execute(
            "SELECT side, level_index, price, quantity FROM book_levels "
            "ORDER BY CASE side WHEN 'bid' THEN 0 ELSE 1 END, level_index"
        ).fetchall()

    assert stored_raw == (Decimal("1.234567"),)
    assert stored_market == (
        Decimal("65000.123456789123456000"),
        Decimal("1234.567890123456789000"),
    )
    assert stored_levels == [
        ("bid", 0, book.bids[0].price, book.bids[0].quantity),
        ("bid", 1, book.bids[1].price, book.bids[1].quantity),
        ("ask", 0, book.asks[0].price, book.asks[0].quantity),
        ("ask", 1, book.asks[1].price, book.asks[1].quantity),
    ]


@pytest.mark.parametrize(
    ("append_name", "record"),
    [
        ("append_raw", raw_envelope()),
        ("append_instrument", instrument_spec()),
        ("append_funding", funding_observation()),
        ("append_market_snapshot", market_snapshot()),
        ("append_book_snapshot", book_snapshot()),
        ("append_fee_schedule", fee_schedule()),
    ],
)
def test_exact_retry_is_an_idempotent_no_op(
    tmp_path: Path, append_name: str, record: object
) -> None:
    store = open_store(tmp_path / "research.duckdb")
    append = getattr(store, append_name)

    assert append(record) is True
    assert append(record) is False

    store.close()


@pytest.mark.parametrize(
    ("append_name", "original", "conflict"),
    [
        ("append_raw", raw_envelope(), raw_envelope(endpoint="/different")),
        (
            "append_instrument",
            instrument_spec(),
            instrument_spec(price_tick=Decimal("0.2"), source_hash=OTHER_SOURCE_HASH),
        ),
        (
            "append_funding",
            funding_observation(),
            funding_observation(rate=Decimal("0.0002"), source_hash=OTHER_SOURCE_HASH),
        ),
        (
            "append_market_snapshot",
            market_snapshot(),
            market_snapshot(mark=Decimal("65000.18"), source_hash=OTHER_SOURCE_HASH),
        ),
        (
            "append_book_snapshot",
            book_snapshot(),
            book_snapshot(sequence="12346", source_hash=OTHER_SOURCE_HASH),
        ),
        (
            "append_fee_schedule",
            fee_schedule(),
            fee_schedule(maker_rate=Decimal("0.0002"), source_hash=OTHER_SOURCE_HASH),
        ),
    ],
)
def test_same_identity_with_different_content_fails_closed(
    tmp_path: Path, append_name: str, original: object, conflict: object
) -> None:
    store = open_store(tmp_path / "research.duckdb")
    append = getattr(store, append_name)
    append(original)

    with pytest.raises(ConflictingRecordError):
        append(conflict)

    store.close()


def test_latest_instrument_as_of_excludes_later_observations(tmp_path: Path) -> None:
    store = open_store(tmp_path / "research.duckdb")
    early = instrument_spec(observed_at=NOW - timedelta(minutes=2), source_hash=SOURCE_HASH)
    late = instrument_spec(
        observed_at=NOW + timedelta(minutes=2),
        price_tick=Decimal("0.5"),
        source_hash=OTHER_SOURCE_HASH,
    )
    store.append_instrument(early)
    store.append_instrument(late)

    assert (
        store.latest_instrument_as_of(Venue.BYBIT, "BTCUSDT", NOW - timedelta(minutes=3))
        is None
    )
    assert store.latest_instrument_as_of(Venue.BYBIT, "BTCUSDT", NOW) == early

    store.close()


def test_range_and_as_of_readers_return_ordered_point_in_time_records(tmp_path: Path) -> None:
    store = open_store(tmp_path / "research.duckdb")
    first_funding = funding_observation(
        effective_at=NOW - timedelta(hours=2), observed_at=NOW - timedelta(minutes=4)
    )
    second_funding = funding_observation(
        effective_at=NOW - timedelta(hours=1),
        observed_at=NOW - timedelta(minutes=3),
        rate=Decimal("0.0002"),
        source_hash=OTHER_SOURCE_HASH,
    )
    early_book = book_snapshot(observed_at=NOW - timedelta(minutes=2))
    late_book = book_snapshot(
        cycle_id=UUID("00000000-0000-0000-0000-000000000003"),
        observed_at=NOW + timedelta(minutes=2),
        sequence="later",
        source_hash=OTHER_SOURCE_HASH,
    )
    early_fee = fee_schedule(observed_at=NOW - timedelta(minutes=2))
    late_fee = fee_schedule(
        effective_from=NOW + timedelta(minutes=1),
        observed_at=NOW + timedelta(minutes=2),
        maker_rate=Decimal("0.0002"),
        source_hash=OTHER_SOURCE_HASH,
    )
    store.append_funding(second_funding)
    store.append_funding(first_funding)
    store.append_book_snapshot(early_book)
    store.append_book_snapshot(late_book)
    store.append_fee_schedule(early_fee)
    store.append_fee_schedule(late_fee)

    assert store.funding_between(
        Venue.BYBIT, "BTCUSDT", NOW - timedelta(hours=3), NOW
    ) == (first_funding, second_funding)
    assert store.latest_book_as_of(Venue.BYBIT, "BTCUSDT", NOW) == early_book
    assert store.latest_fee_as_of(Venue.BYBIT, "VIP 0", NOW) == early_fee

    store.close()


def test_transaction_rolls_back_every_appended_row(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    store = open_store(path)

    with pytest.raises(RuntimeError, match="abort unit of work"), store.transaction():
        store.append_raw(raw_envelope())
        store.append_instrument(instrument_spec())
        store.append_funding(funding_observation())
        store.append_market_snapshot(market_snapshot())
        store.append_book_snapshot(book_snapshot())
        store.append_fee_schedule(fee_schedule())
        raise RuntimeError("abort unit of work")

    store.close()
    with duckdb.connect(str(path), read_only=True) as connection:
        counts = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "raw_envelopes",
                "instrument_specs",
                "funding_observations",
                "market_snapshots",
                "book_snapshots",
                "book_levels",
                "fee_schedules",
            )
        )
    assert counts == (0, 0, 0, 0, 0, 0, 0)


def test_nested_transactions_are_rejected_without_committing_outer_work(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    store = open_store(path)

    with pytest.raises(
        RuntimeError, match="nested transactions are not supported"
    ), store.transaction():
        store.append_instrument(instrument_spec())
        with store.transaction():
            pass

    assert store.latest_instrument_as_of(Venue.BYBIT, "BTCUSDT", NOW) is None
    store.close()
