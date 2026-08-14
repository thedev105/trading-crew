from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from contextlib import contextmanager
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
from polytrading.venues.public import AdapterBatch, AdapterWarning
from polytrading.venues.synchronized import (
    PreparedBookCollectionCycle,
    SynchronizedBookCollector,
    persist_prepared_book_cycle,
)

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
LATER = NOW + timedelta(milliseconds=50)
CYCLE_ID = UUID("00000000-0000-0000-0000-000000000777")
ASSETS = frozenset({Asset.SOL, Asset.BTC, Asset.ETH})
VENUE_PAYLOADS = {
    venue: f'{{"venue":"{venue.value}"}}' for venue in (Venue.BYBIT, Venue.HYPERLIQUID)
}
VENUE_HASHES = {
    venue: hashlib.sha256(payload.encode()).hexdigest() for venue, payload in VENUE_PAYLOADS.items()
}


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
        payload_json=VENUE_PAYLOADS[venue],
        source_hash=VENUE_HASHES[venue],
    )


def distinct_raw_envelope(venue: Venue, event_id: UUID) -> RawEnvelope:
    return raw_envelope(venue).model_copy(update={"event_id": event_id})


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


class WrongVenueBatchAdapter:
    venue = Venue.HYPERLIQUID

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        return AdapterBatch(
            raw=(
                raw_envelope(self.venue),
                distinct_raw_envelope(
                    Venue.BYBIT,
                    UUID("00000000-0000-0000-0000-000000000003"),
                ),
            ),
            normalized=tuple(
                book_snapshot(
                    Venue.BYBIT,
                    asset,
                    cycle_id,
                    NOW + timedelta(seconds=10),
                )
                for asset in sorted(assets, key=lambda item: item.value)
            ),
        )


class ForeignExtraRawAdapter:
    venue = Venue.HYPERLIQUID

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        del observed_at
        return AdapterBatch(
            raw=(
                raw_envelope(self.venue),
                distinct_raw_envelope(
                    Venue.BYBIT,
                    UUID("00000000-0000-0000-0000-000000000003"),
                ),
            ),
            normalized=tuple(
                book_snapshot(self.venue, asset, cycle_id, NOW)
                for asset in sorted(assets, key=lambda item: item.value)
            ),
        )


class DuplicateRawIdentityAdapter:
    venue = Venue.HYPERLIQUID

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        del observed_at
        primary = raw_envelope(self.venue)
        extra_payload = '{"venue":"hyperliquid","request":"duplicate-id"}'
        duplicate = primary.model_copy(
            update={
                "endpoint": "/hyperliquid/public/book/duplicate",
                "payload_json": extra_payload,
                "source_hash": hashlib.sha256(extra_payload.encode()).hexdigest(),
            }
        )
        return AdapterBatch(
            raw=(primary, duplicate),
            normalized=tuple(
                book_snapshot(self.venue, asset, cycle_id, NOW)
                for asset in sorted(assets, key=lambda item: item.value)
            ),
        )


class DuplicateSourceHashAdapter(ImmediateBookAdapter):
    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        batch = await super().fetch_order_books(assets, observed_at, cycle_id)
        return AdapterBatch(
            raw=(
                *batch.raw,
                distinct_raw_envelope(
                    self.venue,
                    UUID("00000000-0000-0000-0000-000000000004"),
                ),
            ),
            normalized=batch.normalized,
        )


class DuplicateBookIdentityAdapter:
    venue = Venue.HYPERLIQUID

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        return AdapterBatch(
            raw=(raw_envelope(self.venue),),
            normalized=tuple(
                book_snapshot(self.venue, asset, cycle_id, NOW).model_copy(
                    update={"symbol": "DUPLICATE"}
                )
                for asset in sorted(assets, key=lambda item: item.value)
            ),
        )


class InvalidEvidenceBatchAdapter:
    venue = Venue.HYPERLIQUID

    def __init__(self, corruption: str) -> None:
        self.corruption = corruption

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        raw = raw_envelope(self.venue)
        books = tuple(
            book_snapshot(self.venue, asset, cycle_id, NOW)
            for asset in sorted(assets, key=lambda item: item.value)
        )
        if self.corruption == "bad_digest":
            raw = raw.model_copy(update={"source_hash": "f" * 64})
        elif self.corruption == "orphan_lineage":
            books = tuple(book.model_copy(update={"source_hash": "f" * 64}) for book in books)
        elif self.corruption == "cross_venue_lineage":
            raw = raw.model_copy(update={"venue": Venue.BYBIT})
        else:
            raise AssertionError(f"unknown corruption: {self.corruption}")
        return AdapterBatch(raw=(raw,), normalized=books)


class UnsupportedNormalizedBatchAdapter:
    venue = Venue.HYPERLIQUID

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        return AdapterBatch(
            raw=(raw_envelope(self.venue),),
            normalized=(object(),),  # type: ignore[arg-type]
        )


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


class RecordingStore:
    def __init__(self, *, fail_cycle: bool = False) -> None:
        self.events: list[str] = []
        self.fail_cycle = fail_cycle

    @contextmanager
    def transaction(self):  # type: ignore[no-untyped-def]
        self.events.append("begin")
        try:
            yield self
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    def append_raw(self, record: object) -> bool:
        self.events.append(f"raw:{record.event_id}")
        return True

    def append_book_snapshot(self, record: object) -> bool:
        self.events.append(f"book:{record.venue.value}:{record.asset.value}")
        return True

    def append_book_collection_cycle(self, record: object) -> bool:
        self.events.append(f"cycle:{record.cycle_id}")
        if self.fail_cycle:
            raise RuntimeError("cycle insert failed")
        return True


def complete_adapters() -> tuple[ImmediateBookAdapter, ImmediateBookAdapter]:
    return (
        ImmediateBookAdapter(Venue.BYBIT),
        ImmediateBookAdapter(Venue.HYPERLIQUID),
    )


def complete_prepared_book_cycle() -> PreparedBookCollectionCycle:
    collector = SynchronizedBookCollector(
        store=None,
        clock=SequenceClock(NOW, LATER),
        cycle_id_factory=lambda: CYCLE_ID,
    )
    return asyncio.run(collector.prepare_once(complete_adapters(), frozenset(Asset), NOW))


def collector(
    store: DuckDBStore,
    *,
    clock: Callable[[], datetime] | None = None,
    warning_sink: Callable[[AdapterWarning], None] | None = None,
) -> SynchronizedBookCollector:
    return SynchronizedBookCollector(
        store,
        clock=clock or SequenceClock(NOW, NOW + timedelta(milliseconds=50)),
        cycle_id_factory=lambda: CYCLE_ID,
        warning_sink=warning_sink,
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
    assert cycle.source_hashes == tuple(sorted(VENUE_HASHES.values()))
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


def test_invalid_batch_evidence_is_excluded_while_failed_cycle_persists(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)

    cycle = asyncio.run(
        collector(store).collect_once(
            (ImmediateBookAdapter(Venue.BYBIT), WrongVenueBatchAdapter()), ASSETS, NOW
        )
    )

    assert cycle.status == "failed"
    assert cycle.failure_codes == ("hyperliquid:venue_mismatch",)
    assert cycle.effective_timestamps == (NOW, NOW, NOW)
    assert cycle.max_effective_skew_ms == Decimal("0")
    assert cycle.source_hashes == (VENUE_HASHES[Venue.BYBIT],)
    assert store.latest_book_cycle_as_of(NOW + timedelta(seconds=1)) == cycle
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT venue, source_hash FROM raw_envelopes").fetchall() == [
            (Venue.BYBIT.value, VENUE_HASHES[Venue.BYBIT])
        ]
        assert connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (1,)


def test_foreign_extra_raw_is_scoped_failure_with_canonical_queryable_valid_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "foreign-extra.duckdb"
    store = DuckDBStore(path)
    prepared = asyncio.run(
        SynchronizedBookCollector(
            store=None,
            clock=SequenceClock(NOW, LATER),
            cycle_id_factory=lambda: CYCLE_ID,
        ).prepare_once(
            (ImmediateBookAdapter(Venue.BYBIT), ForeignExtraRawAdapter()),
            ASSETS,
            NOW,
        )
    )

    assert prepared.cycle.status == "failed"
    assert prepared.cycle.failure_codes == ("hyperliquid:venue_mismatch",)
    assert tuple(raw.event_id for raw in prepared.raw_records) == (UUID(int=1),)
    assert {(book.venue, book.asset) for book in prepared.books} == {
        (Venue.BYBIT, Asset.BTC),
        (Venue.BYBIT, Asset.ETH),
        (Venue.BYBIT, Asset.SOL),
    }
    assert prepared.cycle.source_hashes == (VENUE_HASHES[Venue.BYBIT],)

    assert persist_prepared_book_cycle(store, prepared) is True
    assert store.latest_book_cycle_as_of(LATER) == prepared.cycle
    assert store.book_collection_cycles_completed_between(NOW, LATER, LATER) == (prepared.cycle,)
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
        raw_rows = connection.execute(
            "SELECT event_id, venue, source_hash FROM raw_envelopes"
        ).fetchall()
    assert counts == (1, 0, 0, 1)
    assert raw_rows == [(UUID(int=1), Venue.BYBIT.value, VENUE_HASHES[Venue.BYBIT])]


def test_duplicate_raw_event_id_is_scoped_failure_instead_of_database_rollback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate-event.duckdb"
    store = DuckDBStore(path)
    prepared = asyncio.run(
        SynchronizedBookCollector(
            store=None,
            clock=SequenceClock(NOW, LATER),
            cycle_id_factory=lambda: CYCLE_ID,
        ).prepare_once(
            (DuplicateSourceHashAdapter(Venue.BYBIT), DuplicateRawIdentityAdapter()),
            ASSETS,
            NOW,
        )
    )

    assert prepared.cycle.status == "failed"
    assert prepared.cycle.failure_codes == ("hyperliquid:duplicate_raw_identity",)
    assert tuple(raw.event_id for raw in prepared.raw_records) == (UUID(int=1), UUID(int=4))
    assert {(book.venue, book.asset) for book in prepared.books} == {
        (Venue.BYBIT, Asset.BTC),
        (Venue.BYBIT, Asset.ETH),
        (Venue.BYBIT, Asset.SOL),
    }
    assert prepared.cycle.source_hashes == (VENUE_HASHES[Venue.BYBIT],)

    assert persist_prepared_book_cycle(store, prepared) is True
    assert store.latest_book_cycle_as_of(LATER) == prepared.cycle
    assert store.book_collection_cycles_completed_between(NOW, LATER, LATER) == (prepared.cycle,)
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
        event_ids = connection.execute(
            "SELECT event_id FROM raw_envelopes ORDER BY event_id"
        ).fetchall()
    assert counts == (2, 0, 0, 1)
    assert event_ids == [(UUID(int=1),), (UUID(int=4),)]


def test_duplicate_book_identity_is_failed_cycle_instead_of_database_rollback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)

    cycle = asyncio.run(
        collector(store).collect_once(
            (ImmediateBookAdapter(Venue.BYBIT), DuplicateBookIdentityAdapter()), ASSETS, NOW
        )
    )

    assert cycle.status == "failed"
    assert cycle.failure_codes == ("hyperliquid:duplicate_book_identity",)
    assert cycle.effective_timestamps == (NOW, NOW, NOW)
    assert cycle.max_effective_skew_ms == Decimal("0")
    assert cycle.source_hashes == (VENUE_HASHES[Venue.BYBIT],)
    assert store.latest_book_cycle_as_of(NOW + timedelta(seconds=1)) == cycle
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT venue, source_hash FROM raw_envelopes").fetchall() == [
            (Venue.BYBIT.value, VENUE_HASHES[Venue.BYBIT])
        ]
        assert connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (1,)


@pytest.mark.parametrize(
    ("corruption", "failure"),
    [
        ("bad_digest", "raw_source_hash_mismatch"),
        ("orphan_lineage", "normalized_lineage_mismatch"),
        ("cross_venue_lineage", "venue_mismatch"),
    ],
)
def test_invalid_evidence_batch_is_venue_failure_without_persisted_evidence(
    tmp_path: Path,
    corruption: str,
    failure: str,
) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)

    cycle = asyncio.run(
        collector(store).collect_once(
            (ImmediateBookAdapter(Venue.BYBIT), InvalidEvidenceBatchAdapter(corruption)),
            ASSETS,
            NOW,
        )
    )

    assert cycle.status == "failed"
    assert cycle.failure_codes == (f"hyperliquid:{failure}",)
    assert cycle.source_hashes == (VENUE_HASHES[Venue.BYBIT],)
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT venue, source_hash FROM raw_envelopes").fetchall() == [
            (Venue.BYBIT.value, VENUE_HASHES[Venue.BYBIT])
        ]
        assert connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (1,)


def test_unsupported_normalized_record_is_failed_cycle_without_invalid_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)

    cycle = asyncio.run(
        collector(store).collect_once(
            (ImmediateBookAdapter(Venue.BYBIT), UnsupportedNormalizedBatchAdapter()),
            ASSETS,
            NOW,
        )
    )

    assert cycle.status == "failed"
    assert cycle.failure_codes == ("hyperliquid:invalid_normalized_record",)
    assert cycle.source_hashes == (VENUE_HASHES[Venue.BYBIT],)
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT venue, source_hash FROM raw_envelopes").fetchall() == [
            (Venue.BYBIT.value, VENUE_HASHES[Venue.BYBIT])
        ]
        assert connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (1,)


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


class WarningBookAdapter(ImmediateBookAdapter):
    def __init__(self, venue: Venue, *, corrupt_hash: bool = False) -> None:
        super().__init__(venue)
        self.corrupt_hash = corrupt_hash

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        batch = await super().fetch_order_books(assets, observed_at, cycle_id)
        raw = batch.raw[0]
        if self.corrupt_hash:
            raw = raw.model_copy(update={"source_hash": "f" * 64})
        return AdapterBatch(
            raw=(raw,),
            normalized=batch.normalized,
            warnings=(
                AdapterWarning(
                    code="LOCAL_TIME",
                    venue=self.venue,
                    endpoint="/public/book",
                    symbol="BTC",
                    message="local receipt time was used",
                ),
            ),
        )


def test_validated_book_batch_sends_structured_warning_after_persistence(tmp_path: Path) -> None:
    # Catches book limitations disappearing before the operator-facing boundary.
    store = DuckDBStore(tmp_path / "warning.duckdb")
    warnings: list[AdapterWarning] = []

    cycle = asyncio.run(
        collector(store, warning_sink=warnings.append).collect_once(
            (WarningBookAdapter(Venue.HYPERLIQUID),), ASSETS, NOW
        )
    )

    assert cycle.status == "complete"
    assert warnings == [
        AdapterWarning(
            code="LOCAL_TIME",
            venue=Venue.HYPERLIQUID,
            endpoint="/public/book",
            symbol="BTC",
            message="local receipt time was used",
        )
    ]
    assert store.latest_book_cycle_as_of(NOW + timedelta(seconds=1)) == cycle
    store.close()


def test_invalid_book_batch_does_not_send_untrusted_warning(tmp_path: Path) -> None:
    # Catches warning text from a rejected/corrupt batch reaching an operator.
    store = DuckDBStore(tmp_path / "invalid-warning.duckdb")
    warnings: list[AdapterWarning] = []

    cycle = asyncio.run(
        collector(store, warning_sink=warnings.append).collect_once(
            (WarningBookAdapter(Venue.HYPERLIQUID, corrupt_hash=True),), ASSETS, NOW
        )
    )

    assert cycle.status == "failed"
    assert warnings == []
    store.close()


def test_prepare_once_has_no_store_side_effect() -> None:
    store = RecordingStore()
    collector = SynchronizedBookCollector(store=None, clock=SequenceClock(NOW, LATER))
    prepared = asyncio.run(collector.prepare_once(complete_adapters(), frozenset(Asset), NOW))

    assert prepared.cycle.status == "complete"
    assert store.events == []


def test_prepared_book_cycle_persists_in_one_transaction() -> None:
    store = RecordingStore()
    prepared = complete_prepared_book_cycle()

    assert persist_prepared_book_cycle(store, prepared) is True
    assert store.events[0] == "begin"
    assert store.events[-1] == "commit"


def test_prepared_book_cycle_rolls_back_when_cycle_append_fails() -> None:
    store = RecordingStore(fail_cycle=True)
    prepared = complete_prepared_book_cycle()

    with pytest.raises(RuntimeError, match="cycle insert failed"):
        persist_prepared_book_cycle(store, prepared)

    assert store.events[0] == "begin"
    assert store.events[-1] == "rollback"
    assert "commit" not in store.events


def test_collect_once_delegates_to_prepare_then_persist() -> None:
    store = RecordingStore()
    prepared = complete_prepared_book_cycle()
    collector = SynchronizedBookCollector(store)
    calls: list[tuple[object, object, object]] = []

    async def prepare_once(adapters: object, assets: object, observed_at: object):  # type: ignore[no-untyped-def]
        calls.append((adapters, assets, observed_at))
        return prepared

    collector.prepare_once = prepare_once  # type: ignore[method-assign]
    adapters = complete_adapters()

    cycle = asyncio.run(collector.collect_once(adapters, ASSETS, NOW))

    assert calls == [(adapters, ASSETS, NOW)]
    assert cycle == prepared.cycle
    assert store.events[0] == "begin"
    assert store.events[-1] == "commit"
