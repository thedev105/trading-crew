from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.domain.models import StrictRecord, normalize_utc_timestamp
from polytrading.venues.funding_cycle_models import FUNDING_POINT_IN_TIME_LAG

FUNDING_HEALTH_PROTOCOL_VERSION = "funding-collection-health-v1"
FUNDING_HEALTH_MAX_HOURS = 2_160
FUNDING_HEALTH_WARNINGS = (
    "Research only: collection health is not strategy or return evidence.",
    "No credentials, accounts, positions, or orders were accessed.",
)
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FundingBoundaryStatus(StrEnum):
    MISSING = "missing"
    LATE = "late"
    DEGRADED = "degraded"
    COMPLETE = "complete"


class FundingCollectionHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


def resolve_health_window(
    as_of: datetime, requested_hours: int
) -> tuple[datetime, datetime, datetime]:
    if isinstance(requested_hours, bool) or not isinstance(requested_hours, int):
        raise TypeError("requested hours must be an integer")
    if requested_hours < 1 or requested_hours > FUNDING_HEALTH_MAX_HOURS:
        raise ValueError("requested hours must be between 1 and 2160")
    normalized_as_of = normalize_utc_timestamp(as_of)
    latest = normalized_as_of.replace(minute=0, second=0, microsecond=0)
    if normalized_as_of < latest + FUNDING_POINT_IN_TIME_LAG:
        latest -= timedelta(hours=1)
    first = latest - timedelta(hours=requested_hours - 1)
    if first < _UNIX_EPOCH:
        raise ValueError("health window must not precede Unix epoch")
    return normalized_as_of, first, latest


class FundingBoundaryHealth(StrictRecord):
    schema_version: Literal[1]
    cycle_end: datetime
    status: FundingBoundaryStatus
    attempt_count: Annotated[int, Field(ge=0)]
    complete_attempt_count: Annotated[int, Field(ge=0)]
    degraded_attempt_count: Annotated[int, Field(ge=0)]
    late_attempt_count: Annotated[int, Field(ge=0)]
    selected_cycle_id: UUID | None
    selected_request_completed_at: datetime | None
    selected_source_hashes: tuple[Sha256, ...]
    reason_codes: tuple[str, ...]

    @field_validator("cycle_end", "selected_request_completed_at")
    @classmethod
    def normalize_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @field_validator("selected_source_hashes")
    @classmethod
    def require_canonical_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("selected source hashes must be sorted and unique")
        return value

    @field_validator("reason_codes")
    @classmethod
    def require_canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_consistent_boundary(self) -> FundingBoundaryHealth:
        if any((self.cycle_end.minute, self.cycle_end.second, self.cycle_end.microsecond)):
            raise ValueError("cycle end must align to a whole UTC hour")
        if self.attempt_count != (
            self.complete_attempt_count + self.degraded_attempt_count + self.late_attempt_count
        ):
            raise ValueError("attempt counts must sum to attempt count")

        required_status = _boundary_status(
            complete_count=self.complete_attempt_count,
            degraded_count=self.degraded_attempt_count,
            late_count=self.late_attempt_count,
        )
        if self.status is not required_status:
            raise ValueError("boundary status does not match attempts")

        selected_fields_present = (
            self.selected_cycle_id is not None and self.selected_request_completed_at is not None
        )
        selected_fields_absent = (
            self.selected_cycle_id is None and self.selected_request_completed_at is None
        )
        if self.status is FundingBoundaryStatus.MISSING:
            if not selected_fields_absent or self.selected_source_hashes:
                raise ValueError("selected attempt fields must be absent for missing boundary")
        elif not selected_fields_present:
            raise ValueError("selected attempt fields must be present together")

        if (
            self.selected_request_completed_at is not None
            and self.selected_request_completed_at < self.cycle_end
        ):
            raise ValueError("selected completion must not precede boundary")

        if self.reason_codes != _boundary_reasons(
            required_status,
            attempt_count=self.attempt_count,
            complete_count=self.complete_attempt_count,
        ):
            raise ValueError("reason codes do not match boundary attempts")
        return self


class FundingCollectionHealthReport(StrictRecord):
    schema_version: Literal[1]
    protocol_version: Literal["funding-collection-health-v1"]
    as_of: datetime
    latest_auditable_boundary: datetime
    first_boundary: datetime
    last_boundary: datetime
    requested_hours: Annotated[int, Field(ge=1, le=FUNDING_HEALTH_MAX_HOURS)]
    boundaries: tuple[FundingBoundaryHealth, ...]
    status: FundingCollectionHealthStatus
    complete_boundary_count: Annotated[int, Field(ge=0)]
    degraded_boundary_count: Annotated[int, Field(ge=0)]
    late_boundary_count: Annotated[int, Field(ge=0)]
    missing_boundary_count: Annotated[int, Field(ge=0)]
    complete_coverage: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    current_complete_streak: Annotated[int, Field(ge=0)]
    source_hashes: tuple[Sha256, ...]
    warnings: tuple[str, str]

    @field_validator("as_of", "latest_auditable_boundary", "first_boundary", "last_boundary")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("boundaries")
    @classmethod
    def require_canonical_boundaries(
        cls, value: tuple[FundingBoundaryHealth, ...]
    ) -> tuple[FundingBoundaryHealth, ...]:
        ends = tuple(item.cycle_end for item in value)
        if tuple(sorted(set(ends))) != ends:
            raise ValueError("boundaries must be ordered and unique")
        return value

    @field_validator("source_hashes")
    @classmethod
    def require_canonical_source_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("source hashes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_consistent_report(self) -> FundingCollectionHealthReport:
        normalized_as_of, expected_first, expected_last = resolve_health_window(
            self.as_of, self.requested_hours
        )
        if (
            self.as_of != normalized_as_of
            or self.latest_auditable_boundary != expected_last
            or self.first_boundary != expected_first
            or self.last_boundary != expected_last
        ):
            raise ValueError("report fields do not match health window")

        expected_boundaries = tuple(
            expected_first + timedelta(hours=index) for index in range(self.requested_hours)
        )
        if tuple(item.cycle_end for item in self.boundaries) != expected_boundaries:
            raise ValueError("boundaries must cover the exact health window")
        if any(
            item.selected_request_completed_at is not None
            and item.selected_request_completed_at > self.as_of
            for item in self.boundaries
        ):
            raise ValueError("selected completion must not follow as-of")

        counts = {
            status: sum(item.status is status for item in self.boundaries)
            for status in FundingBoundaryStatus
        }
        actual_counts = (
            self.complete_boundary_count,
            self.degraded_boundary_count,
            self.late_boundary_count,
            self.missing_boundary_count,
        )
        expected_counts = (
            counts[FundingBoundaryStatus.COMPLETE],
            counts[FundingBoundaryStatus.DEGRADED],
            counts[FundingBoundaryStatus.LATE],
            counts[FundingBoundaryStatus.MISSING],
        )
        if actual_counts != expected_counts:
            raise ValueError("boundary counts do not match boundaries")

        expected_coverage = Decimal(self.complete_boundary_count) / Decimal(self.requested_hours)
        if self.complete_coverage != expected_coverage:
            raise ValueError("complete coverage does not match boundaries")

        expected_streak = 0
        for item in reversed(self.boundaries):
            if item.status is not FundingBoundaryStatus.COMPLETE:
                break
            expected_streak += 1
        if self.current_complete_streak != expected_streak:
            raise ValueError("current complete streak does not match boundaries")

        required_status = (
            FundingCollectionHealthStatus.CRITICAL
            if self.missing_boundary_count or self.late_boundary_count
            else FundingCollectionHealthStatus.DEGRADED
            if self.degraded_boundary_count
            else FundingCollectionHealthStatus.HEALTHY
        )
        if self.status is not required_status:
            raise ValueError("health status does not match boundaries")

        expected_hashes = tuple(
            sorted(
                {
                    source_hash
                    for item in self.boundaries
                    for source_hash in item.selected_source_hashes
                }
            )
        )
        if self.source_hashes != expected_hashes:
            raise ValueError("source hashes do not match selected boundaries")
        if self.warnings != FUNDING_HEALTH_WARNINGS:
            raise ValueError("report must contain the exact research warnings")
        return self


def _boundary_status(
    *, complete_count: int, degraded_count: int, late_count: int
) -> FundingBoundaryStatus:
    if complete_count:
        return FundingBoundaryStatus.COMPLETE
    if degraded_count:
        return FundingBoundaryStatus.DEGRADED
    if late_count:
        return FundingBoundaryStatus.LATE
    return FundingBoundaryStatus.MISSING


def _boundary_reasons(
    status: FundingBoundaryStatus, *, attempt_count: int, complete_count: int
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if status is FundingBoundaryStatus.MISSING:
        reasons.add("BOUNDARY_MISSING")
    elif status is FundingBoundaryStatus.LATE:
        reasons.add("BOUNDARY_LATE_ONLY")
    elif status is FundingBoundaryStatus.DEGRADED:
        reasons.add("BOUNDARY_DEGRADED_ONLY")
    if attempt_count > 1:
        reasons.add("MULTIPLE_ATTEMPTS")
    if complete_count > 1:
        reasons.add("MULTIPLE_COMPLETE_ATTEMPTS")
    return tuple(sorted(reasons))
