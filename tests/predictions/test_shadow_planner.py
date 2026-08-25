from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from polytrading.predictions.candidates_models import (
    CandidateDisposition,
    CandidateLeg,
    CandidateRelationship,
    DeterministicProvenance,
    RelationshipType,
)
from polytrading.predictions.domain import (
    PredictionBookLevel,
    PredictionBookSnapshot,
    PredictionFeeRate,
    PredictionVenue,
)
from polytrading.predictions.economics import evaluate_basket_economics
from polytrading.predictions.economics_models import (
    PredictionEconomicsPolicy,
    ScanReport,
    deterministic_scan_report_id,
)
from polytrading.predictions.proofs_models import ProofArtifact, TerminalState
from polytrading.predictions.risk import (
    PredictionRiskPolicy,
    RiskGateDecision,
    ShadowPortfolioState,
)

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
CANDIDATE_ID = UUID("10000000-0000-0000-0000-000000000001")
PROOF_ID = UUID("20000000-0000-0000-0000-000000000001")
RULE_A = UUID("30000000-0000-0000-0000-000000000001")
RULE_B = UUID("30000000-0000-0000-0000-000000000002")
CYCLE_ID = UUID("40000000-0000-0000-0000-000000000001")

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
ECONOMICS_POLICY_HASH = "d45a27b13ef02be291c69b99ffded95721b4c3c8bbe7bb206c71680ce7ad8260"
RISK_POLICY_HASH = "b898b57d97fd9a5a55496a75d4f0fed8c9f48c01783365658e34a312472d9e6a"
ORDER = "polymarket:market-b:token-b -> polymarket:market-a:token-a"


def _planner() -> object:
    return importlib.import_module("polytrading.predictions.shadow_planner")


def _level(price: str, size: str) -> PredictionBookLevel:
    return PredictionBookLevel(price=Decimal(price), size=Decimal(size))


def _candidate(**overrides: object) -> CandidateRelationship:
    values: dict[str, object] = {
        "schema_version": 1,
        "candidate_id": CANDIDATE_ID,
        "trial_family_id": "shadow-planner-test",
        "relationship_type": RelationshipType.BINARY_COMPLEMENT,
        "legs": (
            CandidateLeg(
                venue=PredictionVenue.POLYMARKET,
                market_id="market-a",
                outcome_index=0,
                outcome_token_id="token-a",
                rule_version_id=RULE_A,
                rule_source_hash=HASH_A,
            ),
            CandidateLeg(
                venue=PredictionVenue.POLYMARKET,
                market_id="market-b",
                outcome_index=1,
                outcome_token_id="token-b",
                rule_version_id=RULE_B,
                rule_source_hash=HASH_B,
            ),
        ),
        "information_cutoff": NOW,
        "observed_at": NOW,
        "provenance": DeterministicProvenance(
            kind="deterministic",
            generator="shadow-test",
            generator_version="1",
            code_revision="abc123",
        ),
        "propositions": (),
        "unresolved_fields": (),
        "contradictions": (),
        "invalidation_conditions": ("any participating rule_version change",),
        "review_status": "reviewed",
        "disposition": CandidateDisposition.PROOF_READY,
        "superseded_by_candidate_id": None,
    }
    values.update(overrides)
    return CandidateRelationship(**values)


def _proof(**overrides: object) -> ProofArtifact:
    values: dict[str, object] = {
        "schema_version": 1,
        "proof_id": PROOF_ID,
        "candidate_id": CANDIDATE_ID,
        "template": "binary_complement@1",
        "compiler_version": "1",
        "status": "proof_ready",
        "rejection_reason": None,
        "terminal_states": (
            TerminalState(
                state_id="a-wins", description="A pays", leg_payouts=(Decimal("1"), Decimal("0"))
            ),
            TerminalState(
                state_id="b-wins", description="B pays", leg_payouts=(Decimal("0"), Decimal("1"))
            ),
        ),
        "minimum_basket_payout": Decimal("1"),
        "maximum_basket_payout": Decimal("1"),
        "assumptions": (),
        "excluded_states": (),
        "equivalence_matrix": None,
        "rule_version_ids": (RULE_A, RULE_B),
        "source_hashes": (HASH_B, HASH_C),
        "review_identity": "reviewer@example.test",
        "invalidation_conditions": ("any participating rule_version change",),
        "information_cutoff": NOW,
        "observed_at": NOW,
    }
    values.update(overrides)
    return ProofArtifact(**values)


def _book(
    *,
    leg_index: int,
    bids: tuple[PredictionBookLevel, ...],
    asks: tuple[PredictionBookLevel, ...],
    source_hash: str,
) -> PredictionBookSnapshot:
    suffix = "a" if leg_index == 0 else "b"
    return PredictionBookSnapshot(
        schema_version=1,
        cycle_id=CYCLE_ID,
        venue=PredictionVenue.POLYMARKET,
        market_id=f"market-{suffix}",
        outcome_token_id=f"token-{suffix}",
        bids=bids,
        asks=asks,
        sequence=str(leg_index),
        effective_at=NOW,
        observed_at=NOW,
        source_hash=source_hash,
    )


def _books() -> dict[int, PredictionBookSnapshot]:
    return {
        0: _book(
            leg_index=0,
            bids=(_level("0.15", "8"),),
            asks=(_level("0.20", "2"), _level("0.30", "6")),
            source_hash=HASH_D,
        ),
        1: _book(
            leg_index=1,
            bids=(_level("0.35", "2"), _level("0.25", "3")),
            asks=(_level("0.40", "1"), _level("0.50", "4")),
            source_hash=HASH_E,
        ),
    }


def _fees() -> dict[int, PredictionFeeRate]:
    return {
        0: PredictionFeeRate(
            schema_version=1,
            venue=PredictionVenue.POLYMARKET,
            market_id="market-a",
            maker_rate=Decimal("0"),
            taker_rate=Decimal("0.01"),
            observed_at=NOW,
            source_hash=HASH_F,
        ),
        1: PredictionFeeRate(
            schema_version=1,
            venue=PredictionVenue.POLYMARKET,
            market_id="market-b",
            maker_rate=Decimal("0"),
            taker_rate=Decimal("0.02"),
            observed_at=NOW,
            source_hash=HASH_A,
        ),
    }


def _economics_policy(**overrides: object) -> PredictionEconomicsPolicy:
    values: dict[str, object] = {
        "policy_id": "research-test",
        "policy_version": "7",
        "gas_conversion_redemption_reserve_usd": Decimal("0"),
        "currency_basis_reserve_rate": Decimal("0"),
        "transfer_cost_usd": Decimal("0"),
        "capital_lockup_rate_per_day": Decimal("0"),
        "assumed_capital_lock_days": Decimal("0"),
        "operational_cost_usd": Decimal("0"),
        "partial_fill_reserve_rate": Decimal("0.10"),
        "latency_reserve_rate": Decimal("0"),
        "dispute_delay_reserve_rate": Decimal("0"),
        "venue_failure_reserve_rate": Decimal("0"),
        "max_book_age_seconds": 10,
    }
    values.update(overrides)
    return PredictionEconomicsPolicy(**values)


def _risk_policy() -> PredictionRiskPolicy:
    return PredictionRiskPolicy(policy_version="risk-test")


def _portfolio(**overrides: object) -> ShadowPortfolioState:
    values: dict[str, object] = {
        "total_equity_usd": Decimal("10000"),
        "open_exposure_usd_by_cluster": {},
        "peak_equity_usd": Decimal("10000"),
        "equity_24h_ago_usd": Decimal("10000"),
        "open_proposal_count": 0,
    }
    values.update(overrides)
    return ShadowPortfolioState(**values)


def _shadow_scan(
    *,
    candidate: CandidateRelationship | None = None,
    proof: ProofArtifact | None = None,
    books: dict[int, PredictionBookSnapshot] | None = None,
    fees: dict[int, PredictionFeeRate] | None = None,
    policy: PredictionEconomicsPolicy | None = None,
    **overrides: object,
) -> ScanReport:
    candidate = candidate or _candidate()
    proof = proof or _proof()
    books = books or _books()
    fees = fees or _fees()
    policy = policy or _economics_policy()
    economics = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )
    values: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "proof_id": proof.proof_id,
        "decision": "SHADOW_CANDIDATE",
        "reason": "positive proof-backed basket",
        "economics": economics,
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "as_of": NOW,
        "observed_at": NOW,
    }
    values.update(overrides)
    values["report_id"] = deterministic_scan_report_id(
        candidate_id=values["candidate_id"],
        proof_id=values["proof_id"],
        decision=values["decision"],
        reason=values["reason"],
        economics=values["economics"],
        policy_id=values["policy_id"],
        policy_version=values["policy_version"],
        as_of=values["as_of"],
    )
    return ScanReport(**values)


def _plan(**overrides: object) -> object:
    inputs: dict[str, object] = {
        "scan_report": _shadow_scan(),
        "candidate": _candidate(),
        "proof": _proof(),
        "books": _books(),
        "fees": _fees(),
        "economics_policy": _economics_policy(),
        "risk_policy": _risk_policy(),
        "portfolio": _portfolio(),
        "as_of": NOW,
        "expiry_window_seconds": 90,
    }
    inputs.update(overrides)
    return _planner().plan_shadow_proposal(**inputs)


def _unchecked(model: Any, method: str, **updates: object) -> Any:
    if method == "model_copy":
        return model.model_copy(update=updates)
    values = {name: getattr(model, name) for name in type(model).model_fields}
    return type(model).model_construct(**(values | updates))


def test_happy_path_freezes_every_plan_field_and_is_id_stable() -> None:
    """Wrong ordering, arithmetic, lineage, prose, or identity content corrupts replay."""
    first = _plan()
    second = _plan()

    assert first == second
    assert first.proposal_id == second.proposal_id
    assert first.schema_version == 1
    assert first.candidate_id == CANDIDATE_ID
    assert first.proof_id == PROOF_ID
    assert first.scan_report_id == _shadow_scan().report_id
    assert first.bottleneck_leg_index == 1
    assert first.max_quantity == Decimal("5")
    assert first.order_policy == "taker_cross_only"
    assert first.expires_at == NOW + timedelta(seconds=90)
    assert first.completion_path == f"Acquire legs in order {ORDER}; complete every remaining leg."
    assert first.cancellation_path == f"Cancel every unfilled order for sequence {ORDER}."
    assert first.unwind_path == (
        f"If sequence {ORDER} cannot complete, unwind filled legs in reverse order."
    )
    assert first.max_incomplete_exposure_usd == Decimal("2.40")
    assert first.max_incomplete_loss_usd == Decimal("1.190")
    assert first.frozen_hashes == (
        HASH_A,
        RISK_POLICY_HASH,
        HASH_B,
        HASH_C,
        ECONOMICS_POLICY_HASH,
        HASH_D,
        HASH_E,
        HASH_F,
    )
    assert first.policy_id == "research-test"
    assert first.policy_version == "7"
    assert first.risk_policy_version == "risk-test"
    assert first.minimum_basket_payout == Decimal("1")
    assert first.kill_conditions == (
        "any participating rule_version change",
        "book evidence older than policy max age",
        "risk drawdown threshold breached",
    )
    assert first.information_cutoff == NOW
    assert first.observed_at == NOW
    assert tuple(leg.model_dump() for leg in first.legs) == (
        {
            "leg_index": 1,
            "venue": PredictionVenue.POLYMARKET,
            "market_id": "market-b",
            "outcome_token_id": "token-b",
            "sequence_position": 0,
            "limit_price_levels": (
                (Decimal("0.40"), Decimal("1")),
                (Decimal("0.50"), Decimal("4")),
            ),
            "max_quantity": Decimal("5"),
        },
        {
            "leg_index": 0,
            "venue": PredictionVenue.POLYMARKET,
            "market_id": "market-a",
            "outcome_token_id": "token-a",
            "sequence_position": 1,
            "limit_price_levels": (
                (Decimal("0.20"), Decimal("2")),
                (Decimal("0.30"), Decimal("3")),
            ),
            "max_quantity": Decimal("5"),
        },
    )


def test_risk_halved_size_trims_every_ladder_and_recomputes_incomplete_loss() -> None:
    """Applying only max_quantity would retain frozen orders for twice the allowed size."""
    portfolio = _portfolio(
        total_equity_usd=Decimal("9500"),
        peak_equity_usd=Decimal("10000"),
        equity_24h_ago_usd=Decimal("9500"),
    )

    plan = _plan(portfolio=portfolio)

    assert plan.max_quantity == Decimal("2.5")
    assert plan.max_incomplete_exposure_usd == Decimal("1.150")
    assert plan.max_incomplete_loss_usd == Decimal("0.4400")
    assert tuple((leg.max_quantity, leg.limit_price_levels) for leg in plan.legs) == (
        (Decimal("2.5"), ((Decimal("0.40"), Decimal("1")), (Decimal("0.50"), Decimal("1.5")))),
        (Decimal("2.5"), ((Decimal("0.20"), Decimal("2")), (Decimal("0.30"), Decimal("0.5")))),
    )


def test_risk_halved_size_reruns_positive_economics_with_flat_costs() -> None:
    """A positive capped basket must retain flat costs while freezing its final ladders."""
    portfolio = _portfolio(
        total_equity_usd=Decimal("9500"),
        peak_equity_usd=Decimal("10000"),
        equity_24h_ago_usd=Decimal("9500"),
    )
    policy = _economics_policy(operational_cost_usd=Decimal("0.50"))

    plan = _plan(portfolio=portfolio, economics_policy=policy)

    # q=2.5: floor 2.5 - acquisition 1.70 - fees .0285 - flat .50 - reserve .17 = .1015.
    assert plan.max_quantity == Decimal("2.5")
    assert plan.max_incomplete_exposure_usd == Decimal("1.150")
    assert sum(size for _, size in plan.legs[0].limit_price_levels) == Decimal("2.5")


def test_risk_halved_size_refuses_when_flat_costs_make_final_economics_nonpositive() -> None:
    """Full-size profitability must not admit a half-size basket made negative by flat costs."""
    portfolio = _portfolio(
        total_equity_usd=Decimal("9500"),
        peak_equity_usd=Decimal("10000"),
        equity_24h_ago_usd=Decimal("9500"),
    )
    policy = _economics_policy(operational_cost_usd=Decimal("0.70"))

    refusal = _plan(portfolio=portfolio, economics_policy=policy)

    # Full q=5 surplus is .169; capped q=2.5 surplus is -.0985 after the unchanged flat cost.
    assert refusal.reason == "MISSING_EVIDENCE"
    assert refusal.risk is None
    assert refusal.detail


def test_non_shadow_scan_is_refused_before_planning() -> None:
    """A rejected scan must never become a proposal."""
    scan = ScanReport(
        report_id=UUID("50000000-0000-0000-0000-000000000001"),
        candidate_id=CANDIDATE_ID,
        proof_id=None,
        decision="REJECTED",
        reason="not economical",
        economics=None,
        policy_id="research-test",
        policy_version="7",
        as_of=NOW,
        observed_at=NOW,
    )

    refusal = _plan(scan_report=scan)

    assert refusal.reason == "SCAN_NOT_SHADOW_CANDIDATE"
    assert refusal.risk is None
    assert refusal.detail


@pytest.mark.parametrize(
    "overrides",
    (
        {"proof": _proof(candidate_id=UUID("60000000-0000-0000-0000-000000000001"))},
        {"scan_report": _shadow_scan(candidate_id=UUID("60000000-0000-0000-0000-000000000002"))},
        {"proof": _proof(proof_id=UUID("60000000-0000-0000-0000-000000000003"))},
        {"proof": _proof(information_cutoff=NOW + timedelta(seconds=1))},
        {"scan_report": _shadow_scan(as_of=NOW + timedelta(seconds=1))},
        {
            "proof": _proof(
                status="rejected",
                rejection_reason="RULE_VERSION_CHANGED",
                terminal_states=(),
                minimum_basket_payout=None,
                maximum_basket_payout=None,
            )
        },
    ),
)
def test_noncurrent_proof_or_identity_is_refused(overrides: dict[str, object]) -> None:
    """Identity, status, and point-in-time mismatches must fail closed."""
    refusal = _plan(**overrides)

    assert refusal.reason == "PROOF_NOT_CURRENT"
    assert refusal.risk is None
    assert refusal.detail


@pytest.mark.parametrize("evidence_case", ["missing_book", "nonpositive_surplus", "shallow_bid"])
def test_missing_or_moved_evidence_is_refused(evidence_case: str) -> None:
    """The planner must re-evaluate asks and conservatively price the first-leg unwind."""
    books = _books()
    policy = _economics_policy()
    if evidence_case == "missing_book":
        books.pop(0)
    elif evidence_case == "nonpositive_surplus":
        policy = _economics_policy(operational_cost_usd=Decimal("1"))
    else:
        books[1] = _book(
            leg_index=1,
            bids=(_level("0.35", "2"),),
            asks=(_level("0.40", "1"), _level("0.50", "4")),
            source_hash=HASH_E,
        )

    refusal = _plan(books=books, economics_policy=policy)

    assert refusal.reason == "MISSING_EVIDENCE"
    assert refusal.risk is None
    assert refusal.detail


@pytest.mark.parametrize(
    "evidence_case",
    ("swapped_books", "wrong_token", "wrong_fee_venue", "wrong_specific_fee_market"),
)
def test_mismatched_evidence_identity_is_refused(evidence_case: str) -> None:
    """Positionally supplied evidence must not be attributed to a different candidate leg."""
    books = _books()
    fees = _fees()
    if evidence_case == "swapped_books":
        books = {0: books[1], 1: books[0]}
    elif evidence_case == "wrong_token":
        books[0] = books[0].model_copy(update={"outcome_token_id": "wrong-token"})
    elif evidence_case == "wrong_fee_venue":
        fees[0] = fees[0].model_copy(update={"venue": PredictionVenue.KALSHI})
    else:
        fees[0] = fees[0].model_copy(update={"market_id": "different-market"})

    refusal = _plan(books=books, fees=fees)

    assert refusal.reason == "MISSING_EVIDENCE"
    assert refusal.risk is None
    assert refusal.detail == "invalid or mismatched planner evidence"


def test_venue_default_fee_identity_is_accepted() -> None:
    """A fee with no market ID is the venue default and remains valid for either market."""
    fees = _fees()
    fees[0] = fees[0].model_copy(update={"market_id": None})

    plan = _plan(fees=fees)

    assert plan.max_quantity == Decimal("5")
    assert HASH_F in plan.frozen_hashes


def test_risk_refusal_wraps_the_exact_gate_decision() -> None:
    """Planner risk refusal must preserve the independent gate's typed explanation."""
    portfolio = _portfolio(
        total_equity_usd=Decimal("100"),
        peak_equity_usd=Decimal("100"),
        equity_24h_ago_usd=Decimal("100"),
    )

    refusal = _plan(portfolio=portfolio)

    assert refusal.reason == "RISK_REFUSED"
    assert isinstance(refusal.risk, RiskGateDecision)
    assert refusal.risk.allowed is False
    assert refusal.risk.reason == "INCOMPLETE_LOSS_TOO_LARGE"
    assert refusal.detail


def test_event_cluster_falls_back_to_candidate_id_and_accepts_override() -> None:
    """Using the wrong cluster key can bypass or invent concentration exposure."""
    fallback_exposure = _portfolio(open_exposure_usd_by_cluster={str(CANDIDATE_ID): Decimal("997")})
    native_exposure = _portfolio(open_exposure_usd_by_cluster={"native-event": Decimal("997")})

    fallback_refusal = _plan(portfolio=fallback_exposure)
    override_plan = _plan(portfolio=fallback_exposure, event_cluster_id="native-event")
    override_refusal = _plan(portfolio=native_exposure, event_cluster_id="native-event")

    assert fallback_refusal.reason == "RISK_REFUSED"
    assert fallback_refusal.risk.reason == "CLUSTER_CONCENTRATION"
    assert override_plan.max_quantity == Decimal("5")
    assert override_refusal.reason == "RISK_REFUSED"
    assert override_refusal.risk.reason == "CLUSTER_CONCENTRATION"


def test_explicit_empty_event_cluster_is_not_replaced_by_fallback() -> None:
    """Only an absent cluster ID falls back; a supplied native identifier is authoritative."""
    portfolio = _portfolio(open_exposure_usd_by_cluster={"": Decimal("997")})

    refusal = _plan(portfolio=portfolio, event_cluster_id="")

    assert refusal.reason == "RISK_REFUSED"
    assert refusal.risk.reason == "CLUSTER_CONCENTRATION"


@pytest.mark.parametrize("method", ["model_copy", "model_construct"])
@pytest.mark.parametrize(
    "input_case",
    ("scan", "economics_policy", "risk_policy", "book", "portfolio"),
)
def test_unchecked_invalid_models_are_revalidated_and_safely_refused(
    method: str, input_case: str
) -> None:
    """Unchecked Pydantic construction must not bypass the planner's public trust boundary."""
    overrides: dict[str, object]
    if input_case == "scan":
        overrides = {"scan_report": _unchecked(_shadow_scan(), method, decision="NOT_A_DECISION")}
    elif input_case == "economics_policy":
        overrides = {
            "economics_policy": _unchecked(
                _economics_policy(), method, partial_fill_reserve_rate=Decimal("-1")
            )
        }
    elif input_case == "risk_policy":
        overrides = {
            "risk_policy": _unchecked(
                _risk_policy(), method, max_basket_fraction_of_equity=Decimal("0.50")
            )
        }
    elif input_case == "book":
        books = _books()
        books[0] = _unchecked(books[0], method, source_hash="invalid")
        overrides = {"books": books}
    else:
        overrides = {"portfolio": _unchecked(_portfolio(), method, total_equity_usd=Decimal("-1"))}

    refusal = _plan(**overrides)

    assert refusal.reason == "MISSING_EVIDENCE"
    assert refusal.risk is None
    assert refusal.detail == "invalid or mismatched planner evidence"


@pytest.mark.parametrize(
    "field",
    (
        "scan_report",
        "candidate",
        "proof",
        "books",
        "fees",
        "economics_policy",
        "risk_policy",
        "portfolio",
    ),
)
def test_arbitrary_non_model_inputs_are_safely_refused(field: str) -> None:
    """The trust boundary must reject, rather than coerce, arbitrary caller objects."""
    value: object = {0: "not-a-model"} if field in {"books", "fees"} else "not-a-model"

    refusal = _plan(**{field: value})

    assert refusal.reason == "MISSING_EVIDENCE"
    assert refusal.risk is None
    assert refusal.detail == "invalid or mismatched planner evidence"


def test_proposal_id_changes_with_every_persisted_plan_content_class() -> None:
    """Proposal identity must cover expiry, ladders, lineage hashes, policy, and sized quantity."""
    baseline = _plan()
    later_expiry = _plan(expiry_window_seconds=91)

    ladder_books = _books()
    ladder_books[0] = _book(
        leg_index=0,
        bids=ladder_books[0].bids,
        asks=(_level("0.21", "2"), _level("0.30", "6")),
        source_hash=HASH_D,
    )
    changed_ladder = _plan(books=ladder_books)

    hash_books = _books()
    hash_books[0] = hash_books[0].model_copy(update={"source_hash": "0" * 64})
    changed_hash = _plan(books=hash_books)
    changed_policy = _plan(
        economics_policy=_economics_policy(partial_fill_reserve_rate=Decimal("0.09"))
    )
    halved = _plan(
        portfolio=_portfolio(
            total_equity_usd=Decimal("9500"),
            peak_equity_usd=Decimal("10000"),
            equity_24h_ago_usd=Decimal("9500"),
        )
    )

    ids = {
        baseline.proposal_id,
        later_expiry.proposal_id,
        changed_ladder.proposal_id,
        changed_hash.proposal_id,
        changed_policy.proposal_id,
        halved.proposal_id,
    }
    assert len(ids) == 6
    assert _plan().proposal_id == baseline.proposal_id
