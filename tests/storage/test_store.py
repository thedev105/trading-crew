import importlib.resources
from datetime import datetime, timedelta
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
from polytrading.venues.funding_cycle_models import (
    FUNDING_CYCLE_PROTOCOL_VERSION,
    FUNDING_CYCLE_WARNINGS,
    FundingCaptureOutcome,
    FundingCollectionCycle,
    FundingCycleItem,
    FundingCycleStatus,
    InstrumentCaptureOutcome,
)
from tests.domain.factories import NOW, SOURCE_HASH, instrument_spec

OTHER_SOURCE_HASH = "b" * 64
THIRD_SOURCE_HASH = "c" * 64
FOURTH_SOURCE_HASH = "d" * 64


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


def funding_collection_cycle(**overrides: object) -> FundingCollectionCycle:
    cycle_end = overrides.pop("cycle_end", NOW)
    items = (
        FundingCycleItem(
            schema_version=1,
            venue=Venue.BYBIT,
            asset=Asset.BTC,
            symbol="BTCUSDT",
            instrument_outcome=InstrumentCaptureOutcome.CAPTURED,
            funding_outcome=FundingCaptureOutcome.NO_SETTLEMENT,
            instrument_observed_at=cycle_end + timedelta(minutes=1),
            funding_effective_at=None,
            funding_observed_at=cycle_end + timedelta(minutes=1),
            instrument_source_hashes=(SOURCE_HASH,),
            funding_source_hashes=(OTHER_SOURCE_HASH,),
            reason_codes=(),
        ),
        FundingCycleItem(
            schema_version=1,
            venue=Venue.HYPERLIQUID,
            asset=Asset.BTC,
            symbol="BTC",
            instrument_outcome=InstrumentCaptureOutcome.CAPTURED,
            funding_outcome=FundingCaptureOutcome.CAPTURED,
            instrument_observed_at=cycle_end + timedelta(minutes=1),
            funding_effective_at=cycle_end,
            funding_observed_at=cycle_end + timedelta(minutes=2),
            instrument_source_hashes=(THIRD_SOURCE_HASH,),
            funding_source_hashes=(FOURTH_SOURCE_HASH,),
            reason_codes=(),
        ),
    )
    values: dict[str, object] = {
        "schema_version": 1,
        "protocol_version": FUNDING_CYCLE_PROTOCOL_VERSION,
        "cycle_id": UUID("00000000-0000-0000-0000-000000000902"),
        "cycle_end": cycle_end,
        "assets": (Asset.BTC,),
        "venues": (Venue.BYBIT, Venue.HYPERLIQUID),
        "request_started_at": cycle_end + timedelta(seconds=30),
        "request_completed_at": cycle_end + timedelta(minutes=2),
        "items": items,
        "status": FundingCycleStatus.COMPLETE,
        "source_hashes": (SOURCE_HASH, OTHER_SOURCE_HASH, THIRD_SOURCE_HASH, FOURTH_SOURCE_HASH),
        "warnings": FUNDING_CYCLE_WARNINGS,
    }
    values.update(overrides)
    return FundingCollectionCycle(**values)


def open_store(path: Path) -> DuckDBStore:
    return DuckDBStore(path)


def test_migration_version_is_recorded_exactly_once_across_reopens(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"

    open_store(path).close()
    open_store(path).close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute(
            "SELECT version, count(*) FROM schema_migrations GROUP BY version"
        ).fetchall() == [(1, 1), (2, 1), (3, 1)]


def test_unknown_applied_migration_gap_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    open_store(path).close()
    with duckdb.connect(str(path)) as connection:
        connection.execute("INSERT INTO schema_migrations VALUES (?, ?)", [4, NOW])

    with pytest.raises(RuntimeError, match="not a known prefix"):
        open_store(path)


def test_read_only_store_requires_the_exact_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    open_store(path).close()
    with duckdb.connect(str(path)) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")

    with pytest.raises(RuntimeError, match="read-only store requires current schema"):
        DuckDBStore(path, read_only=True)


def test_migration_sql_is_available_as_packaged_data() -> None:
    migration = importlib.resources.files("polytrading.storage.schema").joinpath("001_initial.sql")

    assert migration.is_file()
    assert "CREATE TABLE schema_migrations" in migration.read_text(encoding="utf-8")

    forward_migration = importlib.resources.files("polytrading.storage.schema").joinpath(
        "003_forward_funding_cycles.sql"
    )
    assert forward_migration.is_file()
    assert "CREATE TABLE funding_collection_cycles" in forward_migration.read_text(encoding="utf-8")


def test_funding_collection_cycle_round_trips_and_exact_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    store = open_store(tmp_path / "research.duckdb")
    record = funding_collection_cycle()

    assert store.append_funding_collection_cycle(record) is True
    assert store.append_funding_collection_cycle(record) is False
    assert store.funding_collection_cycles_between(NOW, NOW) == (record,)
    store.close()


def test_funding_collection_cycle_conflict_fails_closed(tmp_path: Path) -> None:
    store = open_store(tmp_path / "research.duckdb")
    original = funding_collection_cycle()
    conflict = funding_collection_cycle(request_completed_at=NOW + timedelta(minutes=3))
    store.append_funding_collection_cycle(original)

    with pytest.raises(
        ConflictingRecordError,
        match="conflicting funding collection cycle for immutable identity",
    ):
        store.append_funding_collection_cycle(conflict)
    store.close()


def test_funding_collection_cycles_use_closed_boundaries_and_stable_attempt_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.duckdb"
    store = open_store(path)
    first = funding_collection_cycle()
    second = funding_collection_cycle(
        cycle_id=UUID("00000000-0000-0000-0000-000000000903"),
        request_completed_at=NOW + timedelta(minutes=3),
    )
    later = funding_collection_cycle(
        cycle_id=UUID("00000000-0000-0000-0000-000000000904"),
        cycle_end=NOW + timedelta(hours=1),
    )
    for record in (later, second, first):
        store.append_funding_collection_cycle(record)

    assert store.funding_collection_cycles_between(NOW, NOW + timedelta(hours=1)) == (
        first,
        second,
        later,
    )
    store.close()

    read_only = DuckDBStore(path, read_only=True)
    assert read_only.funding_collection_cycles_between(NOW, NOW) == (first, second)
    read_only.close()


def test_funding_collection_cycle_query_rejects_reversed_or_naive_windows(
    tmp_path: Path,
) -> None:
    store = open_store(tmp_path / "research.duckdb")

    with pytest.raises(ValueError, match="start must be less than or equal to end"):
        store.funding_collection_cycles_between(NOW, NOW - timedelta(hours=1))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.funding_collection_cycles_between(NOW.replace(tzinfo=None), NOW)
    store.close()


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
        stored_raw = connection.execute("SELECT request_latency_ms FROM raw_envelopes").fetchone()
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


def test_funding_revisions_respect_open_start_closed_end_and_knowledge_cutoff(
    tmp_path: Path,
) -> None:
    store = open_store(tmp_path / "research.duckdb")
    start = NOW - timedelta(hours=16)
    middle = NOW - timedelta(hours=8)
    known_as_of = NOW + timedelta(minutes=1)
    for record in (
        funding_observation(effective_at=start, observed_at=start + timedelta(minutes=1)),
        funding_observation(
            effective_at=middle,
            observed_at=middle + timedelta(minutes=1),
            source_hash=OTHER_SOURCE_HASH,
        ),
        funding_observation(
            effective_at=middle,
            observed_at=known_as_of + timedelta(minutes=1),
            source_hash=THIRD_SOURCE_HASH,
        ),
        funding_observation(
            effective_at=NOW,
            observed_at=known_as_of,
            source_hash=FOURTH_SOURCE_HASH,
        ),
    ):
        store.append_funding(record)

    rows = store.funding_revisions_between(
        Venue.BYBIT,
        "BTCUSDT",
        start,
        NOW,
        known_as_of,
    )

    assert [(row.effective_at, row.observed_at, row.source_hash) for row in rows] == [
        (middle, middle + timedelta(minutes=1), OTHER_SOURCE_HASH),
        (NOW, known_as_of, FOURTH_SOURCE_HASH),
    ]
    store.close()


def test_funding_revisions_require_ordered_window_and_knowledge_cutoff(tmp_path: Path) -> None:
    store = open_store(tmp_path / "research.duckdb")

    with pytest.raises(ValueError, match="start must be less than or equal to end"):
        store.funding_revisions_between(Venue.BYBIT, "BTCUSDT", NOW, NOW - timedelta(hours=1), NOW)
    with pytest.raises(ValueError, match="known_as_of must be greater than or equal to end"):
        store.funding_revisions_between(
            Venue.BYBIT, "BTCUSDT", NOW - timedelta(hours=1), NOW, NOW - timedelta(microseconds=1)
        )

    store.close()


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


def test_raw_retry_ignores_decimal_storage_scale_normalization(tmp_path: Path) -> None:
    store = open_store(tmp_path / "research.duckdb")
    record = raw_envelope(request_latency_ms=Decimal("1.0"))

    assert store.append_raw(record) is True
    assert store.append_raw(record) is False

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

    assert store.latest_instrument_as_of(Venue.BYBIT, "BTCUSDT", NOW - timedelta(minutes=3)) is None
    assert store.latest_instrument_as_of(Venue.BYBIT, "BTCUSDT", NOW) == early

    store.close()


def test_instrument_logical_version_rejects_a_different_instrument_id(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    store = open_store(path)
    original = instrument_spec()
    store.append_instrument(original)

    with pytest.raises(
        ConflictingRecordError,
        match=r"^conflicting instrument spec for immutable identity$",
    ):
        store.append_instrument(
            instrument_spec(
                instrument_id="alternate-id-for-the-same-logical-version",
                source_hash=OTHER_SOURCE_HASH,
            )
        )

    assert store.latest_instrument_as_of(Venue.BYBIT, "BTCUSDT", NOW) == original
    assert store.append_instrument(original) is False
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (1,)


@pytest.mark.parametrize("reader", ["latest_instrument_as_of", "latest_fee_as_of"])
def test_direct_as_of_readers_reject_naive_cutoffs(tmp_path: Path, reader: str) -> None:
    store = open_store(tmp_path / "research.duckdb")
    lookup = getattr(store, reader)
    identity = (
        (Venue.BYBIT, "BTCUSDT")
        if reader == "latest_instrument_as_of"
        else (
            Venue.BYBIT,
            "VIP 0",
        )
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        lookup(*identity, datetime(2026, 8, 12, 12))

    store.close()


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 8, 12, 10), NOW),
        (NOW - timedelta(hours=2), datetime(2026, 8, 12, 12)),
    ],
)
def test_funding_between_rejects_naive_bounds(
    tmp_path: Path, start: datetime, end: datetime
) -> None:
    store = open_store(tmp_path / "research.duckdb")

    with pytest.raises(ValueError, match="timezone-aware"):
        store.funding_between(Venue.BYBIT, "BTCUSDT", start, end)

    store.close()


def test_funding_between_rejects_reversed_range(tmp_path: Path) -> None:
    store = open_store(tmp_path / "research.duckdb")

    with pytest.raises(ValueError, match="start must be less than or equal to end"):
        store.funding_between(Venue.BYBIT, "BTCUSDT", NOW, NOW - timedelta(seconds=1))

    store.close()


def test_funding_between_returns_latest_revision_known_by_end(tmp_path: Path) -> None:
    store = open_store(tmp_path / "research.duckdb")
    effective_at = NOW - timedelta(hours=1)
    initial = funding_observation(
        effective_at=effective_at,
        observed_at=NOW - timedelta(minutes=30),
        rate=Decimal("0.0001"),
    )
    latest_known = funding_observation(
        effective_at=effective_at,
        observed_at=NOW - timedelta(minutes=10),
        rate=Decimal("0.0002"),
        source_hash=OTHER_SOURCE_HASH,
    )
    late_revision = funding_observation(
        effective_at=effective_at,
        observed_at=NOW + timedelta(minutes=1),
        rate=Decimal("0.0003"),
        source_hash="c" * 64,
    )
    earlier_effective = funding_observation(
        effective_at=NOW - timedelta(hours=2),
        observed_at=NOW - timedelta(minutes=20),
        rate=Decimal("0.0004"),
        source_hash="d" * 64,
    )
    for record in (late_revision, initial, latest_known, earlier_effective):
        store.append_funding(record)

    assert store.funding_between(Venue.BYBIT, "BTCUSDT", NOW - timedelta(hours=2), NOW) == (
        earlier_effective,
        latest_known,
    )

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

    assert store.funding_between(Venue.BYBIT, "BTCUSDT", NOW - timedelta(hours=3), NOW) == (
        first_funding,
        second_funding,
    )
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

    with (
        pytest.raises(RuntimeError, match="nested transactions are not supported"),
        store.transaction(),
    ):
        store.append_instrument(instrument_spec())
        with store.transaction():
            pass

    assert store.latest_instrument_as_of(Venue.BYBIT, "BTCUSDT", NOW) is None
    store.close()
