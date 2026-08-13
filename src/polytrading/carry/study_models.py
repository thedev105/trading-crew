from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from polytrading.domain.models import StrictRecord, normalize_utc_timestamp


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
