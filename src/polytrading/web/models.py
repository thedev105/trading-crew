from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from polytrading.carry.audit import AuditStatus
from polytrading.carry.discovery_models import VenueDiscoveryReport
from polytrading.carry.dossier_models import ContractDossierReport
from polytrading.carry.economics_models import (
    EconomicsDecision,
    FundingDirection,
)
from polytrading.domain.models import Asset, StrictRecord, Venue, normalize_utc_timestamp
from polytrading.trial.health_models import LighterDydxTrialHealthReport
from polytrading.venues.funding_cycle_models import FundingCycleStatus
from polytrading.venues.funding_health_models import FundingCollectionHealthReport

RESEARCH_WARNING = "Research only — no trading authority."
NonnegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

_VENUES = (Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX, Venue.LIGHTER)
_ASSETS = (Asset.BTC, Asset.ETH, Asset.SOL)
_EXPECTED_MARKETS = tuple((venue, asset) for venue in _VENUES for asset in _ASSETS)


def _symbol(venue: Venue, asset: Asset) -> str:
    if venue is Venue.BYBIT:
        return f"{asset.value}USDT"
    if venue is Venue.DYDX:
        return f"{asset.value}-USD"
    return asset.value


class MarketEvidenceRow(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    asset: Asset
    symbol: str
    instrument_observed_at: datetime | None
    funding_rate: Decimal | None
    funding_interval_hours: Decimal | None
    funding_effective_at: datetime | None
    funding_observed_at: datetime | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread_bps: NonnegativeDecimal | None
    book_effective_at: datetime | None
    book_observed_at: datetime | None

    @field_validator(
        "instrument_observed_at",
        "funding_effective_at",
        "funding_observed_at",
        "book_effective_at",
        "book_observed_at",
    )
    @classmethod
    def normalize_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @field_validator("funding_interval_hours")
    @classmethod
    def require_positive_funding_interval(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("funding interval must be positive")
        return value

    @field_validator("best_bid", "best_ask")
    @classmethod
    def require_positive_book_prices(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("book prices must be positive")
        return value

    @model_validator(mode="after")
    def require_coherent_evidence(self) -> MarketEvidenceRow:
        if self.symbol != _symbol(self.venue, self.asset):
            raise ValueError("symbol must match venue and asset")

        funding_fields = (
            self.funding_rate,
            self.funding_interval_hours,
            self.funding_effective_at,
            self.funding_observed_at,
        )
        if not (
            all(value is None for value in funding_fields)
            or all(value is not None for value in funding_fields)
        ):
            raise ValueError("funding fields must be present together")

        book_fields = (
            self.best_bid,
            self.best_ask,
            self.spread_bps,
            self.book_effective_at,
            self.book_observed_at,
        )
        if not (
            all(value is None for value in book_fields)
            or all(value is not None for value in book_fields)
        ):
            raise ValueError("book fields must be present together")
        if (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid >= self.best_ask
        ):
            raise ValueError("best bid must be less than best ask")
        return self


class CarryEvidenceRow(StrictRecord):
    schema_version: Literal[1]
    asset: Asset
    status: AuditStatus
    funding_ready: bool
    book_ready: bool
    hourly_spread: Decimal | None
    reason_codes: tuple[str, ...]

    @field_validator("reason_codes")
    @classmethod
    def require_canonical_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("reason codes must be sorted and unique")
        return value


class EconomicsSummaryRow(StrictRecord):
    schema_version: Literal[1]
    asset: Asset
    report_available: bool
    decision: EconomicsDecision | None
    direction: FundingDirection | None
    primary_reason_code: str | None
    assigned_capital_usd: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None
    conservative_7d_net_usd: Decimal | None
    conservative_14d_net_usd: Decimal | None
    conservative_28d_net_usd: Decimal | None
    known_as_of: datetime | None
    evaluated_at: datetime | None
    stress_pass: bool | None

    @field_validator("known_as_of", "evaluated_at")
    @classmethod
    def normalize_optional_timestamp(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @field_validator("primary_reason_code")
    @classmethod
    def require_nonblank_reason(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("primary reason code must not be blank")
        return value

    @model_validator(mode="after")
    def require_coherent_availability(self) -> EconomicsSummaryRow:
        report_fields = (
            self.decision,
            self.direction,
            self.primary_reason_code,
            self.assigned_capital_usd,
            self.conservative_7d_net_usd,
            self.conservative_14d_net_usd,
            self.conservative_28d_net_usd,
            self.known_as_of,
            self.evaluated_at,
            self.stress_pass,
        )
        if not self.report_available:
            if any(value is not None for value in report_fields):
                raise ValueError("unavailable economics row must withhold report fields")
            return self
        if self.decision is None or self.known_as_of is None or self.evaluated_at is None:
            raise ValueError("available economics row requires decision and timestamps")
        if self.evaluated_at < self.known_as_of:
            raise ValueError("economics evaluation time must not precede evidence cutoff")
        complete_fields = (
            self.assigned_capital_usd,
            self.conservative_7d_net_usd,
            self.conservative_14d_net_usd,
            self.conservative_28d_net_usd,
            self.stress_pass,
        )
        has_complete = all(value is not None for value in complete_fields)
        has_partial = any(value is not None for value in complete_fields)
        if has_partial and not has_complete:
            raise ValueError("complete economics fields must be present together")
        if has_complete and self.direction is None:
            raise ValueError("complete economics fields require a direction")
        if self.decision is EconomicsDecision.SHADOW_CANDIDATE:
            if not has_complete or self.primary_reason_code is not None:
                raise ValueError("shadow summary requires complete fields and no reason")
        elif self.primary_reason_code is None:
            raise ValueError("non-shadow summary requires a primary reason")
        if self.decision is EconomicsDecision.INSUFFICIENT_EVIDENCE and (
            self.direction is not None or has_complete
        ):
            raise ValueError("insufficient summary must withhold direction and economics")
        return self


class FundingCycleSummary(StrictRecord):
    schema_version: Literal[1]
    cycle_id: UUID
    cycle_end: datetime
    request_completed_at: datetime
    status: FundingCycleStatus

    @field_validator("cycle_end", "request_completed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)


class BookCycleSummary(StrictRecord):
    schema_version: Literal[1]
    cycle_id: UUID
    request_completed_at: datetime
    status: Literal["complete", "failed", "skew_exceeds_research_target"]
    max_effective_skew_ms: NonnegativeDecimal

    @field_validator("request_completed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)


class EvidenceCounts(StrictRecord):
    raw_envelopes: Annotated[int, Field(ge=0)]
    instrument_specs: Annotated[int, Field(ge=0)]
    funding_observations: Annotated[int, Field(ge=0)]
    market_snapshots: Annotated[int, Field(ge=0)]
    book_snapshots: Annotated[int, Field(ge=0)]
    book_collection_cycles: Annotated[int, Field(ge=0)]
    funding_collection_cycles: Annotated[int, Field(ge=0)]
    lighter_dydx_funding_cycles: Annotated[int, Field(ge=0)]


class OperationRecipes(StrictRecord):
    collect_public: str
    collect_books_once: str
    collect_current_funding: str
    inspect_funding_health: str
    collect_trial_funding: str
    collect_trial_books_burst: str
    collect_trial_books_once: str
    inspect_trial_health: str
    import_trial_fees: str
    evaluate_trial_btc: str
    trial_scheduler_example: str

    @field_validator("*")
    @classmethod
    def require_nonblank_recipe(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("operation recipe must not be blank")
        return value


class DashboardSnapshot(StrictRecord):
    schema_version: Literal[1]
    as_of: datetime
    database_name: str
    warning: Literal["Research only — no trading authority."]
    funding_health: FundingCollectionHealthReport
    trial_health: LighterDydxTrialHealthReport
    latest_funding_cycle: FundingCycleSummary | None
    latest_book_cycle: BookCycleSummary | None
    compatibility_dossier: ContractDossierReport | None
    venue_discovery: VenueDiscoveryReport | None
    markets: tuple[MarketEvidenceRow, ...]
    carry_rows: tuple[CarryEvidenceRow, ...]
    economics_rows: tuple[EconomicsSummaryRow, ...]
    evidence_counts: EvidenceCounts
    operation_recipes: OperationRecipes

    @field_validator("as_of")
    @classmethod
    def normalize_as_of(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("database_name")
    @classmethod
    def require_basename(cls, value: str) -> str:
        if not value or value in {".", ".."} or Path(value).name != value:
            raise ValueError("database name must be a nonblank basename")
        return value

    @field_validator("markets")
    @classmethod
    def require_canonical_markets(
        cls, value: tuple[MarketEvidenceRow, ...]
    ) -> tuple[MarketEvidenceRow, ...]:
        if tuple((row.venue, row.asset) for row in value) != _EXPECTED_MARKETS:
            raise ValueError("markets must cover every venue and asset in canonical order")
        return value

    @field_validator("carry_rows")
    @classmethod
    def require_canonical_carry(
        cls, value: tuple[CarryEvidenceRow, ...]
    ) -> tuple[CarryEvidenceRow, ...]:
        if tuple(row.asset for row in value) != _ASSETS:
            raise ValueError("carry rows must cover BTC, ETH, and SOL in canonical order")
        return value

    @field_validator("economics_rows")
    @classmethod
    def require_canonical_economics(
        cls, value: tuple[EconomicsSummaryRow, ...]
    ) -> tuple[EconomicsSummaryRow, ...]:
        if tuple(row.asset for row in value) != _ASSETS:
            raise ValueError("economics rows must cover BTC, ETH, and SOL in canonical order")
        return value

    @model_validator(mode="after")
    def require_one_point_in_time(self) -> DashboardSnapshot:
        if self.funding_health.as_of != self.as_of:
            raise ValueError("funding health must use dashboard as-of")
        if self.trial_health.as_of != self.as_of:
            raise ValueError("trial health must use dashboard as-of")
        trial_timestamps = (
            self.trial_health.latest_auditable_boundary,
            self.trial_health.trial_started_at,
            *(item.cycle_end for item in self.trial_health.recent_boundaries),
            *(item.latest_funding_boundary for item in self.trial_health.assets),
            *(item.latest_book_completed_at for item in self.trial_health.assets),
            *(item.effective_from for item in self.trial_health.reviewed_fees),
            *(item.observed_at for item in self.trial_health.reviewed_fees),
        )
        if any(timestamp is not None and timestamp > self.as_of for timestamp in trial_timestamps):
            raise ValueError("trial evidence must not follow dashboard as-of")
        if (
            self.latest_funding_cycle is not None
            and self.latest_funding_cycle.request_completed_at > self.as_of
        ):
            raise ValueError("funding cycle must not follow dashboard as-of")
        if (
            self.latest_book_cycle is not None
            and self.latest_book_cycle.request_completed_at > self.as_of
        ):
            raise ValueError("book cycle must not follow dashboard as-of")
        if (
            self.compatibility_dossier is not None
            and self.compatibility_dossier.observed_at > self.as_of
        ):
            raise ValueError("dossier must not follow dashboard as-of")
        if self.venue_discovery is not None and self.venue_discovery.observed_at > self.as_of:
            raise ValueError("discovery must not follow dashboard as-of")
        if self.compatibility_dossier is not None and self.venue_discovery is not None:
            legacy_candidate = next(
                (
                    candidate
                    for candidate in self.venue_discovery.candidates
                    if candidate.dossier_id == "hyperliquid-dydx-core-v1"
                ),
                None,
            )
            if legacy_candidate != self.compatibility_dossier:
                raise ValueError("legacy dossier must match discovery candidate")
        timestamps = (
            timestamp
            for row in self.markets
            for timestamp in (
                row.instrument_observed_at,
                row.funding_effective_at,
                row.funding_observed_at,
                row.book_effective_at,
                row.book_observed_at,
            )
            if timestamp is not None
        )
        if any(timestamp > self.as_of for timestamp in timestamps):
            raise ValueError("market evidence must not follow dashboard as-of")
        if any(
            timestamp is not None and timestamp > self.as_of
            for row in self.economics_rows
            for timestamp in (row.known_as_of, row.evaluated_at)
        ):
            raise ValueError("economics report must not follow dashboard as-of")
        return self
