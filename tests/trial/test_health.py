import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

import polytrading.trial as trial_api
from polytrading.domain.models import Asset, FundingObservation, Venue
from polytrading.storage.store import DuckDBStore
from polytrading.trial.funding import record_late_lighter_dydx_cycle
from polytrading.trial.funding_models import (
    LighterDydxFundingCycle,
    TrialFundingCycleStatus,
    TrialFundingOutcome,
)
from polytrading.trial.health import (
    LighterDydxTrialHealthAuditor,
    ProjectedAssetEvidence,
    project_earliest_evaluation_end,
)
from polytrading.trial.health_models import TrialCollectionStatus, TrialEvidenceStatus
from tests.book_evidence_seed import bulk_append_book_evidence
from tests.funding_evidence_seed import bulk_append_funding_evidence
from tests.trial.funding_helpers import trial_funding_cycle
from tests.trial.test_book_evidence import (
    DYDX_HASH,
    LIGHTER_HASH,
    append_pair,
    book_pair_records,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
AS_OF = datetime(2026, 8, 14, 7, 6, tzinfo=UTC)
AS_OF_HOUR = datetime(2026, 8, 14, 7, tzinfo=UTC)
FUNDING_HASH = "2" * 64


def test_trial_package_exports_health_operations() -> None:
    assert trial_api.LighterDydxTrialHealthAuditor is LighterDydxTrialHealthAuditor
    assert trial_api.project_earliest_evaluation_end is project_earliest_evaluation_end
    assert trial_api.select_hourly_trial_books.__name__ == "select_hourly_trial_books"
    assert trial_api.TrialEconomicsEvaluationSummary.__name__ == ("TrialEconomicsEvaluationSummary")


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


def complete_funding_boundary_records(
    boundary: datetime, identity: int
) -> tuple[tuple[FundingObservation, ...], LighterDydxFundingCycle]:
    cycle = trial_funding_cycle(cycle_id=UUID(int=identity), cycle_end=boundary)
    observations = tuple(
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
        for item in cycle.items
    )
    return observations, cycle


def append_complete_funding_boundary(store: DuckDBStore, boundary: datetime, identity: int) -> None:
    observations, cycle = complete_funding_boundary_records(boundary, identity)
    for observation in observations:
        store.append_funding(observation)
    store.append_lighter_dydx_funding_cycle(cycle)


def append_degraded_funding_boundary(store: DuckDBStore, boundary: datetime, identity: int) -> None:
    complete = trial_funding_cycle(cycle_id=UUID(int=identity), cycle_end=boundary)
    items = tuple(
        item.model_copy(
            update={
                "funding_outcome": TrialFundingOutcome.MISSING_EXPECTED,
                "funding_effective_at": None,
                "reason_codes": ("FUNDING_MISSING_EXPECTED",),
            }
        )
        if item.venue is Venue.DYDX and item.asset is Asset.BTC
        else item
        for item in complete.items
    )
    cycle = trial_funding_cycle(
        cycle_id=UUID(int=identity),
        cycle_end=boundary,
        items=items,
        status=TrialFundingCycleStatus.DEGRADED,
    )
    for item in cycle.items:
        if item.funding_effective_at is None:
            continue
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
    bulk_book_evidence: bool = True,
) -> DuckDBStore:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    first = AS_OF_HOUR - timedelta(hours=hours - 1)
    book_evidence = []
    funding_observations = []
    funding_cycles = []
    with store.transaction():
        for index in range(hours):
            boundary = first + timedelta(hours=index)
            observations, cycle = complete_funding_boundary_records(boundary, index + 1)
            funding_observations.extend(observations)
            funding_cycles.append(cycle)
            for offset, asset in enumerate(Asset, start=1):
                if asset is Asset.BTC and boundary in missing_book_hours:
                    continue
                identity = 10_000 + index * 10 + offset
                if not bulk_book_evidence:
                    append_pair(store, identity, boundary, asset=asset)
                    continue
                cycle, snapshots = book_pair_records(identity, boundary, asset=asset)
                book_evidence.append((cycle, snapshots))
        bulk_append_funding_evidence(
            store,
            funding_observations,
            funding_cycles,
            tmp_path,
        )
        if bulk_book_evidence:
            bulk_append_book_evidence(store, book_evidence, tmp_path)
    return store


@pytest.fixture(scope="module")
def complete_24_hour_trial_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("complete-24-hour-trial")
    store = seed_complete_trial_hours(directory, hours=24)
    path = directory / "trial.duckdb"
    store.close()
    return path


@pytest.fixture(scope="module")
def complete_2160_hour_trial_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("complete-2160-hour-trial")
    store = seed_complete_trial_hours(directory, hours=2_160)
    path = directory / "trial.duckdb"
    store.close()
    return path


def copied_trial_store(tmp_path: Path, template: Path) -> DuckDBStore:
    path = tmp_path / "trial.duckdb"
    shutil.copyfile(template, path)
    return DuckDBStore(path)


def test_bulk_book_template_seed_matches_rowwise_evidence(tmp_path: Path) -> None:
    rowwise_directory = tmp_path / "rowwise"
    bulk_directory = tmp_path / "bulk"
    rowwise_directory.mkdir()
    bulk_directory.mkdir()
    rowwise = seed_complete_trial_hours(
        rowwise_directory,
        hours=2,
        bulk_book_evidence=False,
    )
    bulk = seed_complete_trial_hours(bulk_directory, hours=2)

    assert LighterDydxTrialHealthAuditor(bulk).audit(AS_OF, 2) == (
        LighterDydxTrialHealthAuditor(rowwise).audit(AS_OF, 2)
    )
    for query in (
        """
        SELECT cycle_id, epoch_us(request_completed_at), status,
               CAST(record_json AS VARCHAR), record_hash
        FROM book_collection_cycles ORDER BY cycle_id
        """,
        """
        SELECT cycle_id, venue, symbol, asset, depth_limit, sequence,
               epoch_us(effective_at), epoch_us(observed_at), source_hash,
               schema_version, record_hash
        FROM book_snapshots ORDER BY cycle_id, venue, symbol
        """,
        """
        SELECT cycle_id, venue, symbol, epoch_us(observed_at), side, level_index,
               price, quantity, order_count, record_hash
        FROM book_levels ORDER BY cycle_id, venue, symbol, side, level_index
        """,
    ):
        assert (
            bulk._connection.execute(query).fetchall()
            == rowwise._connection.execute(query).fetchall()
        )
    bulk.close()
    rowwise.close()


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


@pytest.mark.parametrize(
    ("completion_offset", "expected_count"),
    ((timedelta(seconds=-1), 0), (timedelta(0), 1)),
)
def test_trial_book_evidence_requires_completion_at_or_after_prospective_start(
    tmp_path: Path,
    completion_offset: timedelta,
    expected_count: int,
) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    with store.transaction():
        append_complete_funding_boundary(store, AS_OF_HOUR, 1)
        for offset, asset in enumerate(Asset, start=1):
            append_pair(
                store,
                76_000 + offset,
                AS_OF_HOUR + completion_offset,
                asset=asset,
            )

    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 1)

    assert report.trial_started_at == AS_OF_HOUR
    assert all(item.paired_book_hours == expected_count for item in report.assets)
    assert all(item.dense_book_pair_count == expected_count for item in report.assets)
    expected_latest = AS_OF_HOUR if expected_count else None
    assert all(item.latest_book_completed_at == expected_latest for item in report.assets)
    assert (DYDX_HASH in report.source_hashes) is bool(expected_count)
    assert (LIGHTER_HASH in report.source_hashes) is bool(expected_count)
    store.close()


def test_calendar_immaturity_is_collecting_not_degraded(
    tmp_path: Path, complete_24_hour_trial_database: Path
) -> None:
    store = copied_trial_store(tmp_path, complete_24_hour_trial_database)
    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 24)
    assert report.status is TrialCollectionStatus.COLLECTING
    assert all(item.status is TrialEvidenceStatus.COMPLETE for item in report.recent_boundaries)
    store.close()


def test_health_bulk_loads_book_headers_once_without_reconstructing_levels(
    tmp_path: Path,
    complete_24_hour_trial_database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = copied_trial_store(tmp_path, complete_24_hour_trial_database)
    cycle_reads = 0
    header_reads = 0
    real_cycle_reader = store.book_collection_cycles_completed_between
    real_header_reader = store.book_snapshot_headers_for_cycles

    def cycles(*args: object, **kwargs: object) -> object:
        nonlocal cycle_reads
        cycle_reads += 1
        return real_cycle_reader(*args, **kwargs)  # type: ignore[arg-type]

    def headers(*args: object, **kwargs: object) -> object:
        nonlocal header_reads
        header_reads += 1
        return real_header_reader(*args, **kwargs)  # type: ignore[arg-type]

    def reject_level_reader(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("health reconstructed book levels")

    monkeypatch.setattr(store, "book_collection_cycles_completed_between", cycles)
    monkeypatch.setattr(store, "book_snapshot_headers_for_cycles", headers)
    monkeypatch.setattr(store, "books_for_cycle", reject_level_reader)

    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 24)

    assert report.status is TrialCollectionStatus.COLLECTING
    assert cycle_reads == 1
    assert header_reads == 1
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


def test_missing_newest_funding_after_earlier_evidence_returns_degraded_report(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    first = AS_OF_HOUR - timedelta(hours=1)
    with store.transaction():
        append_complete_funding_boundary(store, first, 1)
        for index, boundary in enumerate((first, AS_OF_HOUR)):
            for offset, asset in enumerate(Asset, start=1):
                append_pair(
                    store,
                    80_000 + index * 10 + offset,
                    boundary,
                    asset=asset,
                )

    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 2)

    assert report.status is TrialCollectionStatus.DEGRADED
    assert report.assets[0].latest_funding_boundary == first
    assert AS_OF_HOUR in report.assets[0].missing_current_funding_boundaries
    assert report.recent_boundaries[-1].assets[0].funding_status is TrialEvidenceStatus.MISSING
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


def test_degraded_only_funding_attempt_is_visible_instead_of_missing(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    with store.transaction():
        append_degraded_funding_boundary(store, AS_OF_HOUR, 1)
        for offset, asset in enumerate(Asset, start=1):
            append_pair(store, 81_000 + offset, AS_OF_HOUR, asset=asset)

    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 1)
    boundary = report.recent_boundaries[0]

    assert boundary.status is TrialEvidenceStatus.DEGRADED
    assert boundary.assets[0].funding_status is TrialEvidenceStatus.DEGRADED
    assert "BOUNDARY_DEGRADED_ONLY" in boundary.reason_codes
    assert "FUNDING_BTC_DYDX_MISSING_EXPECTED" in boundary.reason_codes
    assert all(item.funding_status is TrialEvidenceStatus.COMPLETE for item in boundary.assets[1:])
    store.close()


def test_late_only_funding_attempt_stays_late_after_trial_has_started(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    first = AS_OF_HOUR - timedelta(hours=1)
    with store.transaction():
        append_complete_funding_boundary(store, first, 1)
        store.append_lighter_dydx_funding_cycle(
            record_late_lighter_dydx_cycle(
                frozenset(Asset),
                AS_OF_HOUR,
                AS_OF_HOUR + timedelta(minutes=5, microseconds=1),
                cycle_id_factory=lambda: UUID(int=2),
            )
        )
        for index, boundary in enumerate((first, AS_OF_HOUR)):
            for offset, asset in enumerate(Asset, start=1):
                append_pair(store, 81_100 + index * 10 + offset, boundary, asset=asset)

    boundary = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 2).recent_boundaries[-1]

    assert boundary.status is TrialEvidenceStatus.LATE
    assert all(item.funding_status is TrialEvidenceStatus.LATE for item in boundary.assets)
    assert all(item.book_status is TrialEvidenceStatus.COMPLETE for item in boundary.assets)
    assert "BOUNDARY_LATE_ONLY" in boundary.reason_codes
    assert "FUNDING_BTC_LATE_ONLY" in boundary.reason_codes
    store.close()


def test_complete_funding_pair_wins_over_degraded_attempt_history(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    with store.transaction():
        append_degraded_funding_boundary(store, AS_OF_HOUR, 2)
        append_complete_funding_boundary(store, AS_OF_HOUR, 1)
        for offset, asset in enumerate(Asset, start=1):
            append_pair(store, 81_200 + offset, AS_OF_HOUR, asset=asset)

    boundary = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 1).recent_boundaries[0]

    assert boundary.status is TrialEvidenceStatus.COMPLETE
    assert boundary.complete_attempt_count == 1
    assert boundary.degraded_attempt_count == 1
    assert all(item.funding_status is TrialEvidenceStatus.COMPLETE for item in boundary.assets)
    assert "MULTIPLE_ATTEMPTS" in boundary.reason_codes
    store.close()


@pytest.mark.parametrize(
    ("book_status", "failed_count", "skewed_count", "reason"),
    (
        ("failed", 1, 0, "BOOK_BTC_FAILED_ATTEMPTS"),
        ("skew_exceeds_research_target", 0, 1, "BOOK_BTC_SKEWED_ATTEMPTS"),
    ),
)
def test_failed_or_skewed_book_attempt_is_degraded_and_counted(
    tmp_path: Path,
    book_status: str,
    failed_count: int,
    skewed_count: int,
    reason: str,
) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    with store.transaction():
        append_complete_funding_boundary(store, AS_OF_HOUR, 1)
        append_pair(store, 82_001, AS_OF_HOUR, asset=Asset.BTC, status=book_status)
        for offset, asset in enumerate((Asset.ETH, Asset.SOL), start=2):
            append_pair(store, 82_000 + offset, AS_OF_HOUR, asset=asset)

    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 1)
    boundary = report.recent_boundaries[0]

    assert boundary.assets[0].book_status is TrialEvidenceStatus.DEGRADED
    assert boundary.failed_book_attempt_count == failed_count
    assert boundary.skewed_book_attempt_count == skewed_count
    assert reason in boundary.reason_codes
    store.close()


def test_complete_book_wins_while_failed_and_skewed_attempt_counts_remain_visible(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    with store.transaction():
        append_complete_funding_boundary(store, AS_OF_HOUR, 1)
        for offset, asset in enumerate(Asset, start=1):
            append_pair(store, 83_000 + offset, AS_OF_HOUR, asset=asset)
        append_pair(store, 83_010, AS_OF_HOUR, asset=Asset.BTC, status="failed")
        append_pair(
            store,
            83_011,
            AS_OF_HOUR,
            asset=Asset.BTC,
            status="skew_exceeds_research_target",
        )

    boundary = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 1).recent_boundaries[0]

    assert boundary.assets[0].book_status is TrialEvidenceStatus.COMPLETE
    assert boundary.failed_book_attempt_count == 1
    assert boundary.skewed_book_attempt_count == 1
    assert "BOOK_BTC_FAILED_ATTEMPTS" in boundary.reason_codes
    assert "BOOK_BTC_SKEWED_ATTEMPTS" in boundary.reason_codes
    store.close()


def test_health_conserves_every_hash_from_only_the_selected_funding_item(
    tmp_path: Path,
) -> None:
    selected_extra = "5" * 64
    unselected_extra = "6" * 64
    future_extra = "7" * 64
    store = DuckDBStore(tmp_path / "trial.duckdb")

    def cycle_with_extra(identity: int, extra: str, completed_at: datetime):
        original = trial_funding_cycle(cycle_id=UUID(int=identity), cycle_end=AS_OF_HOUR)
        items = tuple(
            item.model_copy(
                update={
                    "funding_source_hashes": tuple(sorted((*item.funding_source_hashes, extra)))
                }
            )
            if item.venue is Venue.DYDX and item.asset is Asset.BTC
            else item
            for item in original.items
        )
        return trial_funding_cycle(
            cycle_id=UUID(int=identity),
            cycle_end=AS_OF_HOUR,
            request_completed_at=completed_at,
            items=items,
        )

    with store.transaction():
        selected = cycle_with_extra(1, selected_extra, AS_OF_HOUR + timedelta(seconds=20))
        for item in selected.items:
            store.append_funding(
                FundingObservation(
                    schema_version=1,
                    venue=item.venue,
                    symbol=item.symbol,
                    asset=item.asset,
                    rate=Decimal("0.0001"),
                    interval_hours=Decimal("1"),
                    effective_at=AS_OF_HOUR,
                    observed_at=AS_OF_HOUR + timedelta(seconds=12),
                    source_hash=FUNDING_HASH,
                )
            )
        store.append_lighter_dydx_funding_cycle(selected)
        store.append_funding(
            FundingObservation(
                schema_version=1,
                venue=Venue.DYDX,
                symbol="BTC-USD",
                asset=Asset.BTC,
                rate=Decimal("0.0002"),
                interval_hours=Decimal("1"),
                effective_at=AS_OF_HOUR,
                observed_at=AS_OF_HOUR + timedelta(seconds=13),
                source_hash=unselected_extra,
            )
        )
        store.append_lighter_dydx_funding_cycle(
            cycle_with_extra(3, future_extra, AS_OF + timedelta(seconds=1))
        )
        for offset, asset in enumerate(Asset, start=1):
            append_pair(store, 84_000 + offset, AS_OF_HOUR, asset=asset)

    report = LighterDydxTrialHealthAuditor(store).audit(AS_OF, 1)

    assert selected_extra in report.source_hashes
    assert unselected_extra not in report.source_hashes
    assert future_extra not in report.source_hashes
    store.close()


def test_health_selects_latest_cutoff_safe_economics_summary_for_each_asset(
    tmp_path: Path,
) -> None:
    from polytrading.carry.economics_models import EconomicsDecision
    from tests.carry.test_economics_models import KNOWN_AS_OF
    from tests.carry.test_economics_models import report as economics_report

    store = DuckDBStore(tmp_path / "trial.duckdb")
    selected = economics_report(
        evaluation_id=UUID(int=91_001),
        decision=EconomicsDecision.INSUFFICIENT_EVIDENCE,
        reason_codes=("BOOK_COVERAGE_INSUFFICIENT", "FUNDING_COVERAGE_INSUFFICIENT"),
        direction=None,
        short_venue=None,
        long_venue=None,
        economics=None,
    )
    future = economics_report(
        evaluation_id=UUID(int=91_002),
        evaluated_at=selected.evaluated_at + timedelta(seconds=10),
    )
    store.append_economic_evaluation(selected)
    store.append_economic_evaluation(future)

    report = LighterDydxTrialHealthAuditor(store).audit(
        selected.evaluated_at + timedelta(seconds=1), 1
    )
    btc, eth, sol = report.economics

    assert btc.available is True
    assert btc.evaluation_schema_version == 2
    assert btc.evaluation_id == selected.evaluation_id
    assert btc.policy_hash == selected.policy_hash
    assert btc.known_as_of == KNOWN_AS_OF
    assert btc.evaluated_at == selected.evaluated_at
    assert btc.decision is EconomicsDecision.INSUFFICIENT_EVIDENCE
    assert btc.reason_codes == selected.reason_codes
    assert eth.available is False
    assert sol.available is False
    assert future.evaluation_id not in {item.evaluation_id for item in report.economics}
    store.close()


def test_health_represents_legacy_economics_without_inventing_policy_or_decision(
    tmp_path: Path,
) -> None:
    from tests.carry.test_economics_models import legacy_report_json
    from tests.carry.test_economics_models import report as economics_report

    store = DuckDBStore(tmp_path / "trial.duckdb")
    legacy = economics_report(evaluation_id=UUID(int=92_001))
    store._connection.execute(
        """
        INSERT INTO economic_evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?::JSON, ?, ?)
        """,
        [
            legacy.evaluation_id,
            legacy.asset.value,
            legacy.known_as_of,
            legacy.evaluated_at,
            legacy.decision.value,
            legacy.direction.value,
            legacy.policy_hash,
            legacy_report_json(legacy),
            1,
            "9" * 64,
        ],
    )

    summary = LighterDydxTrialHealthAuditor(store).audit(legacy.evaluated_at, 1).economics[0]

    assert summary.available is True
    assert summary.evaluation_schema_version == 1
    assert summary.policy_hash is None
    assert summary.decision is legacy.decision
    assert summary.reason_codes == ("LEGACY_ECONOMICS_SCHEMA_UNSUPPORTED",)
    store.close()


def test_complete_2160_hour_trial_is_ready_for_economics_evaluation(
    tmp_path: Path, complete_2160_hour_trial_database: Path
) -> None:
    store = copied_trial_store(tmp_path, complete_2160_hour_trial_database)
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
