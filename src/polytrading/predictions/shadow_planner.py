from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import ValidationError

from polytrading.predictions.candidates_models import CandidateRelationship
from polytrading.predictions.domain import (
    PredictionBookLevel,
    PredictionBookSnapshot,
    PredictionFeeRate,
    PredictionRecord,
    normalize_utc_timestamp,
)
from polytrading.predictions.economics import evaluate_basket_economics
from polytrading.predictions.economics_models import (
    LegExecutionPlan,
    PredictionEconomicsPolicy,
    ScanReport,
)
from polytrading.predictions.proofs_models import ProofArtifact
from polytrading.predictions.risk import (
    PredictionRiskPolicy,
    RiskGateDecision,
    ShadowPortfolioState,
    evaluate_risk_gate,
)
from polytrading.predictions.shadow_models import (
    ShadowLegPlan,
    ShadowPlan,
    deterministic_proposal_id,
)


class PlanRefusal(PredictionRecord):
    reason: Literal[
        "SCAN_NOT_SHADOW_CANDIDATE",
        "PROOF_NOT_CURRENT",
        "RISK_REFUSED",
        "MISSING_EVIDENCE",
    ]
    detail: str
    risk: RiskGateDecision | None


class _PriceLevel(Protocol):
    price: Decimal
    size: Decimal


_KILL_CONDITIONS = (
    "any participating rule_version change",
    "book evidence older than policy max age",
    "risk drawdown threshold breached",
)
_INVALID_EVIDENCE_DETAIL = "invalid or mismatched planner evidence"


def plan_shadow_proposal(
    *,
    scan_report: ScanReport,
    candidate: CandidateRelationship,
    proof: ProofArtifact,
    books: Mapping[int, PredictionBookSnapshot | None],
    fees: Mapping[int, PredictionFeeRate | None],
    economics_policy: PredictionEconomicsPolicy,
    risk_policy: PredictionRiskPolicy,
    portfolio: ShadowPortfolioState,
    as_of: datetime,
    expiry_window_seconds: int,
    event_cluster_id: str | None = None,
) -> ShadowPlan | PlanRefusal:
    """Build one evidence-frozen, deterministic research-only shadow proposal."""
    try:
        scan_report = _revalidate_record(scan_report, ScanReport)
        candidate = _revalidate_record(candidate, CandidateRelationship)
        proof = _revalidate_record(proof, ProofArtifact)
        books = _revalidate_evidence_mapping(books, PredictionBookSnapshot)
        fees = _revalidate_evidence_mapping(fees, PredictionFeeRate)
        economics_policy = _revalidate_record(economics_policy, PredictionEconomicsPolicy)
        risk_policy = _revalidate_record(risk_policy, PredictionRiskPolicy)
        portfolio = _revalidate_record(portfolio, ShadowPortfolioState)
    except (TypeError, ValueError, ValidationError):
        return _invalid_evidence_refusal()

    as_of = normalize_utc_timestamp(as_of)
    if expiry_window_seconds <= 0:
        raise ValueError("expiry_window_seconds must be positive")

    if scan_report.decision != "SHADOW_CANDIDATE":
        return PlanRefusal(
            reason="SCAN_NOT_SHADOW_CANDIDATE",
            detail=f"scan report decision is {scan_report.decision}",
            risk=None,
        )

    current_failure = _proof_current_failure(scan_report, candidate, proof, as_of)
    if current_failure is not None:
        return PlanRefusal(reason="PROOF_NOT_CURRENT", detail=current_failure, risk=None)

    if not _evidence_identity_matches(candidate, books, fees):
        return _invalid_evidence_refusal()

    economics = evaluate_basket_economics(
        proof,
        candidate,
        books=books,
        fees=fees,
        policy=economics_policy,
        as_of=as_of,
    )
    if economics.status != "evaluated":
        return PlanRefusal(
            reason="MISSING_EVIDENCE",
            detail=f"economics could not be evaluated: {economics.insufficiency_reason}",
            risk=None,
        )
    if economics.conservative_surplus_usd <= 0:
        return PlanRefusal(
            reason="MISSING_EVIDENCE",
            detail="fresh economics no longer has positive conservative surplus",
            risk=None,
        )

    ordered_indices = tuple(
        sorted(
            range(len(candidate.legs)),
            key=lambda index: (_visible_ask_depth(books[index]), index),
        )
    )
    first_index = ordered_indices[0]
    first_plan = economics.leg_plans[first_index]
    first_book = books[first_index]
    assert first_book is not None
    full_unwind_proceeds = _walk_bid_proceeds(first_book.bids, economics.quantity)
    if full_unwind_proceeds is None:
        return PlanRefusal(
            reason="MISSING_EVIDENCE",
            detail="bottleneck leg has insufficient bid depth for conservative unwind",
            risk=None,
        )

    full_incomplete_loss = _incomplete_loss(
        acquisition_cost=first_plan.acquisition_cost_usd,
        unwind_proceeds=full_unwind_proceeds,
        partial_fill_reserve_rate=economics_policy.partial_fill_reserve_rate,
    )
    risk = evaluate_risk_gate(
        basket_cost_usd=economics.all_in_cost_usd,
        max_incomplete_loss_usd=full_incomplete_loss,
        event_cluster_id=(
            event_cluster_id if event_cluster_id is not None else str(candidate.candidate_id)
        ),
        portfolio=portfolio,
        policy=risk_policy,
    )
    if not risk.allowed:
        return PlanRefusal(
            reason="RISK_REFUSED",
            detail=f"risk gate refused proposal: {risk.reason}",
            risk=risk,
        )

    quantity = economics.quantity * risk.size_multiplier
    capped_books = _cap_ask_books(books, candidate, quantity)
    final_economics = evaluate_basket_economics(
        proof,
        candidate,
        books=capped_books,
        fees=fees,
        policy=economics_policy,
        as_of=as_of,
    )
    if final_economics.status != "evaluated":
        return PlanRefusal(
            reason="MISSING_EVIDENCE",
            detail=(
                "risk-sized economics could not be evaluated: "
                f"{final_economics.insufficiency_reason}"
            ),
            risk=None,
        )
    if final_economics.conservative_surplus_usd <= 0:
        return PlanRefusal(
            reason="MISSING_EVIDENCE",
            detail="risk-sized economics does not have positive conservative surplus",
            risk=None,
        )

    shadow_legs = tuple(
        _shadow_leg(
            leg_plan=final_economics.leg_plans[index],
            quantity=final_economics.quantity,
            sequence_position=sequence_position,
        )
        for sequence_position, index in enumerate(ordered_indices)
    )
    quantity = final_economics.quantity
    first_acquisition_cost = final_economics.leg_plans[first_index].acquisition_cost_usd
    unwind_proceeds = _walk_bid_proceeds(first_book.bids, quantity)
    if unwind_proceeds is None:
        return PlanRefusal(
            reason="MISSING_EVIDENCE",
            detail="bottleneck leg has insufficient bid depth for conservative unwind",
            risk=None,
        )
    incomplete_loss = _incomplete_loss(
        acquisition_cost=first_acquisition_cost,
        unwind_proceeds=unwind_proceeds,
        partial_fill_reserve_rate=economics_policy.partial_fill_reserve_rate,
    )

    order = " -> ".join(_leg_name(leg) for leg in shadow_legs)
    plan_fields: dict[str, object] = {
        "schema_version": 1,
        "candidate_id": candidate.candidate_id,
        "proof_id": proof.proof_id,
        "scan_report_id": scan_report.report_id,
        "legs": shadow_legs,
        "bottleneck_leg_index": first_index,
        "max_quantity": quantity,
        "order_policy": "taker_cross_only",
        "expires_at": as_of + timedelta(seconds=expiry_window_seconds),
        "completion_path": f"Acquire legs in order {order}; complete every remaining leg.",
        "cancellation_path": f"Cancel every unfilled order for sequence {order}.",
        "unwind_path": (
            f"If sequence {order} cannot complete, unwind filled legs in reverse order."
        ),
        "max_incomplete_exposure_usd": first_acquisition_cost,
        "max_incomplete_loss_usd": incomplete_loss,
        "frozen_hashes": _frozen_hashes(proof, books, fees, economics_policy, risk_policy),
        "policy_id": economics_policy.policy_id,
        "policy_version": economics_policy.policy_version,
        "risk_policy_version": risk.policy_version,
        "minimum_basket_payout": proof.minimum_basket_payout,
        "kill_conditions": _KILL_CONDITIONS,
        "information_cutoff": as_of,
        "observed_at": as_of,
    }
    provisional = ShadowPlan(proposal_id=UUID(int=0), **plan_fields)
    proposal_id = deterministic_proposal_id(
        scan_report.report_id,
        provisional.model_dump(mode="json", exclude={"proposal_id"}),
    )
    return ShadowPlan(proposal_id=proposal_id, **plan_fields)


def _revalidate_record[RecordT: PredictionRecord](
    value: object, record_type: type[RecordT]
) -> RecordT:
    if not isinstance(value, record_type):
        raise TypeError(f"expected {record_type.__name__}")
    return record_type.model_validate(value.model_dump())


def _revalidate_evidence_mapping[RecordT: PredictionRecord](
    value: object,
    record_type: type[RecordT],
) -> dict[int, RecordT | None]:
    if not isinstance(value, Mapping):
        raise TypeError("evidence must be an index mapping")

    validated: dict[int, RecordT | None] = {}
    for index, record in value.items():
        if not isinstance(index, int) or isinstance(index, bool):
            raise TypeError("evidence indices must be integers")
        validated[index] = None if record is None else _revalidate_record(record, record_type)
    return validated


def _invalid_evidence_refusal() -> PlanRefusal:
    return PlanRefusal(
        reason="MISSING_EVIDENCE",
        detail=_INVALID_EVIDENCE_DETAIL,
        risk=None,
    )


def _evidence_identity_matches(
    candidate: CandidateRelationship,
    books: Mapping[int, PredictionBookSnapshot | None],
    fees: Mapping[int, PredictionFeeRate | None],
) -> bool:
    for index, leg in enumerate(candidate.legs):
        book = books.get(index)
        if book is not None and (
            book.venue != leg.venue
            or book.market_id != leg.market_id
            or book.outcome_token_id != leg.outcome_token_id
        ):
            return False

        fee = fees.get(index)
        if fee is not None and (
            fee.venue != leg.venue or (fee.market_id is not None and fee.market_id != leg.market_id)
        ):
            return False
    return True


def _cap_ask_books(
    books: Mapping[int, PredictionBookSnapshot | None],
    candidate: CandidateRelationship,
    quantity: Decimal,
) -> dict[int, PredictionBookSnapshot | None]:
    capped = dict(books)
    for index in range(len(candidate.legs)):
        book = books[index]
        assert book is not None
        values = book.model_dump()
        values["asks"] = tuple(
            {"price": price, "size": size} for price, size in _trim_book_levels(book.asks, quantity)
        )
        capped[index] = PredictionBookSnapshot.model_validate(values)
    return capped


def _trim_book_levels(
    levels: Sequence[PredictionBookLevel], quantity: Decimal
) -> tuple[tuple[Decimal, Decimal], ...]:
    return _trim_levels(tuple((level.price, level.size) for level in levels), quantity)


def _proof_current_failure(
    scan_report: ScanReport,
    candidate: CandidateRelationship,
    proof: ProofArtifact,
    as_of: datetime,
) -> str | None:
    if scan_report.candidate_id != candidate.candidate_id:
        return "scan report and candidate identities do not agree"
    if proof.candidate_id != candidate.candidate_id:
        return "proof and candidate identities do not agree"
    if scan_report.proof_id != proof.proof_id:
        return "scan report does not cite the supplied proof"
    if proof.status != "proof_ready":
        return f"proof status is {proof.status}"
    if proof.information_cutoff > as_of:
        return "proof information cutoff is after planning as_of"
    if scan_report.as_of > as_of:
        return "scan report cutoff is after planning as_of"
    return None


def _visible_ask_depth(book: PredictionBookSnapshot | None) -> Decimal:
    assert book is not None
    return sum((level.size for level in book.asks), Decimal("0"))


def _shadow_leg(
    *, leg_plan: LegExecutionPlan, quantity: Decimal, sequence_position: int
) -> ShadowLegPlan:
    return ShadowLegPlan(
        leg_index=leg_plan.leg_index,
        venue=leg_plan.venue,
        market_id=leg_plan.market_id,
        outcome_token_id=leg_plan.outcome_token_id,
        sequence_position=sequence_position,
        limit_price_levels=_trim_levels(leg_plan.depth_walked_levels, quantity),
        max_quantity=quantity,
    )


def _trim_levels(
    levels: Sequence[tuple[Decimal, Decimal]], quantity: Decimal
) -> tuple[tuple[Decimal, Decimal], ...]:
    remaining = quantity
    trimmed: list[tuple[Decimal, Decimal]] = []
    for price, available in levels:
        if remaining <= 0:
            break
        take = min(available, remaining)
        trimmed.append((price, take))
        remaining -= take
    return tuple(trimmed)


def _walk_bid_proceeds(bids: Sequence[_PriceLevel], quantity: Decimal) -> Decimal | None:
    remaining = quantity
    proceeds = Decimal("0")
    for level in bids:
        if remaining <= 0:
            break
        take = min(level.size, remaining)
        proceeds += level.price * take
        remaining -= take
    return proceeds if remaining == 0 else None


def _incomplete_loss(
    *,
    acquisition_cost: Decimal,
    unwind_proceeds: Decimal,
    partial_fill_reserve_rate: Decimal,
) -> Decimal:
    spread_reserve = max(acquisition_cost - unwind_proceeds, Decimal("0"))
    return acquisition_cost * partial_fill_reserve_rate + spread_reserve


def _leg_name(leg: ShadowLegPlan) -> str:
    token = leg.outcome_token_id if leg.outcome_token_id is not None else "-"
    return f"{leg.venue.value}:{leg.market_id}:{token}"


def _policy_hash(policy: PredictionRecord) -> str:
    canonical = json.dumps(
        policy.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _frozen_hashes(
    proof: ProofArtifact,
    books: Mapping[int, PredictionBookSnapshot | None],
    fees: Mapping[int, PredictionFeeRate | None],
    economics_policy: PredictionEconomicsPolicy,
    risk_policy: PredictionRiskPolicy,
) -> tuple[str, ...]:
    hashes = set(proof.source_hashes)
    hashes.update(book.source_hash for book in books.values() if book is not None)
    hashes.update(fee.source_hash for fee in fees.values() if fee is not None)
    hashes.add(_policy_hash(economics_policy))
    hashes.add(_policy_hash(risk_policy))
    return tuple(sorted(item.lower() for item in hashes))
