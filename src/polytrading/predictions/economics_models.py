from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from polytrading.predictions.domain import NonNegativeDecimal, PredictionRecord, PredictionVenue

InsufficiencyReason = Literal[
    "MISSING_BOOK",
    "STALE_BOOK",
    "CROSSED_BOOK",
    "MISSING_FEE",
    "ZERO_EXECUTABLE_DEPTH",
]

ScanDecision = Literal["SHADOW_CANDIDATE", "REJECTED", "INSUFFICIENT_EVIDENCE"]


class PredictionEconomicsPolicy(PredictionRecord):
    """Frozen, versioned reserve parameters for the conservative economics engine.

    Every reserve/rate field here is a deliberately conservative research-mode
    default (see ``DEFAULT_RESEARCH_POLICY``); nothing here is tuned to make any
    particular opportunity look attractive. ``policy_id``/``policy_version`` let a
    persisted ``EconomicsResult`` (or, in a later increment, a ``ScanReport``) cite
    exactly which frozen threshold set produced it -- required by spec section 7's
    "frozen policy thresholds" gate for a ``SHADOW_CANDIDATE``.
    """

    policy_id: str
    policy_version: str

    # --- all_in_cost_usd components (beyond depth-walked acquisition cost) ---
    gas_conversion_redemption_reserve_usd: NonNegativeDecimal
    currency_basis_reserve_rate: NonNegativeDecimal
    transfer_cost_usd: NonNegativeDecimal
    capital_lockup_rate_per_day: NonNegativeDecimal
    # v1 ruling (SDD ledger, Task 10): capital lock time is a policy constant here
    # rather than derived per-market from a market's end_at/resolution horizon --
    # conservative and simple; revisit once the scan engine can join market
    # horizons in.
    assumed_capital_lock_days: NonNegativeDecimal
    operational_cost_usd: NonNegativeDecimal

    # --- failure_reserve_usd components ---
    partial_fill_reserve_rate: NonNegativeDecimal
    latency_reserve_rate: NonNegativeDecimal
    dispute_delay_reserve_rate: NonNegativeDecimal
    venue_failure_reserve_rate: NonNegativeDecimal

    # --- evidence-freshness gate ---
    max_book_age_seconds: Annotated[int, Field(ge=0)]


class LegExecutionPlan(PredictionRecord):
    """One leg's depth-walked fill plan at the basket's bottleneck quantity."""

    leg_index: int
    venue: PredictionVenue
    market_id: str
    outcome_token_id: str | None
    depth_walked_levels: tuple[tuple[Decimal, Decimal], ...]
    filled_quantity: NonNegativeDecimal
    acquisition_cost_usd: NonNegativeDecimal


class EconomicsResult(PredictionRecord):
    """Spec section 7's conservative cost/reserve/surplus evaluation for one basket.

    ``status=="insufficient_evidence"`` carries a typed ``insufficiency_reason`` and
    every numeric/collection field zeroed/empty (``max_capital_lock_days`` excepted --
    it is a pure echo of the policy's constant, never evidence-dependent); an
    ``"evaluated"`` result carries no reason and the fully computed figures.
    """

    status: Literal["evaluated", "insufficient_evidence"]
    insufficiency_reason: InsufficiencyReason | None
    quantity: NonNegativeDecimal
    leg_plans: tuple[LegExecutionPlan, ...]
    proven_floor_usd: NonNegativeDecimal
    all_in_cost_usd: NonNegativeDecimal
    failure_reserve_usd: NonNegativeDecimal
    conservative_surplus_usd: Decimal
    return_on_assigned_capital: Decimal
    capacity_usd_at_current_depth: NonNegativeDecimal
    stranded_collateral_by_venue: dict[str, Decimal]
    max_capital_lock_days: NonNegativeDecimal
    doubled_cost_surplus_usd: Decimal

    @model_validator(mode="after")
    def _require_consistent_result(self) -> EconomicsResult:
        if self.status == "insufficient_evidence":
            if self.insufficiency_reason is None:
                raise ValueError("an insufficient_evidence result requires an insufficiency_reason")
            if self.leg_plans:
                raise ValueError("an insufficient_evidence result must not carry leg plans")
        elif self.insufficiency_reason is not None:
            raise ValueError("an evaluated result must not carry an insufficiency_reason")
        return self


# Fixed namespace for deterministic scan-report identity (UUIDv5). Generated once via
# uuid4() and pinned here as a literal -- never regenerate this value, or every
# previously derived report_id would silently change identity, breaking append-idempotent
# persistence (see ``deterministic_scan_report_id``).
_SCAN_REPORT_IDENTITY_NAMESPACE = UUID("602940c3-ac97-499d-a120-33da483a1055")


class ScanReport(PredictionRecord):
    """Task 11's append-only per-candidate scan decision (spec section 8/9's gate).

    A scan joins a candidate's latest proof (if any) with fresh book/fee evidence and
    ``DEFAULT_RESEARCH_POLICY`` into exactly one of three fail-closed outcomes:
    ``SHADOW_CANDIDATE`` (a proof-backed, economically-positive basket -- research
    shadow tracking only, never a live trading signal), ``REJECTED``, or
    ``INSUFFICIENT_EVIDENCE``. ``proof_id``/``economics`` are the report's own citation
    of exactly which persisted proof and computed economics evaluation (if any)
    justify ``decision``; ``policy_id``/``policy_version`` cite the frozen policy
    thresholds evaluated against, matching ``EconomicsResult``'s own citation
    convention (spec section 7's "frozen policy thresholds" gate).
    """

    report_id: UUID
    candidate_id: UUID
    proof_id: UUID | None
    decision: ScanDecision
    reason: str
    economics: EconomicsResult | None
    policy_id: str
    policy_version: str
    as_of: datetime
    observed_at: datetime

    @model_validator(mode="after")
    def _require_consistent_report(self) -> ScanReport:
        if self.decision == "SHADOW_CANDIDATE":
            if self.proof_id is None:
                raise ValueError("a SHADOW_CANDIDATE report requires a proof_id")
            if self.economics is None or self.economics.status != "evaluated":
                raise ValueError("a SHADOW_CANDIDATE report requires an evaluated economics result")
            if self.economics.conservative_surplus_usd <= 0:
                raise ValueError(
                    "a SHADOW_CANDIDATE report requires a positive conservative surplus"
                )
        return self


def deterministic_scan_report_id(
    *,
    candidate_id: UUID,
    proof_id: UUID | None,
    decision: ScanDecision,
    reason: str,
    economics: EconomicsResult | None,
    policy_id: str,
    policy_version: str,
    as_of: datetime,
) -> UUID:
    """Derive a stable identity for one candidate's scan decision at one ``as_of``.

    Deliberately excludes ``observed_at`` (mirrors ``candidates_models.
    deterministic_candidate_id`` and ``proofs._finalize``'s append-idempotent style):
    re-running ``predictions scan`` at the *same* ``as_of`` over unchanged evidence
    always reproduces the same ``report_id``, so persisting it again is a no-op rather
    than a conflict. A later ``as_of`` (or evidence that has since changed) is folded
    into the hash and legitimately mints a new report -- a scan is a fresh evaluation
    of current state, not a regenerated fact about an immutable identity.
    """
    canonical = json.dumps(
        [
            str(candidate_id),
            str(proof_id) if proof_id is not None else None,
            decision,
            reason,
            economics.model_dump(mode="json") if economics is not None else None,
            policy_id,
            policy_version,
            as_of.isoformat(),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_SCAN_REPORT_IDENTITY_NAMESPACE, canonical)


# Conservative research-mode defaults. These are deliberately small-but-nonzero so
# that every friction the spec's formula names is actually charged for in v1 --
# nothing here is calibrated against observed opportunities (spec section 7's
# thresholds are preregistered and may not be relaxed because results are weak).
DEFAULT_RESEARCH_POLICY = PredictionEconomicsPolicy(
    policy_id="research-v1",
    policy_version="1",
    # $2 flat reserve for gas / stablecoin conversion / redemption round-trips.
    gas_conversion_redemption_reserve_usd=Decimal("2.00"),
    # 25 bps of acquisition cost reserved against stablecoin/currency basis drift.
    currency_basis_reserve_rate=Decimal("0.0025"),
    # $1 flat deposit/withdrawal/transfer cost per basket.
    transfer_cost_usd=Decimal("1.00"),
    # 2 bps/day capital-lockup benchmark (~7.3%/yr), applied over the assumed lock.
    capital_lockup_rate_per_day=Decimal("0.0002"),
    # 3 days assumed capital lock time (policy constant in v1; see class docstring).
    assumed_capital_lock_days=Decimal("3"),
    # $0.50 flat ordinary operational cost per basket.
    operational_cost_usd=Decimal("0.50"),
    # Failure reserves: 1% partial-fill/unwind, 0.5% latency, 0.5% dispute delay,
    # 0.25% venue/account/custody/settlement-divergence -- each conservative and
    # measured against acquisition cost.
    partial_fill_reserve_rate=Decimal("0.01"),
    latency_reserve_rate=Decimal("0.005"),
    dispute_delay_reserve_rate=Decimal("0.005"),
    venue_failure_reserve_rate=Decimal("0.0025"),
    # A book older than 5 seconds relative to as_of is not simultaneously-known
    # evidence and cannot back an executable economics evaluation.
    max_book_age_seconds=5,
)
