from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from polytrading.domain.models import Asset, FundingObservation, Venue
from polytrading.storage.store import DuckDBStore
from polytrading.trial.funding_models import (
    TRIAL_FUNDING_POINT_IN_TIME_LAG,
    TrialFundingOutcome,
)


@dataclass(frozen=True)
class SelectedProspectiveFunding:
    cycle_id: UUID
    observation: FundingObservation


@dataclass(frozen=True)
class ProspectiveFundingSelection:
    observations: tuple[FundingObservation, ...]
    selected_cycle_ids: tuple[UUID, ...]
    conflict_boundaries: tuple[datetime, ...]
    source_hashes: tuple[str, ...]


def select_prospective_funding(
    store: DuckDBStore,
    venue: Venue,
    symbol: str,
    asset: Asset,
    start: datetime,
    end: datetime,
    known_as_of: datetime,
) -> ProspectiveFundingSelection:
    cycles = store.lighter_dydx_funding_cycles_between(start, end, known_as_of)
    observations = store.funding_revisions_between(venue, symbol, start, end, known_as_of)
    rows_by_identity: dict[
        tuple[Venue, str, Asset, datetime, datetime, str], list[FundingObservation]
    ] = defaultdict(list)
    for observation in observations:
        rows_by_identity[
            (
                observation.venue,
                observation.symbol,
                observation.asset,
                observation.effective_at,
                observation.observed_at,
                observation.source_hash,
            )
        ].append(observation)

    candidates: dict[datetime, list[SelectedProspectiveFunding]] = defaultdict(list)
    source_hashes: set[str] = set()
    for cycle in cycles:
        if not start < cycle.cycle_end <= end:
            continue
        cutoff = cycle.cycle_end + TRIAL_FUNDING_POINT_IN_TIME_LAG
        if cycle.request_started_at > cutoff:
            continue
        item = next(
            (
                item
                for item in cycle.items
                if item.venue is venue and item.asset is asset and item.symbol == symbol
            ),
            None,
        )
        if (
            item is None
            or item.funding_outcome is not TrialFundingOutcome.CAPTURED
            or item.funding_effective_at != cycle.cycle_end
            or item.funding_observed_at is None
            or item.funding_observed_at > cutoff
        ):
            continue
        for source_hash in item.funding_source_hashes:
            identity = (
                venue,
                symbol,
                asset,
                cycle.cycle_end,
                item.funding_observed_at,
                source_hash,
            )
            for observation in rows_by_identity.get(identity, ()):
                if observation.interval_hours != 1:
                    continue
                selected = SelectedProspectiveFunding(
                    cycle_id=cycle.cycle_id, observation=observation
                )
                candidates[cycle.cycle_end].append(selected)
                source_hashes.update(item.funding_source_hashes)

    selected_rows: list[SelectedProspectiveFunding] = []
    conflicts: list[datetime] = []
    for boundary in sorted(candidates):
        boundary_candidates = candidates[boundary]
        values = {
            (
                candidate.observation.venue,
                candidate.observation.symbol,
                candidate.observation.asset,
                candidate.observation.rate,
                candidate.observation.interval_hours,
                candidate.observation.effective_at,
            )
            for candidate in boundary_candidates
        }
        if len(values) != 1:
            conflicts.append(boundary)
            continue
        selected_rows.append(
            min(
                boundary_candidates,
                key=lambda candidate: (
                    candidate.observation.observed_at,
                    candidate.cycle_id,
                ),
            )
        )

    return ProspectiveFundingSelection(
        observations=tuple(item.observation for item in selected_rows),
        selected_cycle_ids=tuple(item.cycle_id for item in selected_rows),
        conflict_boundaries=tuple(conflicts),
        source_hashes=tuple(sorted(source_hashes)),
    )
