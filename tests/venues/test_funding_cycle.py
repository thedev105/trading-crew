from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from polytrading.domain.models import Asset, FundingObservation, Venue
from polytrading.storage.store import DuckDBStore
from polytrading.venues.funding_cycle import (
    PointInTimeFundingCollector,
    record_late_funding_cycle,
)
from polytrading.venues.funding_cycle_models import (
    FundingCaptureOutcome,
    FundingCycleStatus,
    InstrumentCaptureOutcome,
)
from polytrading.venues.public import AdapterBatch
from tests.venues.funding_cycle_helpers import (
    ASSETS,
    CYCLE_END,
    FakeFundingAdapter,
    SequenceClock,
    funding_batch,
    instrument_batch,
)


def adapters(
    *,
    cycle_end: datetime = CYCLE_END,
    observed_at: datetime | None = None,
    event_offset: int = 0,
) -> tuple[FakeFundingAdapter, ...]:
    observation = observed_at or cycle_end + timedelta(minutes=1)
    bybit = FakeFundingAdapter(
        Venue.BYBIT,
        instrument_batch(
            Venue.BYBIT,
            ASSETS,
            observed_at=observation,
            event_int=event_offset + 100,
        ),
        {
            asset: funding_batch(
                Venue.BYBIT,
                asset,
                effective_at=cycle_end,
                observed_at=observation,
                event_int=event_offset + 110 + index,
                include_record=False,
            )
            for index, asset in enumerate(sorted(ASSETS, key=lambda item: item.value))
        },
    )
    hyperliquid = FakeFundingAdapter(
        Venue.HYPERLIQUID,
        instrument_batch(
            Venue.HYPERLIQUID,
            ASSETS,
            observed_at=observation,
            event_int=event_offset + 200,
        ),
        {
            asset: funding_batch(
                Venue.HYPERLIQUID,
                asset,
                effective_at=cycle_end,
                observed_at=observation,
                event_int=event_offset + 210 + index,
            )
            for index, asset in enumerate(sorted(ASSETS, key=lambda item: item.value))
        },
    )
    return bybit, hyperliquid


def preseed_bybit_basis(store: DuckDBStore) -> None:
    batch = instrument_batch(
        Venue.BYBIT,
        ASSETS,
        observed_at=CYCLE_END - timedelta(hours=1),
        event_int=90,
    )
    for record in batch.normalized:
        store.append_instrument(record)


def test_collector_requires_assets_and_exactly_both_unique_venues(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    bybit, hyperliquid = adapters()
    clock = SequenceClock(CYCLE_END + timedelta(seconds=30))
    collector = PointInTimeFundingCollector(store, clock=clock)

    with pytest.raises(ValueError, match="at least one asset is required"):
        asyncio.run(collector.collect_once((bybit, hyperliquid), frozenset(), CYCLE_END))
    with pytest.raises(ValueError, match="both Bybit and Hyperliquid adapters are required"):
        asyncio.run(collector.collect_once((bybit,), ASSETS, CYCLE_END))
    with pytest.raises(ValueError, match="public adapters must have unique venues"):
        asyncio.run(collector.collect_once((bybit, bybit), ASSETS, CYCLE_END))
    store.close()


def test_collector_queries_only_the_exact_boundary_and_persists_complete_cycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)
    preseed_bybit_basis(store)
    bybit, hyperliquid = adapters()
    clock = SequenceClock(
        CYCLE_END + timedelta(seconds=30),
        CYCLE_END + timedelta(minutes=2),
    )
    collector = PointInTimeFundingCollector(
        store,
        clock=clock,
        cycle_id_factory=lambda: UUID("00000000-0000-0000-0000-000000000950"),
    )

    cycle = asyncio.run(collector.collect_once((hyperliquid, bybit), ASSETS, CYCLE_END))

    assert cycle.status is FundingCycleStatus.COMPLETE
    assert [(item.venue, item.asset) for item in cycle.items] == [
        (Venue.BYBIT, Asset.BTC),
        (Venue.BYBIT, Asset.ETH),
        (Venue.BYBIT, Asset.SOL),
        (Venue.HYPERLIQUID, Asset.BTC),
        (Venue.HYPERLIQUID, Asset.ETH),
        (Venue.HYPERLIQUID, Asset.SOL),
    ]
    assert all(
        item.instrument_outcome is InstrumentCaptureOutcome.CAPTURED for item in cycle.items
    )
    assert [item.funding_outcome for item in cycle.items] == [
        FundingCaptureOutcome.NO_SETTLEMENT,
        FundingCaptureOutcome.NO_SETTLEMENT,
        FundingCaptureOutcome.NO_SETTLEMENT,
        FundingCaptureOutcome.CAPTURED,
        FundingCaptureOutcome.CAPTURED,
        FundingCaptureOutcome.CAPTURED,
    ]
    assert all(
        start == CYCLE_END and end == CYCLE_END
        for adapter in (bybit, hyperliquid)
        for _asset, start, end, _observed_at in adapter.funding_calls
    )
    assert store.funding_collection_cycles_between(CYCLE_END, CYCLE_END) == (cycle,)
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (8,)
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM funding_collection_cycles").fetchone() == (
            1,
        )


def test_instrument_and_funding_requests_start_concurrently_by_phase(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    preseed_bybit_basis(store)
    bybit, hyperliquid = adapters()

    async def scenario() -> FundingCycleStatus:
        instrument_release = asyncio.Event()
        funding_release = asyncio.Event()
        instrument_started = 0
        funding_started = 0

        class GatedAdapter(FakeFundingAdapter):
            async def fetch_instruments(
                self, assets: frozenset[Asset], observed_at: datetime
            ) -> AdapterBatch:
                nonlocal instrument_started
                instrument_started += 1
                if instrument_started == 2:
                    instrument_release.set()
                await instrument_release.wait()
                return await super().fetch_instruments(assets, observed_at)

            async def fetch_funding_history(
                self,
                asset: Asset,
                start: datetime,
                end: datetime,
                observed_at: datetime,
            ) -> AdapterBatch:
                nonlocal funding_started
                funding_started += 1
                if funding_started == 6:
                    funding_release.set()
                await funding_release.wait()
                return await super().fetch_funding_history(asset, start, end, observed_at)

        gated = tuple(
            GatedAdapter(adapter.venue, adapter.instrument_result, adapter.funding_results)
            for adapter in (bybit, hyperliquid)
        )
        collector = PointInTimeFundingCollector(
            store,
            clock=SequenceClock(
                CYCLE_END + timedelta(seconds=30),
                CYCLE_END + timedelta(minutes=2),
            ),
        )
        cycle = await asyncio.wait_for(
            collector.collect_once(gated, ASSETS, CYCLE_END), timeout=1
        )
        assert instrument_started == 2
        assert funding_started == 6
        return cycle.status

    assert asyncio.run(scenario()) is FundingCycleStatus.COMPLETE
    store.close()


def test_first_cycle_bootstraps_bybit_without_backdating_new_instruments(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    bybit, hyperliquid = adapters()
    collector = PointInTimeFundingCollector(
        store,
        clock=SequenceClock(
            CYCLE_END + timedelta(seconds=30),
            CYCLE_END + timedelta(minutes=2),
        ),
    )

    cycle = asyncio.run(collector.collect_once((bybit, hyperliquid), ASSETS, CYCLE_END))

    assert cycle.status is FundingCycleStatus.DEGRADED
    assert bybit.funding_calls == []
    assert [item.funding_outcome for item in cycle.items[:3]] == [
        FundingCaptureOutcome.BOOTSTRAP_REQUIRED,
        FundingCaptureOutcome.BOOTSTRAP_REQUIRED,
        FundingCaptureOutcome.BOOTSTRAP_REQUIRED,
    ]
    assert all(
        item.reason_codes == ("BYBIT_INSTRUMENT_BOOTSTRAP_REQUIRED",)
        for item in cycle.items[:3]
    )
    assert store.latest_instrument_as_of(Venue.BYBIT, "BTCUSDT", CYCLE_END) is None
    assert (
        store.latest_instrument_as_of(
            Venue.BYBIT, "BTCUSDT", CYCLE_END + timedelta(minutes=2)
        )
        is not None
    )
    store.close()


def test_later_cycle_uses_previously_committed_bybit_basis(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    first_bybit, first_hyperliquid = adapters()
    first = PointInTimeFundingCollector(
        store,
        clock=SequenceClock(
            CYCLE_END + timedelta(seconds=30),
            CYCLE_END + timedelta(minutes=2),
        ),
    )
    asyncio.run(first.collect_once((first_bybit, first_hyperliquid), ASSETS, CYCLE_END))

    next_end = CYCLE_END + timedelta(hours=1)
    next_bybit, next_hyperliquid = adapters(cycle_end=next_end, event_offset=1_000)
    next_bybit.funding_results = {
        asset: funding_batch(
            Venue.BYBIT,
            asset,
            effective_at=next_end,
            observed_at=next_end + timedelta(minutes=1),
            event_int=310 + index,
        )
        for index, asset in enumerate(sorted(ASSETS, key=lambda item: item.value))
    }
    second = PointInTimeFundingCollector(
        store,
        clock=SequenceClock(next_end + timedelta(seconds=30), next_end + timedelta(minutes=2)),
    )

    cycle = asyncio.run(second.collect_once((next_bybit, next_hyperliquid), ASSETS, next_end))

    assert cycle.status is FundingCycleStatus.COMPLETE
    assert all(
        item.funding_outcome is FundingCaptureOutcome.CAPTURED for item in cycle.items
    )
    assert len(next_bybit.funding_calls) == 3
    store.close()


def test_empty_hyperliquid_response_is_missing_expected_but_raw_is_retained(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    preseed_bybit_basis(store)
    bybit, hyperliquid = adapters()
    hyperliquid.funding_results[Asset.ETH] = funding_batch(
        Venue.HYPERLIQUID,
        Asset.ETH,
        effective_at=CYCLE_END,
        observed_at=CYCLE_END + timedelta(minutes=1),
        event_int=999,
        include_record=False,
    )
    collector = PointInTimeFundingCollector(
        store,
        clock=SequenceClock(
            CYCLE_END + timedelta(seconds=30),
            CYCLE_END + timedelta(minutes=2),
        ),
    )

    cycle = asyncio.run(collector.collect_once((bybit, hyperliquid), ASSETS, CYCLE_END))
    eth = next(
        item
        for item in cycle.items
        if item.venue is Venue.HYPERLIQUID and item.asset is Asset.ETH
    )

    assert cycle.status is FundingCycleStatus.DEGRADED
    assert eth.funding_outcome is FundingCaptureOutcome.MISSING_EXPECTED
    assert eth.funding_observed_at == CYCLE_END + timedelta(minutes=1)
    assert eth.reason_codes == ("FUNDING_MISSING_EXPECTED",)
    assert len(eth.funding_source_hashes) == 1
    store.close()


def test_empty_batch_without_raw_response_fails_the_item(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    preseed_bybit_basis(store)
    bybit, hyperliquid = adapters()
    hyperliquid.funding_results[Asset.ETH] = AdapterBatch(raw=(), normalized=())
    collector = PointInTimeFundingCollector(
        store,
        clock=SequenceClock(
            CYCLE_END + timedelta(seconds=30),
            CYCLE_END + timedelta(minutes=2),
        ),
    )

    cycle = asyncio.run(collector.collect_once((bybit, hyperliquid), ASSETS, CYCLE_END))
    eth = next(
        item
        for item in cycle.items
        if item.venue is Venue.HYPERLIQUID and item.asset is Asset.ETH
    )

    assert eth.funding_outcome is FundingCaptureOutcome.FAILED
    assert eth.reason_codes == ("FUNDING_FAILED:hyperliquid:ETH:ValueError",)
    store.close()


def test_instrument_failure_does_not_hide_successful_funding(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    preseed_bybit_basis(store)
    bybit, hyperliquid = adapters()
    hyperliquid.instrument_result = TimeoutError("secret endpoint details")
    collector = PointInTimeFundingCollector(
        store,
        clock=SequenceClock(
            CYCLE_END + timedelta(seconds=30),
            CYCLE_END + timedelta(minutes=2),
        ),
    )

    cycle = asyncio.run(collector.collect_once((bybit, hyperliquid), ASSETS, CYCLE_END))
    hyperliquid_items = tuple(
        item for item in cycle.items if item.venue is Venue.HYPERLIQUID
    )

    assert cycle.status is FundingCycleStatus.DEGRADED
    assert all(
        item.instrument_outcome is InstrumentCaptureOutcome.FAILED
        and item.funding_outcome is FundingCaptureOutcome.CAPTURED
        for item in hyperliquid_items
    )
    assert all(
        item.reason_codes == ("INSTRUMENT_FAILED:hyperliquid:TimeoutError",)
        for item in hyperliquid_items
    )
    assert "secret" not in str(cycle.model_dump())
    store.close()


@pytest.mark.parametrize(
    "invalid_record",
    [
        lambda record: record.model_copy(update={"venue": Venue.BYBIT}),
        lambda record: record.model_copy(update={"symbol": "ETHUSDT"}),
        lambda record: record.model_copy(update={"asset": Asset.ETH}),
        lambda record: record.model_copy(
            update={"effective_at": CYCLE_END - timedelta(hours=1)}
        ),
    ],
)
def test_invalid_funding_identity_fails_only_that_item(
    tmp_path: Path,
    invalid_record: object,
) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    preseed_bybit_basis(store)
    bybit, hyperliquid = adapters()
    original = hyperliquid.funding_results[Asset.BTC]
    assert not isinstance(original, BaseException)
    record = original.normalized[0]
    assert isinstance(record, FundingObservation)
    hyperliquid.funding_results[Asset.BTC] = original.__class__(
        raw=original.raw,
        normalized=(invalid_record(record),),
    )
    collector = PointInTimeFundingCollector(
        store,
        clock=SequenceClock(
            CYCLE_END + timedelta(seconds=30),
            CYCLE_END + timedelta(minutes=2),
        ),
    )

    cycle = asyncio.run(collector.collect_once((bybit, hyperliquid), ASSETS, CYCLE_END))
    btc = next(
        item
        for item in cycle.items
        if item.venue is Venue.HYPERLIQUID and item.asset is Asset.BTC
    )

    assert btc.funding_outcome is FundingCaptureOutcome.FAILED
    assert btc.funding_source_hashes == ()
    assert btc.reason_codes == ("FUNDING_FAILED:hyperliquid:BTC:ValueError",)
    assert all(
        item.funding_outcome is FundingCaptureOutcome.CAPTURED
        for item in cycle.items
        if item.venue is Venue.HYPERLIQUID and item.asset is not Asset.BTC
    )
    store.close()


def test_funding_cancellation_is_not_converted_to_a_cycle_failure(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    preseed_bybit_basis(store)
    bybit, hyperliquid = adapters()
    hyperliquid.funding_results[Asset.BTC] = asyncio.CancelledError()
    collector = PointInTimeFundingCollector(
        store,
        clock=SequenceClock(CYCLE_END + timedelta(seconds=30)),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(collector.collect_once((bybit, hyperliquid), ASSETS, CYCLE_END))
    assert store.funding_collection_cycles_between(CYCLE_END, CYCLE_END) == ()
    store.close()


def test_late_component_timestamp_makes_the_whole_cycle_late(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    preseed_bybit_basis(store)
    observed_at = CYCLE_END + timedelta(minutes=5, microseconds=1)
    bybit, hyperliquid = adapters(observed_at=observed_at)
    collector = PointInTimeFundingCollector(
        store,
        clock=SequenceClock(
            CYCLE_END + timedelta(seconds=30),
            CYCLE_END + timedelta(minutes=6),
        ),
    )

    cycle = asyncio.run(collector.collect_once((bybit, hyperliquid), ASSETS, CYCLE_END))

    assert cycle.status is FundingCycleStatus.LATE
    store.close()


def test_late_cycle_records_the_gap_without_raw_or_normalized_rows(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)

    cycle = record_late_funding_cycle(
        store,
        ASSETS,
        CYCLE_END,
        CYCLE_END + timedelta(minutes=6),
        cycle_id_factory=lambda: UUID("00000000-0000-0000-0000-000000000951"),
    )

    assert cycle.status is FundingCycleStatus.LATE
    assert all(
        item.instrument_outcome is InstrumentCaptureOutcome.LATE_NOT_COLLECTED
        and item.funding_outcome is FundingCaptureOutcome.LATE_NOT_COLLECTED
        and item.reason_codes == ("COLLECTION_WINDOW_MISSED",)
        for item in cycle.items
    )
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM funding_collection_cycles").fetchone() == (
            1,
        )


def test_cycle_transaction_rolls_back_every_new_evidence_when_cycle_append_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)
    preseed_bybit_basis(store)
    bybit, hyperliquid = adapters()
    collector = PointInTimeFundingCollector(
        store,
        clock=SequenceClock(
            CYCLE_END + timedelta(seconds=30),
            CYCLE_END + timedelta(minutes=2),
        ),
    )

    def fail_cycle_append(record: object) -> bool:
        raise RuntimeError("cycle append failed")

    monkeypatch.setattr(store, "append_funding_collection_cycle", fail_cycle_append)
    with pytest.raises(RuntimeError, match="cycle append failed"):
        asyncio.run(collector.collect_once((bybit, hyperliquid), ASSETS, CYCLE_END))
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (3,)
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM funding_collection_cycles").fetchone() == (
            0,
        )
