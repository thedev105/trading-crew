from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.venues.funding_health_models import (
    FUNDING_HEALTH_PROTOCOL_VERSION,
    FUNDING_HEALTH_WARNINGS,
    FundingBoundaryHealth,
    FundingBoundaryStatus,
    FundingCollectionHealthReport,
    FundingCollectionHealthStatus,
    resolve_health_window,
)

AS_OF = datetime(2026, 8, 13, 17, 5, tzinfo=UTC)
BOUNDARY = datetime(2026, 8, 13, 17, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64


def boundary(**overrides: object) -> FundingBoundaryHealth:
    values: dict[str, object] = {
        "schema_version": 1,
        "cycle_end": BOUNDARY,
        "status": FundingBoundaryStatus.COMPLETE,
        "attempt_count": 1,
        "complete_attempt_count": 1,
        "degraded_attempt_count": 0,
        "late_attempt_count": 0,
        "selected_cycle_id": UUID("00000000-0000-0000-0000-000000000971"),
        "selected_request_completed_at": BOUNDARY + timedelta(minutes=2),
        "selected_source_hashes": (HASH_A,),
        "reason_codes": (),
    }
    values.update(overrides)
    return FundingBoundaryHealth(**values)


def report(**overrides: object) -> FundingCollectionHealthReport:
    current = boundary()
    values: dict[str, object] = {
        "schema_version": 1,
        "protocol_version": FUNDING_HEALTH_PROTOCOL_VERSION,
        "as_of": AS_OF,
        "latest_auditable_boundary": BOUNDARY,
        "first_boundary": BOUNDARY,
        "last_boundary": BOUNDARY,
        "requested_hours": 1,
        "boundaries": (current,),
        "status": FundingCollectionHealthStatus.HEALTHY,
        "complete_boundary_count": 1,
        "degraded_boundary_count": 0,
        "late_boundary_count": 0,
        "missing_boundary_count": 0,
        "complete_coverage": Decimal("1"),
        "current_complete_streak": 1,
        "source_hashes": (HASH_A,),
        "warnings": FUNDING_HEALTH_WARNINGS,
    }
    values.update(overrides)
    return FundingCollectionHealthReport(**values)


@pytest.mark.parametrize(
    ("as_of", "expected_last"),
    [
        (
            datetime(2026, 8, 13, 17, 4, 59, 999999, tzinfo=UTC),
            datetime(2026, 8, 13, 16, tzinfo=UTC),
        ),
        (datetime(2026, 8, 13, 17, 5, tzinfo=UTC), BOUNDARY),
    ],
)
def test_health_window_uses_only_closed_collection_windows(
    as_of: datetime, expected_last: datetime
) -> None:
    normalized, first, last = resolve_health_window(as_of, 24)

    assert normalized == as_of
    assert last == expected_last
    assert first == last - timedelta(hours=23)


@pytest.mark.parametrize("hours", [True, 1.5, "24"])
def test_health_window_requires_an_integer_hour_count(hours: object) -> None:
    with pytest.raises(TypeError, match="requested hours must be an integer"):
        resolve_health_window(AS_OF, hours)  # type: ignore[arg-type]


@pytest.mark.parametrize("hours", [0, -1, 2_161])
def test_health_window_rejects_unbounded_hour_counts(hours: int) -> None:
    with pytest.raises(ValueError, match="requested hours must be between 1 and 2160"):
        resolve_health_window(AS_OF, hours)


def test_health_window_rejects_naive_and_pre_epoch_windows() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_health_window(AS_OF.replace(tzinfo=None), 1)
    with pytest.raises(ValueError, match="health window must not precede Unix epoch"):
        resolve_health_window(datetime(1970, 1, 1, 0, 4, tzinfo=UTC), 1)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"cycle_end": BOUNDARY.replace(minute=1)}, "whole UTC hour"),
        ({"attempt_count": 2}, "attempt counts must sum"),
        (
            {"status": FundingBoundaryStatus.DEGRADED},
            "boundary status does not match attempts",
        ),
        ({"selected_cycle_id": None}, "selected attempt fields must be present together"),
        ({"reason_codes": ("BOUNDARY_MISSING",)}, "reason codes do not match"),
        ({"selected_source_hashes": (HASH_B, HASH_A)}, "sorted and unique"),
    ],
)
def test_boundary_health_rejects_contradictory_evidence(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        boundary(**overrides)


def test_missing_boundary_requires_no_selected_attempt_and_exact_reason() -> None:
    missing = boundary(
        status=FundingBoundaryStatus.MISSING,
        attempt_count=0,
        complete_attempt_count=0,
        selected_cycle_id=None,
        selected_request_completed_at=None,
        selected_source_hashes=(),
        reason_codes=("BOUNDARY_MISSING",),
    )

    assert missing.status is FundingBoundaryStatus.MISSING

    with pytest.raises(ValidationError, match="selected attempt fields must be absent"):
        boundary(
            status=FundingBoundaryStatus.MISSING,
            attempt_count=0,
            complete_attempt_count=0,
            selected_source_hashes=(),
            reason_codes=("BOUNDARY_MISSING",),
        )


def test_duplicate_complete_attempts_require_both_duplicate_reasons() -> None:
    duplicate = boundary(
        attempt_count=2,
        complete_attempt_count=2,
        reason_codes=("MULTIPLE_ATTEMPTS", "MULTIPLE_COMPLETE_ATTEMPTS"),
    )

    assert duplicate.attempt_count == 2

    with pytest.raises(ValidationError, match="reason codes do not match"):
        boundary(attempt_count=2, complete_attempt_count=2)


def test_report_accepts_exact_counts_coverage_streak_and_hash_union() -> None:
    first = boundary(
        cycle_end=BOUNDARY - timedelta(hours=2),
        status=FundingBoundaryStatus.MISSING,
        attempt_count=0,
        complete_attempt_count=0,
        selected_cycle_id=None,
        selected_request_completed_at=None,
        selected_source_hashes=(),
        reason_codes=("BOUNDARY_MISSING",),
    )
    second = boundary(
        cycle_end=BOUNDARY - timedelta(hours=1),
        selected_cycle_id=UUID("00000000-0000-0000-0000-000000000972"),
        selected_request_completed_at=BOUNDARY - timedelta(minutes=58),
        selected_source_hashes=(HASH_B,),
    )
    third = boundary()

    result = report(
        first_boundary=BOUNDARY - timedelta(hours=2),
        requested_hours=3,
        boundaries=(first, second, third),
        status=FundingCollectionHealthStatus.CRITICAL,
        complete_boundary_count=2,
        missing_boundary_count=1,
        complete_coverage=Decimal("0.6666666666666666666666666667"),
        current_complete_streak=2,
        source_hashes=(HASH_A, HASH_B),
    )

    assert result.current_complete_streak == 2
    assert result.complete_coverage == Decimal("0.6666666666666666666666666667")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"latest_auditable_boundary": BOUNDARY - timedelta(hours=1)}, "health window"),
        ({"boundaries": ()}, "boundaries must cover"),
        ({"complete_boundary_count": 0}, "boundary counts do not match"),
        ({"complete_coverage": Decimal("0.5")}, "coverage does not match"),
        ({"current_complete_streak": 0}, "complete streak does not match"),
        ({"status": FundingCollectionHealthStatus.DEGRADED}, "health status does not match"),
        ({"source_hashes": ()}, "source hashes do not match"),
        ({"warnings": ("changed", "warning")}, "exact research warnings"),
    ],
)
def test_report_rejects_derived_field_mismatches(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        report(**overrides)
