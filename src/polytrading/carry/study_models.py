from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.domain.models import Asset, StrictRecord, normalize_utc_timestamp

PROTOCOL_VERSION = "hl-bybit-funding-persistence-v1"
OMITTED_COSTS = (
    "basis_pnl",
    "collateral_effects",
    "failure_reserve",
    "fees",
    "financing",
    "slippage",
    "taxes",
)
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AvailabilityClass(StrEnum):
    POINT_IN_TIME = "point_in_time"
    HISTORICAL_RECONSTRUCTION = "historical_reconstruction"
    INSUFFICIENT_DATA = "insufficient_data"


class StudyDecision(StrEnum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    REPLICATION_FAILED = "REPLICATION_FAILED"
    FORWARD_TEST_REQUIRED = "FORWARD_TEST_REQUIRED"
    NET_FORWARD_GATE_REQUIRED = "NET_FORWARD_GATE_REQUIRED"


class IncompleteBlock(StrictRecord):
    schema_version: Literal[1]
    block_end: datetime
    reason_codes: Annotated[tuple[str, ...], Field(min_length=1)]

    @field_validator("block_end")
    @classmethod
    def require_utc_block_end(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("reason_codes")
    @classmethod
    def canonicalize_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("incomplete-block reasons must be sorted and unique")
        return value


class PairedFundingBlock(StrictRecord):
    schema_version: Literal[1]
    block_start: datetime
    block_end: datetime
    bybit_rate: Decimal
    hyperliquid_rate: Decimal
    spread: Decimal

    @field_validator("block_start", "block_end")
    @classmethod
    def require_utc_block_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_increasing_window_and_spread_identity(self) -> PairedFundingBlock:
        if self.block_end <= self.block_start:
            raise ValueError("funding block end must follow start")
        if self.spread != self.hyperliquid_rate - self.bybit_rate:
            raise ValueError("spread must equal hyperliquid rate minus bybit rate")
        return self


class CoverageSummary(StrictRecord):
    schema_version: Literal[1]
    requested_blocks: Annotated[int, Field(ge=1)]
    bybit_complete_blocks: Annotated[int, Field(ge=0)]
    hyperliquid_complete_blocks: Annotated[int, Field(ge=0)]
    paired_complete_blocks: Annotated[int, Field(ge=0)]
    coverage_ratio: Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))]
    first_paired_at: datetime | None
    last_paired_at: datetime | None
    incomplete_blocks: tuple[IncompleteBlock, ...]

    @field_validator("first_paired_at", "last_paired_at")
    @classmethod
    def require_utc_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_consistent_counts_and_ratio(self) -> CoverageSummary:
        counts = (
            self.bybit_complete_blocks,
            self.hyperliquid_complete_blocks,
            self.paired_complete_blocks,
        )
        if any(count > self.requested_blocks for count in counts):
            raise ValueError("complete block count cannot exceed requested blocks")
        if self.paired_complete_blocks > min(
            self.bybit_complete_blocks, self.hyperliquid_complete_blocks
        ):
            raise ValueError("paired blocks cannot exceed venue-complete blocks")
        expected_ratio = Decimal(self.paired_complete_blocks) / Decimal(self.requested_blocks)
        if self.coverage_ratio != expected_ratio:
            raise ValueError("coverage ratio does not match paired and requested blocks")
        paired_times_present = self.first_paired_at is not None and self.last_paired_at is not None
        if paired_times_present != (self.paired_complete_blocks > 0):
            raise ValueError("paired timestamps must match paired block count")
        if (
            self.first_paired_at is not None
            and self.last_paired_at is not None
            and self.last_paired_at < self.first_paired_at
        ):
            raise ValueError("last paired timestamp must not precede first")
        if len(self.incomplete_blocks) != self.requested_blocks - self.paired_complete_blocks:
            raise ValueError("incomplete block records must cover every unpaired block")
        block_ends = tuple(block.block_end for block in self.incomplete_blocks)
        if tuple(sorted(set(block_ends))) != block_ends:
            raise ValueError("incomplete blocks must be ordered and unique")
        return self


class DistributionSummary(StrictRecord):
    schema_version: Literal[1]
    count: Annotated[int, Field(ge=1)]
    mean: Decimal
    median: Decimal
    percentile_05: Decimal
    percentile_95: Decimal
    minimum: Decimal
    maximum: Decimal
    positive_fraction: Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))]
    zero_fraction: Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))]
    negative_fraction: Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))]

    @model_validator(mode="after")
    def require_consistent_distribution(self) -> DistributionSummary:
        if not (
            self.minimum <= self.percentile_05 <= self.median <= self.percentile_95 <= self.maximum
        ):
            raise ValueError("distribution order statistics are inconsistent")
        if self.positive_fraction + self.zero_fraction + self.negative_fraction != Decimal(1):
            raise ValueError("distribution sign fractions must sum to one")
        return self


class HoldingWindowSummary(StrictRecord):
    schema_version: Literal[1]
    holding_days: Literal[7, 14, 28]
    block_count: Annotated[int, Field(ge=1)]
    distribution: DistributionSummary

    @model_validator(mode="after")
    def require_expected_block_count(self) -> HoldingWindowSummary:
        if self.block_count != self.holding_days * 3:
            raise ValueError("holding block count must equal three blocks per day")
        return self


class MonthlyContribution(StrictRecord):
    schema_version: Literal[1]
    month: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}$")]
    gross_funding: Decimal


class StudyStatistics(StrictRecord):
    schema_version: Literal[1]
    block_distribution: DistributionSummary
    sign_persistence: Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))] | None
    sign_reversals: Annotated[int, Field(ge=0)]
    longest_adverse_run: Annotated[int, Field(ge=0)]
    cumulative_gross_funding: Decimal
    maximum_drawdown: Annotated[Decimal, Field(ge=Decimal(0))]
    gross_annualized_mean: Decimal
    monthly_contributions: Annotated[tuple[MonthlyContribution, ...], Field(min_length=1)]
    cumulative_without_best_month: Decimal
    holding_windows: Annotated[tuple[HoldingWindowSummary, ...], Field(min_length=3, max_length=3)]

    @field_validator("monthly_contributions")
    @classmethod
    def canonicalize_months(
        cls, value: tuple[MonthlyContribution, ...]
    ) -> tuple[MonthlyContribution, ...]:
        months = tuple(item.month for item in value)
        if tuple(sorted(set(months))) != months:
            raise ValueError("monthly contributions must be ordered and unique")
        return value

    @field_validator("holding_windows")
    @classmethod
    def require_frozen_holding_windows(
        cls, value: tuple[HoldingWindowSummary, ...]
    ) -> tuple[HoldingWindowSummary, ...]:
        if tuple(item.holding_days for item in value) != (7, 14, 28):
            raise ValueError("holding windows must be ordered as 7, 14, and 28 days")
        return value

    @model_validator(mode="after")
    def require_cumulative_identities(self) -> StudyStatistics:
        monthly_total = sum((item.gross_funding for item in self.monthly_contributions), Decimal(0))
        if monthly_total != self.cumulative_gross_funding:
            raise ValueError("monthly contributions must sum to cumulative gross funding")
        best_month = max(item.gross_funding for item in self.monthly_contributions)
        if self.cumulative_without_best_month != self.cumulative_gross_funding - best_month:
            raise ValueError("best-month exclusion does not match monthly contributions")
        return self


class CarryPersistenceReport(StrictRecord):
    schema_version: Literal[1]
    protocol_version: Literal["hl-bybit-funding-persistence-v1"]
    asset: Asset
    start: datetime
    end: datetime
    known_as_of: datetime
    availability: AvailabilityClass
    coverage: CoverageSummary
    statistics: StudyStatistics | None
    decision: StudyDecision
    decision_reasons: tuple[str, ...]
    source_hashes: tuple[Sha256, ...]
    economic_basis: Literal["gross_funding_only"]
    omitted_costs: tuple[str, ...]

    @field_validator("start", "end", "known_as_of")
    @classmethod
    def require_utc_request_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("decision_reasons")
    @classmethod
    def canonicalize_decision_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("decision reasons must be ordered and unique")
        return value

    @field_validator("source_hashes")
    @classmethod
    def canonicalize_source_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("source hashes must be ordered and unique")
        return value

    @model_validator(mode="after")
    def require_consistent_request_and_decision(self) -> CarryPersistenceReport:
        if self.start >= self.end:
            raise ValueError("report start must precede end")
        if self.known_as_of < self.end:
            raise ValueError("report knowledge cutoff must not precede end")
        is_insufficient = self.decision is StudyDecision.INSUFFICIENT_DATA
        if is_insufficient != (self.statistics is None):
            raise ValueError("only insufficient reports may withhold statistics")
        requires_reasons = self.decision in (
            StudyDecision.INSUFFICIENT_DATA,
            StudyDecision.REPLICATION_FAILED,
        )
        if requires_reasons != bool(self.decision_reasons):
            raise ValueError("decision reasons must match the research decision")
        if self.omitted_costs != OMITTED_COSTS:
            raise ValueError("gross report must disclose the exact omitted costs")
        return self
