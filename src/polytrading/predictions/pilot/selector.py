"""Deterministic pilot opportunity selection and frozen plan compilation.

Nothing here scores, guesses, or ranks by opinion: eligibility is a set of independent gates over
persisted evidence, ranking is the spec's fixed field order with a stable identity tie-break, and a
compiled plan is frozen before it is ever shown to the operator for approval.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from polytrading.predictions.domain import PredictionVenue, Sha256
from polytrading.predictions.execution.models import (
    ImmediateOrderType,
    canonical_execution_hash,
)
from polytrading.predictions.pilot.models import (
    PilotLimits,
    PilotProofFamily,
    PilotRecord,
    UtcTimestamp,
)
from polytrading.predictions.pilot.policy import COMPILED_PILOT_CEILINGS
from polytrading.predictions.pilot.qualification import (
    MAXIMUM_INCOMPLETE_LEG_LOSS_RATE,
    PILOT_EQUITY_USD,
)
from polytrading.predictions.shadow_models import ShadowState
from polytrading.predictions.storage.store import PredictionMarketStore

# Evidence older than this is not a coherent cutoff for a live decision.
MAXIMUM_EVIDENCE_AGE = timedelta(minutes=5)
_LATENCY_STRESS_SCENARIO = "latency_5s"
_BASELINE_SCENARIO = "baseline"
_SURVIVING_STATES = frozenset({ShadowState.COMPLETE, ShadowState.RECONCILED})

PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

PilotRejectionCode = Literal[
    "FAMILY_NOT_ENABLED",
    "PROOF_NOT_READY",
    "EVIDENCE_STALE",
    "SURPLUS_NOT_POSITIVE",
    "STRESSED_SURPLUS_NOT_POSITIVE",
    "INCOMPLETE_LOSS_EXCEEDED",
    "SHADOW_REPLAY_MISSING",
    "CROSS_VENUE_LEG",
    "WALLET_MISMATCH",
    "INSUFFICIENT_BALANCE",
    "INSUFFICIENT_ALLOWANCE",
    "ORDER_NOTIONAL_EXCEEDED",
    "STRATEGY_NOTIONAL_EXCEEDED",
    "KILL_ENGAGED",
]
TieBreakField = Literal[
    "incomplete_loss_ratio",
    "stressed_surplus_usd",
    "capacity_usd",
    "proof_id",
]


class PilotAccountState(PilotRecord):
    """The account facts a selection depends on, read fresh at one cutoff."""

    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    collateral_usd: NonNegativeDecimal
    allowance_usd: NonNegativeDecimal
    kill_engaged: bool
    observed_at: UtcTimestamp


class PilotLeg(PilotRecord):
    """One exact immediate-or-cancel leg. Resting order types have no representation here."""

    leg_index: Annotated[int, Field(ge=0)]
    outcome_token_id: str
    side: Literal["buy", "sell"]
    limit_price: PositiveDecimal
    size: PositiveDecimal
    order_type: ImmediateOrderType

    @property
    def notional(self) -> Decimal:
        return self.limit_price * self.size


class PilotOpportunity(PilotRecord):
    """One eligible strategy with every ranking field the operator will see."""

    schema_version: Literal[1]
    proof_id: UUID
    candidate_id: UUID
    proposal_id: UUID
    proof_family: PilotProofFamily
    legs: tuple[PilotLeg, ...]
    current_surplus_usd: Decimal
    stressed_surplus_usd: Decimal
    capacity_usd: NonNegativeDecimal
    incomplete_loss_usd: NonNegativeDecimal
    deployed_capital_usd: PositiveDecimal
    evidence_hashes: tuple[Sha256, ...]
    information_cutoff: UtcTimestamp

    @field_validator("legs")
    @classmethod
    def _require_ordered_legs(cls, value: tuple[PilotLeg, ...]) -> tuple[PilotLeg, ...]:
        if len(value) < 2:
            raise ValueError("a pilot strategy needs at least two legs")
        if [leg.leg_index for leg in value] != list(range(len(value))):
            raise ValueError("legs must be indexed consecutively from zero")
        return value

    @property
    def incomplete_loss_ratio(self) -> Decimal:
        return self.incomplete_loss_usd / self.deployed_capital_usd

    @property
    def gross_notional(self) -> Decimal:
        return sum((leg.notional for leg in self.legs), Decimal("0"))


class FrozenRecoveryBranch(PilotRecord):
    """One precomputed risk-reducing action for a specific incomplete state."""

    trigger: Literal["LEG_INCOMPLETE", "LEG_UNKNOWN", "PRESENCE_LOST"]
    leg: PilotLeg
    worst_case_exposure_before_usd: NonNegativeDecimal
    worst_case_exposure_after_usd: NonNegativeDecimal
    additional_deployed_capital_usd: Literal[Decimal("0")] = Decimal("0")

    @model_validator(mode="after")
    def _require_risk_reduction(self) -> FrozenRecoveryBranch:
        if self.worst_case_exposure_after_usd >= self.worst_case_exposure_before_usd:
            raise ValueError("a recovery branch must reduce worst-case incomplete exposure")
        return self


class FrozenPilotPlan(PilotRecord):
    """The exact plan an approval binds: legs, recovery, budgets, and evidence."""

    schema_version: Literal[1]
    proof_id: UUID
    proposal_id: UUID
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    legs: tuple[PilotLeg, ...]
    recovery_branches: tuple[FrozenRecoveryBranch, ...]
    gross_notional_usd: PositiveDecimal
    deployed_capital_usd: PositiveDecimal
    worst_case_incomplete_loss_usd: NonNegativeDecimal
    effective_limits: PilotLimits
    evidence_hashes: tuple[Sha256, ...]
    deadline: UtcTimestamp
    information_cutoff: UtcTimestamp

    @property
    def plan_hash(self) -> Sha256:
        return canonical_execution_hash(self)


class PilotSelectionError(ValueError):
    """An opportunity the pilot refuses, named by a stable code."""

    def __init__(self, code: PilotRejectionCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def rank_pilot_opportunities(
    opportunities: Sequence[PilotOpportunity],
) -> tuple[PilotOpportunity, ...]:
    """Rank by the spec's fixed field order; the proof id is the final, stable tie-break."""

    return tuple(
        sorted(
            opportunities,
            key=lambda item: (
                item.incomplete_loss_ratio,
                -item.stressed_surplus_usd,
                -item.capacity_usd,
                str(item.proof_id),
            ),
        )
    )


def first_tie_break_field(left: PilotOpportunity, right: PilotOpportunity) -> TieBreakField:
    """Name the first ranking field that separated two neighbours, for the operator to see."""

    if left.incomplete_loss_ratio != right.incomplete_loss_ratio:
        return "incomplete_loss_ratio"
    if left.stressed_surplus_usd != right.stressed_surplus_usd:
        return "stressed_surplus_usd"
    if left.capacity_usd != right.capacity_usd:
        return "capacity_usd"
    return "proof_id"


def require_eligible(
    opportunity: PilotOpportunity,
    *,
    account: PilotAccountState,
    limits: PilotLimits,
    enabled_families: frozenset[PilotProofFamily],
    as_of: datetime,
) -> None:
    """Apply every eligibility gate independently; the first failure names itself."""

    if account.kill_engaged:
        raise PilotSelectionError("KILL_ENGAGED", "the account is killed")
    if opportunity.proof_family not in enabled_families:
        raise PilotSelectionError(
            "FAMILY_NOT_ENABLED", f"{opportunity.proof_family.value} is not qualified"
        )
    if as_of - opportunity.information_cutoff > MAXIMUM_EVIDENCE_AGE:
        raise PilotSelectionError("EVIDENCE_STALE", "the cited evidence is no longer current")
    if opportunity.current_surplus_usd <= 0:
        raise PilotSelectionError("SURPLUS_NOT_POSITIVE", "conservative surplus is not positive")
    if opportunity.stressed_surplus_usd <= 0:
        raise PilotSelectionError(
            "STRESSED_SURPLUS_NOT_POSITIVE", "surplus does not survive the latency stress"
        )
    if opportunity.incomplete_loss_usd >= MAXIMUM_INCOMPLETE_LEG_LOSS_RATE * PILOT_EQUITY_USD:
        raise PilotSelectionError(
            "INCOMPLETE_LOSS_EXCEEDED", "modeled incomplete-leg loss is at or above the cap"
        )
    for leg in opportunity.legs:
        if leg.notional > limits.order_notional:
            raise PilotSelectionError(
                "ORDER_NOTIONAL_EXCEEDED", f"leg {leg.leg_index} exceeds the order ceiling"
            )
    if opportunity.gross_notional > limits.strategy_gross_notional:
        raise PilotSelectionError(
            "STRATEGY_NOTIONAL_EXCEEDED", "strategy gross notional exceeds its ceiling"
        )
    if opportunity.deployed_capital_usd > limits.session_deployed_capital:
        raise PilotSelectionError(
            "STRATEGY_NOTIONAL_EXCEEDED", "deployed capital exceeds the session ceiling"
        )
    if account.collateral_usd < opportunity.deployed_capital_usd:
        raise PilotSelectionError("INSUFFICIENT_BALANCE", "reconciled collateral is too low")
    if account.allowance_usd < opportunity.gross_notional:
        raise PilotSelectionError("INSUFFICIENT_ALLOWANCE", "the approved allowance is too low")


def eligible_opportunities(
    store: PredictionMarketStore,
    account: PilotAccountState,
    as_of: datetime,
    *,
    limits: PilotLimits | None = None,
    enabled_families: frozenset[PilotProofFamily] | None = None,
) -> tuple[PilotOpportunity, ...]:
    """Rebuild every currently eligible single-Polymarket strategy from persisted evidence."""

    effective_limits = limits or COMPILED_PILOT_CEILINGS
    families = enabled_families or frozenset(PilotProofFamily)
    artifacts = {
        artifact.proof_id: artifact
        for artifact in store.verified_proof_artifacts_as_of(as_of)
        if artifact.status == "proof_ready"
    }
    experiments = store.verified_shadow_experiments_as_of(as_of)
    stressed = {
        experiment.proposal_id
        for experiment in experiments
        if experiment.scenario_id == _LATENCY_STRESS_SCENARIO
        and experiment.terminal_state in _SURVIVING_STATES
        and experiment.paper_pnl_usd is not None
        and experiment.paper_pnl_usd > 0
    }
    replayed = {
        experiment.proposal_id
        for experiment in experiments
        if experiment.scenario_id == _BASELINE_SCENARIO
        and experiment.terminal_state in _SURVIVING_STATES
    }
    reports = {
        report.proof_id: report
        for report in store.verified_scan_reports_as_of(as_of)
        if report.proof_id is not None
        and report.economics is not None
        and report.economics.status == "evaluated"
    }

    eligible: list[PilotOpportunity] = []
    for plan in store.verified_shadow_plans_as_of(as_of):
        artifact = artifacts.get(plan.proof_id)
        report = reports.get(plan.proof_id)
        if artifact is None or report is None or report.economics is None:
            continue
        if any(leg.venue is not PredictionVenue.POLYMARKET for leg in plan.legs):
            continue
        family = _family_for(artifact.template)
        if family is None:
            continue
        if plan.proposal_id not in replayed:
            continue
        opportunity = PilotOpportunity(
            schema_version=1,
            proof_id=plan.proof_id,
            candidate_id=plan.candidate_id,
            proposal_id=plan.proposal_id,
            proof_family=family,
            legs=_legs_from_plan(plan),
            current_surplus_usd=report.economics.conservative_surplus_usd,
            stressed_surplus_usd=(
                report.economics.doubled_cost_surplus_usd
                if plan.proposal_id in stressed
                else Decimal("0")
            ),
            capacity_usd=report.economics.capacity_usd_at_current_depth,
            incomplete_loss_usd=plan.max_incomplete_loss_usd,
            deployed_capital_usd=max(report.economics.all_in_cost_usd, Decimal("0.01")),
            evidence_hashes=tuple(sorted(set(artifact.source_hashes) | set(plan.frozen_hashes))),
            information_cutoff=min(plan.information_cutoff, report.as_of),
        )
        try:
            require_eligible(
                opportunity,
                account=account,
                limits=effective_limits,
                enabled_families=families,
                as_of=as_of,
            )
        except PilotSelectionError:
            continue
        eligible.append(opportunity)
    return rank_pilot_opportunities(eligible)


def _family_for(template: str) -> PilotProofFamily | None:
    for family in PilotProofFamily:
        if family.value == template:
            return family
    return None


def _legs_from_plan(plan: object) -> tuple[PilotLeg, ...]:
    legs: list[PilotLeg] = []
    for index, leg in enumerate(plan.legs):  # type: ignore[attr-defined]
        price, size = leg.limit_price_levels[0]
        legs.append(
            PilotLeg(
                leg_index=index,
                outcome_token_id=leg.outcome_token_id or leg.market_id,
                side="buy",
                limit_price=price,
                size=min(size, leg.max_quantity),
                order_type=ImmediateOrderType.FAK,
            )
        )
    return tuple(legs)


def compile_frozen_pilot_plan(
    opportunity: PilotOpportunity,
    limits: PilotLimits,
    account_state: PilotAccountState,
    *,
    deadline: datetime,
) -> FrozenPilotPlan:
    """Freeze one plan and its bounded recovery tree before any approval can bind it."""

    require_eligible(
        opportunity,
        account=account_state,
        limits=limits,
        enabled_families=frozenset(PilotProofFamily),
        as_of=opportunity.information_cutoff,
    )
    recovery = _recovery_branches(opportunity)
    for branch in recovery:
        if branch.leg.notional > limits.order_notional:
            raise PilotSelectionError(
                "ORDER_NOTIONAL_EXCEEDED", "a recovery leg exceeds the order ceiling"
            )
    recovery_notional = sum((branch.leg.notional for branch in recovery), Decimal("0"))
    if opportunity.gross_notional + recovery_notional > limits.strategy_gross_notional:
        raise PilotSelectionError(
            "STRATEGY_NOTIONAL_EXCEEDED",
            "strategy gross notional including recovery exceeds its ceiling",
        )
    return FrozenPilotPlan(
        schema_version=1,
        proof_id=opportunity.proof_id,
        proposal_id=opportunity.proposal_id,
        account_fingerprint=account_state.account_fingerprint,
        wallet_fingerprint=account_state.wallet_fingerprint,
        legs=opportunity.legs,
        recovery_branches=recovery,
        gross_notional_usd=opportunity.gross_notional + recovery_notional,
        deployed_capital_usd=opportunity.deployed_capital_usd,
        worst_case_incomplete_loss_usd=opportunity.incomplete_loss_usd,
        effective_limits=limits,
        evidence_hashes=opportunity.evidence_hashes,
        deadline=deadline,
        information_cutoff=opportunity.information_cutoff,
    )


def _recovery_branches(opportunity: PilotOpportunity) -> tuple[FrozenRecoveryBranch, ...]:
    """One unwind per leg that could be left incomplete, each strictly exposure-reducing."""

    branches: list[FrozenRecoveryBranch] = []
    for leg in opportunity.legs[:-1]:
        exposure_before = leg.notional
        unwind = PilotLeg(
            leg_index=leg.leg_index,
            outcome_token_id=leg.outcome_token_id,
            side="sell" if leg.side == "buy" else "buy",
            limit_price=leg.limit_price,
            size=leg.size,
            order_type=ImmediateOrderType.FOK,
        )
        branches.append(
            FrozenRecoveryBranch(
                trigger="LEG_INCOMPLETE",
                leg=unwind,
                worst_case_exposure_before_usd=exposure_before,
                worst_case_exposure_after_usd=opportunity.incomplete_loss_usd,
            )
        )
    return tuple(branches)


__all__ = [
    "FrozenPilotPlan",
    "FrozenRecoveryBranch",
    "PilotAccountState",
    "PilotLeg",
    "PilotOpportunity",
    "PilotSelectionError",
    "compile_frozen_pilot_plan",
    "eligible_opportunities",
    "first_tie_break_field",
    "rank_pilot_opportunities",
    "require_eligible",
]
