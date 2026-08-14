from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import StringConstraints, field_validator, model_validator

from polytrading.domain.models import Asset, StrictRecord, Venue, normalize_utc_timestamp

TRIAL_FUNDING_PROTOCOL_VERSION = "lighter-dydx-prospective-funding-v1"
TRIAL_FUNDING_POINT_IN_TIME_LAG = timedelta(minutes=5)
TRIAL_FUNDING_WARNINGS = (
    "Research only: this cycle measures prospective public funding evidence, not returns.",
    "Readiness does not authorize paper or live trading.",
    "No credentials, accounts, balances, positions, orders, fills, or transfers were accessed.",
)
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

_EXPECTED_VENUES = (Venue.DYDX, Venue.LIGHTER)
_EXPECTED_SYMBOLS = {
    Venue.DYDX: {asset: f"{asset.value}-USD" for asset in Asset},
    Venue.LIGHTER: {asset: asset.value for asset in Asset},
}


class TrialInstrumentOutcome(StrEnum):
    CAPTURED = "captured"
    FAILED = "failed"
    LATE_NOT_COLLECTED = "late_not_collected"


class TrialFundingOutcome(StrEnum):
    CAPTURED = "captured"
    MISSING_EXPECTED = "missing_expected"
    FAILED = "failed"
    LATE_NOT_COLLECTED = "late_not_collected"


class TrialFundingCycleStatus(StrEnum):
    COMPLETE = "complete"
    DEGRADED = "degraded"
    LATE = "late"


def validate_trial_cycle_timing(
    cycle_end: datetime, now: datetime
) -> tuple[datetime, datetime, bool]:
    normalized_cycle_end = normalize_utc_timestamp(cycle_end)
    normalized_now = normalize_utc_timestamp(now)
    if any(
        (
            normalized_cycle_end.minute,
            normalized_cycle_end.second,
            normalized_cycle_end.microsecond,
        )
    ):
        raise ValueError("cycle end must align to a whole UTC hour")
    if normalized_now < normalized_cycle_end:
        raise ValueError("collection clock precedes cycle end")
    return (
        normalized_cycle_end,
        normalized_now,
        normalized_now > normalized_cycle_end + TRIAL_FUNDING_POINT_IN_TIME_LAG,
    )


def resolve_current_trial_cycle_end(now: datetime) -> datetime:
    normalized_now = normalize_utc_timestamp(now)
    return normalized_now.replace(minute=0, second=0, microsecond=0)


class LighterDydxFundingItem(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    asset: Asset
    symbol: str
    instrument_outcome: TrialInstrumentOutcome
    funding_outcome: TrialFundingOutcome
    instrument_observed_at: datetime | None
    funding_effective_at: datetime | None
    funding_observed_at: datetime | None
    instrument_source_hashes: tuple[Sha256, ...]
    funding_source_hashes: tuple[Sha256, ...]
    reason_codes: tuple[str, ...]

    @field_validator("instrument_observed_at", "funding_effective_at", "funding_observed_at")
    @classmethod
    def require_utc_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @field_validator("instrument_source_hashes", "funding_source_hashes")
    @classmethod
    def require_canonical_source_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("source hashes must be sorted and unique")
        return value

    @field_validator("reason_codes")
    @classmethod
    def require_canonical_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("reason codes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_consistent_component_evidence(self) -> "LighterDydxFundingItem":
        if self.symbol != _EXPECTED_SYMBOLS[self.venue][self.asset]:
            raise ValueError("symbol does not match venue and asset")

        instrument_captured = self.instrument_outcome is TrialInstrumentOutcome.CAPTURED
        if instrument_captured != (self.instrument_observed_at is not None):
            raise ValueError("instrument outcome timestamps are inconsistent")
        if instrument_captured != bool(self.instrument_source_hashes):
            raise ValueError("instrument source hashes do not match outcome")

        funding_captured = self.funding_outcome is TrialFundingOutcome.CAPTURED
        funding_responded = self.funding_outcome in (
            TrialFundingOutcome.CAPTURED,
            TrialFundingOutcome.MISSING_EXPECTED,
        )
        if funding_captured != (self.funding_effective_at is not None) or funding_responded != (
            self.funding_observed_at is not None
        ):
            raise ValueError("funding outcome timestamps are inconsistent")
        if funding_responded != bool(self.funding_source_hashes):
            raise ValueError("funding source hashes do not match outcome")
        if (
            self.funding_effective_at is not None
            and self.funding_observed_at is not None
            and self.funding_observed_at < self.funding_effective_at
        ):
            raise ValueError("funding observation must not precede effective time")

        self._validate_reason_codes()
        return self

    def _validate_reason_codes(self) -> None:
        expected_exact: set[str] = set()
        if self.instrument_outcome is TrialInstrumentOutcome.LATE_NOT_COLLECTED:
            expected_exact.add("COLLECTION_WINDOW_MISSED")
        if self.funding_outcome is TrialFundingOutcome.MISSING_EXPECTED:
            expected_exact.add("FUNDING_MISSING_EXPECTED")
        elif self.funding_outcome is TrialFundingOutcome.LATE_NOT_COLLECTED:
            expected_exact.add("COLLECTION_WINDOW_MISSED")

        instrument_failures = tuple(
            code
            for code in self.reason_codes
            if code.startswith(f"INSTRUMENT_FAILED:{self.venue.value}:")
        )
        funding_failures = tuple(
            code
            for code in self.reason_codes
            if code.startswith(f"FUNDING_FAILED:{self.venue.value}:{self.asset.value}:")
        )
        if (self.instrument_outcome is TrialInstrumentOutcome.FAILED) != (
            len(instrument_failures) == 1
        ) or (self.funding_outcome is TrialFundingOutcome.FAILED) != (len(funding_failures) == 1):
            raise ValueError("reason codes do not match component outcomes")
        if any(not code.rsplit(":", maxsplit=1)[-1].isidentifier() for code in instrument_failures):
            raise ValueError("reason codes do not match component outcomes")
        if any(not code.rsplit(":", maxsplit=1)[-1].isidentifier() for code in funding_failures):
            raise ValueError("reason codes do not match component outcomes")
        recognized = expected_exact | set(instrument_failures) | set(funding_failures)
        if set(self.reason_codes) != recognized:
            raise ValueError("reason codes do not match component outcomes")


class LighterDydxFundingCycle(StrictRecord):
    schema_version: Literal[1]
    protocol_version: Literal["lighter-dydx-prospective-funding-v1"]
    cycle_id: UUID
    cycle_end: datetime
    assets: tuple[Asset, ...]
    venues: tuple[Venue, Venue]
    request_started_at: datetime
    request_completed_at: datetime
    items: tuple[LighterDydxFundingItem, ...]
    status: TrialFundingCycleStatus
    source_hashes: tuple[Sha256, ...]
    warnings: tuple[str, str, str]

    @field_validator("cycle_end", "request_started_at", "request_completed_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("assets")
    @classmethod
    def require_canonical_assets(cls, value: tuple[Asset, ...]) -> tuple[Asset, ...]:
        if not value or tuple(sorted(set(value), key=lambda asset: asset.value)) != value:
            raise ValueError("assets must be ordered and unique")
        return value

    @field_validator("venues")
    @classmethod
    def require_expected_venues(cls, value: tuple[Venue, Venue]) -> tuple[Venue, Venue]:
        if value != _EXPECTED_VENUES:
            raise ValueError("venues must be dYdX followed by Lighter")
        return value

    @field_validator("items")
    @classmethod
    def require_canonical_items(
        cls, value: tuple[LighterDydxFundingItem, ...]
    ) -> tuple[LighterDydxFundingItem, ...]:
        pairs = tuple((item.venue.value, item.asset.value) for item in value)
        if tuple(sorted(set(pairs))) != pairs:
            raise ValueError("items must be ordered by venue and asset")
        return value

    @field_validator("source_hashes")
    @classmethod
    def require_canonical_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("source hashes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_consistent_cycle(self) -> "LighterDydxFundingCycle":
        _, _, invocation_started_late = validate_trial_cycle_timing(
            self.cycle_end, self.request_started_at
        )
        if self.request_completed_at < self.request_started_at:
            raise ValueError("request completion must not precede request start")

        all_components_missed = all(
            item.instrument_outcome is TrialInstrumentOutcome.LATE_NOT_COLLECTED
            and item.funding_outcome is TrialFundingOutcome.LATE_NOT_COLLECTED
            for item in self.items
        )
        any_component_missed = any(
            item.instrument_outcome is TrialInstrumentOutcome.LATE_NOT_COLLECTED
            or item.funding_outcome is TrialFundingOutcome.LATE_NOT_COLLECTED
            for item in self.items
        )
        if (invocation_started_late and not all_components_missed) or (
            not invocation_started_late and any_component_missed
        ):
            raise ValueError("late invocation requires every component missed")

        expected_pairs = tuple((venue, asset) for venue in self.venues for asset in self.assets)
        actual_pairs = tuple((item.venue, item.asset) for item in self.items)
        if actual_pairs != expected_pairs:
            raise ValueError("items must cover every requested venue and asset")

        cutoff = self.cycle_end + TRIAL_FUNDING_POINT_IN_TIME_LAG
        for item in self.items:
            if (
                item.funding_effective_at is not None
                and item.funding_effective_at != self.cycle_end
            ):
                raise ValueError("funding effective time must equal cycle end")
            for observed_at in (item.instrument_observed_at, item.funding_observed_at):
                if observed_at is not None and observed_at < self.cycle_end:
                    raise ValueError("item observation must not precede cycle end")
                if observed_at is not None and observed_at < self.request_started_at:
                    raise ValueError("item observation must not precede request start")
                if observed_at is not None and observed_at > self.request_completed_at:
                    raise ValueError("item observation must not follow request completion")

        expected_hashes = tuple(
            sorted(
                {
                    source_hash
                    for item in self.items
                    for source_hash in (
                        *item.instrument_source_hashes,
                        *item.funding_source_hashes,
                    )
                }
            )
        )
        if self.source_hashes != expected_hashes:
            raise ValueError("cycle source hashes must equal item source hashes")
        if self.warnings != TRIAL_FUNDING_WARNINGS:
            raise ValueError("cycle must contain the exact research warnings")

        has_late_component = invocation_started_late or any(
            observed_at is not None and observed_at > cutoff
            for item in self.items
            for observed_at in (item.instrument_observed_at, item.funding_observed_at)
        )
        has_degraded_component = any(
            item.instrument_outcome is TrialInstrumentOutcome.FAILED
            or item.funding_outcome
            in (TrialFundingOutcome.MISSING_EXPECTED, TrialFundingOutcome.FAILED)
            for item in self.items
        )
        required_status = (
            TrialFundingCycleStatus.LATE
            if has_late_component
            else TrialFundingCycleStatus.DEGRADED
            if has_degraded_component
            else TrialFundingCycleStatus.COMPLETE
        )
        if self.status is not required_status:
            raise ValueError("cycle status does not match item evidence")
        return self
