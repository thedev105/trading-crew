from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.carry.economics_models import EconomicsDecision, require_machine_reason_codes
from polytrading.domain.models import Asset, StrictRecord, Venue, normalize_utc_timestamp

NonnegativeInt = Annotated[int, Field(ge=0)]
NonnegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Fraction = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

TRIAL_HEALTH_PROTOCOL_VERSION = "lighter-dydx-trial-health-v1"
TRIAL_HEALTH_WARNINGS: tuple[str, str, str] = (
    "Research only: this report measures public evidence collection, not expected returns.",
    "READY_FOR_ECONOMICS_EVALUATION does not authorize paper or live trading.",
    "No credentials, accounts, balances, positions, orders, fills, or transfers were accessed.",
)

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ASSETS = (Asset.BTC, Asset.ETH, Asset.SOL)
_FEE_VENUES = (Venue.DYDX, Venue.LIGHTER)
_HOUR = timedelta(hours=1)
_POINT_IN_TIME_LAG = timedelta(minutes=5)
_TRAINING_HOURS = 720
_EVALUATION_HOURS = 1_440
_TOTAL_FUNDING_HOURS = 2_160
_CURRENT_FUNDING_HOURS = 168
_MINIMUM_COVERAGE = Decimal("0.99")
_MAXIMUM_BOOK_AGE_SECONDS = Decimal("30")
_MAXIMUM_BOOK_SKEW_MS = Decimal("1000")


class _ValidatedCopyRecord(StrictRecord):
    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        values = self.model_dump(mode="python", round_trip=True)
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


class TrialCollectionStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    COLLECTING = "COLLECTING"
    DEGRADED = "DEGRADED"
    READY_FOR_ECONOMICS_EVALUATION = "READY_FOR_ECONOMICS_EVALUATION"


class TrialEvidenceStatus(StrEnum):
    MISSING = "missing"
    LATE = "late"
    DEGRADED = "degraded"
    COMPLETE = "complete"


@dataclass(frozen=True)
class TrialWindowBoundaries:
    training_funding: tuple[datetime, ...]
    evaluation_funding: tuple[datetime, ...]
    total_funding: tuple[datetime, ...]
    evaluation_books: tuple[datetime, ...]
    current_funding: tuple[datetime, ...]

    def __post_init__(self) -> None:
        named_windows = (
            ("training funding", self.training_funding, _TRAINING_HOURS),
            ("evaluation funding", self.evaluation_funding, _EVALUATION_HOURS),
            ("total funding", self.total_funding, _TOTAL_FUNDING_HOURS),
            ("evaluation books", self.evaluation_books, _EVALUATION_HOURS),
            ("current funding", self.current_funding, _CURRENT_FUNDING_HOURS),
        )
        for name, boundaries, expected_length in named_windows:
            if not isinstance(boundaries, tuple) or len(boundaries) != expected_length:
                raise ValueError(f"{name} must contain exactly {expected_length} boundaries")
            if any(
                boundary.tzinfo is None or boundary.utcoffset() != timedelta(0)
                for boundary in boundaries
            ):
                raise ValueError(f"{name} boundaries must be UTC-aware")
            if any(
                any((boundary.minute, boundary.second, boundary.microsecond))
                for boundary in boundaries
            ):
                raise ValueError(f"{name} boundaries must align to whole UTC hours")
            if any(right - left != _HOUR for left, right in pairwise(boundaries)):
                raise ValueError(f"{name} boundaries must be strictly hourly consecutive")

        if self.training_funding + self.evaluation_funding != self.total_funding:
            raise ValueError("training and evaluation funding must partition total funding")
        if self.evaluation_books != self.evaluation_funding:
            raise ValueError("evaluation book and funding boundaries must be identical")
        if self.current_funding != self.total_funding[-_CURRENT_FUNDING_HOURS:]:
            raise ValueError("current funding must share the final total-funding anchor")


def resolve_latest_auditable_trial_boundary(as_of: datetime) -> datetime:
    """Return the last hourly boundary whose inclusive five-minute close has elapsed."""
    normalized_as_of = normalize_utc_timestamp(as_of)
    current_hour = normalized_as_of.replace(minute=0, second=0, microsecond=0)
    latest = (
        current_hour
        if normalized_as_of >= current_hour + _POINT_IN_TIME_LAG
        else current_hour - _HOUR
    )
    if latest < _UNIX_EPOCH:
        raise ValueError("trial boundary must not precede Unix epoch")
    return latest


def trial_window_boundaries(study_end: datetime) -> TrialWindowBoundaries:
    """Build the protocol's inclusive hourly evidence windows ending at ``study_end``."""
    normalized_end = normalize_utc_timestamp(study_end)
    if any((normalized_end.minute, normalized_end.second, normalized_end.microsecond)):
        raise ValueError("study end must align to a whole UTC hour")
    if normalized_end - timedelta(hours=_TOTAL_FUNDING_HOURS - 1) < _UNIX_EPOCH:
        raise ValueError("trial windows must not precede Unix epoch")

    total = _hourly_window(normalized_end, _TOTAL_FUNDING_HOURS)
    training = total[:_TRAINING_HOURS]
    evaluation = total[_TRAINING_HOURS:]
    return TrialWindowBoundaries(
        training_funding=training,
        evaluation_funding=evaluation,
        total_funding=total,
        evaluation_books=evaluation,
        current_funding=_hourly_window(normalized_end, _CURRENT_FUNDING_HOURS),
    )


class TrialBoundaryAssetHealth(_ValidatedCopyRecord):
    schema_version: Literal[1]
    asset: Asset
    funding_status: TrialEvidenceStatus
    book_status: TrialEvidenceStatus
    selected_funding_cycle_ids: tuple[UUID, ...]
    selected_book_cycle_id: UUID | None
    failed_book_attempt_count: NonnegativeInt
    skewed_book_attempt_count: NonnegativeInt
    reason_codes: tuple[str, ...]

    @field_validator("selected_funding_cycle_ids")
    @classmethod
    def require_canonical_cycle_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("selected funding cycle IDs must be ordered and unique")
        return value

    @field_validator("reason_codes")
    @classmethod
    def require_canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_consistent_asset_evidence(self) -> TrialBoundaryAssetHealth:
        reasons: set[str] = set()
        if self.funding_status is TrialEvidenceStatus.COMPLETE:
            if not self.selected_funding_cycle_ids:
                raise ValueError("complete funding requires selected cycle IDs")
        else:
            if self.selected_funding_cycle_ids:
                raise ValueError("nonqualifying funding cannot select cycle IDs")
            if self.funding_status is TrialEvidenceStatus.DEGRADED:
                asset = self.asset.value
                degraded = {
                    f"FUNDING_{asset}_REVISION_CONFLICT",
                    f"FUNDING_{asset}_LINKAGE_MISSING",
                    *(
                        f"FUNDING_{asset}_{venue}_{outcome}"
                        for venue in ("DYDX", "LIGHTER")
                        for outcome in ("FAILED", "MISSING_EXPECTED")
                    ),
                    *(f"INSTRUMENT_{asset}_{venue}_FAILED" for venue in ("DYDX", "LIGHTER")),
                }
                selected_degraded = set(self.reason_codes) & degraded
                if not selected_degraded or any(
                    code.startswith((f"FUNDING_{asset}_", f"INSTRUMENT_{asset}_"))
                    and code not in degraded
                    for code in self.reason_codes
                ):
                    raise ValueError("degraded funding requires sanitized reason codes")
                reasons.update(selected_degraded)
            elif self.funding_status is TrialEvidenceStatus.LATE:
                reasons.add(f"FUNDING_{self.asset.value}_LATE_ONLY")
            else:
                reasons.add(f"FUNDING_{self.asset.value}_MISSING")

        if self.book_status is TrialEvidenceStatus.COMPLETE:
            if self.selected_book_cycle_id is None:
                raise ValueError("complete book requires a selected cycle ID")
        else:
            if self.selected_book_cycle_id is not None:
                raise ValueError("nonqualifying book cannot select a cycle ID")
            if self.book_status is TrialEvidenceStatus.DEGRADED:
                if self.failed_book_attempt_count == 0 and self.skewed_book_attempt_count == 0:
                    raise ValueError("degraded book requires failed or skewed attempts")
            elif self.book_status is TrialEvidenceStatus.MISSING:
                if self.failed_book_attempt_count or self.skewed_book_attempt_count:
                    raise ValueError("missing book cannot hide failed or skewed attempts")
                reasons.add(f"BOOK_{self.asset.value}_MISSING")
            else:
                raise ValueError("book evidence does not use a late status")

        if self.failed_book_attempt_count:
            reasons.add(f"BOOK_{self.asset.value}_FAILED_ATTEMPTS")
        if self.skewed_book_attempt_count:
            reasons.add(f"BOOK_{self.asset.value}_SKEWED_ATTEMPTS")

        if self.reason_codes != tuple(sorted(reasons)):
            raise ValueError("reason codes do not match asset evidence")
        return self


class TrialBoundaryHealth(_ValidatedCopyRecord):
    schema_version: Literal[1]
    cycle_end: datetime
    status: TrialEvidenceStatus
    attempt_count: NonnegativeInt
    complete_attempt_count: NonnegativeInt
    degraded_attempt_count: NonnegativeInt
    late_attempt_count: NonnegativeInt
    failed_book_attempt_count: NonnegativeInt
    skewed_book_attempt_count: NonnegativeInt
    assets: tuple[TrialBoundaryAssetHealth, ...]
    reason_codes: tuple[str, ...]

    @field_validator("cycle_end")
    @classmethod
    def require_utc_cycle_end(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("assets")
    @classmethod
    def require_canonical_assets(
        cls, value: tuple[TrialBoundaryAssetHealth, ...]
    ) -> tuple[TrialBoundaryAssetHealth, ...]:
        if tuple(item.asset for item in value) != _ASSETS:
            raise ValueError("assets must use canonical BTC/ETH/SOL order")
        return value

    @field_validator("reason_codes")
    @classmethod
    def require_canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_consistent_boundary(self) -> TrialBoundaryHealth:
        if any((self.cycle_end.minute, self.cycle_end.second, self.cycle_end.microsecond)):
            raise ValueError("cycle end must align to a whole UTC hour")
        if self.attempt_count != (
            self.complete_attempt_count + self.degraded_attempt_count + self.late_attempt_count
        ):
            raise ValueError("attempt counts must sum to attempt count")
        if self.failed_book_attempt_count < max(
            item.failed_book_attempt_count for item in self.assets
        ) or self.skewed_book_attempt_count < max(
            item.skewed_book_attempt_count for item in self.assets
        ):
            raise ValueError("book attempt counts must cover asset attempt history")
        if (self.failed_book_attempt_count == 0) != all(
            item.failed_book_attempt_count == 0 for item in self.assets
        ) or (self.skewed_book_attempt_count == 0) != all(
            item.skewed_book_attempt_count == 0 for item in self.assets
        ):
            raise ValueError("book attempt counts must match asset attempt history")

        expected_status = min((item.funding_status for item in self.assets), key=_evidence_rank)
        expected_status = min(
            (expected_status, *(item.book_status for item in self.assets)), key=_evidence_rank
        )
        if self.status is not expected_status:
            raise ValueError("boundary status does not match conservative asset evidence")
        expected_reasons = set(
            _boundary_reasons(
                self.status,
                attempt_count=self.attempt_count,
                complete_attempt_count=self.complete_attempt_count,
            )
        )
        expected_reasons.update(reason for asset in self.assets for reason in asset.reason_codes)
        if self.reason_codes != tuple(sorted(expected_reasons)):
            raise ValueError("reason codes do not match boundary evidence")
        return self


class ReviewedFeeEvidenceSummary(_ValidatedCopyRecord):
    schema_version: Literal[1]
    venue: Venue
    tier_name: str
    effective_from: datetime
    observed_at: datetime
    source_hash: Sha256

    @field_validator("effective_from", "observed_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_supported_fee_evidence(self) -> ReviewedFeeEvidenceSummary:
        if self.venue not in _FEE_VENUES:
            raise ValueError("fee evidence venue must be dYdX or Lighter")
        if not self.tier_name.strip():
            raise ValueError("fee tier name must not be blank")
        if self.effective_from > self.observed_at:
            raise ValueError("fee effective time must not follow observation")
        return self


class TrialEconomicsEvaluationSummary(_ValidatedCopyRecord):
    schema_version: Literal[1]
    asset: Asset
    available: bool
    evaluation_schema_version: Literal[1, 2] | None
    evaluation_id: UUID | None
    policy_hash: Sha256 | None
    known_as_of: datetime | None
    evaluated_at: datetime | None
    decision: EconomicsDecision | None
    reason_codes: tuple[str, ...]

    @field_validator("known_as_of", "evaluated_at")
    @classmethod
    def normalize_optional_economics_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @field_validator("reason_codes")
    @classmethod
    def require_canonical_economics_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("economics reason codes must be sorted and unique")
        return require_machine_reason_codes(value)

    @model_validator(mode="after")
    def require_coherent_economics_summary(self) -> TrialEconomicsEvaluationSummary:
        identity = (
            self.evaluation_schema_version,
            self.evaluation_id,
            self.known_as_of,
            self.evaluated_at,
            self.decision,
        )
        if not self.available:
            if (
                any(value is not None for value in (*identity, self.policy_hash))
                or self.reason_codes
            ):
                raise ValueError("unavailable economics summary must withhold evaluation evidence")
            return self
        if any(value is None for value in identity):
            raise ValueError("available economics summary requires immutable evaluation identity")
        assert self.known_as_of is not None and self.evaluated_at is not None
        if self.evaluated_at < self.known_as_of:
            raise ValueError("economics evaluation must not precede its evidence cutoff")
        if self.evaluation_schema_version == 1:
            if self.policy_hash is not None:
                raise ValueError("legacy economics summary cannot invent a policy hash")
            if "LEGACY_ECONOMICS_SCHEMA_UNSUPPORTED" not in self.reason_codes:
                raise ValueError("legacy economics summary requires an explicit blocker")
        elif self.policy_hash is None:
            raise ValueError("schema-two economics summary requires a policy hash")
        return self


class TrialAssetCoverage(_ValidatedCopyRecord):
    schema_version: Literal[1]
    asset: Asset
    requested_training_funding_hours: Literal[720]
    paired_training_funding_hours: NonnegativeInt
    training_funding_coverage: Fraction
    missing_training_funding_boundaries: tuple[datetime, ...]
    requested_evaluation_funding_hours: Literal[1440]
    paired_evaluation_funding_hours: NonnegativeInt
    evaluation_funding_coverage: Fraction
    missing_evaluation_funding_boundaries: tuple[datetime, ...]
    requested_total_funding_hours: Literal[2160]
    paired_total_funding_hours: NonnegativeInt
    total_funding_coverage: Fraction
    requested_book_hours: Literal[1440]
    paired_book_hours: NonnegativeInt
    book_coverage: Fraction
    missing_book_boundaries: tuple[datetime, ...]
    current_funding_paired_hours: NonnegativeInt
    current_funding_consecutive: bool
    missing_current_funding_boundaries: tuple[datetime, ...]
    dense_book_pair_count: NonnegativeInt
    consecutive_dense_sample_count: NonnegativeInt
    latest_funding_boundary: datetime | None
    latest_book_completed_at: datetime | None
    latest_book_age_seconds: NonnegativeDecimal | None
    latest_book_skew_ms: NonnegativeDecimal | None
    fresh_book_ready: bool
    historical_windows_mature: bool
    projected_earliest_evaluation_end: datetime | None
    reason_codes: tuple[str, ...]

    @field_validator(
        "missing_training_funding_boundaries",
        "missing_evaluation_funding_boundaries",
        "missing_book_boundaries",
        "missing_current_funding_boundaries",
    )
    @classmethod
    def normalize_missing_boundaries(cls, value: tuple[datetime, ...]) -> tuple[datetime, ...]:
        normalized = tuple(normalize_utc_timestamp(item) for item in value)
        if tuple(sorted(set(normalized))) != normalized:
            raise ValueError("missing boundaries must be strictly ordered and unique")
        if any(any((item.minute, item.second, item.microsecond)) for item in normalized):
            raise ValueError("missing boundaries must align to whole UTC hours")
        return normalized

    @field_validator(
        "latest_funding_boundary",
        "latest_book_completed_at",
        "projected_earliest_evaluation_end",
    )
    @classmethod
    def normalize_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @field_validator("reason_codes")
    @classmethod
    def require_canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_consistent_coverage(self) -> TrialAssetCoverage:
        counts_and_coverages = (
            (
                "training funding",
                self.requested_training_funding_hours,
                self.paired_training_funding_hours,
                self.training_funding_coverage,
                self.missing_training_funding_boundaries,
            ),
            (
                "evaluation funding",
                self.requested_evaluation_funding_hours,
                self.paired_evaluation_funding_hours,
                self.evaluation_funding_coverage,
                self.missing_evaluation_funding_boundaries,
            ),
            (
                "book",
                self.requested_book_hours,
                self.paired_book_hours,
                self.book_coverage,
                self.missing_book_boundaries,
            ),
        )
        for name, requested, paired, coverage, missing in counts_and_coverages:
            if paired != requested - len(missing):
                raise ValueError(f"{name} paired count does not match missing boundaries")
            expected_coverage = Decimal(paired) / Decimal(requested)
            if coverage != expected_coverage:
                raise ValueError(f"{name} coverage does not match paired count")

        if self.paired_total_funding_hours != (
            self.paired_training_funding_hours + self.paired_evaluation_funding_hours
        ):
            raise ValueError("total funding paired count does not match partition")
        expected_total_coverage = Decimal(self.paired_total_funding_hours) / Decimal(
            self.requested_total_funding_hours
        )
        if self.total_funding_coverage != expected_total_coverage:
            raise ValueError("total funding coverage does not match paired count")
        if self.current_funding_paired_hours != _CURRENT_FUNDING_HOURS - len(
            self.missing_current_funding_boundaries
        ):
            raise ValueError("current funding paired count does not match missing boundaries")
        expected_current_consecutive = self.current_funding_paired_hours == _CURRENT_FUNDING_HOURS
        if self.current_funding_consecutive is not expected_current_consecutive:
            raise ValueError("current funding consecutive flag does not match exact window")
        if self.consecutive_dense_sample_count > self.dense_book_pair_count:
            raise ValueError("consecutive dense samples must not exceed dense book pairs")

        self._require_latest_funding_boundary_aligned()
        expected_fresh = (
            self.latest_book_completed_at is not None
            and self.latest_book_age_seconds is not None
            and self.latest_book_skew_ms is not None
            and self.latest_book_age_seconds <= _MAXIMUM_BOOK_AGE_SECONDS
            and self.latest_book_skew_ms <= _MAXIMUM_BOOK_SKEW_MS
        )
        if self.fresh_book_ready is not expected_fresh:
            raise ValueError("fresh book ready does not match latest book evidence")
        latest_fields = (
            self.latest_book_completed_at,
            self.latest_book_age_seconds,
            self.latest_book_skew_ms,
        )
        if any(item is None for item in latest_fields) and any(
            item is not None for item in latest_fields
        ):
            raise ValueError("latest book evidence fields must be present together")

        expected_mature = (
            self.training_funding_coverage >= _MINIMUM_COVERAGE
            and self.evaluation_funding_coverage >= _MINIMUM_COVERAGE
            and self.total_funding_coverage >= _MINIMUM_COVERAGE
            and self.book_coverage >= _MINIMUM_COVERAGE
            and self.current_funding_consecutive
            and self.consecutive_dense_sample_count >= 1
        )
        if self.historical_windows_mature is not expected_mature:
            raise ValueError("historical windows mature does not match coverage evidence")
        if self.reason_codes != _coverage_reasons(self):
            raise ValueError("reason codes do not match coverage evidence")
        return self

    def _require_latest_funding_boundary_aligned(self) -> None:
        if self.latest_funding_boundary is None:
            return
        if any(
            (
                self.latest_funding_boundary.minute,
                self.latest_funding_boundary.second,
                self.latest_funding_boundary.microsecond,
            )
        ):
            raise ValueError("latest funding boundary must align to a whole UTC hour")


class LighterDydxTrialHealthReport(_ValidatedCopyRecord):
    schema_version: Literal[1]
    protocol_version: Literal["lighter-dydx-trial-health-v1"]
    as_of: datetime
    latest_auditable_boundary: datetime
    recent_hours: Annotated[int, Field(ge=1, le=2160)]
    trial_started_at: datetime | None
    elapsed_auditable_hours: NonnegativeInt
    status: TrialCollectionStatus
    recent_boundaries: tuple[TrialBoundaryHealth, ...]
    assets: tuple[TrialAssetCoverage, ...]
    dossier_available: bool
    reviewed_fees: tuple[ReviewedFeeEvidenceSummary, ...]
    economics: tuple[TrialEconomicsEvaluationSummary, ...]
    source_hashes: tuple[Sha256, ...]
    warnings: tuple[str, str, str]

    @field_validator("as_of", "latest_auditable_boundary", "trial_started_at")
    @classmethod
    def normalize_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @field_validator("recent_boundaries")
    @classmethod
    def require_ordered_recent_boundaries(
        cls, value: tuple[TrialBoundaryHealth, ...]
    ) -> tuple[TrialBoundaryHealth, ...]:
        ends = tuple(item.cycle_end for item in value)
        if tuple(sorted(set(ends))) != ends:
            raise ValueError("recent boundaries must be ordered and unique")
        return value

    @field_validator("assets")
    @classmethod
    def require_canonical_assets(
        cls, value: tuple[TrialAssetCoverage, ...]
    ) -> tuple[TrialAssetCoverage, ...]:
        if tuple(item.asset for item in value) != _ASSETS:
            raise ValueError("assets must use canonical BTC/ETH/SOL order")
        return value

    @field_validator("reviewed_fees")
    @classmethod
    def require_canonical_fee_order(
        cls, value: tuple[ReviewedFeeEvidenceSummary, ...]
    ) -> tuple[ReviewedFeeEvidenceSummary, ...]:
        keys = tuple(
            (
                _FEE_VENUES.index(item.venue),
                item.tier_name,
                item.effective_from,
                item.observed_at,
                item.source_hash,
            )
            for item in value
        )
        if len(set(keys)) != len(keys):
            raise ValueError("reviewed fees must be unique")
        if tuple(sorted(keys)) != keys:
            raise ValueError("reviewed fees must use canonical dYdX/Lighter order")
        return value

    @field_validator("economics")
    @classmethod
    def require_canonical_economics(
        cls, value: tuple[TrialEconomicsEvaluationSummary, ...]
    ) -> tuple[TrialEconomicsEvaluationSummary, ...]:
        if tuple(item.asset for item in value) != _ASSETS:
            raise ValueError("economics must use canonical BTC/ETH/SOL order")
        return value

    @field_validator("source_hashes")
    @classmethod
    def require_canonical_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("source hashes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_consistent_report(self) -> LighterDydxTrialHealthReport:
        if self.latest_auditable_boundary != resolve_latest_auditable_trial_boundary(self.as_of):
            raise ValueError("latest auditable boundary does not match as-of")
        if any(
            (
                self.latest_auditable_boundary.minute,
                self.latest_auditable_boundary.second,
                self.latest_auditable_boundary.microsecond,
            )
        ):
            raise ValueError("latest auditable boundary must align to a whole UTC hour")
        if (
            any(item.cycle_end > self.latest_auditable_boundary for item in self.recent_boundaries)
            or any(
                item.latest_funding_boundary is not None
                and item.latest_funding_boundary > self.latest_auditable_boundary
                for item in self.assets
            )
            or any(item.observed_at > self.as_of for item in self.reviewed_fees)
            or any(
                timestamp is not None and timestamp > self.as_of
                for item in self.economics
                for timestamp in (item.known_as_of, item.evaluated_at)
            )
        ):
            raise ValueError("report must not contain future evidence")
        for asset in self.assets:
            if asset.latest_book_completed_at is not None:
                if asset.latest_book_completed_at > self.as_of:
                    raise ValueError("latest book completion must not follow as-of")
                assert asset.latest_book_age_seconds is not None
                expected_age = Decimal(
                    (self.as_of - asset.latest_book_completed_at) // timedelta(microseconds=1)
                ) / Decimal(1_000_000)
                if asset.latest_book_age_seconds != expected_age:
                    raise ValueError("latest book age must equal as-of minus completion")
            if (
                asset.historical_windows_mature
                and asset.latest_funding_boundary != self.latest_auditable_boundary
            ):
                raise ValueError("mature asset funding anchor must equal latest boundary")

        current_windows = trial_window_boundaries(self.latest_auditable_boundary)
        for asset in self.assets:
            current_missing_windows = (
                (asset.missing_training_funding_boundaries, current_windows.training_funding),
                (asset.missing_evaluation_funding_boundaries, current_windows.evaluation_funding),
                (asset.missing_book_boundaries, current_windows.evaluation_books),
                (asset.missing_current_funding_boundaries, current_windows.current_funding),
            )
            if any(
                not set(missing).issubset(allowed) for missing, allowed in current_missing_windows
            ):
                raise ValueError("missing boundaries must belong to the current study window")

        if self.trial_started_at is None:
            if self.status is not TrialCollectionStatus.NOT_STARTED:
                raise ValueError("collecting or ready status requires a trial start")
            if self.elapsed_auditable_hours != 0 or self.recent_boundaries:
                raise ValueError("not started report must have no elapsed boundaries")
        else:
            self._require_started_report()

        if self.warnings != TRIAL_HEALTH_WARNINGS:
            raise ValueError("report must contain the exact research warnings")
        return self

    def _require_started_report(self) -> None:
        assert self.trial_started_at is not None
        if self.status is TrialCollectionStatus.NOT_STARTED:
            raise ValueError("not started status must not have a trial start")
        if any(
            (
                self.trial_started_at.minute,
                self.trial_started_at.second,
                self.trial_started_at.microsecond,
            )
        ):
            raise ValueError("trial start must align to a whole UTC hour")
        if self.trial_started_at > self.latest_auditable_boundary:
            raise ValueError("trial start must not follow latest auditable boundary")
        expected_elapsed = ((self.latest_auditable_boundary - self.trial_started_at) // _HOUR) + 1
        if self.elapsed_auditable_hours != expected_elapsed:
            raise ValueError("elapsed auditable hours does not match trial start")
        requested_first = self.latest_auditable_boundary - timedelta(hours=self.recent_hours - 1)
        expected_first = max(requested_first, self.trial_started_at)
        expected_ends = tuple(
            expected_first + timedelta(hours=index)
            for index in range(int((self.latest_auditable_boundary - expected_first) // _HOUR) + 1)
        )
        if tuple(item.cycle_end for item in self.recent_boundaries) != expected_ends:
            raise ValueError("recent boundaries must cover the exact started recent window")

        any_degraded = any(
            item.status is not TrialEvidenceStatus.COMPLETE for item in self.recent_boundaries
        )
        all_ready = all(
            item.historical_windows_mature and item.fresh_book_ready for item in self.assets
        )
        expected_status = (
            TrialCollectionStatus.DEGRADED
            if any_degraded
            else TrialCollectionStatus.READY_FOR_ECONOMICS_EVALUATION
            if all_ready
            else TrialCollectionStatus.COLLECTING
        )
        if self.status is not expected_status:
            raise ValueError("collection status does not match boundary and readiness evidence")


def _hourly_window(study_end: datetime, hours: int) -> tuple[datetime, ...]:
    return tuple(study_end - timedelta(hours=hours - index - 1) for index in range(hours))


def _evidence_rank(status: TrialEvidenceStatus) -> int:
    return {
        TrialEvidenceStatus.MISSING: 0,
        TrialEvidenceStatus.LATE: 1,
        TrialEvidenceStatus.DEGRADED: 2,
        TrialEvidenceStatus.COMPLETE: 3,
    }[status]


def _boundary_reasons(
    status: TrialEvidenceStatus, *, attempt_count: int, complete_attempt_count: int
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if status is TrialEvidenceStatus.MISSING:
        reasons.add("BOUNDARY_MISSING")
    elif status is TrialEvidenceStatus.LATE:
        reasons.add("BOUNDARY_LATE_ONLY")
    elif status is TrialEvidenceStatus.DEGRADED:
        reasons.add("BOUNDARY_DEGRADED_ONLY")
    if attempt_count > 1:
        reasons.add("MULTIPLE_ATTEMPTS")
    if complete_attempt_count > 1:
        reasons.add("MULTIPLE_COMPLETE_ATTEMPTS")
    return tuple(sorted(reasons))


def _coverage_reasons(coverage: TrialAssetCoverage) -> tuple[str, ...]:
    reasons: set[str] = set()
    if coverage.training_funding_coverage < _MINIMUM_COVERAGE:
        reasons.add("FUNDING_TRAINING_COVERAGE_INSUFFICIENT")
    if coverage.evaluation_funding_coverage < _MINIMUM_COVERAGE:
        reasons.add("FUNDING_EVALUATION_COVERAGE_INSUFFICIENT")
    if coverage.total_funding_coverage < _MINIMUM_COVERAGE:
        reasons.add("FUNDING_COVERAGE_INSUFFICIENT")
    if coverage.book_coverage < _MINIMUM_COVERAGE:
        reasons.add("BOOK_COVERAGE_INSUFFICIENT")
    if not coverage.current_funding_consecutive:
        reasons.add("CURRENT_FUNDING_WINDOW_INSUFFICIENT")
    if coverage.consecutive_dense_sample_count < 1:
        reasons.add("LATENCY_SAMPLES_MISSING")
    if coverage.latest_book_completed_at is None:
        reasons.add("BOOK_LATEST_MISSING")
    else:
        assert coverage.latest_book_age_seconds is not None
        assert coverage.latest_book_skew_ms is not None
        if coverage.latest_book_age_seconds > _MAXIMUM_BOOK_AGE_SECONDS:
            reasons.add("BOOK_LATEST_STALE")
        if coverage.latest_book_skew_ms > _MAXIMUM_BOOK_SKEW_MS:
            reasons.add("BOOK_LATEST_SKEW_EXCEEDED")
    return tuple(sorted(reasons))
