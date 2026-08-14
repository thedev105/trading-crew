from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from polytrading.domain.models import Asset, FundingObservation, Venue
from polytrading.storage.store import DuckDBStore
from polytrading.trial.funding_lineage import select_prospective_funding
from polytrading.trial.funding_models import (
    LighterDydxFundingCycle,
    TrialFundingCycleStatus,
    TrialFundingOutcome,
)
from tests.trial.funding_helpers import CYCLE_END, trial_funding_cycle


def funding_observation(
    *,
    venue: Venue = Venue.DYDX,
    symbol: str = "BTC-USD",
    asset: Asset = Asset.BTC,
    rate: Decimal = Decimal("0.0001"),
    interval_hours: Decimal = Decimal("1"),
    effective_at=CYCLE_END,
    observed_at=CYCLE_END + timedelta(minutes=1),
    source_hash: str = "7" * 64,
) -> FundingObservation:
    return FundingObservation(
        schema_version=1,
        venue=venue,
        symbol=symbol,
        asset=asset,
        rate=rate,
        interval_hours=interval_hours,
        effective_at=effective_at,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def candidate_cycle(
    *,
    cycle_id: UUID,
    observation: FundingObservation,
    degraded_other_asset: bool = False,
    late_other_asset: bool = False,
) -> LighterDydxFundingCycle:
    items = list(trial_funding_cycle(cycle_end=observation.effective_at).items)
    target_index = next(
        index
        for index, item in enumerate(items)
        if item.venue is observation.venue and item.asset is observation.asset
    )
    items[target_index] = items[target_index].model_copy(
        update={
            "funding_observed_at": observation.observed_at,
            "funding_source_hashes": (observation.source_hash,),
        }
    )
    status = TrialFundingCycleStatus.COMPLETE
    request_completed_at = observation.effective_at + timedelta(minutes=2)
    if degraded_other_asset:
        other_index = next(
            index
            for index, item in enumerate(items)
            if item.venue is Venue.DYDX and item.asset is Asset.ETH
        )
        items[other_index] = items[other_index].model_copy(
            update={
                "funding_outcome": TrialFundingOutcome.FAILED,
                "funding_effective_at": None,
                "funding_observed_at": None,
                "funding_source_hashes": (),
                "reason_codes": ("FUNDING_FAILED:dydx:ETH:TimeoutError",),
            }
        )
        status = TrialFundingCycleStatus.DEGRADED
    if late_other_asset:
        other_index = next(
            index
            for index, item in enumerate(items)
            if item.venue is Venue.DYDX and item.asset is Asset.ETH
        )
        items[other_index] = items[other_index].model_copy(
            update={"funding_observed_at": observation.effective_at + timedelta(minutes=6)}
        )
        request_completed_at = observation.effective_at + timedelta(minutes=7)
        status = TrialFundingCycleStatus.LATE
    return trial_funding_cycle(
        cycle_id=cycle_id,
        cycle_end=observation.effective_at,
        request_started_at=observation.effective_at + timedelta(seconds=10),
        request_completed_at=request_completed_at,
        items=tuple(items),
        status=status,
    )


def seed_mixed_funding_lineage(
    store: DuckDBStore,
) -> tuple[FundingObservation, FundingObservation, FundingObservation]:
    generic = funding_observation(
        observed_at=CYCLE_END + timedelta(seconds=30), source_hash="7" * 64
    )
    late = funding_observation(observed_at=CYCLE_END + timedelta(minutes=6), source_hash="8" * 64)
    eligible = funding_observation(source_hash="9" * 64)
    store.append_funding(generic)
    store.append_funding(late)
    store.append_funding(eligible)
    store.append_lighter_dydx_funding_cycle(
        candidate_cycle(cycle_id=UUID(int=2), observation=late, late_other_asset=True)
    )
    store.append_lighter_dydx_funding_cycle(
        candidate_cycle(cycle_id=UUID(int=3), observation=eligible)
    )
    return generic, late, eligible


def test_only_on_time_candidate_linkage_selects_funding(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    generic, late, eligible = seed_mixed_funding_lineage(store)

    selected = select_prospective_funding(
        store,
        Venue.DYDX,
        "BTC-USD",
        Asset.BTC,
        CYCLE_END - timedelta(hours=1),
        CYCLE_END,
        CYCLE_END + timedelta(minutes=5),
    )

    assert selected.observations == (eligible,)
    assert selected.selected_cycle_ids == (UUID(int=3),)
    assert generic.source_hash not in selected.source_hashes
    assert late.source_hash not in selected.source_hashes
    store.close()


@pytest.mark.parametrize(
    "mismatch",
    ["venue", "asset", "symbol", "effective_at", "observed_at", "interval", "source_hash"],
)
def test_exact_candidate_identity_mismatches_are_excluded(tmp_path: Path, mismatch: str) -> None:
    expected = funding_observation(source_hash="a" * 64)
    values = expected.model_dump()
    values.update(
        {
            "venue": Venue.LIGHTER,
            "asset": Asset.ETH,
            "symbol": "BTC",
            "effective_at": CYCLE_END - timedelta(minutes=1),
            "observed_at": CYCLE_END + timedelta(minutes=2),
            "interval": Decimal("8"),
            "source_hash": "b" * 64,
        }
    )
    field = "interval_hours" if mismatch == "interval" else mismatch
    mismatched = expected.model_copy(update={field: values[mismatch]})
    store = DuckDBStore(tmp_path / f"{mismatch}.duckdb")
    store.append_funding(mismatched)
    store.append_lighter_dydx_funding_cycle(
        candidate_cycle(cycle_id=UUID(int=10), observation=expected)
    )

    selected = select_prospective_funding(
        store,
        Venue.DYDX,
        "BTC-USD",
        Asset.BTC,
        CYCLE_END - timedelta(hours=1),
        CYCLE_END,
        CYCLE_END + timedelta(minutes=5),
    )

    assert selected.observations == ()
    assert selected.selected_cycle_ids == ()
    assert mismatched.source_hash not in selected.source_hashes
    store.close()


def test_start_boundary_is_exclusive(tmp_path: Path) -> None:
    observation = funding_observation(
        effective_at=CYCLE_END - timedelta(hours=1),
        observed_at=CYCLE_END - timedelta(hours=1) + timedelta(minutes=1),
    )
    store = DuckDBStore(tmp_path / "exclusive.duckdb")
    store.append_funding(observation)
    store.append_lighter_dydx_funding_cycle(
        candidate_cycle(cycle_id=UUID(int=11), observation=observation)
    )

    selected = select_prospective_funding(
        store,
        Venue.DYDX,
        "BTC-USD",
        Asset.BTC,
        CYCLE_END - timedelta(hours=1),
        CYCLE_END,
        CYCLE_END + timedelta(minutes=5),
    )

    assert selected.observations == ()
    store.close()


def test_late_cycle_item_observed_after_cutoff_is_excluded(tmp_path: Path) -> None:
    observation = funding_observation(
        observed_at=CYCLE_END + timedelta(minutes=6), source_hash="b" * 64
    )
    store = DuckDBStore(tmp_path / "late-item.duckdb")
    store.append_funding(observation)
    store.append_lighter_dydx_funding_cycle(
        candidate_cycle(cycle_id=UUID(int=13), observation=observation, late_other_asset=True)
    )

    selected = select_prospective_funding(
        store,
        Venue.DYDX,
        "BTC-USD",
        Asset.BTC,
        CYCLE_END - timedelta(hours=1),
        CYCLE_END,
        CYCLE_END + timedelta(minutes=7),
    )

    assert selected.observations == ()
    assert observation.source_hash not in selected.source_hashes
    store.close()


@pytest.mark.parametrize("cycle_condition", ["degraded", "late"])
def test_other_asset_cycle_condition_does_not_disqualify_timely_captured_item(
    tmp_path: Path, cycle_condition: str
) -> None:
    observation = funding_observation(source_hash="c" * 64)
    store = DuckDBStore(tmp_path / f"{cycle_condition}.duckdb")
    store.append_funding(observation)
    store.append_lighter_dydx_funding_cycle(
        candidate_cycle(
            cycle_id=UUID(int=12),
            observation=observation,
            degraded_other_asset=cycle_condition == "degraded",
            late_other_asset=cycle_condition == "late",
        )
    )

    selected = select_prospective_funding(
        store,
        Venue.DYDX,
        "BTC-USD",
        Asset.BTC,
        CYCLE_END - timedelta(hours=1),
        CYCLE_END,
        CYCLE_END + timedelta(minutes=7),
    )

    assert selected.observations == (observation,)
    assert selected.selected_cycle_ids == (UUID(int=12),)
    store.close()


def test_same_value_retries_select_earliest_observation_then_uuid(tmp_path: Path) -> None:
    first_boundary = CYCLE_END - timedelta(hours=1)
    later = funding_observation(
        effective_at=first_boundary,
        observed_at=first_boundary + timedelta(minutes=2),
        source_hash="d" * 64,
    )
    earlier = later.model_copy(
        update={
            "observed_at": first_boundary + timedelta(minutes=1),
            "source_hash": "e" * 64,
        }
    )
    tied = funding_observation(source_hash="f" * 64)
    store = DuckDBStore(tmp_path / "retries.duckdb")
    for observation in (later, earlier, tied):
        store.append_funding(observation)
    for cycle_id, observation in (
        (UUID(int=20), later),
        (UUID(int=21), earlier),
        (UUID(int=23), tied),
        (UUID(int=22), tied),
    ):
        store.append_lighter_dydx_funding_cycle(
            candidate_cycle(cycle_id=cycle_id, observation=observation)
        )

    selected = select_prospective_funding(
        store,
        Venue.DYDX,
        "BTC-USD",
        Asset.BTC,
        CYCLE_END - timedelta(hours=2),
        CYCLE_END,
        CYCLE_END + timedelta(minutes=5),
    )

    assert selected.observations == (earlier, tied)
    assert selected.selected_cycle_ids == (UUID(int=21), UUID(int=22))
    assert selected.conflict_boundaries == ()
    assert selected.source_hashes == tuple(
        sorted((later.source_hash, earlier.source_hash, tied.source_hash))
    )
    store.close()


def test_conflicting_linked_revisions_withhold_boundary_and_retain_hashes(
    tmp_path: Path,
) -> None:
    first = funding_observation(source_hash="a" * 64)
    second = first.model_copy(
        update={
            "rate": Decimal("0.0002"),
            "observed_at": CYCLE_END + timedelta(minutes=2),
            "source_hash": "b" * 64,
        }
    )
    store = DuckDBStore(tmp_path / "conflict.duckdb")
    for cycle_id, observation in ((UUID(int=30), first), (UUID(int=31), second)):
        store.append_funding(observation)
        store.append_lighter_dydx_funding_cycle(
            candidate_cycle(cycle_id=cycle_id, observation=observation)
        )

    selected = select_prospective_funding(
        store,
        Venue.DYDX,
        "BTC-USD",
        Asset.BTC,
        CYCLE_END - timedelta(hours=1),
        CYCLE_END,
        CYCLE_END + timedelta(minutes=5),
    )

    assert selected.observations == ()
    assert selected.selected_cycle_ids == ()
    assert selected.conflict_boundaries == (CYCLE_END,)
    assert selected.source_hashes == (first.source_hash, second.source_hash)
    store.close()
