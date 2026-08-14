from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.carry.economics_models import EconomicsDecision
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
    TrialEconomicsEvaluationSummary,
    TrialEvidenceStatus,
    TrialWindowBoundaries,
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


def test_direct_trial_window_construction_is_fail_closed() -> None:
    windows = trial_window_boundaries(at(17))
    values = {
        "training_funding": windows.training_funding,
        "evaluation_funding": windows.evaluation_funding,
        "total_funding": windows.total_funding,
        "evaluation_books": windows.evaluation_books,
        "current_funding": windows.current_funding,
    }
    assert TrialWindowBoundaries(**values) == windows

    invalid_updates = (
        {"training_funding": windows.training_funding[:-1]},
        {
            "evaluation_funding": (
                windows.evaluation_funding[0].replace(tzinfo=None),
                *windows.evaluation_funding[1:],
            )
        },
        {
            "total_funding": (
                *windows.total_funding[:100],
                windows.total_funding[99],
                *windows.total_funding[101:],
            )
        },
        {
            "evaluation_books": tuple(
                boundary - timedelta(hours=1) for boundary in windows.evaluation_books
            )
        },
        {
            "current_funding": tuple(
                boundary - timedelta(hours=1) for boundary in windows.current_funding
            )
        },
    )
    for update in invalid_updates:
        with pytest.raises(ValueError):
            TrialWindowBoundaries(**(values | update))


def asset_health(asset: Asset, **overrides: object) -> TrialBoundaryAssetHealth:
    values: dict[str, object] = {
        "schema_version": 1,
        "asset": asset,
        "funding_status": TrialEvidenceStatus.COMPLETE,
        "book_status": TrialEvidenceStatus.COMPLETE,
        "selected_funding_cycle_ids": (UUID(int=asset_index(asset) + 1),),
        "selected_book_cycle_id": UUID(int=asset_index(asset) + 11),
        "failed_book_attempt_count": 0,
        "skewed_book_attempt_count": 0,
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
        "failed_book_attempt_count": 0,
        "skewed_book_attempt_count": 0,
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
        "latest_book_completed_at": at(17, 4, 30),
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


def economics(asset: Asset, **overrides: object) -> TrialEconomicsEvaluationSummary:
    values: dict[str, object] = {
        "schema_version": 1,
        "asset": asset,
        "available": False,
        "evaluation_schema_version": None,
        "evaluation_id": None,
        "policy_hash": None,
        "known_as_of": None,
        "evaluated_at": None,
        "decision": None,
        "reason_codes": (),
    }
    values.update(overrides)
    return TrialEconomicsEvaluationSummary(**values)


def test_reviewed_fee_evidence_rejects_unsupported_venue_and_blank_tier() -> None:
    with pytest.raises(ValidationError, match="dYdX or Lighter"):
        fee(Venue.BYBIT)
    with pytest.raises(ValidationError, match="must not be blank"):
        fee(Venue.DYDX, tier_name="   ")


def test_report_accepts_canonical_empty_or_multiple_reviewed_fee_tiers() -> None:
    assert report(reviewed_fees=()).reviewed_fees == ()
    fees = (
        fee(Venue.DYDX, tier_name="alpha"),
        fee(Venue.DYDX, tier_name="zeta", source_hash="c" * 64),
        fee(Venue.LIGHTER, tier_name="beta"),
    )
    assert report(reviewed_fees=fees).reviewed_fees == fees

    with pytest.raises(ValidationError, match="canonical"):
        report(reviewed_fees=tuple(reversed(fees)))
    with pytest.raises(ValidationError, match="unique"):
        report(reviewed_fees=(fees[0], fees[0]))


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
        "economics": tuple(economics(asset) for asset in Asset),
        "source_hashes": (HASH_A, HASH_B),
        "warnings": TRIAL_HEALTH_WARNINGS,
    }
    values.update(overrides)
    return LighterDydxTrialHealthReport(**values)


@pytest.mark.parametrize(
    ("record", "update"),
    [
        (asset_health(Asset.BTC), {"reason_codes": ("Z", "A")}),
        (boundary(), {"status": TrialEvidenceStatus.DEGRADED}),
        (fee(Venue.DYDX), {"venue": Venue.BYBIT}),
        (coverage(Asset.BTC), {"paired_total_funding_hours": 0}),
        (report(), {"warnings": ("changed", *TRIAL_HEALTH_WARNINGS[1:])}),
    ],
)
def test_health_record_copy_revalidates_updates(record: object, update: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        record.model_copy(update=update)  # type: ignore[attr-defined]


def test_boundary_asset_validated_copy_supports_asset_helpers() -> None:
    copied = asset_health(Asset.BTC).model_copy(update={"asset": Asset.ETH})

    assert copied.asset is Asset.ETH


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


def test_boundary_asset_statuses_require_exact_selections_and_reasons() -> None:
    missing = asset_health(
        Asset.BTC,
        funding_status=TrialEvidenceStatus.MISSING,
        book_status=TrialEvidenceStatus.MISSING,
        selected_funding_cycle_ids=(),
        selected_book_cycle_id=None,
        reason_codes=("BOOK_BTC_MISSING", "FUNDING_BTC_MISSING"),
    )
    conflict = asset_health(
        Asset.ETH,
        funding_status=TrialEvidenceStatus.DEGRADED,
        book_status=TrialEvidenceStatus.COMPLETE,
        selected_funding_cycle_ids=(),
        reason_codes=("FUNDING_ETH_REVISION_CONFLICT",),
    )
    assert missing.reason_codes == ("BOOK_BTC_MISSING", "FUNDING_BTC_MISSING")
    assert conflict.reason_codes == ("FUNDING_ETH_REVISION_CONFLICT",)

    invalid_updates = (
        {"selected_funding_cycle_ids": ()},
        {
            "funding_status": TrialEvidenceStatus.MISSING,
            "reason_codes": ("FUNDING_BTC_MISSING",),
        },
        {"selected_book_cycle_id": None},
        {
            "book_status": TrialEvidenceStatus.MISSING,
            "reason_codes": ("BOOK_BTC_MISSING",),
        },
        {
            "funding_status": TrialEvidenceStatus.MISSING,
            "selected_funding_cycle_ids": (),
        },
    )
    for update in invalid_updates:
        with pytest.raises(ValidationError):
            asset_health(Asset.BTC).model_copy(update=update)

    with pytest.raises(ValidationError, match="sanitized"):
        asset_health(Asset.BTC).model_copy(
            update={
                "funding_status": TrialEvidenceStatus.DEGRADED,
                "selected_funding_cycle_ids": (),
                "reason_codes": ("FUNDING_BTC_MACHINE_PATH_SECRET",),
            }
        )


def test_boundary_reasons_are_exact_union_of_boundary_and_asset_reasons() -> None:
    btc = asset_health(
        Asset.BTC,
        funding_status=TrialEvidenceStatus.MISSING,
        selected_funding_cycle_ids=(),
        reason_codes=("FUNDING_BTC_MISSING",),
    )
    reasons = ("BOUNDARY_MISSING", "FUNDING_BTC_MISSING")

    assert (
        boundary(
            status=TrialEvidenceStatus.MISSING,
            assets=(btc, asset_health(Asset.ETH), asset_health(Asset.SOL)),
            reason_codes=reasons,
        ).reason_codes
        == reasons
    )
    with pytest.raises(ValidationError, match="reason codes do not match"):
        boundary(
            status=TrialEvidenceStatus.MISSING,
            assets=(btc, asset_health(Asset.ETH), asset_health(Asset.SOL)),
            reason_codes=("BOUNDARY_MISSING",),
        )


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


def test_report_owns_missing_boundary_containment_anchor() -> None:
    values = coverage_values(training=712, books=1426)
    outside = trial_window_boundaries(at(17)).total_funding[0] - timedelta(hours=1)
    invalid_missing = (outside, *values["missing_training_funding_boundaries"][1:])
    values["missing_training_funding_boundaries"] = invalid_missing

    asset = TrialAssetCoverage(**values)
    assets = tuple(asset.model_copy(update={"asset": item}) for item in Asset)
    with pytest.raises(ValidationError, match="current study window"):
        report(status=TrialCollectionStatus.COLLECTING, assets=assets)


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


def test_economics_summary_rejects_partial_legacy_and_future_evidence() -> None:
    unavailable = economics(Asset.BTC)
    with pytest.raises(ValidationError, match="withhold"):
        unavailable.model_copy(update={"evaluation_id": UUID(int=88)})

    current = economics(
        Asset.BTC,
        available=True,
        evaluation_schema_version=2,
        evaluation_id=UUID(int=89),
        policy_hash="c" * 64,
        known_as_of=at(16),
        evaluated_at=at(16, second=1),
        decision=EconomicsDecision.REJECTED,
        reason_codes=("COMPATIBILITY_BLOCKING",),
    )
    assert current.policy_hash == "c" * 64
    with pytest.raises(ValidationError, match="policy hash"):
        current.model_copy(update={"policy_hash": None})
    with pytest.raises(ValidationError, match="legacy"):
        current.model_copy(
            update={
                "evaluation_schema_version": 1,
                "reason_codes": ("LEGACY_ECONOMICS_SCHEMA_UNSUPPORTED",),
            }
        )

    future = current.model_copy(update={"evaluated_at": at(17, 5, microsecond=1)})
    values = report().model_dump()
    values["economics"] = (
        future,
        economics(Asset.ETH),
        economics(Asset.SOL),
    )
    with pytest.raises(ValidationError, match="future evidence"):
        LighterDydxTrialHealthReport(**values)


@pytest.mark.parametrize(
    "reason_code",
    [
        "/private/evidence/economics.json",
        "https://private.example.test/economics?token=secret",
        "API_TOKEN=secret response-body=confidential",
        "compatibility_blocking",
        "1_LEADING_DIGIT",
        "HAS-DASH",
    ],
)
def test_economics_summary_rejects_non_machine_reason_codes(reason_code: str) -> None:
    with pytest.raises(ValidationError, match="uppercase machine identifier"):
        economics(
            Asset.BTC,
            available=True,
            evaluation_schema_version=2,
            evaluation_id=UUID(int=90),
            policy_hash="c" * 64,
            known_as_of=at(16),
            evaluated_at=at(16, second=1),
            decision=EconomicsDecision.REJECTED,
            reason_codes=(reason_code,),
        )


def test_economics_summary_accepts_current_and_legacy_machine_reason_codes() -> None:
    current = economics(
        Asset.BTC,
        available=True,
        evaluation_schema_version=2,
        evaluation_id=UUID(int=91),
        policy_hash="c" * 64,
        known_as_of=at(16),
        evaluated_at=at(16, second=1),
        decision=EconomicsDecision.REJECTED,
        reason_codes=("BOOK_COVERAGE_INSUFFICIENT",),
    )
    legacy = economics(
        Asset.ETH,
        available=True,
        evaluation_schema_version=1,
        evaluation_id=UUID(int=92),
        policy_hash=None,
        known_as_of=at(16),
        evaluated_at=at(16, second=1),
        decision=EconomicsDecision.REJECTED,
        reason_codes=("LEGACY_ECONOMICS_SCHEMA_UNSUPPORTED",),
    )

    assert current.reason_codes == ("BOOK_COVERAGE_INSUFFICIENT",)
    assert legacy.reason_codes == ("LEGACY_ECONOMICS_SCHEMA_UNSUPPORTED",)


def test_recent_boundaries_cover_only_started_intersection() -> None:
    at_16 = boundary(cycle_end=at(16))
    at_17 = boundary()
    started = report(
        recent_hours=3,
        trial_started_at=at(16),
        elapsed_auditable_hours=2,
        recent_boundaries=(at_16, at_17),
    )
    assert tuple(item.cycle_end for item in started.recent_boundaries) == (at(16), at(17))

    with pytest.raises(ValidationError, match="exact started recent window"):
        report(
            recent_hours=3,
            trial_started_at=at(16),
            elapsed_auditable_hours=2,
            recent_boundaries=(boundary(cycle_end=at(15)), at_16, at_17),
        )
    with pytest.raises(ValidationError, match="exact started recent window"):
        started.model_copy(update={"recent_boundaries": (at_17,)})


@pytest.mark.parametrize(
    ("completed_at", "age_seconds"),
    [
        (at(17, 5, microsecond=1), Decimal("0")),
        (at(17, 4, 30), Decimal("29")),
    ],
)
def test_report_rejects_future_or_fabricated_latest_book_age(
    completed_at: datetime, age_seconds: Decimal
) -> None:
    assets = tuple(
        coverage(
            asset,
            latest_book_completed_at=completed_at,
            latest_book_age_seconds=age_seconds,
        )
        for asset in Asset
    )

    with pytest.raises(ValidationError, match="latest book"):
        report(assets=assets)


@pytest.mark.parametrize("anchor", [None, at(16)])
def test_mature_assets_require_current_funding_anchor(anchor: datetime | None) -> None:
    assets = tuple(coverage(asset, latest_funding_boundary=anchor) for asset in Asset)

    with pytest.raises(ValidationError, match="funding anchor"):
        report(assets=assets)


def test_report_anchors_missing_windows_to_current_study_end() -> None:
    stale_windows = trial_window_boundaries(at(16))
    values = coverage_values(training=712, books=1426)
    values.update(
        latest_funding_boundary=at(16),
        missing_training_funding_boundaries=stale_windows.training_funding[:8],
    )
    stale = TrialAssetCoverage(**values)
    assets = tuple(stale.model_copy(update={"asset": asset}) for asset in Asset)

    with pytest.raises(ValidationError, match="current study window"):
        report(status=TrialCollectionStatus.COLLECTING, assets=assets)


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
