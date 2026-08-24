from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from polytrading.predictions.candidates_models import RelationshipType
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.economics import evaluate_basket_economics
from polytrading.predictions.economics_models import PredictionEconomicsPolicy
from tests.predictions.candidate_helpers import candidate_relationship, leg
from tests.predictions.domain_helpers import fee_rate, level, prediction_book_snapshot
from tests.predictions.proof_helpers import proof_artifact

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)


def _policy(**overrides: object) -> PredictionEconomicsPolicy:
    values: dict[str, object] = {
        "policy_id": "test-policy",
        "policy_version": "1",
        "gas_conversion_redemption_reserve_usd": Decimal("1.00"),
        "currency_basis_reserve_rate": Decimal("0.001"),
        "transfer_cost_usd": Decimal("0.50"),
        "capital_lockup_rate_per_day": Decimal("0.0001"),
        "assumed_capital_lock_days": Decimal("3"),
        "operational_cost_usd": Decimal("0.25"),
        "partial_fill_reserve_rate": Decimal("0.01"),
        "latency_reserve_rate": Decimal("0.005"),
        "dispute_delay_reserve_rate": Decimal("0.005"),
        "venue_failure_reserve_rate": Decimal("0.0025"),
        "max_book_age_seconds": 60,
    }
    values.update(overrides)
    return PredictionEconomicsPolicy(**values)


def _candidate(**overrides: object):
    values: dict[str, object] = {
        "legs": (
            leg(venue=PredictionVenue.POLYMARKET, outcome_index=0, outcome_token_id="111"),
            leg(venue=PredictionVenue.POLYMARKET, outcome_index=1, outcome_token_id="222"),
        ),
    }
    values.update(overrides)
    return candidate_relationship(**values)


def _proof(**overrides: object):
    values: dict[str, object] = {
        "minimum_basket_payout": Decimal("1.05"),
        "maximum_basket_payout": Decimal("1.05"),
    }
    values.update(overrides)
    return proof_artifact(**values)


def _leg0_book(**overrides: object):
    values: dict[str, object] = {
        "outcome_token_id": "111",
        "bids": (level("0.35", "10"),),
        "asks": (level("0.40", "100"), level("0.45", "50")),
        "observed_at": NOW,
    }
    values.update(overrides)
    return prediction_book_snapshot(**values)


def _leg1_book(**overrides: object):
    values: dict[str, object] = {
        "outcome_token_id": "222",
        "bids": (level("0.50", "10"),),
        "asks": (level("0.55", "60"), level("0.60", "80")),
        "observed_at": NOW,
    }
    values.update(overrides)
    return prediction_book_snapshot(**values)


class _StubLevel:
    def __init__(self, price: Decimal, size: Decimal) -> None:
        self.price = price
        self.size = size


class _StubBook:
    def __init__(self, bids: tuple, asks: tuple, observed_at: datetime) -> None:
        self.bids = bids
        self.asks = asks
        self.observed_at = observed_at


def test_hand_computed_two_leg_multi_level_depth_walk_surplus_to_the_cent() -> None:
    candidate = _candidate()
    proof = _proof()
    policy = _policy()
    books = {0: _leg0_book(), 1: _leg1_book()}
    fees = {
        0: fee_rate(venue=PredictionVenue.POLYMARKET, taker_rate=Decimal("0.01")),
        1: fee_rate(venue=PredictionVenue.POLYMARKET, taker_rate=Decimal("0.02")),
    }

    result = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result.status == "evaluated"
    assert result.insufficiency_reason is None

    # Bottleneck: leg0 total ask depth = 150, leg1 total ask depth = 140 -> q = 140.
    assert result.quantity == Decimal("140")

    leg0_plan, leg1_plan = result.leg_plans
    assert leg0_plan.leg_index == 0
    assert leg0_plan.depth_walked_levels == (
        (Decimal("0.40"), Decimal("100")),
        (Decimal("0.45"), Decimal("40")),
    )
    assert leg0_plan.filled_quantity == Decimal("140")
    # 100*0.40 + 40*0.45 = 40.00 + 18.00 = 58.00
    assert leg0_plan.acquisition_cost_usd == Decimal("58.00")

    assert leg1_plan.leg_index == 1
    assert leg1_plan.depth_walked_levels == (
        (Decimal("0.55"), Decimal("60")),
        (Decimal("0.60"), Decimal("80")),
    )
    assert leg1_plan.filled_quantity == Decimal("140")
    # 60*0.55 + 80*0.60 = 33.00 + 48.00 = 81.00
    assert leg1_plan.acquisition_cost_usd == Decimal("81.00")

    acquisition_total = Decimal("58.00") + Decimal("81.00")
    assert acquisition_total == Decimal("139.00")

    # fees: 58.00*0.01 + 81.00*0.02 = 0.58 + 1.62 = 2.20
    fee_total = Decimal("58.00") * Decimal("0.01") + Decimal("81.00") * Decimal("0.02")
    assert fee_total == Decimal("2.2000")

    currency_basis_reserve = acquisition_total * policy.currency_basis_reserve_rate
    capital_lockup_reserve = (
        acquisition_total * policy.capital_lockup_rate_per_day * policy.assumed_capital_lock_days
    )
    expected_all_in_cost = (
        acquisition_total
        + fee_total
        + policy.gas_conversion_redemption_reserve_usd
        + currency_basis_reserve
        + policy.transfer_cost_usd
        + capital_lockup_reserve
        + policy.operational_cost_usd
    )
    assert expected_all_in_cost == Decimal("143.1307")
    assert result.all_in_cost_usd == expected_all_in_cost

    expected_failure_reserve = acquisition_total * (
        policy.partial_fill_reserve_rate
        + policy.latency_reserve_rate
        + policy.dispute_delay_reserve_rate
        + policy.venue_failure_reserve_rate
    )
    assert expected_failure_reserve == Decimal("3.1275")
    assert result.failure_reserve_usd == expected_failure_reserve

    expected_proven_floor = Decimal("140") * Decimal("1.05")
    assert expected_proven_floor == Decimal("147.00")
    assert result.proven_floor_usd == expected_proven_floor

    expected_surplus = expected_proven_floor - expected_all_in_cost - expected_failure_reserve
    assert expected_surplus == Decimal("0.7418")
    assert result.conservative_surplus_usd == expected_surplus

    expected_roac = expected_surplus / expected_all_in_cost
    assert result.return_on_assigned_capital == expected_roac

    assert result.capacity_usd_at_current_depth == acquisition_total

    assert result.stranded_collateral_by_venue == {"polymarket": acquisition_total}

    assert result.max_capital_lock_days == policy.assumed_capital_lock_days

    expected_doubled_surplus = (
        expected_proven_floor - 2 * expected_all_in_cost - 2 * expected_failure_reserve
    )
    assert expected_doubled_surplus == Decimal("-145.5164")
    assert result.doubled_cost_surplus_usd == expected_doubled_surplus
    assert result.doubled_cost_surplus_usd < result.conservative_surplus_usd


def test_doubled_costs_are_strictly_lower_than_single_costs() -> None:
    candidate = _candidate()
    proof = _proof()
    policy = _policy()
    books = {0: _leg0_book(), 1: _leg1_book()}
    fees = {
        0: fee_rate(venue=PredictionVenue.POLYMARKET, taker_rate=Decimal("0.01")),
        1: fee_rate(venue=PredictionVenue.POLYMARKET, taker_rate=Decimal("0.02")),
    }

    result = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result.doubled_cost_surplus_usd < result.conservative_surplus_usd


def test_determinism_same_inputs_produce_identical_results() -> None:
    candidate = _candidate()
    proof = _proof()
    policy = _policy()
    books = {0: _leg0_book(), 1: _leg1_book()}
    fees = {
        0: fee_rate(venue=PredictionVenue.POLYMARKET, taker_rate=Decimal("0.01")),
        1: fee_rate(venue=PredictionVenue.POLYMARKET, taker_rate=Decimal("0.02")),
    }

    result_a = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )
    result_b = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result_a == result_b


def test_stranded_collateral_by_venue_groups_across_distinct_venues() -> None:
    candidate = _candidate(
        relationship_type=RelationshipType.CROSS_VENUE_EQUIVALENCE,
        legs=(
            leg(venue=PredictionVenue.POLYMARKET, outcome_index=None, outcome_token_id="111"),
            leg(venue=PredictionVenue.KALSHI, outcome_index=None, outcome_token_id="222"),
        ),
    )
    proof = _proof()
    policy = _policy()
    books = {
        0: prediction_book_snapshot(
            venue=PredictionVenue.POLYMARKET,
            outcome_token_id="111",
            bids=(level("0.35", "10"),),
            asks=(level("0.40", "10"),),
            observed_at=NOW,
        ),
        1: prediction_book_snapshot(
            venue=PredictionVenue.KALSHI,
            outcome_token_id="222",
            bids=(level("0.50", "10"),),
            asks=(level("0.55", "10"),),
            observed_at=NOW,
        ),
    }
    fees = {
        0: fee_rate(venue=PredictionVenue.POLYMARKET, taker_rate=Decimal("0.01")),
        1: fee_rate(venue=PredictionVenue.KALSHI, taker_rate=Decimal("0.01")),
    }

    result = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result.status == "evaluated"
    assert result.quantity == Decimal("10")
    assert result.stranded_collateral_by_venue == {
        "polymarket": Decimal("4.00"),
        "kalshi": Decimal("5.50"),
    }


def test_missing_book_is_insufficient_evidence() -> None:
    candidate = _candidate()
    proof = _proof()
    policy = _policy()
    books = {0: _leg0_book()}  # leg 1's book missing
    fees = {
        0: fee_rate(taker_rate=Decimal("0.01")),
        1: fee_rate(taker_rate=Decimal("0.02")),
    }

    result = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result.status == "insufficient_evidence"
    assert result.insufficiency_reason == "MISSING_BOOK"
    assert result.quantity == Decimal("0")
    assert result.leg_plans == ()
    assert result.conservative_surplus_usd == Decimal("0")
    assert result.stranded_collateral_by_venue == {}


def test_explicit_none_book_is_also_missing_book() -> None:
    candidate = _candidate()
    proof = _proof()
    policy = _policy()
    books = {0: _leg0_book(), 1: None}
    fees = {
        0: fee_rate(taker_rate=Decimal("0.01")),
        1: fee_rate(taker_rate=Decimal("0.02")),
    }

    result = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result.status == "insufficient_evidence"
    assert result.insufficiency_reason == "MISSING_BOOK"


def test_stale_book_is_insufficient_evidence() -> None:
    candidate = _candidate()
    proof = _proof()
    policy = _policy(max_book_age_seconds=5)
    stale_observed_at = NOW - timedelta(seconds=6)
    books = {0: _leg0_book(), 1: _leg1_book(observed_at=stale_observed_at)}
    fees = {
        0: fee_rate(taker_rate=Decimal("0.01")),
        1: fee_rate(taker_rate=Decimal("0.02")),
    }

    result = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result.status == "insufficient_evidence"
    assert result.insufficiency_reason == "STALE_BOOK"


def test_book_exactly_at_max_age_is_not_stale() -> None:
    candidate = _candidate()
    proof = _proof()
    policy = _policy(max_book_age_seconds=5)
    edge_observed_at = NOW - timedelta(seconds=5)
    books = {0: _leg0_book(), 1: _leg1_book(observed_at=edge_observed_at)}
    fees = {
        0: fee_rate(taker_rate=Decimal("0.01")),
        1: fee_rate(taker_rate=Decimal("0.02")),
    }

    result = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result.status == "evaluated"


def test_crossed_book_is_insufficient_evidence_via_duck_typed_stub() -> None:
    """A real stored PredictionBookSnapshot can never be crossed (domain validator
    rejects it at construction -- see
    test_domain_validator_rejects_crossed_book_at_construction below), so this
    exercises the CROSSED_BOOK branch via a duck-typed stub object rather than a
    genuine stored snapshot.
    """
    candidate = _candidate()
    proof = _proof()
    policy = _policy()
    crossed_stub = _StubBook(
        bids=(_StubLevel(Decimal("0.60"), Decimal("10")),),
        asks=(_StubLevel(Decimal("0.55"), Decimal("10")),),
        observed_at=NOW,
    )
    books = {0: _leg0_book(), 1: crossed_stub}
    fees = {
        0: fee_rate(taker_rate=Decimal("0.01")),
        1: fee_rate(taker_rate=Decimal("0.02")),
    }

    result = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result.status == "insufficient_evidence"
    assert result.insufficiency_reason == "CROSSED_BOOK"


def test_domain_validator_rejects_crossed_book_at_construction() -> None:
    """Documents that CROSSED_BOOK is structurally unreachable through any actually
    stored PredictionBookSnapshot: the domain validator itself rejects a crossed
    book before it could ever reach the economics engine.
    """
    with pytest.raises(ValidationError):
        prediction_book_snapshot(
            bids=(level("0.60", "10"),),
            asks=(level("0.55", "10"),),
        )


def test_missing_fee_is_insufficient_evidence() -> None:
    candidate = _candidate()
    proof = _proof()
    policy = _policy()
    books = {0: _leg0_book(), 1: _leg1_book()}
    fees = {0: fee_rate(taker_rate=Decimal("0.01"))}  # leg 1's fee missing

    result = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result.status == "insufficient_evidence"
    assert result.insufficiency_reason == "MISSING_FEE"


def test_zero_executable_depth_is_insufficient_evidence_via_duck_typed_stub() -> None:
    """A real stored PredictionBookSnapshot always carries positive ask depth (the
    domain validator requires non-empty asks with positive per-level size), so this
    exercises the ZERO_EXECUTABLE_DEPTH branch via a duck-typed stub with an empty
    ask side -- the same structural-unreachability shape as CROSSED_BOOK above.
    """
    candidate = _candidate()
    proof = _proof()
    policy = _policy()
    empty_depth_stub = _StubBook(
        bids=(_StubLevel(Decimal("0.30"), Decimal("10")),), asks=(), observed_at=NOW
    )
    books = {0: _leg0_book(), 1: empty_depth_stub}
    fees = {
        0: fee_rate(taker_rate=Decimal("0.01")),
        1: fee_rate(taker_rate=Decimal("0.02")),
    }

    result = evaluate_basket_economics(
        proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
    )

    assert result.status == "insufficient_evidence"
    assert result.insufficiency_reason == "ZERO_EXECUTABLE_DEPTH"


def test_non_proof_ready_proof_raises_value_error() -> None:
    candidate = _candidate()
    proof = _proof(
        status="insufficient_evidence",
        rejection_reason="MISSING_ATTESTATION",
        minimum_basket_payout=None,
        maximum_basket_payout=None,
        terminal_states=(),
        assumptions=(),
    )
    policy = _policy()
    books = {0: _leg0_book(), 1: _leg1_book()}
    fees = {
        0: fee_rate(taker_rate=Decimal("0.01")),
        1: fee_rate(taker_rate=Decimal("0.02")),
    }

    with pytest.raises(ValueError):
        evaluate_basket_economics(
            proof, candidate, books=books, fees=fees, policy=policy, as_of=NOW
        )
