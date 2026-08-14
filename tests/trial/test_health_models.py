from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.domain.models import Asset, Venue
from polytrading.trial.health_models import (
    TRIAL_HEALTH_PROTOCOL_VERSION,
    TRIAL_HEALTH_WARNINGS,
    LighterDydxTrialHealthReport,
    ReviewedFeeEvidenceSummary,
    TrialAssetCoverage,
    TrialBoundaryAssetHealth,
    TrialBoundaryHealth,
    TrialCollectionStatus,
    TrialEvidenceStatus,
    resolve_latest_auditable_trial_boundary,
    trial_window_boundaries,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def at(hour: int, minute: int = 0, second: int = 0, microsecond: int = 0) -> datetime:
    return datetime(2026, 8, 13, hour, minute, second, microsecond, tzinfo=UTC)


@pytest.mark.parametrize(
    ("as_of", "latest"),
    [
        (at(17, 4, 59, 999999), at(16)),
        (at(17, 5), at(17)),
    ],
)
def test_latest_auditable_trial_boundary_closes_at_five_minutes(
    as_of: datetime, latest: datetime
) -> None:
    assert resolve_latest_auditable_trial_boundary(as_of) == latest


def test_trial_windows_have_exact_frozen_lengths() -> None:
    windows = trial_window_boundaries(at(17))
    assert len(windows.training_funding) == 720
    assert len(windows.evaluation_funding) == 1_440
    assert len(windows.total_funding) == 2_160
    assert len(windows.evaluation_books) == 1_440
    assert len(windows.current_funding) == 168
    assert windows.total_funding[-1] == at(17)


def test_trial_windows_are_consecutive_and_partition_total_funding() -> None:
    windows = trial_window_boundaries(at(17))
    for boundaries in (
        windows.training_funding,
        windows.evaluation_funding,
        windows.total_funding,
        windows.evaluation_books,
        windows.current_funding,
    ):
        assert all(right - left == timedelta(hours=1) for left, right in pairwise(boundaries))
    assert windows.training_funding + windows.evaluation_funding == windows.total_funding


@pytest.mark.parametrize(
    "as_of", [at(17).replace(tzinfo=None), datetime(1970, 1, 1, 0, 4, tzinfo=UTC)]
)
def test_latest_boundary_rejects_naive_and_pre_epoch_cutoffs(as_of: datetime) -> None:
    with pytest.raises(ValueError):
        resolve_latest_auditable_trial_boundary(as_of)


def test_trial_windows_require_whole_hour_end() -> None:
    with pytest.raises(ValueError, match="whole UTC hour"):
        trial_window_boundaries(at(17, 1))


def asset_health(asset: Asset, **overrides: object) -> TrialBoundaryAssetHealth:
    values: dict[str, object] = {
        "schema_version": 1,
        "asset": asset,
        "funding_status": TrialEvidenceStatus.COMPLETE,
        "book_status": TrialEvidenceStatus.COMPLETE,
        "selected_funding_cycle_ids": (UUID(int=asset_index(asset) + 1),),
        "selected_book_cycle_id": UUID(int=asset_index(asset) + 11),
        "reason_codes": (),
    }
    values.update(overrides)
    return TrialBoundaryAssetHealth(**values)


def asset_index(asset: Asset) -> int:
    return (Asset.BTC, Asset.ETH, Asset.SOL).index(asset)


def boundary(**overrides: object) -> TrialBoundaryHealth:
    values: dict[str, object] = {
        "schema_version": 1,
        "cycle_end": at(17),
        "status": TrialEvidenceStatus.COMPLETE,
        "attempt_count": 1,
        "complete_attempt_count": 1,
        "degraded_attempt_count": 0,
        "late_attempt_count": 0,
        "assets": tuple(asset_health(asset) for asset in Asset),
        "reason_codes": (),
    }
    values.update(overrides)
    return TrialBoundaryHealth(**values)


def coverage(asset: Asset, **overrides: object) -> TrialAssetCoverage:
    windows = trial_window_boundaries(at(17))
    values: dict[str, object] = {
        "schema_version": 1,
        "asset": asset,
        "requested_training_funding_hours": 720,
        "paired_training_funding_hours": 720,
        "training_funding_coverage": Decimal(1),
        "missing_training_funding_boundaries": (),
        "requested_evaluation_funding_hours": 1440,
        "paired_evaluation_funding_hours": 1440,
        "evaluation_funding_coverage": Decimal(1),
        "missing_evaluation_funding_boundaries": (),
        "requested_total_funding_hours": 2160,
        "paired_total_funding_hours": 2160,
        "total_funding_coverage": Decimal(1),
        "requested_book_hours": 1440,
        "paired_book_hours": 1440,
        "book_coverage": Decimal(1),
        "missing_book_boundaries": (),
        "current_funding_paired_hours": 168,
        "current_funding_consecutive": True,
        "missing_current_funding_boundaries": (),
        "dense_book_pair_count": 1,
        "consecutive_dense_sample_count": 1,
        "latest_funding_boundary": windows.total_funding[-1],
        "latest_book_completed_at": at(17),
        "latest_book_age_seconds": Decimal("30"),
        "latest_book_skew_ms": Decimal("1000"),
        "fresh_book_ready": True,
        "historical_windows_mature": True,
        "projected_earliest_evaluation_end": at(17),
        "reason_codes": (),
    }
    values.update(overrides)
    return TrialAssetCoverage(**values)


def fee(venue: Venue, **overrides: object) -> ReviewedFeeEvidenceSummary:
    values: dict[str, object] = {
        "schema_version": 1,
        "venue": venue,
        "tier_name": "standard",
        "effective_from": at(16),
        "observed_at": at(17),
        "source_hash": HASH_A if venue is Venue.DYDX else HASH_B,
    }
    values.update(overrides)
    return ReviewedFeeEvidenceSummary(**values)


def report(**overrides: object) -> LighterDydxTrialHealthReport:
    current = boundary()
    values: dict[str, object] = {
        "schema_version": 1,
        "protocol_version": TRIAL_HEALTH_PROTOCOL_VERSION,
        "as_of": at(17, 5),
        "latest_auditable_boundary": at(17),
        "recent_hours": 1,
        "trial_started_at": at(17),
        "elapsed_auditable_hours": 1,
        "status": TrialCollectionStatus.READY_FOR_ECONOMICS_EVALUATION,
        "recent_boundaries": (current,),
        "assets": tuple(coverage(asset) for asset in Asset),
        "dossier_available": True,
        "reviewed_fees": (fee(Venue.DYDX), fee(Venue.LIGHTER)),
        "source_hashes": (HASH_A, HASH_B),
        "warnings": TRIAL_HEALTH_WARNINGS,
    }
    values.update(overrides)
    return LighterDydxTrialHealthReport(**values)


def test_boundary_health_requires_canonical_assets_counts_status_and_reasons() -> None:
    assert boundary().status is TrialEvidenceStatus.COMPLETE
    with pytest.raises(ValidationError, match="assets must use canonical"):
        boundary(assets=tuple(reversed(boundary().assets)))
    with pytest.raises(ValidationError, match="attempt counts must sum"):
        boundary(attempt_count=2)
    with pytest.raises(ValidationError, match="boundary status does not match"):
        boundary(status=TrialEvidenceStatus.DEGRADED)
    with pytest.raises(ValidationError, match="reason codes do not match"):
        boundary(reason_codes=("BOUNDARY_MISSING",))


def coverage_values(
    *, training: int = 720, evaluation: int = 1440, books: int = 1440
) -> dict[str, object]:
    base = coverage(Asset.BTC).model_dump()
    windows = trial_window_boundaries(at(17))
    base.update(
        paired_training_funding_hours=training,
        training_funding_coverage=Decimal(training) / Decimal(720),
        missing_training_funding_boundaries=windows.training_funding[: 720 - training],
        paired_evaluation_funding_hours=evaluation,
        evaluation_funding_coverage=Decimal(evaluation) / Decimal(1440),
        missing_evaluation_funding_boundaries=windows.evaluation_funding[: 1440 - evaluation],
        paired_total_funding_hours=training + evaluation,
        total_funding_coverage=Decimal(training + evaluation) / Decimal(2160),
        paired_book_hours=books,
        book_coverage=Decimal(books) / Decimal(1440),
        missing_book_boundaries=windows.evaluation_books[: 1440 - books],
    )
    reasons = set()
    if training < 713:
        reasons.add("FUNDING_TRAINING_COVERAGE_INSUFFICIENT")
    if evaluation < 1426:
        reasons.add("FUNDING_EVALUATION_COVERAGE_INSUFFICIENT")
    if training + evaluation < 2139:
        reasons.add("FUNDING_COVERAGE_INSUFFICIENT")
    if books < 1426:
        reasons.add("BOOK_COVERAGE_INSUFFICIENT")
    base["historical_windows_mature"] = not reasons
    base["reason_codes"] = tuple(sorted(reasons))
    return base


def test_asset_coverage_uses_exact_ninety_nine_percent_thresholds() -> None:
    assert (
        "FUNDING_TRAINING_COVERAGE_INSUFFICIENT"
        in TrialAssetCoverage(**coverage_values(training=712)).reason_codes
    )
    assert (
        "FUNDING_TRAINING_COVERAGE_INSUFFICIENT"
        not in TrialAssetCoverage(**coverage_values(training=713)).reason_codes
    )
    assert (
        "FUNDING_EVALUATION_COVERAGE_INSUFFICIENT"
        in TrialAssetCoverage(**coverage_values(evaluation=1425)).reason_codes
    )
    assert (
        "FUNDING_EVALUATION_COVERAGE_INSUFFICIENT"
        not in TrialAssetCoverage(**coverage_values(evaluation=1426)).reason_codes
    )
    assert (
        "BOOK_COVERAGE_INSUFFICIENT"
        in TrialAssetCoverage(**coverage_values(books=1425)).reason_codes
    )
    assert (
        "BOOK_COVERAGE_INSUFFICIENT"
        not in TrialAssetCoverage(**coverage_values(books=1426)).reason_codes
    )
    assert (
        "FUNDING_COVERAGE_INSUFFICIENT"
        in TrialAssetCoverage(**coverage_values(training=713, evaluation=1425)).reason_codes
    )
    assert (
        "FUNDING_COVERAGE_INSUFFICIENT"
        not in TrialAssetCoverage(**coverage_values(training=713, evaluation=1426)).reason_codes
    )


def test_asset_coverage_requires_exact_current_window_and_derived_booleans() -> None:
    base = coverage(Asset.BTC).model_dump()
    base.update(
        current_funding_paired_hours=167,
        current_funding_consecutive=False,
        missing_current_funding_boundaries=trial_window_boundaries(at(17)).current_funding[:1],
        historical_windows_mature=False,
        reason_codes=("CURRENT_FUNDING_WINDOW_INSUFFICIENT",),
    )
    assert TrialAssetCoverage(**base).current_funding_paired_hours == 167
    invalid = coverage(Asset.BTC).model_dump()
    invalid["current_funding_paired_hours"] = 167
    with pytest.raises(ValidationError, match="current funding"):
        TrialAssetCoverage(**invalid)


def test_report_requires_exact_statuses_ordering_and_elapsed_inclusive_count() -> None:
    assert report().elapsed_auditable_hours == 1
    with pytest.raises(ValidationError, match="elapsed"):
        report(elapsed_auditable_hours=0)
    with pytest.raises(ValidationError, match="canonical dYdX/Lighter"):
        report(reviewed_fees=(fee(Venue.LIGHTER), fee(Venue.DYDX)))
    with pytest.raises(ValidationError, match="source hashes"):
        report(source_hashes=(HASH_B, HASH_A))
    with pytest.raises(ValidationError, match="exact research warnings"):
        report(warnings=("changed", *TRIAL_HEALTH_WARNINGS[1:]))


def test_report_status_coherence_including_calendar_immaturity() -> None:
    with pytest.raises(ValidationError, match="collection status"):
        report(status=TrialCollectionStatus.COLLECTING)
    immature = tuple(
        TrialAssetCoverage(**coverage_values(training=712, books=1426)).model_copy(
            update={"asset": asset}
        )
        for asset in Asset
    )
    collecting = report(status=TrialCollectionStatus.COLLECTING, assets=immature)
    assert collecting.status is TrialCollectionStatus.COLLECTING
    with pytest.raises(ValidationError, match="collection status"):
        report(assets=immature)


def test_not_started_has_no_start_or_boundaries() -> None:
    value = report(
        trial_started_at=None,
        elapsed_auditable_hours=0,
        status=TrialCollectionStatus.NOT_STARTED,
        recent_boundaries=(),
    )
    assert value.status is TrialCollectionStatus.NOT_STARTED
    with pytest.raises(ValidationError, match="not started"):
        report(status=TrialCollectionStatus.NOT_STARTED)
