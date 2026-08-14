from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from polytrading.domain.models import Asset, Venue
from polytrading.storage.store import DuckDBStore
from polytrading.trial.book_evidence import (
    eligible_lighter_dydx_book_pair,
    select_hourly_trial_books,
)
from tests.domain.factories import book_collection_cycle, book_snapshot

BOUNDARY = datetime(2026, 8, 14, 7, tzinfo=UTC)
DYDX_HASH = "3" * 64
LIGHTER_HASH = "4" * 64


def append_pair(
    store: DuckDBStore,
    identity: int,
    completed_at: datetime,
    *,
    asset: Asset = Asset.BTC,
    skew_ms: Decimal = Decimal("100"),
    status: str = "complete",
    venues: tuple[Venue, ...] = (Venue.DYDX, Venue.LIGHTER),
    cycle_hashes: tuple[str, ...] = (DYDX_HASH, LIGHTER_HASH),
    dydx_symbol: str | None = None,
    lighter_symbol: str | None = None,
    snapshot_observed_at: datetime | None = None,
    snapshot_effective_at: datetime | None = None,
    duplicate_dydx: bool = False,
) -> None:
    cycle_id = UUID(int=identity)
    dydx_effective = snapshot_effective_at or completed_at - timedelta(
        microseconds=int(skew_ms * 1_000)
    )
    lighter_effective = snapshot_effective_at or completed_at
    observed_at = snapshot_observed_at or completed_at
    store.append_book_collection_cycle(
        book_collection_cycle(
            cycle_id=cycle_id,
            assets=(asset,),
            venues=venues,
            request_started_at=completed_at - timedelta(seconds=1),
            request_completed_at=completed_at,
            effective_timestamps=(dydx_effective, lighter_effective),
            max_effective_skew_ms=skew_ms,
            status=status,
            failure_codes=() if status == "complete" else ("FAILED",),
            source_hashes=cycle_hashes,
        )
    )
    if Venue.DYDX in venues:
        store.append_book_snapshot(
            book_snapshot(
                cycle_id=cycle_id,
                venue=Venue.DYDX,
                symbol=dydx_symbol or f"{asset.value}-USD",
                asset=asset,
                effective_at=dydx_effective,
                observed_at=observed_at,
                source_hash=DYDX_HASH,
            )
        )
        if duplicate_dydx:
            store.append_book_snapshot(
                book_snapshot(
                    cycle_id=cycle_id,
                    venue=Venue.DYDX,
                    symbol=f"{asset.value}-DUPLICATE",
                    asset=asset,
                    effective_at=dydx_effective,
                    observed_at=observed_at,
                    source_hash=DYDX_HASH,
                )
            )
    if Venue.LIGHTER in venues:
        store.append_book_snapshot(
            book_snapshot(
                cycle_id=cycle_id,
                venue=Venue.LIGHTER,
                symbol=lighter_symbol or asset.value,
                asset=asset,
                effective_at=lighter_effective,
                observed_at=observed_at,
                source_hash=LIGHTER_HASH,
            )
        )


def seeded_book_store(tmp_path: Path, completions: tuple[datetime, ...]) -> DuckDBStore:
    store = DuckDBStore(tmp_path / "books.duckdb")
    for identity, completed_at in enumerate(completions, start=1):
        append_pair(store, identity, completed_at)
    return store


def test_hourly_book_selection_never_looks_after_boundary(tmp_path: Path) -> None:
    store = seeded_book_store(
        tmp_path,
        completions=(BOUNDARY - timedelta(seconds=1), BOUNDARY + timedelta(microseconds=1)),
    )
    selected = select_hourly_trial_books(
        store,
        Asset.BTC,
        BOUNDARY - timedelta(hours=1),
        BOUNDARY,
        BOUNDARY + timedelta(minutes=5),
        maximum_age_seconds=Decimal("300"),
        maximum_skew_ms=Decimal("1000"),
    )
    assert len(selected) == 1
    assert selected[0].cycle.request_completed_at == BOUNDARY - timedelta(seconds=1)
    store.close()


@pytest.mark.parametrize(
    ("age", "expected"),
    ((timedelta(minutes=5), 1), (timedelta(minutes=5, microseconds=1), 0)),
)
def test_hourly_book_age_limit_is_exact(tmp_path: Path, age: timedelta, expected: int) -> None:
    store = seeded_book_store(tmp_path, completions=(BOUNDARY - age,))
    selected = select_hourly_trial_books(
        store,
        Asset.BTC,
        BOUNDARY - timedelta(hours=1),
        BOUNDARY,
        BOUNDARY + timedelta(minutes=5),
        maximum_age_seconds=Decimal("300"),
        maximum_skew_ms=Decimal("1000"),
    )
    assert len(selected) == expected
    store.close()


@pytest.mark.parametrize(("skew", "expected"), ((Decimal("1000"), True), (Decimal("1001"), False)))
def test_cycle_skew_limit_is_inclusive(tmp_path: Path, skew: Decimal, expected: bool) -> None:
    store = DuckDBStore(tmp_path / "books.duckdb")
    append_pair(store, 1, BOUNDARY - timedelta(seconds=1), skew_ms=skew)
    cycle = store.latest_book_cycle_as_of(BOUNDARY)
    assert cycle is not None
    assert (
        eligible_lighter_dydx_book_pair(store, cycle, Asset.BTC, BOUNDARY, Decimal("1000"))
        is not None
    ) is expected
    store.close()


@pytest.mark.parametrize(
    "overrides",
    (
        {"status": "failed"},
        {"venues": (Venue.DYDX,)},
        {"cycle_hashes": ()},
        {"dydx_symbol": "BTC"},
        {"lighter_symbol": "BTC-USD"},
        {"cycle_hashes": (LIGHTER_HASH,)},
        {"snapshot_observed_at": BOUNDARY + timedelta(microseconds=1)},
        {"snapshot_effective_at": BOUNDARY + timedelta(microseconds=1)},
        {"duplicate_dydx": True},
    ),
)
def test_malformed_or_future_book_cycle_is_ineligible(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    store = DuckDBStore(tmp_path / "books.duckdb")
    append_pair(store, 1, BOUNDARY, **overrides)  # type: ignore[arg-type]
    cycle = store.latest_book_cycle_as_of(BOUNDARY)
    assert cycle is not None
    assert (
        eligible_lighter_dydx_book_pair(store, cycle, Asset.BTC, BOUNDARY, Decimal("1000")) is None
    )
    store.close()


def test_hourly_labels_do_not_rewrite_source_timestamps(tmp_path: Path) -> None:
    completed_at = BOUNDARY - timedelta(seconds=2)
    store = seeded_book_store(tmp_path, completions=(completed_at,))
    selected = select_hourly_trial_books(
        store,
        Asset.BTC,
        BOUNDARY - timedelta(hours=1),
        BOUNDARY,
        BOUNDARY + timedelta(minutes=5),
        maximum_age_seconds=Decimal("300"),
        maximum_skew_ms=Decimal("1000"),
    )
    assert selected[0].pair.effective_at == BOUNDARY
    assert selected[0].pair.dydx.effective_at == completed_at - timedelta(milliseconds=100)
    assert selected[0].pair.lighter.observed_at == completed_at
    store.close()


def test_hourly_window_is_start_exclusive_and_end_inclusive(tmp_path: Path) -> None:
    store = seeded_book_store(tmp_path, completions=(BOUNDARY - timedelta(hours=1, seconds=1),))
    selected = select_hourly_trial_books(
        store,
        Asset.BTC,
        BOUNDARY - timedelta(hours=2),
        BOUNDARY,
        BOUNDARY + timedelta(minutes=5),
        maximum_age_seconds=Decimal("3601"),
        maximum_skew_ms=Decimal("1000"),
    )
    assert tuple(item.pair.effective_at for item in selected) == (
        BOUNDARY - timedelta(hours=1),
        BOUNDARY,
    )
    store.close()
