from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

import polytrading.trial.books as trial_books
from polytrading.domain.models import (
    Asset,
    BookLevel,
    Level2BookSnapshot,
    RawEnvelope,
    Venue,
)
from polytrading.trial.books import TrialBookRunSummary, run_trial_book_session
from polytrading.trial.writer_lease import WriterLeaseUnavailable, database_writer_lease
from polytrading.venues.public import AdapterBatch

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


class FakeBookAdapter:
    def __init__(
        self,
        venue: Venue,
        *,
        fail: bool = False,
        effective_offset: timedelta = timedelta(),
        advance: Callable[[], None] | None = None,
    ) -> None:
        self.venue = venue
        self.fail = fail
        self.effective_offset = effective_offset
        self.advance = advance
        self.calls: list[frozenset[Asset]] = []

    async def fetch_order_books(
        self,
        assets: frozenset[Asset],
        observed_at: datetime,
        cycle_id: UUID,
    ) -> AdapterBatch:
        self.calls.append(assets)
        if self.advance is not None:
            self.advance()
        await asyncio.sleep(0)
        if self.fail:
            raise TimeoutError("public book request failed")
        payload = f'{{"call":{len(self.calls)},"venue":"{self.venue.value}"}}'
        source_hash = hashlib.sha256(payload.encode()).hexdigest()
        raw = RawEnvelope(
            schema_version=1,
            event_id=UUID(int=len(self.calls) * 10 + (1 if self.venue is Venue.DYDX else 2)),
            venue=self.venue,
            endpoint=f"/{self.venue.value}/books",
            venue_timestamp=None,
            observed_at=observed_at,
            received_monotonic_ns=len(self.calls),
            request_latency_ms=Decimal("1"),
            source_version="trial-test-v1",
            payload_json=payload,
            source_hash=source_hash,
        )
        books = tuple(
            _book(
                self.venue,
                asset,
                cycle_id,
                observed_at,
                observed_at + self.effective_offset,
                source_hash,
            )
            for asset in sorted(assets, key=lambda item: item.value)
        )
        return AdapterBatch(raw=(raw,), normalized=books)


def _book(
    venue: Venue,
    asset: Asset,
    cycle_id: UUID,
    observed_at: datetime,
    effective_at: datetime,
    source_hash: str,
) -> Level2BookSnapshot:
    middle = {Asset.BTC: Decimal("65000"), Asset.ETH: Decimal("3500"), Asset.SOL: Decimal("150")}[
        asset
    ]
    symbol = f"{asset.value}-USD" if venue is Venue.DYDX else asset.value
    return Level2BookSnapshot(
        schema_version=1,
        cycle_id=cycle_id,
        venue=venue,
        symbol=symbol,
        asset=asset,
        bids=(BookLevel(price=middle - 1, quantity=Decimal("1"), order_count=1),),
        asks=(BookLevel(price=middle + 1, quantity=Decimal("1"), order_count=1),),
        depth_limit=20,
        sequence=None,
        effective_at=effective_at,
        observed_at=observed_at,
        source_hash=source_hash,
    )


class RecordingStore:
    def __init__(
        self,
        events: list[str],
        *,
        cancel_on_cycle: bool = False,
        close_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.cancel_on_cycle = cancel_on_cycle
        self.close_error = close_error

    @contextmanager
    def transaction(self):  # type: ignore[no-untyped-def]
        yield self

    def append_raw(self, _record: object) -> bool:
        return True

    def append_book_snapshot(self, _record: object) -> bool:
        return True

    def append_book_collection_cycle(self, _record: object) -> bool:
        if self.cancel_on_cycle:
            raise asyncio.CancelledError
        return True

    def close(self) -> None:
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error


class RecordingStoreFactory:
    def __init__(
        self,
        *,
        cancel_on_cycle: bool = False,
        close_error: BaseException | None = None,
    ) -> None:
        self.events: list[str] = []
        self.cancel_on_cycle = cancel_on_cycle
        self.close_error = close_error

    def __call__(self, _path: Path) -> RecordingStore:
        self.events.append("open")
        return RecordingStore(
            self.events,
            cancel_on_cycle=self.cancel_on_cycle,
            close_error=self.close_error,
        )


class AdvancingClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.value += delay


def complete_trial_adapters() -> tuple[FakeBookAdapter, FakeBookAdapter]:
    return FakeBookAdapter(Venue.DYDX), FakeBookAdapter(Venue.LIGHTER)


def test_trial_book_session_closes_store_between_five_second_samples(tmp_path: Path) -> None:
    stores = RecordingStoreFactory()
    clock = AdvancingClock()
    summary = asyncio.run(
        run_trial_book_session(
            complete_trial_adapters(),
            tmp_path / "trial.duckdb",
            duration_seconds=11,
            interval_seconds=5,
            monotonic=clock.monotonic,
            wall_clock=lambda: NOW,
            sleep=clock.sleep,
            store_factory=stores,
        )
    )
    assert summary.attempted_cycles == 3
    assert summary.persisted_cycles == 3
    assert stores.events == ["open", "close", "open", "close", "open", "close"]
    assert clock.delays == [5, 5, 1]


def test_trial_book_session_uses_exact_pair_and_all_assets_once(tmp_path: Path) -> None:
    adapters = complete_trial_adapters()
    stores = RecordingStoreFactory()

    summary = asyncio.run(
        run_trial_book_session(
            reversed(adapters),
            tmp_path / "trial.duckdb",
            duration_seconds=None,
            interval_seconds=5,
            monotonic=lambda: 0,
            wall_clock=lambda: NOW,
            sleep=asyncio.sleep,
            store_factory=stores,
        )
    )

    assert summary == TrialBookRunSummary(1, 1, 0, 0, 0)
    assert tuple(adapter.venue for adapter in adapters) == (Venue.DYDX, Venue.LIGHTER)
    assert [adapter.calls for adapter in adapters] == [[frozenset(Asset)], [frozenset(Asset)]]


def test_trial_book_session_rejects_non_exact_venue_pair_before_requests(tmp_path: Path) -> None:
    dydx = FakeBookAdapter(Venue.DYDX)
    wrong = FakeBookAdapter(Venue.HYPERLIQUID)

    with pytest.raises(ValueError, match="dYdX and Lighter"):
        asyncio.run(
            run_trial_book_session(
                (dydx, wrong),
                tmp_path / "trial.duckdb",
                duration_seconds=None,
                interval_seconds=5,
                monotonic=lambda: 0,
                wall_clock=lambda: NOW,
                sleep=asyncio.sleep,
                store_factory=RecordingStoreFactory(),
            )
        )

    assert dydx.calls == wrong.calls == []


def test_trial_book_session_attempts_at_most_twelve_zero_latency_samples(tmp_path: Path) -> None:
    clock = AdvancingClock()
    adapters = complete_trial_adapters()

    summary = asyncio.run(
        run_trial_book_session(
            adapters,
            tmp_path / "trial.duckdb",
            duration_seconds=60,
            interval_seconds=5,
            monotonic=clock.monotonic,
            wall_clock=lambda: NOW,
            sleep=clock.sleep,
            store_factory=RecordingStoreFactory(),
        )
    )

    assert summary.attempted_cycles == 12
    assert all(len(adapter.calls) == 12 for adapter in adapters)


def test_trial_book_once_attempts_exactly_once_without_sleep(tmp_path: Path) -> None:
    clock = AdvancingClock()
    summary = asyncio.run(
        run_trial_book_session(
            complete_trial_adapters(),
            tmp_path / "trial.duckdb",
            duration_seconds=None,
            interval_seconds=5,
            monotonic=clock.monotonic,
            wall_clock=lambda: NOW,
            sleep=clock.sleep,
            store_factory=RecordingStoreFactory(),
        )
    )

    assert summary.attempted_cycles == 1
    assert clock.delays == []


def test_trial_book_lease_contention_skips_store_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def busy_lease(_path: Path, *, timeout_seconds: float) -> Iterator[None]:
        assert timeout_seconds == 0
        raise WriterLeaseUnavailable("busy")
        yield

    stores = RecordingStoreFactory()
    monkeypatch.setattr(trial_books, "database_writer_lease", busy_lease)

    summary = asyncio.run(
        run_trial_book_session(
            complete_trial_adapters(),
            tmp_path / "trial.duckdb",
            duration_seconds=None,
            interval_seconds=5,
            monotonic=lambda: 0,
            wall_clock=lambda: NOW,
            sleep=asyncio.sleep,
            store_factory=stores,
        )
    )

    assert summary == TrialBookRunSummary(1, 0, 0, 0, 1)
    assert stores.events == []


@pytest.mark.parametrize(
    ("adapters", "expected"),
    [
        (
            (FakeBookAdapter(Venue.DYDX), FakeBookAdapter(Venue.LIGHTER, fail=True)),
            TrialBookRunSummary(1, 1, 1, 0, 0),
        ),
        (
            (
                FakeBookAdapter(Venue.DYDX),
                FakeBookAdapter(Venue.LIGHTER, effective_offset=timedelta(seconds=2)),
            ),
            TrialBookRunSummary(1, 1, 0, 1, 0),
        ),
    ],
)
def test_trial_book_session_counts_persisted_failed_and_skewed_diagnostics(
    tmp_path: Path,
    adapters: tuple[FakeBookAdapter, FakeBookAdapter],
    expected: TrialBookRunSummary,
) -> None:
    summary = asyncio.run(
        run_trial_book_session(
            adapters,
            tmp_path / "trial.duckdb",
            duration_seconds=None,
            interval_seconds=5,
            monotonic=lambda: 0,
            wall_clock=lambda: NOW,
            sleep=asyncio.sleep,
            store_factory=RecordingStoreFactory(),
        )
    )

    assert summary == expected


def test_trial_book_session_caps_failure_backoff_and_deadline_sleep(tmp_path: Path) -> None:
    clock = AdvancingClock()
    adapters = (
        FakeBookAdapter(Venue.DYDX),
        FakeBookAdapter(Venue.LIGHTER, fail=True),
    )

    summary = asyncio.run(
        run_trial_book_session(
            adapters,
            tmp_path / "trial.duckdb",
            duration_seconds=100,
            interval_seconds=5,
            monotonic=clock.monotonic,
            wall_clock=lambda: NOW,
            sleep=clock.sleep,
            store_factory=RecordingStoreFactory(),
        )
    )

    assert summary == TrialBookRunSummary(6, 6, 6, 0, 0)
    assert clock.delays == [5, 10, 20, 30, 30, 5]


def test_trial_book_session_does_not_catch_up_after_slow_request(tmp_path: Path) -> None:
    clock = AdvancingClock()

    def advance_request() -> None:
        clock.value += 7

    adapters = (
        FakeBookAdapter(Venue.DYDX, advance=advance_request),
        FakeBookAdapter(Venue.LIGHTER),
    )

    summary = asyncio.run(
        run_trial_book_session(
            adapters,
            tmp_path / "trial.duckdb",
            duration_seconds=11,
            interval_seconds=5,
            monotonic=clock.monotonic,
            wall_clock=lambda: NOW,
            sleep=clock.sleep,
            store_factory=RecordingStoreFactory(),
        )
    )

    assert summary.attempted_cycles == 1
    assert clock.delays == [4]


def test_trial_book_session_closes_store_and_releases_lease_on_cancellation(tmp_path: Path) -> None:
    database = tmp_path / "trial.duckdb"
    stores = RecordingStoreFactory(cancel_on_cycle=True)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_trial_book_session(
                complete_trial_adapters(),
                database,
                duration_seconds=None,
                interval_seconds=5,
                monotonic=lambda: 0,
                wall_clock=lambda: NOW,
                sleep=asyncio.sleep,
                store_factory=stores,
            )
        )

    assert stores.events == ["open", "close"]
    with database_writer_lease(database, timeout_seconds=0):
        pass


def test_trial_book_store_close_failure_does_not_replace_primary_cancellation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "trial.duckdb"
    close_secret = OSError("/private/evidence/books.duckdb token=secret lock-owner=private")
    stores = RecordingStoreFactory(cancel_on_cycle=True, close_error=close_secret)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_trial_book_session(
                complete_trial_adapters(),
                database,
                duration_seconds=None,
                interval_seconds=5,
                monotonic=lambda: 0,
                wall_clock=lambda: NOW,
                sleep=asyncio.sleep,
                store_factory=stores,
            )
        )

    assert stores.events == ["open", "close"]
    with database_writer_lease(database, timeout_seconds=0):
        pass


def test_trial_book_store_close_only_failure_escapes_as_sanitizable_error(
    tmp_path: Path,
) -> None:
    database = tmp_path / "trial.duckdb"
    close_secret = OSError(
        "/private/evidence/books.duckdb "
        "https://private.example.test/books?token=secret "
        "response-body=confidential lock-owner=private"
    )
    stores = RecordingStoreFactory(close_error=close_secret)

    with pytest.raises(RuntimeError, match=r"^TRIAL_BOOK_STORE_CLOSE_ERROR$") as captured:
        asyncio.run(
            run_trial_book_session(
                complete_trial_adapters(),
                database,
                duration_seconds=None,
                interval_seconds=5,
                monotonic=lambda: 0,
                wall_clock=lambda: NOW,
                sleep=asyncio.sleep,
                store_factory=stores,
            )
        )

    assert captured.value.__cause__ is close_secret
    assert str(close_secret) not in str(captured.value)
    assert stores.events == ["open", "close"]
    with database_writer_lease(database, timeout_seconds=0):
        pass
