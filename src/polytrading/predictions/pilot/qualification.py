"""Recompute the pilot's live-eligibility evidence from persisted research history.

Nothing here accepts a caller's verdict. Every threshold in spec section 5.3 is recomputed from
verified store reads for one proof family at one cutoff, and each failure carries a stable code.
Strong evidence for one family never qualifies another.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Literal
from uuid import UUID

from pydantic import field_validator

from polytrading.predictions.domain import Sha256
from polytrading.predictions.economics_models import DEFAULT_RESEARCH_POLICY, ScanReport
from polytrading.predictions.experiments import ShadowExperiment
from polytrading.predictions.pilot.models import (
    PilotProofFamily,
    PilotRecord,
    UtcTimestamp,
)
from polytrading.predictions.pilot.policy import COMPILED_PILOT_CEILINGS
from polytrading.predictions.proofs_models import ProofArtifact
from polytrading.predictions.shadow_models import ShadowPlan, ShadowState
from polytrading.predictions.storage.store import PredictionMarketStore

CLASS_G_EVIDENCE_DAYS = 45
ADDITIONAL_SHADOW_DAYS = 30
MINIMUM_LATENCY_1S_OPPORTUNITIES = 25
MINIMUM_LATENCY_5S_OPPORTUNITIES = 10
MINIMUM_MEDIAN_SURPLUS_RATE = Decimal("0.0075")
MINIMUM_MEDIAN_CAPACITY_USD = Decimal("100")
MINIMUM_ANNUAL_CONTRIBUTION_RATE = Decimal("0.02")
MINIMUM_RETURN_PREMIUM = Decimal("0.05")
MAXIMUM_INCOMPLETE_LEG_LOSS_RATE = Decimal("0.0025")
MAXIMUM_DRAWDOWN_RATE = Decimal("0.08")

# The approved cash benchmark is the repository's own capital-lockup benchmark, annualized.
APPROVED_CASH_BENCHMARK_RATE = DEFAULT_RESEARCH_POLICY.capital_lockup_rate_per_day * 365

# Pilot equity is the compiled wallet ceiling: every equity-relative gate is measured against it.
PILOT_EQUITY_USD = COMPILED_PILOT_CEILINGS.wallet_trading_equity

_BASELINE_SCENARIO = "baseline"
_LATENCY_1S_SCENARIO = "latency_1s"
_LATENCY_5S_SCENARIO = "latency_5s"
_SURVIVING_STATES = frozenset({ShadowState.COMPLETE, ShadowState.RECONCILED})
_REWARD_TERMS = re.compile(r"reward|rebate|points|incentive", re.IGNORECASE)

QualificationCode = Literal[
    "EVIDENCE_DAYS_INSUFFICIENT",
    "LATENCY_1S_OPPORTUNITIES_INSUFFICIENT",
    "LATENCY_5S_OPPORTUNITIES_INSUFFICIENT",
    "MEDIAN_SURPLUS_INSUFFICIENT",
    "MEDIAN_CAPACITY_INSUFFICIENT",
    "ANNUAL_CONTRIBUTION_INSUFFICIENT",
    "RETURN_ON_CAPITAL_INSUFFICIENT",
    "FALSE_PAYOFF_CLAIM_PRESENT",
    "INCOMPLETE_LEG_LOSS_EXCEEDED",
    "DRAWDOWN_EXCEEDED",
    "SHADOW_DAYS_INSUFFICIENT",
    "SHADOW_PROFIT_NOT_POSITIVE",
    "SHADOW_PROFIT_REWARD_DEPENDENT",
    "SHADOW_RISK_BREACH",
    "SHADOW_RECONCILIATION_INCOMPLETE",
]


class QualificationGate(PilotRecord):
    """One recomputed threshold, with the observed value that decided it."""

    code: QualificationCode
    satisfied: bool
    observed: Decimal | None
    threshold: Decimal


class QualificationReport(PilotRecord):
    schema_version: Literal[1]
    proof_family: PilotProofFamily
    as_of: UtcTimestamp
    evidence_window_start: UtcTimestamp
    shadow_window_start: UtcTimestamp
    qualified: bool
    gates: tuple[QualificationGate, ...]
    failed_codes: tuple[str, ...]
    evidence_hashes: tuple[Sha256, ...]
    policy_identities: tuple[str, ...]
    protocol_fixture_hashes: tuple[Sha256, ...]

    @field_validator("evidence_hashes", "protocol_fixture_hashes", "policy_identities")
    @classmethod
    def _sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("qualification evidence must be sorted and unique")
        return value


def evaluate_pilot_qualification(
    store: PredictionMarketStore, proof_family: PilotProofFamily, as_of: datetime
) -> QualificationReport:
    """Recompute all Class G and additional-shadow gates; never accept caller booleans."""

    evidence_window_start = as_of - timedelta(days=CLASS_G_EVIDENCE_DAYS)
    shadow_window_start = as_of - timedelta(days=ADDITIONAL_SHADOW_DAYS)

    artifacts = tuple(
        artifact
        for artifact in store.verified_proof_artifacts_as_of(as_of)
        if artifact.template == proof_family.value
    )
    proofs = tuple(
        artifact
        for artifact in artifacts
        if artifact.status == "proof_ready"
        and evidence_window_start <= artifact.information_cutoff <= as_of
    )
    proof_ids = {artifact.proof_id for artifact in proofs}
    reports = tuple(
        report
        for report in store.verified_scan_reports_as_of(as_of)
        if report.proof_id in proof_ids
        and report.economics is not None
        and report.economics.status == "evaluated"
        and evidence_window_start <= report.as_of <= as_of
    )
    plans = tuple(
        plan
        for plan in store.verified_shadow_plans_as_of(as_of)
        if plan.proof_id in proof_ids and _is_single_venue_polymarket(plan)
    )
    proposal_ids = {plan.proposal_id for plan in plans}
    experiments = tuple(
        experiment
        for experiment in store.verified_shadow_experiments_as_of(as_of)
        if experiment.proposal_id in proposal_ids
        and evidence_window_start <= experiment.observed_at <= as_of
    )
    shadow_experiments = tuple(
        experiment for experiment in experiments if experiment.observed_at >= shadow_window_start
    )

    gates = (
        _evidence_days_gate(reports, as_of),
        _latency_gate(
            experiments,
            scenario=_LATENCY_1S_SCENARIO,
            minimum=MINIMUM_LATENCY_1S_OPPORTUNITIES,
            code="LATENCY_1S_OPPORTUNITIES_INSUFFICIENT",
        ),
        _latency_gate(
            experiments,
            scenario=_LATENCY_5S_SCENARIO,
            minimum=MINIMUM_LATENCY_5S_OPPORTUNITIES,
            code="LATENCY_5S_OPPORTUNITIES_INSUFFICIENT",
        ),
        _median_surplus_gate(reports),
        _median_capacity_gate(reports),
        _annual_contribution_gate(shadow_experiments),
        _return_on_capital_gate(reports),
        _false_payoff_gate(artifacts, as_of),
        _incomplete_leg_loss_gate(plans),
        _drawdown_gate(shadow_experiments),
        _shadow_days_gate(shadow_experiments, as_of),
        _shadow_profit_gate(shadow_experiments),
        _reward_dependence_gate(store, proposal_ids, as_of),
        _risk_breach_gate(shadow_experiments),
        _reconciliation_gate(store, shadow_experiments, as_of),
    )
    ordered_gates = tuple(sorted(gates, key=lambda gate: gate.code))
    conformance = store.verified_protocol_conformance_results(as_of)
    return QualificationReport(
        schema_version=1,
        proof_family=proof_family,
        as_of=as_of,
        evidence_window_start=evidence_window_start,
        shadow_window_start=shadow_window_start,
        qualified=all(gate.satisfied for gate in ordered_gates),
        gates=ordered_gates,
        failed_codes=tuple(sorted(gate.code for gate in ordered_gates if not gate.satisfied)),
        evidence_hashes=tuple(
            sorted(
                {hash_ for artifact in proofs for hash_ in artifact.source_hashes}
                | {hash_ for plan in plans for hash_ in plan.frozen_hashes}
            )
        ),
        policy_identities=tuple(
            sorted({f"{report.policy_id}@{report.policy_version}" for report in reports})
        ),
        protocol_fixture_hashes=tuple(
            sorted({hash_ for result in conformance for hash_ in result.fixture_hashes})
        ),
    )


def _is_single_venue_polymarket(plan: ShadowPlan) -> bool:
    return all(leg.venue.value == "polymarket" for leg in plan.legs)


def _gate(
    code: QualificationCode, *, satisfied: bool, observed: Decimal | None, threshold: Decimal
) -> QualificationGate:
    return QualificationGate(code=code, satisfied=satisfied, observed=observed, threshold=threshold)


def _covered_dates(values: Sequence[datetime]) -> set[date]:
    return {value.date() for value in values}


def _required_dates(as_of: datetime, days: int) -> set[date]:
    return {(as_of - timedelta(days=offset)).date() for offset in range(days)}


def _evidence_days_gate(reports: Sequence[ScanReport], as_of: datetime) -> QualificationGate:
    required = _required_dates(as_of, CLASS_G_EVIDENCE_DAYS)
    covered = _covered_dates([report.as_of for report in reports]) & required
    return _gate(
        "EVIDENCE_DAYS_INSUFFICIENT",
        satisfied=len(covered) == CLASS_G_EVIDENCE_DAYS,
        observed=Decimal(len(covered)),
        threshold=Decimal(CLASS_G_EVIDENCE_DAYS),
    )


def _surviving_proposals(experiments: Sequence[ShadowExperiment], scenario: str) -> set[UUID]:
    return {
        experiment.proposal_id
        for experiment in experiments
        if experiment.scenario_id == scenario
        and experiment.terminal_state in _SURVIVING_STATES
        and experiment.paper_pnl_usd is not None
        and experiment.paper_pnl_usd > 0
    }


def _latency_gate(
    experiments: Sequence[ShadowExperiment],
    *,
    scenario: str,
    minimum: int,
    code: QualificationCode,
) -> QualificationGate:
    surviving = _surviving_proposals(experiments, scenario)
    return _gate(
        code,
        satisfied=len(surviving) >= minimum,
        observed=Decimal(len(surviving)),
        threshold=Decimal(minimum),
    )


def _median_surplus_gate(reports: Sequence[ScanReport]) -> QualificationGate:
    rates = [
        report.economics.conservative_surplus_usd / report.economics.all_in_cost_usd
        for report in reports
        if report.economics is not None and report.economics.all_in_cost_usd > 0
    ]
    observed = median(rates) if rates else None
    return _gate(
        "MEDIAN_SURPLUS_INSUFFICIENT",
        satisfied=observed is not None and observed >= MINIMUM_MEDIAN_SURPLUS_RATE,
        observed=observed,
        threshold=MINIMUM_MEDIAN_SURPLUS_RATE,
    )


def _median_capacity_gate(reports: Sequence[ScanReport]) -> QualificationGate:
    capacities = [
        report.economics.capacity_usd_at_current_depth
        for report in reports
        if report.economics is not None
    ]
    observed = median(capacities) if capacities else None
    return _gate(
        "MEDIAN_CAPACITY_INSUFFICIENT",
        satisfied=observed is not None and observed >= MINIMUM_MEDIAN_CAPACITY_USD,
        observed=observed,
        threshold=MINIMUM_MEDIAN_CAPACITY_USD,
    )


def _baseline_pnl(experiments: Sequence[ShadowExperiment]) -> list[tuple[datetime, Decimal]]:
    return sorted(
        (
            (experiment.observed_at, experiment.paper_pnl_usd)
            for experiment in experiments
            if experiment.scenario_id == _BASELINE_SCENARIO and experiment.paper_pnl_usd is not None
        ),
        key=lambda entry: entry[0],
    )


def _annual_contribution_gate(experiments: Sequence[ShadowExperiment]) -> QualificationGate:
    realized = sum((pnl for _, pnl in _baseline_pnl(experiments)), Decimal("0"))
    annualized = realized * Decimal(365) / Decimal(ADDITIONAL_SHADOW_DAYS)
    observed = annualized / PILOT_EQUITY_USD
    return _gate(
        "ANNUAL_CONTRIBUTION_INSUFFICIENT",
        satisfied=observed >= MINIMUM_ANNUAL_CONTRIBUTION_RATE,
        observed=observed,
        threshold=MINIMUM_ANNUAL_CONTRIBUTION_RATE,
    )


def _return_on_capital_gate(reports: Sequence[ScanReport]) -> QualificationGate:
    threshold = APPROVED_CASH_BENCHMARK_RATE + MINIMUM_RETURN_PREMIUM
    annualized = [
        report.economics.return_on_assigned_capital
        * Decimal(365)
        / max(report.economics.max_capital_lock_days, Decimal(1))
        for report in reports
        if report.economics is not None
    ]
    observed = median(annualized) if annualized else None
    return _gate(
        "RETURN_ON_CAPITAL_INSUFFICIENT",
        satisfied=observed is not None and observed >= threshold,
        observed=observed,
        threshold=threshold,
    )


def _false_payoff_gate(artifacts: Sequence[ProofArtifact], as_of: datetime) -> QualificationGate:
    """A guaranteed payoff later overturned by review for the same candidate is a false claim."""

    overturned = 0
    for artifact in artifacts:
        if artifact.status != "proof_ready":
            continue
        overturned += any(
            later.candidate_id == artifact.candidate_id
            and later.status == "rejected"
            and later.information_cutoff > artifact.information_cutoff
            and later.information_cutoff <= as_of
            for later in artifacts
        )
    return _gate(
        "FALSE_PAYOFF_CLAIM_PRESENT",
        satisfied=overturned == 0,
        observed=Decimal(overturned),
        threshold=Decimal(0),
    )


def _percentile(values: Sequence[Decimal], fraction: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int((fraction * Decimal(len(ordered))).to_integral_value(rounding="ROUND_CEILING")) - 1
    return ordered[max(index, 0)]


def _incomplete_leg_loss_gate(plans: Sequence[ShadowPlan]) -> QualificationGate:
    threshold = MAXIMUM_INCOMPLETE_LEG_LOSS_RATE * PILOT_EQUITY_USD
    observed = _percentile([plan.max_incomplete_loss_usd for plan in plans], Decimal("0.99"))
    return _gate(
        "INCOMPLETE_LEG_LOSS_EXCEEDED",
        satisfied=observed is not None and observed < threshold,
        observed=observed,
        threshold=threshold,
    )


def _drawdown_gate(experiments: Sequence[ShadowExperiment]) -> QualificationGate:
    cumulative = Decimal("0")
    peak = Decimal("0")
    drawdown = Decimal("0")
    for _, pnl in _baseline_pnl(experiments):
        cumulative += pnl
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    observed = drawdown / PILOT_EQUITY_USD
    return _gate(
        "DRAWDOWN_EXCEEDED",
        satisfied=observed < MAXIMUM_DRAWDOWN_RATE,
        observed=observed,
        threshold=MAXIMUM_DRAWDOWN_RATE,
    )


def _shadow_days_gate(
    experiments: Sequence[ShadowExperiment], as_of: datetime
) -> QualificationGate:
    required = _required_dates(as_of, ADDITIONAL_SHADOW_DAYS)
    covered = (
        _covered_dates(
            [experiment.observed_at for experiment in experiments if experiment.reconciled]
        )
        & required
    )
    return _gate(
        "SHADOW_DAYS_INSUFFICIENT",
        satisfied=len(covered) == ADDITIONAL_SHADOW_DAYS,
        observed=Decimal(len(covered)),
        threshold=Decimal(ADDITIONAL_SHADOW_DAYS),
    )


def _shadow_profit_gate(experiments: Sequence[ShadowExperiment]) -> QualificationGate:
    realized = sum(
        (
            experiment.paper_pnl_usd
            for experiment in experiments
            if experiment.reconciled and experiment.paper_pnl_usd is not None
        ),
        Decimal("0"),
    )
    return _gate(
        "SHADOW_PROFIT_NOT_POSITIVE",
        satisfied=realized > 0,
        observed=realized,
        threshold=Decimal(0),
    )


def _reward_dependence_gate(
    store: PredictionMarketStore, proposal_ids: set[UUID], as_of: datetime
) -> QualificationGate:
    credited = 0
    for proposal_id in sorted(proposal_ids):
        for posting in store.verified_ledger_postings_for_proposal(proposal_id, as_of):
            credited += bool(_REWARD_TERMS.search(posting.detail))
    return _gate(
        "SHADOW_PROFIT_REWARD_DEPENDENT",
        satisfied=credited == 0,
        observed=Decimal(credited),
        threshold=Decimal(0),
    )


def _risk_breach_gate(experiments: Sequence[ShadowExperiment]) -> QualificationGate:
    breaches = sum(
        1 for experiment in experiments if experiment.terminal_state is ShadowState.UNKNOWN
    )
    return _gate(
        "SHADOW_RISK_BREACH",
        satisfied=breaches == 0,
        observed=Decimal(breaches),
        threshold=Decimal(0),
    )


def _reconciliation_gate(
    store: PredictionMarketStore, experiments: Sequence[ShadowExperiment], as_of: datetime
) -> QualificationGate:
    incomplete = 0
    for proposal_id in sorted({experiment.proposal_id for experiment in experiments}):
        reconciliations = store.verified_shadow_reconciliations_for_proposal(proposal_id, as_of)
        if not reconciliations or any(
            not reconciliation.complete or reconciliation.unexplained_difference_usd != 0
            for reconciliation in reconciliations
        ):
            incomplete += 1
    return _gate(
        "SHADOW_RECONCILIATION_INCOMPLETE",
        satisfied=incomplete == 0,
        observed=Decimal(incomplete),
        threshold=Decimal(0),
    )
