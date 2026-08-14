from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from polytrading.domain.models import Asset, Venue, normalize_utc_timestamp
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from polytrading.trial.writer_lease import WriterLeaseUnavailable, database_writer_lease
from polytrading.venues.public import PublicVenueAdapter
from polytrading.venues.synchronized import (
    SynchronizedBookCollector,
    persist_prepared_book_cycle,
)

_EXPECTED_VENUES = (Venue.DYDX, Venue.LIGHTER)
_MAX_FAILURE_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True)
class TrialBookRunSummary:
    attempted_cycles: int
    persisted_cycles: int
    failed_cycles: int
    skewed_cycles: int
    lease_skipped_cycles: int


class TrialBookStoreCloseError(RuntimeError):
    """A per-cycle trial store failed during cleanup."""

    def __init__(self) -> None:
        super().__init__("TRIAL_BOOK_STORE_CLOSE_ERROR")


async def run_trial_book_session(
    adapters: Iterable[PublicVenueAdapter],
    database_path: Path,
    *,
    duration_seconds: float | None,
    interval_seconds: float,
    monotonic: Callable[[], float],
    wall_clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    store_factory: Callable[[Path], DuckDBStore],
) -> TrialBookRunSummary:
    _require_finite_positive(interval_seconds, "interval seconds")
    if duration_seconds is not None:
        _require_finite_positive(duration_seconds, "duration seconds")

    ordered_adapters = tuple(sorted(adapters, key=lambda adapter: adapter.venue.value))
    adapter_venues = tuple(adapter.venue for adapter in ordered_adapters)
    if adapter_venues != _EXPECTED_VENUES:
        raise ValueError("trial books require exactly the dYdX and Lighter adapters")

    collector = SynchronizedBookCollector(store=None, clock=wall_clock)
    assets = frozenset(Asset)
    started = monotonic()
    deadline = started + duration_seconds if duration_seconds is not None else math.inf
    attempted_cycles = 0
    persisted_cycles = 0
    failed_cycles = 0
    skewed_cycles = 0
    lease_skipped_cycles = 0
    consecutive_failures = 0

    while True:
        if monotonic() >= deadline:
            break
        attempted_cycles += 1
        prepared = await collector.prepare_once(
            ordered_adapters,
            assets,
            normalize_utc_timestamp(wall_clock()),
        )

        persistence_failed = False
        try:
            with database_writer_lease(database_path, timeout_seconds=0):
                store: DuckDBStore | None = None
                active_error: BaseException | None = None
                try:
                    store = store_factory(database_path)
                    persist_prepared_book_cycle(store, prepared)
                except BaseException as error:
                    active_error = error
                    raise
                finally:
                    if store is not None:
                        try:
                            store.close()
                        except Exception as error:
                            if active_error is None:
                                raise TrialBookStoreCloseError() from error
            persisted_cycles += 1
            if prepared.cycle.status == "failed":
                failed_cycles += 1
            elif prepared.cycle.status == "skew_exceeds_research_target":
                skewed_cycles += 1
        except TrialBookStoreCloseError:
            raise
        except WriterLeaseUnavailable:
            lease_skipped_cycles += 1
            persistence_failed = True
        except (ConflictingRecordError, duckdb.Error, OSError, RuntimeError):
            persistence_failed = True

        cycle_failed = persistence_failed or prepared.cycle.status == "failed"
        consecutive_failures = consecutive_failures + 1 if cycle_failed else 0
        if duration_seconds is None:
            break

        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        delay = interval_seconds
        if consecutive_failures:
            delay = _failure_backoff(interval_seconds, consecutive_failures)
        await sleep(min(delay, remaining))

    return TrialBookRunSummary(
        attempted_cycles,
        persisted_cycles,
        failed_cycles,
        skewed_cycles,
        lease_skipped_cycles,
    )


def _require_finite_positive(value: float, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite and positive")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")


def _failure_backoff(interval_seconds: float, consecutive_failures: int) -> float:
    delay = min(interval_seconds, _MAX_FAILURE_BACKOFF_SECONDS)
    for _ in range(consecutive_failures - 1):
        delay = min(delay * 2, _MAX_FAILURE_BACKOFF_SECONDS)
        if delay == _MAX_FAILURE_BACKOFF_SECONDS:
            break
    return delay
