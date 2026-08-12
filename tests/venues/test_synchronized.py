from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from polytrading.domain.models import (
    Asset,
    BookLevel,
    Level2BookSnapshot,
    RawEnvelope,
    Venue,
)
from polytrading.storage.store import DuckDBStore
from polytrading.venues.public import AdapterBatch
from polytrading.venues.synchronized import SynchronizedBookCollector

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
CYCLE_ID = UUID("00000000-0000-0000-0000-000000000777")
ASSETS = frozenset({Asset.SOL, Asset.BTC, Asset.ETH})
VENUE_HASHES = {Venue.BYBIT: "b" * 64, Venue.HYPERLIQUID: "c" * 64}


def raw_envelope(venue: Venue) -> RawEnvelope:
    return RawEnvelope(
        schema_version=1,
        event_id=UUID(int=1 if venue is Venue.BYBIT else 2),
        venue=venue,
        endpoint=f"/{venue.value}/public/book",
        venue_timestamp=NOW,
        observed_at=NOW,
        received_monotonic_ns=123_456_789,
        request_latency_ms=Decimal("1.25"),
        source_version="public-v1",
        payload_json=f'{{"venue":"{venue.value}"}}',
        source_hash=VENUE_HASHES[venue],
    )


def book_snapshot(
    venue: Venue,
    asset: Asset,
    cycle_id: UUID,
    effective_at: datetime,
) -> Level2BookSnapshot:
    mid = {
        Asset.BTC: Decimal("65000"),
        Asset.ETH: Decimal("3500"),
        Asset.SOL: Decimal("150"),
    }[asset]
    symbol = f"{asset.value}{'USDT' if venue is Venue.BYBIT else ''}"
    return Level2BookSnapshot(
        schema_version=1,
        cycle_id=cycle_id,
        venue=venue,
        symbol=symbol,
        asset=asset,
        bids=(BookLevel(price=mid - 1, quantity=Decimal("2"), order_count=1),),
        asks=(BookLevel(price=mid + 1, quantity=Decimal("3"), order_count=1),),
        depth_limit=20,
        sequence=f"{venue.value}-{asset.value}",
        effective_at=effective_at,
        observed_at=NOW,
        source_hash=VENUE_HASHES[venue],
    )


class DelayedBookAdapter:
    def __init__(
        self,
        venue: Venue,
        effective_times: dict[Asset, datetime],
        started: list[Venue],
        completed: list[Venue],
        release: asyncio.Event,
    ) -> None:
        self.venue = venue
        self.effective_times = effective_times
        self.started = started
        self.completed = completed
        self.release = release

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        self.started.append(self.venue)
        if len(self.started) == 2:
            self.release.set()
        await self.release.wait()
        await asyncio.sleep(0)
        self.completed.append(self.venue)
        books = tuple(
            book_snapshot(self.venue, asset, cycle_id, self.effective_times[asset])
            for asset in sorted(assets, key=lambda item: item.value)
        )
        return AdapterBatch(raw=(raw_envelope(self.venue),), normalized=books)


class FailingBookAdapter:
    venue = Venue.HYPERLIQUID

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        await asyncio.sleep(0)
        raise TimeoutError("public endpoint timed out")


class ImmediateBookAdapter:
    def __init__(self, venue: Venue) -> None:
        self.venue = venue

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        return AdapterBatch(
            raw=(raw_envelope(self.venue),),
            normalized=tuple(
                book_snapshot(self.venue, asset, cycle_id, NOW)
                for asset in sorted(assets, key=lambda item: item.value)
            ),
        )


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


def collector(
    store: DuckDBStore,
    *,
    clock: Callable[[], datetime] | None = None,
) -> SynchronizedBookCollector:
    return SynchronizedBookCollector(
        store,
        clock=clock or SequenceClock(NOW, NOW + timedelta(milliseconds=50)),
        cycle_id_factory=lambda: CYCLE_ID,
    )


def test_collect_once_starts_both_requests_and_persists_six_high_skew_books(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)
    started: list[Venue] = []
    completed: list[Venue] = []

    async def run_collection() -> object:
        release = asyncio.Event()
        bybit = DelayedBookAdapter(
            Venue.BYBIT,
            {
                Asset.BTC: NOW - timedelta(milliseconds=1500),
                Asset.ETH: NOW - timedelta(milliseconds=1000),
                Asset.SOL: NOW - timedelta(milliseconds=500),
            },
            started,
            completed,
            release,
        )
        hyperliquid = DelayedBookAdapter(
            Venue.HYPERLIQUID,
            {
                Asset.BTC: NOW - timedelta(milliseconds=250),
                Asset.ETH: NOW + timedelta(milliseconds=500),
                Asset.SOL: NOW + timedelta(milliseconds=1000),
            },
            started,
            completed,
            release,
        )
        return await asyncio.wait_for(
            collector(store).collect_once((hyperliquid, bybit), ASSETS, NOW), timeout=1
        )

    cycle = asyncio.run(run_collection())

    assert set(started) == {Venue.BYBIT, Venue.HYPERLIQUID}
    assert len(completed) == 2
    assert cycle.cycle_id == CYCLE_ID
    assert cycle.assets == (Asset.BTC, Asset.ETH, Asset.SOL)
    assert cycle.venues == (Venue.BYBIT, Venue.HYPERLIQUID)
    assert cycle.max_effective_skew_ms == Decimal("2500")
    assert cycle.status == "skew_exceeds_research_target"
    assert cycle.failure_codes == ()
    assert cycle.source_hashes == (VENUE_HASHES[Venue.BYBIT], VENUE_HASHES[Venue.HYPERLIQUID])
    assert store.latest_book_cycle_as_of(NOW + timedelta(seconds=1)) == cycle
    assert store.latest_book_cycle_as_of(NOW - timedelta(microseconds=1)) is None
    assert store.latest_book_as_of(Venue.BYBIT, "BTCUSDT", NOW + timedelta(seconds=1)) is None
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        persisted_pairs = connection.execute(
            "SELECT venue, asset, cycle_id FROM book_snapshots ORDER BY venue, asset"
        ).fetchall()
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (2,)
    assert persisted_pairs == [
        ("bybit", "BTC", CYCLE_ID),
        ("bybit", "ETH", CYCLE_ID),
        ("bybit", "SOL", CYCLE_ID),
        ("hyperliquid", "BTC", CYCLE_ID),
        ("hyperliquid", "ETH", CYCLE_ID),
        ("hyperliquid", "SOL", CYCLE_ID),
    ]


def test_adapter_failure_persists_only_successful_raw_and_failed_cycle_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)

    cycle = asyncio.run(
        collector(store).collect_once(
            (ImmediateBookAdapter(Venue.BYBIT), FailingBookAdapter()), ASSETS, NOW
        )
    )

    assert cycle.status == "failed"
    assert cycle.failure_codes == ("hyperliquid:TimeoutError",)
    assert cycle.source_hashes == (VENUE_HASHES[Venue.BYBIT],)
    assert store.latest_book_cycle_as_of(NOW + timedelta(seconds=1)) == cycle
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        counts = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "raw_envelopes",
                "book_snapshots",
                "book_levels",
                "book_collection_cycles",
            )
        )
    assert counts == (1, 0, 0, 1)


class CycleFailingStore(DuckDBStore):
    def append_book_collection_cycle(self, record: object) -> bool:
        raise RuntimeError("cycle insert failed")


def test_cycle_insert_failure_rolls_back_raw_and_books(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    store = CycleFailingStore(path)

    with pytest.raises(RuntimeError, match="cycle insert failed"):
        asyncio.run(
            collector(store).collect_once(
                (ImmediateBookAdapter(Venue.BYBIT), ImmediateBookAdapter(Venue.HYPERLIQUID)),
                ASSETS,
                NOW,
            )
        )
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        counts = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "raw_envelopes",
                "book_snapshots",
                "book_levels",
                "book_collection_cycles",
            )
        )
    assert counts == (0, 0, 0, 0)
