from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.domain.models import (
    Asset,
    FeeSchedule,
    StrictRecord,
    Venue,
    normalize_utc_timestamp,
)

PROTOCOL_VERSION = "lighter-dydx-shadow-economics-v1"
RESEARCH_WARNING = (
    "Research only — shadow candidate, not a fill, recommendation, or trading authorization."
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonnegativeDecimal = Annotated[Decimal, Field(ge=Decimal(0), allow_inf_nan=False)]
PositiveDecimal = Annotated[Decimal, Field(gt=Decimal(0), allow_inf_nan=False)]
Fraction = Annotated[
    Decimal,
    Field(ge=Decimal(0), le=Decimal(1), allow_inf_nan=False),
]
PositiveFraction = Annotated[
    Decimal,
    Field(gt=Decimal(0), le=Decimal(1), allow_inf_nan=False),
]
NonnegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(ge=1)]

_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]*")
_ECONOMICS_VENUES = (Venue.DYDX, Venue.LIGHTER)


class EconomicsDecision(StrEnum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    REJECTED = "REJECTED"
    SHADOW_CANDIDATE = "SHADOW_CANDIDATE"


class FundingDirection(StrEnum):
    SHORT_LIGHTER_LONG_DYDX = "short_lighter_long_dydx"
    SHORT_DYDX_LONG_LIGHTER = "short_dydx_long_lighter"


def _require_nonblank(value: str, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must not be blank")
    return value


def require_machine_reason_codes(value: tuple[str, ...]) -> tuple[str, ...]:
    if any(_REASON_CODE.fullmatch(code) is None for code in value):
        raise ValueError("reason code must be an uppercase machine identifier")
    return value


def _require_source_url(value: str, venue: Venue | None = None) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("source URL is invalid") from error
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        raise ValueError("source URL must use HTTPS without user information")
    if port is not None:
        raise ValueError("source URL must not specify a port")
    if venue is None:
        if parsed.hostname is None:
            raise ValueError("source URL must contain a host")
        return value
    expected_host = {
        Venue.DYDX: "help.dydx.trade",
        Venue.LIGHTER: "docs.lighter.xyz",
    }.get(venue)
    if expected_host is None or parsed.hostname != expected_host:
        raise ValueError("source URL must be an official source for venue")
    return value


class VenueExecutionAssumption(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    fee_tier_name: str
    account_type: str
    taker_latency_ms: NonnegativeDecimal
    observed_at: datetime
    source_url: str
    source_hash: Sha256

    @field_validator("fee_tier_name", "account_type")
    @classmethod
    def require_labels(cls, value: str, info: object) -> str:
        label = getattr(info, "field_name", "label").replace("_", " ")
        return _require_nonblank(value, label)

    @model_validator(mode="after")
    def require_supported_official_source(self) -> VenueExecutionAssumption:
        if self.venue not in _ECONOMICS_VENUES:
            raise ValueError("execution assumption venue must be dYdX or Lighter")
        _require_source_url(self.source_url, self.venue)
        return self


class VenueMarginAssumption(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    asset: Asset
    initial_margin_fraction: PositiveFraction
    maintenance_margin_fraction: PositiveFraction
    close_out_margin_fraction: PositiveFraction
    liquidation_penalty_fraction: Fraction
    observed_at: datetime
    source_url: str
    source_hash: Sha256

    @model_validator(mode="after")
    def require_supported_ordered_margin(self) -> VenueMarginAssumption:
        if self.venue not in _ECONOMICS_VENUES:
            raise ValueError("margin assumption venue must be dYdX or Lighter")
        if not (
            self.close_out_margin_fraction
            <= self.maintenance_margin_fraction
            <= self.initial_margin_fraction
        ):
            raise ValueError("margin fractions must satisfy close-out <= maintenance <= initial")
        _require_source_url(self.source_url, self.venue)
        return self


class EconomicsPolicy(StrictRecord):
    schema_version: Literal[1]
    protocol_version: Literal["lighter-dydx-shadow-economics-v1"]
    asset: Asset
    study_end: datetime
    known_as_of: datetime
    account_equity_usd: Annotated[
        Decimal,
        Field(ge=Decimal("3000"), le=Decimal("10000"), allow_inf_nan=False),
    ]
    cash_benchmark_annual_rate: Fraction
    operational_cost_usd: NonnegativeDecimal
    prefunded: bool
    operational_source_url: str
    operational_source_hash: Sha256
    execution_assumptions: tuple[VenueExecutionAssumption, VenueExecutionAssumption]
    margin_assumptions: tuple[VenueMarginAssumption, VenueMarginAssumption]
    training_days: int
    evaluation_days: int
    minimum_coverage: Fraction
    maximum_book_age_seconds: NonnegativeDecimal
    maximum_cycle_skew_ms: NonnegativeDecimal
    maximum_hourly_book_age_seconds: NonnegativeDecimal
    maximum_assigned_equity_fraction: Fraction
    maximum_assigned_usd: NonnegativeDecimal
    incomplete_leg_shock: Fraction
    maximum_incomplete_loss_equity_fraction: Fraction
    minimum_hold_return: Fraction
    minimum_profit_usd: NonnegativeDecimal
    minimum_annualized_return: Fraction
    cash_benchmark_spread: Fraction
    maximum_stress_loss_equity_fraction: Fraction
    maximum_drawdown_fraction: Fraction
    forced_exit_depth_multiplier: NonnegativeDecimal
    doubled_cost_multiplier: NonnegativeDecimal
    minimum_normal_quote_observations: int
    minimum_stress_quote_observations: int

    @field_validator("study_end", "known_as_of")
    @classmethod
    def require_policy_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("operational_source_url")
    @classmethod
    def require_operational_source(cls, value: str) -> str:
        return _require_source_url(value)

    @model_validator(mode="after")
    def require_frozen_protocol(self) -> EconomicsPolicy:
        frozen: tuple[tuple[str, object], ...] = (
            ("training_days", 30),
            ("evaluation_days", 60),
            ("minimum_coverage", Decimal("0.99")),
            ("maximum_book_age_seconds", Decimal("30")),
            ("maximum_cycle_skew_ms", Decimal("1000")),
            ("maximum_hourly_book_age_seconds", Decimal("300")),
            ("maximum_assigned_equity_fraction", Decimal("0.10")),
            ("maximum_assigned_usd", Decimal("500")),
            ("incomplete_leg_shock", Decimal("0.10")),
            ("maximum_incomplete_loss_equity_fraction", Decimal("0.0025")),
            ("minimum_hold_return", Decimal("0.003")),
            ("minimum_profit_usd", Decimal("3")),
            ("minimum_annualized_return", Decimal("0.12")),
            ("cash_benchmark_spread", Decimal("0.05")),
            ("maximum_stress_loss_equity_fraction", Decimal("0.0025")),
            ("maximum_drawdown_fraction", Decimal("0.08")),
            ("forced_exit_depth_multiplier", Decimal("2")),
            ("doubled_cost_multiplier", Decimal("2")),
            ("minimum_normal_quote_observations", 25),
            ("minimum_stress_quote_observations", 10),
        )
        for field_name, expected in frozen:
            if getattr(self, field_name) != expected:
                raise ValueError(f"{field_name.replace('_', ' ')} is frozen by protocol")
        if self.study_end.minute or self.study_end.second or self.study_end.microsecond:
            raise ValueError("study end must be aligned to a whole UTC hour")
        if self.study_end > self.known_as_of:
            raise ValueError("study end must not follow knowledge cutoff")
        if self.known_as_of - self.study_end > timedelta(minutes=65):
            raise ValueError("study end must be no more than 65 minutes before knowledge cutoff")
        if tuple(item.venue for item in self.execution_assumptions) != _ECONOMICS_VENUES:
            raise ValueError("execution assumptions must use canonical dYdX/Lighter order")
        if tuple(item.venue for item in self.margin_assumptions) != _ECONOMICS_VENUES:
            raise ValueError("margin assumptions must use canonical dYdX/Lighter order")
        if any(item.asset is not self.asset for item in self.margin_assumptions):
            raise ValueError("margin assumption asset must match policy asset")
        assumptions = (*self.execution_assumptions, *self.margin_assumptions)
        if any(item.observed_at > self.known_as_of for item in assumptions):
            raise ValueError("policy assumption must be observed by knowledge cutoff")
        if self.operational_cost_usd == 0 and not self.prefunded:
            raise ValueError("zero operational cost requires prefunded venues")
        return self


class EvidenceCoverage(StrictRecord):
    schema_version: Literal[1]
    requested_training_hours: PositiveInt
    paired_training_hours: NonnegativeInt
    training_funding_coverage: Fraction
    requested_evaluation_hours: PositiveInt
    paired_evaluation_hours: NonnegativeInt
    evaluation_funding_coverage: Fraction
    requested_funding_hours: PositiveInt
    paired_funding_hours: NonnegativeInt
    funding_coverage: Fraction
    requested_book_hours: PositiveInt
    paired_book_hours: NonnegativeInt
    book_coverage: Fraction
    latest_book_age_seconds: NonnegativeDecimal | None
    latest_pair_skew_ms: NonnegativeDecimal | None
    latency_sample_count: NonnegativeInt

    @model_validator(mode="after")
    def require_exact_coverage_identities(self) -> EvidenceCoverage:
        pairs = (
            (self.paired_training_hours, self.requested_training_hours),
            (self.paired_evaluation_hours, self.requested_evaluation_hours),
            (self.paired_funding_hours, self.requested_funding_hours),
            (self.paired_book_hours, self.requested_book_hours),
        )
        if any(paired > requested for paired, requested in pairs):
            raise ValueError("paired hours cannot exceed requested hours")
        if self.requested_funding_hours != (
            self.requested_training_hours + self.requested_evaluation_hours
        ):
            raise ValueError("funding requested hours must equal training plus evaluation")
        if self.paired_funding_hours != (self.paired_training_hours + self.paired_evaluation_hours):
            raise ValueError("paired funding hours must equal training plus evaluation")
        if self.requested_book_hours != self.requested_evaluation_hours:
            raise ValueError("book requested hours must equal evaluation hours")
        expected = (
            Decimal(self.paired_training_hours) / Decimal(self.requested_training_hours),
            Decimal(self.paired_evaluation_hours) / Decimal(self.requested_evaluation_hours),
            Decimal(self.paired_funding_hours) / Decimal(self.requested_funding_hours),
            Decimal(self.paired_book_hours) / Decimal(self.requested_book_hours),
        )
        actual = (
            self.training_funding_coverage,
            self.evaluation_funding_coverage,
            self.funding_coverage,
            self.book_coverage,
        )
        labels = ("training funding", "evaluation funding", "funding", "book")
        for label, actual_ratio, expected_ratio in zip(labels, actual, expected, strict=True):
            if actual_ratio != expected_ratio:
                raise ValueError(f"{label} coverage must equal paired divided by requested hours")
        return self


class EconomicsCostBreakdown(StrictRecord):
    schema_version: Literal[1]
    entry_slippage_usd: NonnegativeDecimal
    forced_exit_cost_usd: NonnegativeDecimal
    taker_fee_cost_usd: NonnegativeDecimal
    operational_cost_usd: NonnegativeDecimal
    latency_reserve_usd: NonnegativeDecimal
    normal_cost_usd: NonnegativeDecimal
    doubled_transaction_cost_usd: NonnegativeDecimal

    @model_validator(mode="after")
    def require_cost_identities(self) -> EconomicsCostBreakdown:
        transaction_cost = (
            self.entry_slippage_usd
            + self.forced_exit_cost_usd
            + self.taker_fee_cost_usd
            + self.operational_cost_usd
        )
        if self.normal_cost_usd != transaction_cost + self.latency_reserve_usd:
            raise ValueError("normal cost identity is inconsistent")
        if self.doubled_transaction_cost_usd != (
            transaction_cost * Decimal(2) + self.latency_reserve_usd
        ):
            raise ValueError("doubled transaction cost identity is inconsistent")
        return self


class HorizonEconomics(StrictRecord):
    schema_version: Literal[2]
    holding_days: Literal[7, 14, 28]
    conservative_funding_rate: Decimal
    lighter_funding_rate_sum: Decimal
    dydx_funding_rate_sum: Decimal
    lighter_funding_usd: Decimal
    dydx_funding_usd: Decimal
    gross_funding_usd: Decimal
    funding_reversal_reserve_usd: NonnegativeDecimal
    basis_divergence_rate: NonnegativeDecimal
    basis_divergence_reserve_usd: NonnegativeDecimal
    conservative_net_usd: Decimal
    assigned_capital_return: Decimal
    account_return: Decimal
    annualized_conservative_return: Decimal
    net_positive: bool
    minimum_profit_pass: bool
    annualized_return_pass: bool

    @model_validator(mode="after")
    def require_intrinsic_horizon_identities(self) -> HorizonEconomics:
        if self.gross_funding_usd != self.lighter_funding_usd + self.dydx_funding_usd:
            raise ValueError("gross funding identity must equal both venue funding components")
        expected_annualized = (
            self.assigned_capital_return * Decimal(365) / Decimal(self.holding_days)
        )
        if self.annualized_conservative_return != expected_annualized:
            raise ValueError("annualized return must be simple assigned-capital annualization")
        if self.net_positive is not (self.conservative_net_usd > 0):
            raise ValueError("net-positive flag must match conservative net")
        return self


class CompleteEconomics(StrictRecord):
    schema_version: Literal[2]
    execution_assumptions: tuple[VenueExecutionAssumption, VenueExecutionAssumption]
    margin_assumptions: tuple[VenueMarginAssumption, VenueMarginAssumption]
    fee_schedules: tuple[FeeSchedule, FeeSchedule]
    base_quantity: PositiveDecimal
    lighter_entry_notional_usd: PositiveDecimal
    dydx_entry_notional_usd: PositiveDecimal
    assigned_capital_usd: PositiveDecimal
    account_equity_usd: Annotated[
        Decimal,
        Field(ge=Decimal("3000"), le=Decimal("10000"), allow_inf_nan=False),
    ]
    unused_cash_usd: NonnegativeDecimal
    cash_benchmark_annual_rate: Fraction
    minimum_profit_required_usd: NonnegativeDecimal
    required_annualized_return: NonnegativeDecimal
    prefunded: bool
    operational_source_url: str
    operational_source_hash: Sha256
    costs: EconomicsCostBreakdown
    horizons: tuple[HorizonEconomics, HorizonEconomics, HorizonEconomics]
    normal_quote_observations: NonnegativeInt
    stress_quote_observations: NonnegativeInt
    incomplete_leg_loss_usd: NonnegativeDecimal
    funding_and_forced_exit_loss_rate: NonnegativeDecimal
    modeled_drawdown_rate: NonnegativeDecimal
    modeled_liquidation: bool
    doubled_cost_28d_net_usd: Decimal
    doubled_cost_28d_pass: bool
    stress_loss_pass: bool
    drawdown_pass: bool
    liquidation_pass: bool
    quote_observations_pass: bool

    @model_validator(mode="after")
    def require_complete_identities(self) -> CompleteEconomics:
        _require_source_url(self.operational_source_url)
        if self.costs.operational_cost_usd == 0 and not self.prefunded:
            raise ValueError("zero operational cost requires prefunded venues")
        if tuple(item.venue for item in self.execution_assumptions) != _ECONOMICS_VENUES:
            raise ValueError("execution assumptions must use canonical dYdX/Lighter order")
        if tuple(item.venue for item in self.margin_assumptions) != _ECONOMICS_VENUES:
            raise ValueError("margin assumptions must use canonical dYdX/Lighter order")
        if tuple(item.venue for item in self.fee_schedules) != _ECONOMICS_VENUES:
            raise ValueError("fee schedules must use canonical dYdX/Lighter order")
        for assumption, fee in zip(self.execution_assumptions, self.fee_schedules, strict=True):
            if assumption.fee_tier_name != fee.tier_name:
                raise ValueError("fee schedule tier must match execution assumption")
            if fee.taker_rate < 0:
                raise ValueError("taker fee rate must be nonnegative")
            _require_source_url(fee.source_url, fee.venue)
        if self.assigned_capital_usd != (
            self.lighter_entry_notional_usd + self.dydx_entry_notional_usd
        ):
            raise ValueError("assigned capital must equal both absolute entry notionals")
        if self.unused_cash_usd != self.account_equity_usd - self.assigned_capital_usd:
            raise ValueError("unused cash must equal equity minus assigned capital")
        if self.assigned_capital_usd > min(
            self.account_equity_usd * Decimal("0.10"), Decimal("500")
        ):
            raise ValueError("assigned capital exceeds frozen equity or USD cap")
        if self.incomplete_leg_loss_usd > self.account_equity_usd * Decimal("0.0025"):
            raise ValueError("incomplete-leg loss exceeds frozen account-equity limit")
        expected_minimum_profit = max(Decimal("3"), self.assigned_capital_usd * Decimal("0.003"))
        if self.minimum_profit_required_usd != expected_minimum_profit:
            raise ValueError("minimum profit requirement is inconsistent")
        expected_annualized = max(
            Decimal("0.12"), self.cash_benchmark_annual_rate + Decimal("0.05")
        )
        if self.required_annualized_return != expected_annualized:
            raise ValueError("required annualized return is inconsistent")
        if tuple(item.holding_days for item in self.horizons) != (7, 14, 28):
            raise ValueError("holding horizons must be ordered as 7, 14, and 28 days")
        for item in self.horizons:
            if item.lighter_funding_usd != (
                self.lighter_entry_notional_usd * item.lighter_funding_rate_sum
            ):
                raise ValueError("Lighter funding component identity is inconsistent")
            if item.dydx_funding_usd != (self.dydx_entry_notional_usd * item.dydx_funding_rate_sum):
                raise ValueError("dYdX funding component identity is inconsistent")
            if item.conservative_funding_rate != (
                item.gross_funding_usd / self.assigned_capital_usd
            ):
                raise ValueError("conservative funding rate identity is inconsistent")
            reference_notional = (
                self.lighter_entry_notional_usd + self.dydx_entry_notional_usd
            ) / Decimal(2)
            if item.basis_divergence_reserve_usd != (
                reference_notional * item.basis_divergence_rate
            ):
                raise ValueError("basis divergence reserve identity is inconsistent")
            expected_net = (
                item.gross_funding_usd
                - self.costs.normal_cost_usd
                - item.funding_reversal_reserve_usd
                - item.basis_divergence_reserve_usd
            )
            if item.conservative_net_usd != expected_net:
                raise ValueError("conservative net identity is inconsistent")
            if item.assigned_capital_return != expected_net / self.assigned_capital_usd:
                raise ValueError("assigned-capital return identity is inconsistent")
            if item.account_return != expected_net / self.account_equity_usd:
                raise ValueError("account return identity is inconsistent")
            if item.minimum_profit_pass is not (expected_net >= expected_minimum_profit):
                raise ValueError("minimum-profit pass flag is inconsistent")
            if item.annualized_return_pass is not (
                item.annualized_conservative_return >= expected_annualized
            ):
                raise ValueError("annualized-return pass flag is inconsistent")
        twenty_eight = self.horizons[-1]
        expected_doubled_net = (
            twenty_eight.gross_funding_usd
            - self.costs.doubled_transaction_cost_usd
            - twenty_eight.funding_reversal_reserve_usd
            - twenty_eight.basis_divergence_reserve_usd
        )
        if self.doubled_cost_28d_net_usd != expected_doubled_net:
            raise ValueError("doubled-cost 28-day identity is inconsistent")
        if self.doubled_cost_28d_pass is not (expected_doubled_net > 0):
            raise ValueError("doubled-cost pass flag is inconsistent")
        stress_numerator = (
            twenty_eight.funding_reversal_reserve_usd
            + self.costs.forced_exit_cost_usd
            + self.costs.latency_reserve_usd
        )
        if self.funding_and_forced_exit_loss_rate != stress_numerator / self.account_equity_usd:
            raise ValueError("funding and forced-exit loss identity is inconsistent")
        drawdown_numerator = stress_numerator + twenty_eight.basis_divergence_reserve_usd
        if self.modeled_drawdown_rate != drawdown_numerator / self.assigned_capital_usd:
            raise ValueError("modeled drawdown identity is inconsistent")
        if self.stress_loss_pass is not (
            self.funding_and_forced_exit_loss_rate <= Decimal("0.0025")
        ):
            raise ValueError("stress-loss pass flag is inconsistent")
        if self.drawdown_pass is not (self.modeled_drawdown_rate < Decimal("0.08")):
            raise ValueError("drawdown pass flag is inconsistent")
        if self.liquidation_pass is not (not self.modeled_liquidation):
            raise ValueError("liquidation pass flag is inconsistent")
        if self.quote_observations_pass is not (
            self.normal_quote_observations >= 25 and self.stress_quote_observations >= 10
        ):
            raise ValueError("quote-observation pass flag is inconsistent")
        return self

    @property
    def all_numeric_gates_pass(self) -> bool:
        return (
            all(
                item.net_positive and item.minimum_profit_pass and item.annualized_return_pass
                for item in self.horizons
            )
            and self.doubled_cost_28d_pass
            and self.stress_loss_pass
            and self.drawdown_pass
            and self.liquidation_pass
            and self.quote_observations_pass
        )


class LegacyEconomicEvaluationSummary(StrictRecord):
    """Top-level identity for immutable schema-one reports with invalid legacy math."""

    schema_version: Literal[1]
    protocol_version: Literal["lighter-dydx-shadow-economics-v1"]
    evaluation_id: UUID
    asset: Asset
    known_as_of: datetime
    evaluated_at: datetime
    decision: EconomicsDecision
    reason_codes: tuple[str, ...]
    direction: FundingDirection | None

    @classmethod
    def from_report_json(cls, report_json: str) -> Self:
        payload = json.loads(report_json)
        if not isinstance(payload, dict):
            raise ValueError("legacy economic evaluation must be a JSON object")
        summary_keys = (
            "schema_version",
            "protocol_version",
            "evaluation_id",
            "asset",
            "known_as_of",
            "evaluated_at",
            "decision",
            "reason_codes",
            "direction",
        )
        summary = {key: payload.get(key) for key in summary_keys}
        return cls.model_validate_json(json.dumps(summary, separators=(",", ":"), sort_keys=True))

    @field_validator("known_as_of", "evaluated_at")
    @classmethod
    def require_legacy_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("reason_codes")
    @classmethod
    def require_legacy_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("reason codes must be sorted and unique")
        return require_machine_reason_codes(value)


class CandidateEconomicsReport(StrictRecord):
    schema_version: Literal[2]
    protocol_version: Literal["lighter-dydx-shadow-economics-v1"]
    evaluation_id: UUID
    asset: Asset
    known_as_of: datetime
    evaluated_at: datetime
    training_start: datetime
    training_end: datetime
    evaluation_end: datetime
    policy_hash: Sha256
    source_hashes: tuple[Sha256, ...]
    decision: EconomicsDecision
    reason_codes: tuple[str, ...]
    direction: FundingDirection | None
    short_venue: Venue | None
    long_venue: Venue | None
    coverage: EvidenceCoverage
    economics: CompleteEconomics | None
    warning: Literal[
        "Research only — shadow candidate, not a fill, recommendation, or trading authorization."
    ]

    @field_validator(
        "known_as_of",
        "evaluated_at",
        "training_start",
        "training_end",
        "evaluation_end",
    )
    @classmethod
    def require_report_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("source_hashes")
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
        return require_machine_reason_codes(value)

    @model_validator(mode="after")
    def require_coherent_report(self) -> CandidateEconomicsReport:
        if (
            self.evaluation_end.minute
            or self.evaluation_end.second
            or self.evaluation_end.microsecond
        ):
            raise ValueError("evaluation end must be aligned to a whole UTC hour")
        if self.training_end - self.training_start != timedelta(days=30):
            raise ValueError("training window must contain exactly 30 days")
        if self.evaluation_end - self.training_end != timedelta(days=60):
            raise ValueError("evaluation window must contain exactly 60 days")
        if self.known_as_of < self.evaluation_end:
            raise ValueError("knowledge cutoff must not precede evaluation end")
        if self.known_as_of - self.evaluation_end > timedelta(minutes=65):
            raise ValueError("evaluation end must be no more than 65 minutes before cutoff")
        if self.evaluated_at < self.known_as_of:
            raise ValueError("evaluation time must not precede knowledge cutoff")
        if self.direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
            expected_pair = (Venue.LIGHTER, Venue.DYDX)
        elif self.direction is FundingDirection.SHORT_DYDX_LONG_LIGHTER:
            expected_pair = (Venue.DYDX, Venue.LIGHTER)
        else:
            expected_pair = (None, None)
        if (self.short_venue, self.long_venue) != expected_pair:
            raise ValueError("direction venue mapping is inconsistent")
        if self.decision is EconomicsDecision.INSUFFICIENT_EVIDENCE:
            if not self.reason_codes:
                raise ValueError("insufficient report must contain reason codes")
            if self.direction is not None or self.economics is not None:
                raise ValueError("insufficient report must withhold direction and economics")
            return self
        if self.decision is EconomicsDecision.REJECTED and self.direction is None:
            zero_median = "TRAINING_FUNDING_MEDIAN_ZERO" in self.reason_codes
            if not zero_median or self.economics is not None:
                raise ValueError("directionless rejection must be the zero-median result")
            return self
        if self.direction is None:
            raise ValueError("direction-bearing decision requires a direction")
        if not self.source_hashes:
            raise ValueError("complete report must contain source lineage")
        coverages = (
            self.coverage.training_funding_coverage,
            self.coverage.evaluation_funding_coverage,
            self.coverage.funding_coverage,
            self.coverage.book_coverage,
        )
        if any(value < Decimal("0.99") for value in coverages):
            raise ValueError("direction-bearing report requires complete evidence coverage")
        if (
            self.coverage.latest_book_age_seconds is None
            or self.coverage.latest_pair_skew_ms is None
            or self.coverage.latency_sample_count == 0
        ):
            raise ValueError("direction-bearing report requires complete freshness evidence")
        if self.coverage.latest_book_age_seconds > Decimal("30"):
            raise ValueError("latest book age exceeds frozen protocol limit")
        if self.coverage.latest_pair_skew_ms > Decimal("1000"):
            raise ValueError("latest pair skew exceeds frozen protocol limit")
        if self.economics is None:
            sizing_prefixes = ("DEPTH_", "DELTA_", "CAPITAL_")
            if (
                self.decision is not EconomicsDecision.REJECTED
                or not self.reason_codes
                or any(not code.startswith(sizing_prefixes) for code in self.reason_codes)
            ):
                raise ValueError("economics-free rejection requires only canonical sizing reasons")
            return self
        required_hashes = {
            self.economics.operational_source_hash,
            *(item.source_hash for item in self.economics.execution_assumptions),
            *(item.source_hash for item in self.economics.margin_assumptions),
            *(item.source_hash for item in self.economics.fee_schedules),
        }
        if not required_hashes.issubset(self.source_hashes):
            raise ValueError("source hashes must contain every nested evidence lineage hash")
        if any(item.asset is not self.asset for item in self.economics.margin_assumptions):
            raise ValueError("report asset must match margin assumptions")
        evidence_times = tuple(
            item.observed_at
            for item in (
                *self.economics.execution_assumptions,
                *self.economics.margin_assumptions,
                *self.economics.fee_schedules,
            )
        )
        if any(observed_at > self.known_as_of for observed_at in evidence_times):
            raise ValueError("complete report evidence must be known by cutoff")
        if any(fee.effective_from > self.known_as_of for fee in self.economics.fee_schedules):
            raise ValueError("complete report fee must be effective by cutoff")
        if self.decision is EconomicsDecision.REJECTED:
            if not self.reason_codes:
                raise ValueError("rejected report must contain reason codes")
            return self
        if self.reason_codes or not self.economics.all_numeric_gates_pass:
            raise ValueError("shadow candidate requires no reasons and every numeric gate")
        return self


def canonical_policy_json(policy: EconomicsPolicy) -> str:
    return json.dumps(
        policy.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def policy_hash(policy: EconomicsPolicy) -> str:
    return sha256(canonical_policy_json(policy).encode("utf-8")).hexdigest()
