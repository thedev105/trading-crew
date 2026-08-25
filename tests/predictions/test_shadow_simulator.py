from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

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
from polytrading.predictions.risk import PredictionRiskPolicy, ShadowPortfolioState
from polytrading.predictions.shadow_models import ShadowPlan, ShadowState
from polytrading.predictions.shadow_planner import plan_shadow_proposal

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
CANDIDATE_ID = UUID("10000000-0000-0000-0000-000000000001")
PROOF_ID = UUID("20000000-0000-0000-0000-000000000001")
RULE_A = UUID("30000000-0000-0000-0000-000000000001")
RULE_B = UUID("30000000-0000-0000-0000-000000000002")
RULE_C = UUID("30000000-0000-0000-0000-000000000003")
CYCLE_ID = UUID("40000000-0000-0000-0000-000000000001")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
THREE_CANDIDATE_ID = UUID("10000000-0000-0000-0000-000000000003")
THREE_PROOF_ID = UUID("20000000-0000-0000-0000-000000000003")


def _simulator() -> object:
    return importlib.import_module("polytrading.predictions.shadow_simulator")


def _level(price: str, size: str) -> PredictionBookLevel:
    return PredictionBookLevel(price=Decimal(price), size=Decimal(size))


def _candidate() -> CandidateRelationship:
    return CandidateRelationship(
        schema_version=1,
        candidate_id=CANDIDATE_ID,
        trial_family_id="shadow-simulator-test",
        relationship_type=RelationshipType.BINARY_COMPLEMENT,
        legs=(
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
        information_cutoff=NOW,
        observed_at=NOW,
        provenance=DeterministicProvenance(
            kind="deterministic",
            generator="shadow-test",
            generator_version="1",
            code_revision="abc123",
        ),
        propositions=(),
        unresolved_fields=(),
        contradictions=(),
        invalidation_conditions=("any participating rule_version change",),
        review_status="reviewed",
        disposition=CandidateDisposition.PROOF_READY,
        superseded_by_candidate_id=None,
    )


def _proof() -> ProofArtifact:
    return ProofArtifact(
        schema_version=1,
        proof_id=PROOF_ID,
        candidate_id=CANDIDATE_ID,
        template="binary_complement@1",
        compiler_version="1",
        status="proof_ready",
        rejection_reason=None,
        terminal_states=(
            TerminalState(
                state_id="a-wins", description="A pays", leg_payouts=(Decimal("1"), Decimal("0"))
            ),
            TerminalState(
                state_id="b-wins", description="B pays", leg_payouts=(Decimal("0"), Decimal("1"))
            ),
        ),
        minimum_basket_payout=Decimal("1"),
        maximum_basket_payout=Decimal("1"),
        assumptions=(),
        excluded_states=(),
        equivalence_matrix=None,
        rule_version_ids=(RULE_A, RULE_B),
        source_hashes=(HASH_A, HASH_B),
        review_identity="reviewer@example.test",
        invalidation_conditions=("any participating rule_version change",),
        information_cutoff=NOW,
        observed_at=NOW,
    )


def _book(
    leg_index: int,
    *,
    bids: tuple[PredictionBookLevel, ...] | None = None,
    asks: tuple[PredictionBookLevel, ...] | None = None,
    source_hash: str | None = None,
    observed_at: datetime = NOW,
) -> PredictionBookSnapshot:
    suffix = "a" if leg_index == 0 else "b"
    defaults = {
        0: {
            "bids": (_level("0.15", "5"),),
            "asks": (_level("0.20", "2"), _level("0.30", "3")),
            "source_hash": HASH_C,
        },
        1: {
            "bids": (_level("0.35", "2"), _level("0.25", "3")),
            "asks": (_level("0.40", "1"), _level("0.50", "4")),
            "source_hash": HASH_D,
        },
    }[leg_index]
    return PredictionBookSnapshot(
        schema_version=1,
        cycle_id=CYCLE_ID,
        venue=PredictionVenue.POLYMARKET,
        market_id=f"market-{suffix}",
        outcome_token_id=f"token-{suffix}",
        bids=bids if bids is not None else defaults["bids"],
        asks=asks if asks is not None else defaults["asks"],
        sequence=str(leg_index),
        effective_at=observed_at,
        observed_at=observed_at,
        source_hash=source_hash if source_hash is not None else defaults["source_hash"],
    )


def _books() -> dict[int, PredictionBookSnapshot]:
    return {0: _book(0), 1: _book(1)}


def _fees() -> dict[int, PredictionFeeRate]:
    return {
        0: PredictionFeeRate(
            schema_version=1,
            venue=PredictionVenue.POLYMARKET,
            market_id="market-a",
            maker_rate=Decimal("0"),
            taker_rate=Decimal("0"),
            observed_at=NOW,
            source_hash=HASH_E,
        ),
        1: PredictionFeeRate(
            schema_version=1,
            venue=PredictionVenue.POLYMARKET,
            market_id="market-b",
            maker_rate=Decimal("0"),
            taker_rate=Decimal("0"),
            observed_at=NOW,
            source_hash=HASH_F,
        ),
    }


def _policy() -> PredictionEconomicsPolicy:
    return PredictionEconomicsPolicy(
        policy_id="simulator-test",
        policy_version="1",
        gas_conversion_redemption_reserve_usd=Decimal("0"),
        currency_basis_reserve_rate=Decimal("0"),
        transfer_cost_usd=Decimal("0"),
        capital_lockup_rate_per_day=Decimal("0"),
        assumed_capital_lock_days=Decimal("0"),
        operational_cost_usd=Decimal("0"),
        partial_fill_reserve_rate=Decimal("0"),
        latency_reserve_rate=Decimal("0"),
        dispute_delay_reserve_rate=Decimal("0"),
        venue_failure_reserve_rate=Decimal("0"),
        max_book_age_seconds=10,
    )


def _plan(
    *,
    planning_books: dict[int, PredictionBookSnapshot] | None = None,
    policy: PredictionEconomicsPolicy | None = None,
    expiry_window_seconds: int = 30,
) -> ShadowPlan:
    candidate = _candidate()
    proof = _proof()
    books = planning_books or _books()
    fees = _fees()
    policy = policy or _policy()
    economics = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )
    scan = ScanReport(
        report_id=deterministic_scan_report_id(
            candidate_id=CANDIDATE_ID,
            proof_id=PROOF_ID,
            decision="SHADOW_CANDIDATE",
            reason="positive",
            economics=economics,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            as_of=NOW,
        ),
        candidate_id=CANDIDATE_ID,
        proof_id=PROOF_ID,
        decision="SHADOW_CANDIDATE",
        reason="positive",
        economics=economics,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        as_of=NOW,
        observed_at=NOW,
    )
    plan = plan_shadow_proposal(
        scan_report=scan,
        candidate=candidate,
        proof=proof,
        books=books,
        fees=fees,
        economics_policy=policy,
        risk_policy=PredictionRiskPolicy(policy_version="risk-test"),
        portfolio=ShadowPortfolioState(
            total_equity_usd=Decimal("10000"),
            open_exposure_usd_by_cluster={},
            peak_equity_usd=Decimal("10000"),
            equity_24h_ago_usd=Decimal("10000"),
            open_proposal_count=0,
        ),
        as_of=NOW,
        expiry_window_seconds=expiry_window_seconds,
    )
    assert isinstance(plan, ShadowPlan)
    return plan


def _three_leg_inputs() -> tuple[
    ShadowPlan,
    ProofArtifact,
    CandidateRelationship,
    dict[int, PredictionFeeRate],
]:
    third_rule_hash = "3" * 64
    candidate_values = _candidate().model_dump()
    candidate_values.update(
        {
            "candidate_id": THREE_CANDIDATE_ID,
            "relationship_type": RelationshipType.EXHAUSTIVE_OUTCOME_SET,
            "legs": (
                *_candidate().legs,
                CandidateLeg(
                    venue=PredictionVenue.POLYMARKET,
                    market_id="market-c",
                    outcome_index=2,
                    outcome_token_id="token-c",
                    rule_version_id=RULE_C,
                    rule_source_hash=third_rule_hash,
                ),
            ),
        }
    )
    candidate = CandidateRelationship.model_validate(candidate_values)
    proof_values = _proof().model_dump()
    proof_values.update(
        {
            "proof_id": THREE_PROOF_ID,
            "candidate_id": THREE_CANDIDATE_ID,
            "template": "exhaustive_outcome_set@1",
            "terminal_states": (
                TerminalState(
                    state_id="a-wins",
                    description="A pays",
                    leg_payouts=(Decimal("1"), Decimal("0"), Decimal("0")),
                ),
                TerminalState(
                    state_id="b-wins",
                    description="B pays",
                    leg_payouts=(Decimal("0"), Decimal("1"), Decimal("0")),
                ),
                TerminalState(
                    state_id="c-wins",
                    description="C pays",
                    leg_payouts=(Decimal("0"), Decimal("0"), Decimal("1")),
                ),
            ),
            "rule_version_ids": (RULE_A, RULE_B, RULE_C),
            "source_hashes": (third_rule_hash, HASH_A, HASH_B),
        }
    )
    proof = ProofArtifact.model_validate(proof_values)
    books = {
        **_books(),
        2: PredictionBookSnapshot(
            schema_version=1,
            cycle_id=CYCLE_ID,
            venue=PredictionVenue.POLYMARKET,
            market_id="market-c",
            outcome_token_id="token-c",
            bids=(_level("0.05", "5"),),
            asks=(_level("0.10", "5"),),
            sequence="2",
            effective_at=NOW,
            observed_at=NOW,
            source_hash="7" * 64,
        ),
    }
    fees = {
        **_fees(),
        2: PredictionFeeRate(
            schema_version=1,
            venue=PredictionVenue.POLYMARKET,
            market_id="market-c",
            maker_rate=Decimal("0"),
            taker_rate=Decimal("0"),
            observed_at=NOW,
            source_hash="8" * 64,
        ),
    }
    policy = _policy()
    economics = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )
    scan = ScanReport(
        report_id=deterministic_scan_report_id(
            candidate_id=THREE_CANDIDATE_ID,
            proof_id=THREE_PROOF_ID,
            decision="SHADOW_CANDIDATE",
            reason="positive three-leg basket",
            economics=economics,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            as_of=NOW,
        ),
        candidate_id=THREE_CANDIDATE_ID,
        proof_id=THREE_PROOF_ID,
        decision="SHADOW_CANDIDATE",
        reason="positive three-leg basket",
        economics=economics,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        as_of=NOW,
        observed_at=NOW,
    )
    plan = plan_shadow_proposal(
        scan_report=scan,
        candidate=candidate,
        proof=proof,
        books=books,
        fees=fees,
        economics_policy=policy,
        risk_policy=PredictionRiskPolicy(policy_version="risk-test"),
        portfolio=ShadowPortfolioState(
            total_equity_usd=Decimal("10000"),
            open_exposure_usd_by_cluster={},
            peak_equity_usd=Decimal("10000"),
            equity_24h_ago_usd=Decimal("10000"),
            open_proposal_count=0,
        ),
        as_of=NOW,
        expiry_window_seconds=5,
    )
    assert isinstance(plan, ShadowPlan)
    return plan, proof, candidate, fees


def _simulate(
    *,
    plan: ShadowPlan | None = None,
    provider: object | None = None,
    scenario: object | None = None,
    started_at: datetime = NOW,
    proof: ProofArtifact | None = None,
    candidate: CandidateRelationship | None = None,
    fees: object | None = None,
    policy: PredictionEconomicsPolicy | None = None,
) -> tuple[object, ...]:
    module = _simulator()
    evidence = _books()
    provider = provider or (lambda leg_index, _at: evidence[leg_index])
    return module.simulate_shadow_proposal(
        plan or _plan(),
        proof=proof or _proof(),
        candidate=candidate or _candidate(),
        fees=_fees() if fees is None else fees,
        economics_policy=policy or _policy(),
        books=provider,
        scenario=scenario or module.BASELINE,
        started_at=started_at,
    )


def test_baseline_completes_with_hand_computed_structured_fills() -> None:
    """A simulator that skips or misprices a leg corrupts the append-only fill ledger."""
    events = _simulate()

    assert tuple(event.to_state for event in events) == (
        ShadowState.DISCOVERED,
        ShadowState.PROOF_VALIDATED,
        ShadowState.ECONOMICS_VALIDATED,
        ShadowState.SHADOW_PLANNED,
        ShadowState.FIRST_LEG_SIMULATED,
        ShadowState.COMPLETE,
    )
    assert events[4].quantity_filled == Decimal("5")
    assert tuple(fill.model_dump() for fill in events[4].fills) == (
        {
            "leg_index": 0,
            "side": "buy",
            "price_levels": ((Decimal("0.20"), Decimal("2")), (Decimal("0.30"), Decimal("3"))),
            "quantity": Decimal("5"),
        },
    )
    assert events[5].quantity_filled == Decimal("5")
    assert tuple(fill.model_dump() for fill in events[5].fills) == (
        {
            "leg_index": 1,
            "side": "buy",
            "price_levels": ((Decimal("0.40"), Decimal("1")), (Decimal("0.50"), Decimal("4"))),
            "quantity": Decimal("5"),
        },
    )


def test_latency_uses_event_time_schedule_and_caches_each_point_in_time_read() -> None:
    """Wrong time arithmetic or duplicate reads can make one replay observe different evidence."""
    module = _simulator()
    evidence = _books()
    calls: list[tuple[int, datetime]] = []

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot:
        calls.append((leg_index, at))
        return evidence[leg_index]

    events = _simulate(provider=provider, scenario=module.LATENCY_1S)

    assert calls == [(0, NOW + timedelta(seconds=1)), (1, NOW + timedelta(seconds=2))]
    assert events[4].occurred_at == NOW + timedelta(seconds=1)
    assert events[5].occurred_at == NOW + timedelta(seconds=2)


def test_partial_first_fill_rechecks_canonical_economics_and_completes_reduced_quantity() -> None:
    """Continuing at the stale planned size after a partial first fill creates naked exposure."""
    module = _simulator()
    evidence = _books()
    calls: list[tuple[int, datetime]] = []

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot:
        calls.append((leg_index, at))
        return evidence[leg_index]

    events = _simulate(provider=provider, scenario=module.PARTIAL_FILL_50)

    assert calls == [(0, NOW), (1, NOW)]
    assert events[4].quantity_filled == Decimal("2.5")
    assert events[4].fills[0].price_levels == (
        (Decimal("0.20"), Decimal("1.0")),
        (Decimal("0.30"), Decimal("1.5")),
    )
    assert events[5].to_state is ShadowState.COMPLETE
    assert events[5].quantity_filled == Decimal("2.5")
    assert events[5].fills[0].price_levels == (
        (Decimal("0.40"), Decimal("0.5")),
        (Decimal("0.50"), Decimal("2.0")),
    )


def test_second_leg_rejection_unwinds_first_leg_with_hand_checked_loss() -> None:
    """A known venue rejection must close confirmed exposure at evidenced bid prices."""
    module = _simulator()

    events = _simulate(scenario=module.SECOND_LEG_REJECT)

    assert events[-1].to_state is ShadowState.UNWOUND
    assert events[-1].leg_index == 1
    assert tuple(fill.model_dump() for fill in events[-1].fills) == (
        {
            "leg_index": 0,
            "side": "sell",
            "price_levels": ((Decimal("0.15"), Decimal("5")),),
            "quantity": Decimal("5"),
        },
    )
    # First-leg cost is 2*.20 + 3*.30 = 1.30; unwind proceeds are 5*.15 = .75.
    assert "0.55" in events[-1].detail


def test_short_later_fill_is_confirmed_then_every_acquired_leg_is_unwound() -> None:
    """Discarding a later partial buy or unwinding only the first leg understates exposure."""
    evidence = _books()
    evidence[1] = _book(1, asks=(_level("0.40", "1"),))

    events = _simulate(provider=lambda leg_index, _at: evidence[leg_index])

    assert events[-1].to_state is ShadowState.UNWOUND
    assert tuple((fill.leg_index, fill.side, fill.quantity) for fill in events[-1].fills) == (
        (1, "buy", Decimal("1")),
        (1, "sell", Decimal("1")),
        (0, "sell", Decimal("5")),
    )
    # Acquisitions 1.30 + .40, proceeds .35 + .75, so the realized loss is .60.
    assert "0.60" in events[-1].detail


def test_missing_unwind_book_yields_unknown_without_inventing_sell_fills() -> None:
    """An unavailable exit book cannot be represented as a confirmed unwind."""
    module = _simulator()
    evidence = _books()

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot | None:
        if leg_index == 0 and at == NOW + timedelta(seconds=2):
            return None
        return evidence[leg_index]

    events = _simulate(
        provider=provider,
        scenario=module.StressScenario(
            scenario_id="delayed-reject",
            latency_seconds=1,
            fill_fraction=Decimal("1"),
            failing_leg_index=1,
            unknown_after_leg=None,
        ),
    )

    assert events[-1].to_state is ShadowState.UNKNOWN
    assert events[-1].fills == ()


def test_insufficient_unwind_depth_yields_unknown_with_only_confirmed_partial_sell() -> None:
    """An incomplete exit is UNKNOWN and records only the quantity actually sold."""
    module = _simulator()
    evidence = _books()
    shallow_exit = _book(0, bids=(_level("0.15", "1"),))

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot:
        if leg_index == 0 and at == NOW + timedelta(seconds=2):
            return shallow_exit
        return evidence[leg_index]

    scenario = module.StressScenario(
        scenario_id="shallow-exit",
        latency_seconds=1,
        fill_fraction=Decimal("1"),
        failing_leg_index=1,
        unknown_after_leg=None,
    )
    events = _simulate(provider=provider, scenario=scenario)

    assert events[-1].to_state is ShadowState.UNKNOWN
    assert tuple((fill.leg_index, fill.side, fill.quantity) for fill in events[-1].fills) == (
        (0, "sell", Decimal("1")),
    )


def test_unknown_after_first_halts_before_any_unconfirmed_fill_or_book_read() -> None:
    """Unknown acknowledgement state must halt without manufacturing execution evidence."""
    module = _simulator()
    calls: list[tuple[int, datetime]] = []

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot:
        calls.append((leg_index, at))
        return _books()[leg_index]

    events = _simulate(provider=provider, scenario=module.UNKNOWN_AFTER_FIRST)

    assert calls == []
    assert events[4].to_state is ShadowState.FIRST_LEG_SIMULATED
    assert events[4].quantity_filled is None
    assert events[4].fills == ()
    assert events[5].to_state is ShadowState.UNKNOWN
    assert events[5].fills == ()


def test_provider_gap_on_first_attempt_preserves_legal_chain_and_yields_unknown() -> None:
    """Missing point-in-time evidence is uncertainty, not a skipped state or known no-fill."""
    events = _simulate(provider=lambda _leg_index, _at: None)

    assert tuple(event.to_state for event in events[-2:]) == (
        ShadowState.FIRST_LEG_SIMULATED,
        ShadowState.UNKNOWN,
    )
    assert events[-2].fills == ()
    assert events[-1].fills == ()


def test_expiry_before_first_fill_emits_first_attempt_then_expired_without_reading() -> None:
    """A latency-stressed order must not consult or fill evidence after its frozen expiry."""
    module = _simulator()
    calls: list[tuple[int, datetime]] = []
    plan = _plan(expiry_window_seconds=1)

    events = _simulate(
        plan=plan,
        provider=lambda leg_index, at: calls.append((leg_index, at)),
        scenario=module.LATENCY_5S,
    )

    assert calls == []
    assert events[-2].to_state is ShadowState.FIRST_LEG_SIMULATED
    assert events[-1].to_state is ShadowState.EXPIRED
    assert events[-1].occurred_at == NOW + timedelta(seconds=5)


def test_nonpositive_partial_continuation_economics_unwinds_instead_of_averaging_in() -> None:
    """A reduced basket made negative by fixed costs must not submit its remaining legs."""
    module = _simulator()
    policy = _policy().model_copy(update={"operational_cost_usd": Decimal("0.80")})
    plan = _plan(policy=policy)

    events = _simulate(plan=plan, policy=policy, scenario=module.PARTIAL_FILL_50)

    assert events[-1].to_state is ShadowState.UNWOUND
    assert tuple((fill.leg_index, fill.side, fill.quantity) for fill in events[-1].fills) == (
        (0, "sell", Decimal("2.5")),
    )
    assert "0.275" in events[-1].detail


def test_unknown_on_later_submission_halts_without_reading_or_filling_that_leg() -> None:
    """An UNKNOWN later order cannot be treated as either cancelled or filled."""
    module = _simulator()
    evidence = _books()
    calls: list[tuple[int, datetime]] = []

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot:
        calls.append((leg_index, at))
        return evidence[leg_index]

    scenario = module.StressScenario(
        scenario_id="unknown-second",
        latency_seconds=1,
        fill_fraction=Decimal("1"),
        failing_leg_index=None,
        unknown_after_leg=1,
    )
    events = _simulate(provider=provider, scenario=scenario)

    assert calls == [(0, NOW + timedelta(seconds=1))]
    assert events[-1].to_state is ShadowState.UNKNOWN
    assert events[-1].leg_index == 1
    assert events[-1].fills == ()


def test_provider_gap_on_later_leg_yields_unknown_and_preserves_first_fill() -> None:
    """A missing later snapshot cannot erase the separately confirmed first acquisition."""
    evidence = _books()

    events = _simulate(
        provider=lambda leg_index, _at: evidence[leg_index] if leg_index == 0 else None
    )

    assert events[4].fills[0].leg_index == 0
    assert events[-1].to_state is ShadowState.UNKNOWN
    assert events[-1].fills == ()


def test_expiry_before_later_fill_stops_without_reading_the_expired_leg() -> None:
    """The first confirmed fill must not authorize a later submission beyond plan expiry."""
    module = _simulator()
    evidence = _books()
    calls: list[tuple[int, datetime]] = []

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot:
        calls.append((leg_index, at))
        return evidence[leg_index]

    events = _simulate(
        plan=_plan(expiry_window_seconds=7),
        provider=provider,
        scenario=module.LATENCY_5S,
    )

    assert calls == [(0, NOW + timedelta(seconds=5))]
    assert events[-1].to_state is ShadowState.EXPIRED
    assert events[-1].leg_index == 1


def test_five_second_latency_completes_at_the_second_fill_time_when_not_expired() -> None:
    """Latency must shift each fill read without changing an otherwise executable result."""
    module = _simulator()
    events = _simulate(scenario=module.LATENCY_5S)

    assert events[-1].to_state is ShadowState.COMPLETE
    assert events[4].occurred_at == NOW + timedelta(seconds=5)
    assert events[5].occurred_at == NOW + timedelta(seconds=10)


def test_frozen_limit_tiers_do_not_double_count_current_depth() -> None:
    """Reusing the same current ask in multiple frozen tiers can overfill the basket."""
    evidence = _books()
    evidence[0] = _book(
        0,
        asks=(_level("0.19", "3"), _level("0.29", "5")),
    )

    events = _simulate(provider=lambda leg_index, _at: evidence[leg_index])

    assert events[4].fills[0].price_levels == (
        (Decimal("0.19"), Decimal("3")),
        (Decimal("0.29"), Decimal("2")),
    )
    assert events[4].fills[0].quantity == Decimal("5")


def test_identical_inputs_produce_identical_event_ids_and_content() -> None:
    """Wall-clock or random identity use would make evidence replay non-idempotent."""
    first = _simulate()
    second = _simulate()

    assert first == second
    assert tuple(event.event_id for event in first) == tuple(event.event_id for event in second)
    assert len({event.event_id for event in first}) == len(first)


@pytest.mark.parametrize(
    "field,value",
    (
        ("scenario_id", ""),
        ("latency_seconds", -1),
        ("fill_fraction", Decimal("0")),
        ("fill_fraction", Decimal("1.01")),
        ("failing_leg_index", -1),
        ("unknown_after_leg", -1),
    ),
)
def test_stress_scenario_rejects_invalid_boundaries(field: str, value: object) -> None:
    """Invalid stress inputs must not silently weaken or corrupt a replay."""
    module = _simulator()
    values: dict[str, object] = {
        "scenario_id": "valid",
        "latency_seconds": 0,
        "fill_fraction": Decimal("1"),
        "failing_leg_index": None,
        "unknown_after_leg": None,
    }
    values[field] = value

    with pytest.raises(ValidationError):
        module.StressScenario(**values)


@pytest.mark.parametrize("position_field", ("failing_leg_index", "unknown_after_leg"))
def test_scenario_position_must_identify_a_planned_leg(position_field: str) -> None:
    """An out-of-range failure selector is a malformed replay request, not baseline behavior."""
    module = _simulator()
    values: dict[str, object] = {
        "scenario_id": "bad-position",
        "latency_seconds": 0,
        "fill_fraction": Decimal("1"),
        "failing_leg_index": None,
        "unknown_after_leg": None,
    }
    values[position_field] = 2

    with pytest.raises(ValueError, match="positions"):
        _simulate(scenario=module.StressScenario(**values))


@pytest.mark.parametrize(
    "case",
    (
        "unchecked_plan",
        "unchecked_scenario",
        "proof_identity",
        "candidate_lineage",
        "fee_lineage",
        "extra_fee",
        "policy_lineage",
    ),
)
def test_simulator_revalidates_and_hash_checks_all_frozen_inputs(case: str) -> None:
    """Unchecked or non-frozen caller evidence must fail before any simulated submission."""
    module = _simulator()
    plan = _plan()
    scenario: Any = module.BASELINE
    proof = _proof()
    candidate = _candidate()
    fees: Any = _fees()
    policy = _policy()

    if case == "unchecked_plan":
        bad_leg = plan.legs[0].model_copy(update={"max_quantity": Decimal("0")})
        plan = plan.model_copy(update={"legs": (bad_leg, plan.legs[1])})
    elif case == "unchecked_scenario":
        scenario = module.BASELINE.model_copy(update={"fill_fraction": Decimal("0")})
    elif case == "proof_identity":
        proof = proof.model_copy(update={"proof_id": UUID("90000000-0000-0000-0000-000000000001")})
    elif case == "candidate_lineage":
        changed_leg = candidate.legs[0].model_copy(update={"rule_source_hash": "9" * 64})
        candidate = candidate.model_copy(update={"legs": (changed_leg, candidate.legs[1])})
    elif case == "fee_lineage":
        fees[0] = fees[0].model_copy(update={"source_hash": "9" * 64})
    elif case == "extra_fee":
        fees[2] = fees[0]
    else:
        policy = policy.model_copy(update={"operational_cost_usd": Decimal("0.01")})

    with pytest.raises((TypeError, ValueError, ValidationError)):
        _simulate(
            plan=plan,
            scenario=scenario,
            proof=proof,
            candidate=candidate,
            fees=fees,
            policy=policy,
        )


def test_provider_book_is_revalidated_and_must_be_point_in_time() -> None:
    """Malformed or future runtime evidence must never become a confirmed fill."""
    bad_books = (
        _book(0).model_copy(update={"asks": ()}),
        _book(0, observed_at=NOW + timedelta(seconds=1)),
    )

    for bad_book in bad_books:
        with pytest.raises((ValueError, ValidationError)):
            _simulate(
                provider=lambda leg_index, _at, bad=bad_book: bad if leg_index == 0 else _book(1)
            )


def test_stale_provider_book_yields_unknown_instead_of_a_fill() -> None:
    """A frozen hash does not make old evidence executable beyond the policy age limit."""
    events = _simulate(started_at=NOW + timedelta(seconds=11))

    assert events[-2].fills == ()
    assert events[-1].to_state is ShadowState.UNKNOWN


def test_first_position_rejection_preserves_legal_chain_without_exposure() -> None:
    """A first-order rejection is a known no-fill, not a fabricated acquisition or UNKNOWN."""
    module = _simulator()
    scenario = module.StressScenario(
        scenario_id="first-reject",
        latency_seconds=0,
        fill_fraction=Decimal("1"),
        failing_leg_index=0,
        unknown_after_leg=None,
    )
    calls: list[tuple[int, datetime]] = []

    events = _simulate(
        scenario=scenario,
        provider=lambda leg_index, at: calls.append((leg_index, at)),
    )

    assert calls == []
    assert events[-2].to_state is ShadowState.FIRST_LEG_SIMULATED
    assert events[-2].fills == ()
    assert events[-1].to_state is ShadowState.UNWOUND
    assert events[-1].fills == ()


def test_simulation_cannot_start_before_the_frozen_information_cutoff() -> None:
    """Starting before planning would make the append-only event timestamps run backward."""
    with pytest.raises(ValueError, match="information cutoff"):
        _simulate(started_at=NOW - timedelta(microseconds=1))


def test_changed_event_time_books_are_accepted_and_attributed_to_exact_events() -> None:
    """Latency snapshots belong to event lineage and cannot be required on the earlier plan."""
    module = _simulator()
    first_hash = "1" * 64
    second_hash = "2" * 64
    runtime = {
        (0, NOW + timedelta(seconds=1)): _book(
            0,
            asks=(_level("0.19", "2"), _level("0.29", "3")),
            source_hash=first_hash,
            observed_at=NOW + timedelta(seconds=1),
        ),
        (1, NOW + timedelta(seconds=2)): _book(
            1,
            asks=(_level("0.39", "1"), _level("0.49", "4")),
            source_hash=second_hash,
            observed_at=NOW + timedelta(seconds=2),
        ),
    }

    events = _simulate(
        provider=lambda leg_index, at: runtime[(leg_index, at)], scenario=module.LATENCY_1S
    )

    assert tuple(event.evidence_hashes for event in events[:4]) == ((), (), (), ())
    assert events[4].evidence_hashes == (first_hash,)
    assert events[5].evidence_hashes == (second_hash,)
    assert events[4].fills[0].price_levels[0][0] == Decimal("0.19")
    assert events[5].fills[0].price_levels[0][0] == Decimal("0.39")


def test_five_second_runtime_books_may_change_content_and_lineage_after_planning() -> None:
    """The five-second stress replay must cite its actual snapshots, not planning-time hashes."""
    module = _simulator()
    runtime = {
        0: _book(
            0,
            asks=(_level("0.18", "2"), _level("0.28", "3")),
            source_hash="4" * 64,
            observed_at=NOW + timedelta(seconds=5),
        ),
        1: _book(
            1,
            asks=(_level("0.38", "1"), _level("0.48", "4")),
            source_hash="5" * 64,
            observed_at=NOW + timedelta(seconds=10),
        ),
    }

    events = _simulate(
        provider=lambda leg_index, _at: runtime[leg_index],
        scenario=module.LATENCY_5S,
    )

    assert events[-1].to_state is ShadowState.COMPLETE
    assert events[4].evidence_hashes == ("4" * 64,)
    assert events[5].evidence_hashes == ("5" * 64,)
    assert events[4].fills[0].price_levels[0][0] == Decimal("0.18")
    assert events[5].fills[0].price_levels[0][0] == Decimal("0.38")


def test_partial_prefetch_cache_preserves_terminal_runtime_hash_once() -> None:
    """A prefetched continuation book must retain lineage when its cached copy later fills."""
    module = _simulator()
    first_hash = "1" * 64
    second_hash = "2" * 64
    runtime = {
        0: _book(0, source_hash=first_hash),
        1: _book(1, source_hash=second_hash),
    }
    calls: list[tuple[int, datetime]] = []

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot:
        calls.append((leg_index, at))
        return runtime[leg_index]

    events = _simulate(provider=provider, scenario=module.PARTIAL_FILL_50)

    assert calls == [(0, NOW), (1, NOW)]
    assert events[4].evidence_hashes == (first_hash,)
    assert events[5].evidence_hashes == (second_hash,)


def test_unwind_and_unknown_terminal_retain_every_successful_runtime_read_hash() -> None:
    """Cached or pre-failure books must not disappear when a later unwind read is missing."""
    first_hash = "1" * 64
    later_hash = "2" * 64
    first = _book(0, source_hash=first_hash, observed_at=NOW + timedelta(seconds=1))
    short_later = _book(
        1,
        asks=(_level("0.40", "1"),),
        source_hash=later_hash,
        observed_at=NOW + timedelta(seconds=2),
    )
    module = _simulator()

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot | None:
        if (leg_index, at) == (0, NOW + timedelta(seconds=1)):
            return first
        if (leg_index, at) == (1, NOW + timedelta(seconds=2)):
            return short_later
        return None

    events = _simulate(provider=provider, scenario=module.LATENCY_1S)

    assert events[4].evidence_hashes == (first_hash,)
    assert events[5].to_state is ShadowState.UNKNOWN
    assert events[5].evidence_hashes == (later_hash,)


def test_successful_unwind_cites_fill_cache_and_exit_books_sorted_once() -> None:
    """Terminal unwind lineage includes both the short fill and every point-in-time exit book."""
    module = _simulator()
    first = _book(0, source_hash="1" * 64, observed_at=NOW + timedelta(seconds=1))
    short_later = _book(
        1,
        asks=(_level("0.40", "1"),),
        source_hash="2" * 64,
        observed_at=NOW + timedelta(seconds=2),
    )
    first_exit = _book(0, source_hash="3" * 64, observed_at=NOW + timedelta(seconds=2))

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot:
        if (leg_index, at) == (0, NOW + timedelta(seconds=1)):
            return first
        if leg_index == 1:
            return short_later
        return first_exit

    events = _simulate(provider=provider, scenario=module.LATENCY_1S)

    assert events[-1].to_state is ShadowState.UNWOUND
    assert events[4].evidence_hashes == ("1" * 64,)
    assert events[5].evidence_hashes == ("2" * 64, "3" * 64)


def test_event_identity_changes_when_only_runtime_evidence_hash_changes() -> None:
    """Runtime evidence lineage must participate in deterministic event UUID content."""
    module = _simulator()
    second = _book(1, source_hash="2" * 64, observed_at=NOW + timedelta(seconds=2))

    def run(first_hash: str) -> tuple[object, ...]:
        first = _book(0, source_hash=first_hash, observed_at=NOW + timedelta(seconds=1))
        return _simulate(
            provider=lambda leg_index, _at: first if leg_index == 0 else second,
            scenario=module.LATENCY_1S,
        )

    first = run("1" * 64)
    changed = run("3" * 64)

    assert first[4].fills == changed[4].fills
    assert first[4].event_id != changed[4].event_id
    assert first[5].event_id == changed[5].event_id


@pytest.mark.parametrize(
    (
        "expiry_seconds",
        "started_offset",
        "unknown_position",
        "expected_state",
        "expected_offset",
    ),
    (
        (1, 2, 0, ShadowState.EXPIRED, 2),
        (1, 0, 0, ShadowState.UNKNOWN, 0),
        (1, 1, 0, ShadowState.UNKNOWN, 1),
        (5, 0, 1, ShadowState.UNKNOWN, 5),
        (6, 0, 1, ShadowState.UNKNOWN, 5),
    ),
)
def test_expiry_and_unknown_precedence_follows_submit_then_confirmation_time(
    expiry_seconds: int,
    started_offset: int,
    unknown_position: int,
    expected_state: ShadowState,
    expected_offset: int,
) -> None:
    """UNKNOWN cannot outrank an already-expired submit or lose to a later fill expiry."""
    module = _simulator()
    plan = _plan().model_copy(update={"expires_at": NOW + timedelta(seconds=expiry_seconds)})
    scenario = module.StressScenario(
        scenario_id="combined-expiry-unknown",
        latency_seconds=5,
        fill_fraction=Decimal("1"),
        failing_leg_index=None,
        unknown_after_leg=unknown_position,
    )
    calls: list[tuple[int, datetime]] = []
    runtime = {
        0: _book(0, observed_at=NOW + timedelta(seconds=5)),
        1: _book(1, observed_at=NOW + timedelta(seconds=10)),
    }

    events = _simulate(
        plan=plan,
        scenario=scenario,
        started_at=NOW + timedelta(seconds=started_offset),
        provider=lambda leg_index, at: calls.append((leg_index, at)) or runtime[leg_index],
    )

    assert events[-1].to_state is expected_state
    assert events[-1].occurred_at == NOW + timedelta(seconds=expected_offset)
    if unknown_position == 0:
        assert calls == []
    else:
        assert calls == [(0, NOW + timedelta(seconds=5))]


def test_unknown_precedence_uses_sequence_position_after_plan_reordering() -> None:
    """Scenario position must follow frozen execution order rather than candidate leg index."""
    module = _simulator()
    original = _plan(expiry_window_seconds=5)
    reordered = original.model_copy(
        update={
            "legs": (
                original.legs[0].model_copy(update={"sequence_position": 1}),
                original.legs[1].model_copy(update={"sequence_position": 0}),
            )
        }
    )
    scenario = module.StressScenario(
        scenario_id="reordered-boundary",
        latency_seconds=5,
        fill_fraction=Decimal("1"),
        failing_leg_index=None,
        unknown_after_leg=1,
    )
    calls: list[tuple[int, datetime]] = []
    runtime_first = _book(1, observed_at=NOW + timedelta(seconds=5))

    events = _simulate(
        plan=reordered,
        scenario=scenario,
        provider=lambda leg_index, at: calls.append((leg_index, at)) or runtime_first,
    )

    assert calls == [(1, NOW + timedelta(seconds=5))]
    assert events[4].leg_index == 1
    assert events[-1].to_state is ShadowState.UNKNOWN
    assert events[-1].leg_index == 0
    assert events[-1].occurred_at == reordered.expires_at


def test_three_leg_unknown_at_submit_precedes_later_fill_expiry_and_halts_reads() -> None:
    """Chronological precedence must hold beyond two legs without reading past UNKNOWN."""
    module = _simulator()
    plan, proof, candidate, fees = _three_leg_inputs()
    scenario = module.StressScenario(
        scenario_id="three-leg-boundary",
        latency_seconds=2,
        fill_fraction=Decimal("1"),
        failing_leg_index=None,
        unknown_after_leg=2,
    )
    calls: list[tuple[int, datetime]] = []
    runtime = {
        0: _book(0, source_hash="4" * 64, observed_at=NOW + timedelta(seconds=2)),
        1: _book(1, source_hash="5" * 64, observed_at=NOW + timedelta(seconds=4)),
    }

    def provider(leg_index: int, at: datetime) -> PredictionBookSnapshot:
        calls.append((leg_index, at))
        return runtime[leg_index]

    events = module.simulate_shadow_proposal(
        plan,
        proof=proof,
        candidate=candidate,
        fees=fees,
        economics_policy=_policy(),
        books=provider,
        scenario=scenario,
        started_at=NOW,
    )

    assert calls == [
        (0, NOW + timedelta(seconds=2)),
        (1, NOW + timedelta(seconds=4)),
    ]
    assert events[-1].to_state is ShadowState.UNKNOWN
    assert events[-1].leg_index == 2
    assert events[-1].occurred_at == NOW + timedelta(seconds=4)
    assert events[4].evidence_hashes == ("4" * 64,)
    assert events[5].evidence_hashes == ("5" * 64,)
