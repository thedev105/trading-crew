from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from polytrading.venues.funding_cycle_models import FundingCollectionCycle, FundingCycleStatus
from polytrading.venues.funding_health import FundingCollectionHealthAuditor
from polytrading.venues.funding_health_models import (
    FUNDING_HEALTH_WARNINGS,
    FundingBoundaryStatus,
    FundingCollectionHealthStatus,
)
from tests.venues.funding_health_helpers import (
    HEALTH_AS_OF,
    LATEST_BOUNDARY,
    funding_cycle,
)


class FakeFundingCycleHistory:
    def __init__(self, cycles: tuple[FundingCollectionCycle, ...]) -> None:
        self.cycles = cycles
        self.calls: list[tuple[datetime, datetime]] = []

    def funding_collection_cycles_between(
        self, start: datetime, end: datetime
    ) -> tuple[FundingCollectionCycle, ...]:
        self.calls.append((start, end))
        return tuple(cycle for cycle in self.cycles if start <= cycle.cycle_end <= end)


def test_empty_history_marks_every_boundary_missing_and_critical() -> None:
    store = FakeFundingCycleHistory(())

    report = FundingCollectionHealthAuditor(store).audit(HEALTH_AS_OF, 3)

    expected_first = LATEST_BOUNDARY - timedelta(hours=2)
    assert store.calls == [(expected_first, LATEST_BOUNDARY)]
    assert tuple(item.cycle_end for item in report.boundaries) == (
        expected_first,
        expected_first + timedelta(hours=1),
        LATEST_BOUNDARY,
    )
    assert tuple(item.status for item in report.boundaries) == (
        FundingBoundaryStatus.MISSING,
        FundingBoundaryStatus.MISSING,
        FundingBoundaryStatus.MISSING,
    )
    assert all(item.reason_codes == ("BOUNDARY_MISSING",) for item in report.boundaries)
    assert report.status is FundingCollectionHealthStatus.CRITICAL
    assert report.missing_boundary_count == 3
    assert report.complete_coverage == Decimal(0)
    assert report.current_complete_streak == 0
    assert report.source_hashes == ()
    assert report.warnings == FUNDING_HEALTH_WARNINGS


def test_mixed_history_retains_exact_counts_selection_and_coverage() -> None:
    first = LATEST_BOUNDARY - timedelta(hours=2)
    cycles = (
        funding_cycle(first, FundingCycleStatus.COMPLETE, cycle_int=1),
        funding_cycle(first + timedelta(hours=1), FundingCycleStatus.DEGRADED, cycle_int=2),
        funding_cycle(LATEST_BOUNDARY, FundingCycleStatus.LATE, cycle_int=3),
    )
    store = FakeFundingCycleHistory(cycles)

    report = FundingCollectionHealthAuditor(store).audit(HEALTH_AS_OF, 3)

    assert store.calls == [(first, LATEST_BOUNDARY)]
    assert tuple(item.status for item in report.boundaries) == (
        FundingBoundaryStatus.COMPLETE,
        FundingBoundaryStatus.DEGRADED,
        FundingBoundaryStatus.LATE,
    )
    assert tuple(item.selected_cycle_id for item in report.boundaries) == tuple(
        cycle.cycle_id for cycle in cycles
    )
    assert (
        report.complete_boundary_count,
        report.degraded_boundary_count,
        report.late_boundary_count,
        report.missing_boundary_count,
    ) == (1, 1, 1, 0)
    assert report.status is FundingCollectionHealthStatus.CRITICAL
    assert report.complete_coverage == Decimal("0.3333333333333333333333333333")
    assert report.current_complete_streak == 0
    assert report.boundaries[1].reason_codes == ("BOUNDARY_DEGRADED_ONLY",)
    assert report.boundaries[2].reason_codes == ("BOUNDARY_LATE_ONLY",)
    assert report.source_hashes == tuple(
        sorted({source_hash for cycle in cycles for source_hash in cycle.source_hashes})
    )


@pytest.mark.parametrize(
    "cycles",
    tuple(
        itertools.permutations(
            (
                funding_cycle(LATEST_BOUNDARY, FundingCycleStatus.LATE, cycle_int=10),
                funding_cycle(LATEST_BOUNDARY, FundingCycleStatus.DEGRADED, cycle_int=11),
                funding_cycle(LATEST_BOUNDARY, FundingCycleStatus.COMPLETE, cycle_int=12),
            )
        )
    ),
)
def test_best_attempt_wins_independent_of_input_order(
    cycles: tuple[FundingCollectionCycle, ...],
) -> None:
    report = FundingCollectionHealthAuditor(FakeFundingCycleHistory(cycles)).audit(HEALTH_AS_OF, 1)

    boundary = report.boundaries[0]
    complete = next(cycle for cycle in cycles if cycle.status is FundingCycleStatus.COMPLETE)
    assert boundary.status is FundingBoundaryStatus.COMPLETE
    assert boundary.selected_cycle_id == complete.cycle_id
    assert (
        boundary.attempt_count,
        boundary.complete_attempt_count,
        boundary.degraded_attempt_count,
        boundary.late_attempt_count,
    ) == (3, 1, 1, 1)
    assert boundary.reason_codes == ("MULTIPLE_ATTEMPTS",)


def test_completion_time_then_uuid_break_complete_attempt_ties() -> None:
    later = funding_cycle(
        LATEST_BOUNDARY,
        FundingCycleStatus.COMPLETE,
        cycle_int=20,
        completed_offset=timedelta(minutes=3),
    )
    earlier_high_uuid = funding_cycle(
        LATEST_BOUNDARY,
        FundingCycleStatus.COMPLETE,
        cycle_int=22,
        completed_offset=timedelta(minutes=2),
    )
    earlier_low_uuid = funding_cycle(
        LATEST_BOUNDARY,
        FundingCycleStatus.COMPLETE,
        cycle_int=21,
        completed_offset=timedelta(minutes=2),
    )

    report = FundingCollectionHealthAuditor(
        FakeFundingCycleHistory((later, earlier_high_uuid, earlier_low_uuid))
    ).audit(HEALTH_AS_OF, 1)

    boundary = report.boundaries[0]
    assert boundary.selected_cycle_id == earlier_low_uuid.cycle_id
    assert boundary.selected_request_completed_at == earlier_low_uuid.request_completed_at
    assert boundary.reason_codes == (
        "MULTIPLE_ATTEMPTS",
        "MULTIPLE_COMPLETE_ATTEMPTS",
    )


def test_audit_excludes_attempts_completed_after_the_as_of_cutoff() -> None:
    future_known = funding_cycle(
        LATEST_BOUNDARY,
        FundingCycleStatus.COMPLETE,
        cycle_int=30,
        completed_offset=timedelta(minutes=7),
    )
    store = FakeFundingCycleHistory((future_known,))

    before_completion = FundingCollectionHealthAuditor(store).audit(HEALTH_AS_OF, 1)
    after_completion = FundingCollectionHealthAuditor(store).audit(
        HEALTH_AS_OF + timedelta(minutes=2), 1
    )

    assert before_completion.boundaries[0].status is FundingBoundaryStatus.MISSING
    assert before_completion.boundaries[0].attempt_count == 0
    assert after_completion.boundaries[0].status is FundingBoundaryStatus.COMPLETE
    assert after_completion.boundaries[0].selected_cycle_id == future_known.cycle_id


@settings(max_examples=50, deadline=None)
@given(
    data=st.data(),
    statuses=st.lists(
        st.sampled_from(tuple(FundingBoundaryStatus)),
        min_size=1,
        max_size=24,
    ),
)
def test_audit_conserves_boundary_evidence_for_bounded_windows(
    data: st.DataObject, statuses: list[FundingBoundaryStatus]
) -> None:
    requested_hours = len(statuses)
    first = LATEST_BOUNDARY - timedelta(hours=requested_hours - 1)
    status_to_cycle = {
        FundingBoundaryStatus.COMPLETE: FundingCycleStatus.COMPLETE,
        FundingBoundaryStatus.DEGRADED: FundingCycleStatus.DEGRADED,
        FundingBoundaryStatus.LATE: FundingCycleStatus.LATE,
    }
    cycles = tuple(
        funding_cycle(
            first + timedelta(hours=index),
            status_to_cycle[status],
            cycle_int=index + 100,
        )
        for index, status in enumerate(statuses)
        if status is not FundingBoundaryStatus.MISSING
    )
    shuffled = data.draw(st.permutations(cycles))

    report = FundingCollectionHealthAuditor(FakeFundingCycleHistory(shuffled)).audit(
        HEALTH_AS_OF, requested_hours
    )
    ordered_report = FundingCollectionHealthAuditor(FakeFundingCycleHistory(cycles)).audit(
        HEALTH_AS_OF, requested_hours
    )

    assert report == ordered_report
    assert tuple(item.cycle_end for item in report.boundaries) == tuple(
        first + timedelta(hours=index) for index in range(requested_hours)
    )
    assert [item.status for item in report.boundaries] == statuses
    assert (
        sum(
            (
                report.complete_boundary_count,
                report.degraded_boundary_count,
                report.late_boundary_count,
                report.missing_boundary_count,
            )
        )
        == requested_hours
    )
    complete_count = statuses.count(FundingBoundaryStatus.COMPLETE)
    assert report.complete_coverage == Decimal(complete_count) / Decimal(requested_hours)
    expected_streak = 0
    for status in reversed(statuses):
        if status is not FundingBoundaryStatus.COMPLETE:
            break
        expected_streak += 1
    assert report.current_complete_streak == expected_streak
    assert report.source_hashes == tuple(
        sorted(
            {
                source_hash
                for item in report.boundaries
                for source_hash in item.selected_source_hashes
            }
        )
    )
