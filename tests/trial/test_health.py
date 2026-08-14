from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

import polytrading.trial as trial_api
from polytrading.domain.models import Asset, FundingObservation
from polytrading.storage.store import DuckDBStore
from polytrading.trial.funding import record_late_lighter_dydx_cycle
from polytrading.trial.health import (
    LighterDydxTrialHealthAuditor,
    ProjectedAssetEvidence,
    project_earliest_evaluation_end,
)
from polytrading.trial.health_models import TrialCollectionStatus, TrialEvidenceStatus
from tests.trial.funding_helpers import trial_funding_cycle
from tests.trial.test_book_evidence import append_pair

START = datetime(2026, 1, 1, tzinfo=UTC)
AS_OF = datetime(2026, 8, 14, 7, 6, tzinfo=UTC)
AS_OF_HOUR = datetime(2026, 8, 14, 7, tzinfo=UTC)
FUNDING_HASH = "2" * 64


def test_trial_package_exports_health_operations() -> None:
    assert trial_api.LighterDydxTrialHealthAuditor is LighterDydxTrialHealthAuditor
    assert trial_api.project_earliest_evaluation_end is project_earliest_evaluation_end
    assert trial_api.select_hourly_trial_books.__name__ == "select_hourly_trial_books"


def boundaries(start: datetime, hours: int) -> frozenset[datetime]:
    return frozenset(start + timedelta(hours=index) for index in range(hours))


def projected_evidence(
    *,
    latest: datetime,
    missing_funding: frozenset[datetime] = frozenset(),
    missing_books: frozenset[datetime] = frozenset(),
) -> tuple[ProjectedAssetEvidence, ...]:
    known = boundaries(START, int((latest - START) // timedelta(hours=1)) + 1)
    return tuple(
        ProjectedAssetEvidence(
            asset=asset,
            funding_complete=known - missing_funding,
            book_complete=known - missing_books,
        )
        for asset in Asset
    )


def test_projection_without_trial_start_is_none() -> None:
    assert project_earliest_evaluation_end(None, START, ()) is None


def test_clean_new_trial_projects_start_plus_2159_hours() -> None:
    assert project_earliest_evaluation_end(
        START, START, projected_evidence(latest=START)
    ) == START + timedelta(hours=2_159)


def test_known_misses_within_99_percent_allowances_preserve_projection() -> None:
    latest = START + timedelta(hours=2_159)
    missing = frozenset(
        {
            *(START + timedelta(hours=index) for index in range(7)),
            *(START + timedelta(hours=720 + index) for index in range(14)),
        }
    )
    assert (
        project_earliest_evaluation_end(
            START,
            latest,
            projected_evidence(latest=latest, missing_funding=missing, missing_books=missing),
        )
        == latest
    )


def test_misses_beyond_allowance_shift_projection_until_bad_hours_roll_out() -> None:
    latest = START + timedelta(hours=2_159)
    evaluation_start = START + timedelta(hours=720)
    missing = frozenset(evaluation_start + timedelta(hours=index) for index in range(15))
    assert project_earliest_evaluation_end(
        START,
        latest,
        projected_evidence(latest=latest, missing_funding=missing, missing_books=missing),
    ) == latest + timedelta(hours=1)


def test_final_168_miss_shifts_projection_until_it_rolls_out() -> None:
    latest = START + timedelta(hours=2_159)
    missing = frozenset((latest,))
    assert project_earliest_evaluation_end(
        START,
        latest,
        projected_evidence(latest=latest, missing_funding=missing),
    ) == latest + timedelta(hours=168)


def append_complete_funding_boundary(store: DuckDBStore, boundary: datetime, identity: int) -> None:
    cycle = trial_funding_cycle(cycle_id=UUID(int=identity), cycle_end=boundary)
    for item in cycle.items:
        store.append_funding(
            FundingObservation(
                schema_version=1,
                venue=item.venue,
                symbol=item.symbol,
                asset=item.asset,
                rate=Decimal("0.0001"),
                interval_hours=Decimal("1"),
                effective_at=boundary,
                observed_at=boundary + timedelta(seconds=12),
                source_hash=FUNDING_HASH,
            )
        )
    store.append_lighter_dydx_funding_cycle(cycle)


def seed_complete_trial_hours(
    tmp_path: Path,
    *,
    hours: int,
    missing_book_hours: frozenset[datetime] = frozenset(),
) -> DuckDBStore:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    first = AS_OF_HOUR - timedelta(hours=hours - 1)
    with store.transaction():
        for index in range(hours):
            boundary = first + timedelta(hours=index)
            append_complete_funding_boundary(store, boundary, index + 1)
            for offset, asset in enumerate(Asset, start=1):
                if asset is Asset.BTC and boundary in missing_book_hours:
                    continue
                append_pair(
                    store,
                    10_000 + index * 10 + offset,
                    boundary - timedelta(seconds=1),
                    asset=asset,
                )
    return store


def test_empty_trial_is_not_started(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 24)
    assert report.status is TrialCollectionStatus.NOT_STARTED
    assert report.trial_started_at is None
    assert report.elapsed_auditable_hours == 0
    store.close()


def test_late_only_attempt_does_not_start_trial(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    store.append_lighter_dydx_funding_cycle(
        record_late_lighter_dydx_cycle(
            frozenset(Asset),
            AS_OF_HOUR,
            AS_OF_HOUR + timedelta(minutes=5, microseconds=1),
            cycle_id_factory=lambda: UUID(int=999),
        )
    )
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 24)
    assert report.status is TrialCollectionStatus.NOT_STARTED
    assert report.trial_started_at is None
    store.close()


def test_pre_start_generic_books_remain_unavailable_trial_evidence(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    with store.transaction():
        for index in range(3):
            boundary = AS_OF_HOUR - timedelta(hours=2 - index)
            for offset, asset in enumerate(Asset, start=1):
                append_pair(
                    store,
                    70_000 + index * 10 + offset,
                    boundary - timedelta(seconds=1),
                    asset=asset,
                )
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 3)
    assert report.status is TrialCollectionStatus.NOT_STARTED
    assert all(item.paired_book_hours == 0 for item in report.assets)
    assert all(item.latest_book_completed_at is None for item in report.assets)
    store.close()


def test_started_trial_counts_only_book_boundaries_at_or_after_start(tmp_path: Path) -> None:
    store = seed_complete_trial_hours(tmp_path, hours=1)
    with store.transaction():
        for index in range(2):
            boundary = AS_OF_HOUR - timedelta(hours=2 - index)
            for offset, asset in enumerate(Asset, start=1):
                append_pair(
                    store,
                    75_000 + index * 10 + offset,
                    boundary - timedelta(seconds=1),
                    asset=asset,
                )
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 3)
    assert report.trial_started_at == AS_OF_HOUR
    assert all(item.paired_book_hours == 1 for item in report.assets)
    store.close()


def test_calendar_immaturity_is_collecting_not_degraded(tmp_path: Path) -> None:
    store = seed_complete_trial_hours(tmp_path, hours=24)
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 24)
    assert report.status is TrialCollectionStatus.COLLECTING
    assert all(item.status is TrialEvidenceStatus.COMPLETE for item in report.recent_boundaries)
    store.close()


def test_recent_missing_book_makes_health_degraded(tmp_path: Path) -> None:
    store = seed_complete_trial_hours(
        tmp_path, hours=24, missing_book_hours=frozenset((AS_OF_HOUR,))
    )
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 24)
    assert report.status is TrialCollectionStatus.DEGRADED
    assert "BOOK_BTC_MISSING" in report.recent_boundaries[-1].reason_codes
    assert report.recent_boundaries[-1].assets[1].funding_status is TrialEvidenceStatus.COMPLETE
    assert report.recent_boundaries[-1].assets[1].book_status is TrialEvidenceStatus.COMPLETE
    store.close()


def test_missing_first_book_does_not_move_trial_start_forward(tmp_path: Path) -> None:
    first = AS_OF_HOUR - timedelta(hours=1)
    store = seed_complete_trial_hours(tmp_path, hours=2, missing_book_hours=frozenset((first,)))
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 2)
    assert report.trial_started_at == first
    assert tuple(item.cycle_end for item in report.recent_boundaries) == (first, AS_OF_HOUR)
    assert report.recent_boundaries[0].status is TrialEvidenceStatus.MISSING
    store.close()


def test_duplicate_complete_attempts_warn_without_degrading_complete_evidence(
    tmp_path: Path,
) -> None:
    store = seed_complete_trial_hours(tmp_path, hours=1)
    duplicate = trial_funding_cycle(cycle_id=UUID(int=500), cycle_end=AS_OF_HOUR)
    store.append_lighter_dydx_funding_cycle(duplicate)

    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 1)

    boundary = report.recent_boundaries[0]
    assert boundary.status is TrialEvidenceStatus.COMPLETE
    assert boundary.attempt_count == 2
    assert boundary.complete_attempt_count == 2
    assert "MULTIPLE_ATTEMPTS" in boundary.reason_codes
    assert "MULTIPLE_COMPLETE_ATTEMPTS" in boundary.reason_codes
    store.close()


def test_linked_funding_revision_conflict_degrades_only_affected_asset(
    tmp_path: Path,
) -> None:
    from tests.carry.test_economics_assembler import append_conflicting_trial_revision

    store = seed_complete_trial_hours(tmp_path, hours=1)
    append_conflicting_trial_revision(store, Asset.BTC, AS_OF_HOUR)
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 1)

    boundary = report.recent_boundaries[0]
    assert boundary.status is TrialEvidenceStatus.DEGRADED
    assert "FUNDING_BTC_REVISION_CONFLICT" in boundary.reason_codes
    assert boundary.assets[0].selected_funding_cycle_ids == ()
    assert all(item.funding_status is TrialEvidenceStatus.COMPLETE for item in boundary.assets[1:])
    store.close()


def test_complete_2160_hour_trial_is_ready_for_economics_evaluation(tmp_path: Path) -> None:
    store = seed_complete_trial_hours(tmp_path, hours=2_160)
    with store.transaction():
        for offset, asset in enumerate(Asset, start=1):
            append_pair(store, 90_000 + offset, AS_OF - timedelta(seconds=10), asset=asset)
            append_pair(store, 90_100 + offset, AS_OF - timedelta(seconds=5), asset=asset)

    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 24)

    assert report.status is TrialCollectionStatus.READY_FOR_ECONOMICS_EVALUATION
    assert all(item.historical_windows_mature for item in report.assets)
    assert all(item.current_funding_paired_hours == 168 for item in report.assets)
    assert all(item.consecutive_dense_sample_count >= 1 for item in report.assets)
    assert all(item.fresh_book_ready for item in report.assets)
    assert all(item.projected_earliest_evaluation_end == AS_OF_HOUR for item in report.assets)
    store.close()


@pytest.mark.parametrize("recent_hours", (True, 0, 2_161, 1.5))
def test_audit_rejects_invalid_recent_hours_before_querying(
    tmp_path: Path, recent_hours: object
) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    with pytest.raises((TypeError, ValueError), match="recent hours"):
        LighterDydxTrialHealthAuditor(store).audit(AS_OF, recent_hours)  # type: ignore[arg-type]
    store.close()
