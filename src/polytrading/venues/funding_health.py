from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from polytrading.venues.funding_cycle_models import FundingCollectionCycle, FundingCycleStatus
from polytrading.venues.funding_health_models import (
    FUNDING_HEALTH_PROTOCOL_VERSION,
    FUNDING_HEALTH_WARNINGS,
    FundingBoundaryHealth,
    FundingBoundaryStatus,
    FundingCollectionHealthReport,
    FundingCollectionHealthStatus,
    resolve_health_window,
)

_ATTEMPT_RANK = {
    FundingCycleStatus.COMPLETE: 0,
    FundingCycleStatus.DEGRADED: 1,
    FundingCycleStatus.LATE: 2,
}


class FundingCycleHistory(Protocol):
    def funding_collection_cycles_between(
        self, start: datetime, end: datetime
    ) -> tuple[FundingCollectionCycle, ...]: ...


class FundingCollectionHealthAuditor:
    def __init__(self, store: FundingCycleHistory) -> None:
        self._store = store

    def audit(self, as_of: datetime, requested_hours: int) -> FundingCollectionHealthReport:
        normalized_as_of, first_boundary, last_boundary = resolve_health_window(
            as_of, requested_hours
        )
        attempts_by_boundary: dict[datetime, list[FundingCollectionCycle]] = defaultdict(list)
        for cycle in self._store.funding_collection_cycles_between(first_boundary, last_boundary):
            attempts_by_boundary[cycle.cycle_end].append(cycle)

        boundaries = tuple(
            _audit_boundary(
                first_boundary + timedelta(hours=index),
                tuple(attempts_by_boundary[first_boundary + timedelta(hours=index)]),
            )
            for index in range(requested_hours)
        )
        counts = Counter(item.status for item in boundaries)
        complete_count = counts[FundingBoundaryStatus.COMPLETE]
        degraded_count = counts[FundingBoundaryStatus.DEGRADED]
        late_count = counts[FundingBoundaryStatus.LATE]
        missing_count = counts[FundingBoundaryStatus.MISSING]
        status = (
            FundingCollectionHealthStatus.CRITICAL
            if missing_count or late_count
            else FundingCollectionHealthStatus.DEGRADED
            if degraded_count
            else FundingCollectionHealthStatus.HEALTHY
        )
        current_complete_streak = 0
        for item in reversed(boundaries):
            if item.status is not FundingBoundaryStatus.COMPLETE:
                break
            current_complete_streak += 1
        source_hashes = tuple(
            sorted(
                {source_hash for item in boundaries for source_hash in item.selected_source_hashes}
            )
        )

        return FundingCollectionHealthReport(
            schema_version=1,
            protocol_version=FUNDING_HEALTH_PROTOCOL_VERSION,
            as_of=normalized_as_of,
            latest_auditable_boundary=last_boundary,
            first_boundary=first_boundary,
            last_boundary=last_boundary,
            requested_hours=requested_hours,
            boundaries=boundaries,
            status=status,
            complete_boundary_count=complete_count,
            degraded_boundary_count=degraded_count,
            late_boundary_count=late_count,
            missing_boundary_count=missing_count,
            complete_coverage=Decimal(complete_count) / Decimal(requested_hours),
            current_complete_streak=current_complete_streak,
            source_hashes=source_hashes,
            warnings=FUNDING_HEALTH_WARNINGS,
        )


def _audit_boundary(
    cycle_end: datetime, attempts: tuple[FundingCollectionCycle, ...]
) -> FundingBoundaryHealth:
    counts = Counter(cycle.status for cycle in attempts)
    complete_count = counts[FundingCycleStatus.COMPLETE]
    degraded_count = counts[FundingCycleStatus.DEGRADED]
    late_count = counts[FundingCycleStatus.LATE]
    status = (
        FundingBoundaryStatus.COMPLETE
        if complete_count
        else FundingBoundaryStatus.DEGRADED
        if degraded_count
        else FundingBoundaryStatus.LATE
        if late_count
        else FundingBoundaryStatus.MISSING
    )
    selected = (
        min(
            attempts,
            key=lambda cycle: (
                _ATTEMPT_RANK[cycle.status],
                cycle.request_completed_at,
                str(cycle.cycle_id),
            ),
        )
        if attempts
        else None
    )
    reason_codes: set[str] = set()
    if status is FundingBoundaryStatus.MISSING:
        reason_codes.add("BOUNDARY_MISSING")
    elif status is FundingBoundaryStatus.LATE:
        reason_codes.add("BOUNDARY_LATE_ONLY")
    elif status is FundingBoundaryStatus.DEGRADED:
        reason_codes.add("BOUNDARY_DEGRADED_ONLY")
    if len(attempts) > 1:
        reason_codes.add("MULTIPLE_ATTEMPTS")
    if complete_count > 1:
        reason_codes.add("MULTIPLE_COMPLETE_ATTEMPTS")

    return FundingBoundaryHealth(
        schema_version=1,
        cycle_end=cycle_end,
        status=status,
        attempt_count=len(attempts),
        complete_attempt_count=complete_count,
        degraded_attempt_count=degraded_count,
        late_attempt_count=late_count,
        selected_cycle_id=None if selected is None else selected.cycle_id,
        selected_request_completed_at=(None if selected is None else selected.request_completed_at),
        selected_source_hashes=() if selected is None else selected.source_hashes,
        reason_codes=tuple(sorted(reason_codes)),
    )
