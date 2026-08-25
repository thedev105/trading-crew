from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from itertools import product
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.shadow_models import (
    ALLOWED_TRANSITIONS,
    ShadowEvent,
    ShadowFill,
    ShadowLegPlan,
    ShadowPlan,
    ShadowState,
    derive_current_state,
    deterministic_proposal_id,
)

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
SCAN_REPORT_ID = UUID("00000000-0000-0000-0000-000000001001")
PROPOSAL_ID = UUID("00000000-0000-0000-0000-000000001002")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000001003")
PROOF_ID = UUID("00000000-0000-0000-0000-000000001004")
EVENT_ID = UUID("00000000-0000-0000-0000-000000001005")
HASH_A = "a" * 64
HASH_B = "b" * 64

_STATE_VALUES = (
    "discovered",
    "proof_validated",
    "economics_validated",
    "shadow_planned",
    "first_leg_simulated",
    "complete",
    "unwound",
    "expired",
    "unknown",
    "reconciled",
)
_LEGAL_TRANSITIONS = {
    ("discovered", "proof_validated"),
    ("proof_validated", "economics_validated"),
    ("economics_validated", "shadow_planned"),
    ("shadow_planned", "first_leg_simulated"),
    ("first_leg_simulated", "complete"),
    ("first_leg_simulated", "unwound"),
    ("first_leg_simulated", "expired"),
    ("first_leg_simulated", "unknown"),
    ("complete", "reconciled"),
    ("unwound", "reconciled"),
    ("expired", "reconciled"),
    ("unknown", "reconciled"),
}
_ILLEGAL_TRANSITIONS = tuple(
    (from_state, to_state)
    for from_state, to_state in product(_STATE_VALUES, repeat=2)
    if (from_state, to_state) not in _LEGAL_TRANSITIONS
)


def _leg(**overrides: object) -> ShadowLegPlan:
    values: dict[str, object] = {
        "leg_index": 0,
        "venue": PredictionVenue.POLYMARKET,
        "market_id": "market-a",
        "outcome_token_id": "token-a",
        "sequence_position": 0,
        "limit_price_levels": ((Decimal("0.40"), Decimal("10")),),
        "max_quantity": Decimal("10"),
    }
    values.update(overrides)
    return ShadowLegPlan(**values)


def _plan(**overrides: object) -> ShadowPlan:
    values: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": PROPOSAL_ID,
        "candidate_id": CANDIDATE_ID,
        "proof_id": PROOF_ID,
        "scan_report_id": SCAN_REPORT_ID,
        "legs": (_leg(), _leg(leg_index=1, market_id="market-b", sequence_position=1)),
        "bottleneck_leg_index": 1,
        "max_quantity": Decimal("10"),
        "order_policy": "taker_cross_only",
        "expires_at": NOW + timedelta(minutes=5),
        "completion_path": "Buy the remaining legs after the first fill.",
        "cancellation_path": "Cancel unfilled orders before expiry.",
        "unwind_path": "Sell filled inventory at the best available bids.",
        "max_incomplete_exposure_usd": Decimal("15"),
        "max_incomplete_loss_usd": Decimal("5"),
        "frozen_hashes": (HASH_A, HASH_B),
        "policy_id": "research-v1",
        "policy_version": "1",
        "risk_policy_version": "1",
        "minimum_basket_payout": Decimal("1.00"),
        "kill_conditions": ("book becomes stale",),
        "information_cutoff": NOW,
        "observed_at": NOW,
    }
    values.update(overrides)
    return ShadowPlan(**values)


def _event(**overrides: object) -> ShadowEvent:
    values: dict[str, object] = {
        "schema_version": 1,
        "event_id": EVENT_ID,
        "proposal_id": PROPOSAL_ID,
        "sequence": 0,
        "from_state": None,
        "to_state": ShadowState.DISCOVERED,
        "occurred_at": NOW,
        "detail": "candidate admitted to shadow tracking",
        "quantity_filled": None,
        "leg_index": None,
        "scenario_id": None,
    }
    values.update(overrides)
    return ShadowEvent(**values)


def test_shadow_state_has_the_complete_v1_state_vocabulary() -> None:
    assert tuple(state.value for state in ShadowState) == _STATE_VALUES


def test_allowed_transitions_accept_each_v1_edge() -> None:
    expected = frozenset(
        (ShadowState(left), ShadowState(right)) for left, right in _LEGAL_TRANSITIONS
    )
    assert expected == ALLOWED_TRANSITIONS


@pytest.mark.parametrize(("from_state", "to_state"), _ILLEGAL_TRANSITIONS)
def test_shadow_event_rejects_every_illegal_transition_direction(
    from_state: str, to_state: str
) -> None:
    with pytest.raises(ValidationError, match="transition"):
        _event(
            sequence=1,
            from_state=ShadowState(from_state),
            to_state=ShadowState(to_state),
        )


def test_sequence_zero_event_must_start_discovered_without_a_previous_state() -> None:
    with pytest.raises(ValidationError, match="sequence 0"):
        _event(from_state=ShadowState.PROOF_VALIDATED)

    with pytest.raises(ValidationError, match="sequence 0"):
        _event(to_state=ShadowState.PROOF_VALIDATED)


def test_shadow_event_requires_a_timezone_aware_occurred_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _event(occurred_at=datetime(2026, 8, 24, 12))


def test_shadow_event_normalizes_occurred_at_to_utc() -> None:
    event = _event(occurred_at=datetime(2026, 8, 24, 17, tzinfo=timezone(timedelta(hours=5))))

    assert event.occurred_at == NOW
    assert event.occurred_at.tzinfo is UTC


def test_shadow_plan_freezes_required_execution_and_valuation_inputs() -> None:
    plan = _plan()

    assert plan.order_policy == "taker_cross_only"
    assert plan.minimum_basket_payout == Decimal("1.00")
    assert plan.legs[0].limit_price_levels == ((Decimal("0.40"), Decimal("10")),)
    assert plan.frozen_hashes == (HASH_A, HASH_B)


def test_shadow_plan_requires_at_least_two_legs() -> None:
    with pytest.raises(ValidationError):
        _plan(legs=(_leg(),))


def test_shadow_plan_rejects_bottleneck_outside_its_legs() -> None:
    with pytest.raises(ValidationError, match="bottleneck"):
        _plan(bottleneck_leg_index=2)


def test_shadow_plan_requires_leg_sequence_positions_to_be_a_permutation() -> None:
    with pytest.raises(ValidationError, match="sequence_position"):
        _plan(legs=(_leg(sequence_position=0), _leg(leg_index=1, sequence_position=0)))


def test_shadow_plan_requires_unique_leg_indices() -> None:
    with pytest.raises(ValidationError, match="leg_index"):
        _plan(
            bottleneck_leg_index=0,
            legs=(_leg(leg_index=0), _leg(leg_index=0, sequence_position=1)),
        )


def test_shadow_plan_expires_after_observation() -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        _plan(expires_at=NOW)


def test_shadow_plan_requires_sorted_unique_frozen_hashes() -> None:
    with pytest.raises(ValidationError, match="frozen_hashes"):
        _plan(frozen_hashes=(HASH_B, HASH_A, HASH_A))


def test_shadow_plan_requires_positive_frozen_minimum_basket_payout() -> None:
    with pytest.raises(ValidationError):
        _plan(minimum_basket_payout=Decimal("0"))


@pytest.mark.parametrize(
    "limit_price_levels",
    (
        (),
        ((Decimal("-0.40"), Decimal("1")),),
        ((Decimal("0.40"), Decimal("-1")),),
        ((Decimal("0"), Decimal("1")),),
        ((Decimal("0.40"), Decimal("0")),),
    ),
)
def test_shadow_leg_plan_rejects_empty_or_nonpositive_frozen_limit_levels(
    limit_price_levels: tuple[tuple[Decimal, Decimal], ...],
) -> None:
    with pytest.raises(ValidationError, match="limit_price_levels"):
        _leg(limit_price_levels=limit_price_levels)


def test_shadow_fill_retains_structured_levels_and_requires_their_total_quantity() -> None:
    fill = ShadowFill(
        leg_index=0,
        side="buy",
        price_levels=((Decimal("0.40"), Decimal("2")), (Decimal("0.41"), Decimal("3"))),
        quantity=Decimal("5"),
    )

    assert fill.price_levels[1] == (Decimal("0.41"), Decimal("3"))

    with pytest.raises(ValidationError, match="quantity"):
        ShadowFill(
            leg_index=0,
            side="buy",
            price_levels=((Decimal("0.40"), Decimal("2")),),
            quantity=Decimal("3"),
        )


@pytest.mark.parametrize(
    "price_levels",
    (
        (),
        ((Decimal("0"), Decimal("1")),),
        ((Decimal("0.40"), Decimal("0")),),
    ),
)
def test_shadow_fill_rejects_empty_or_nonpositive_evidence_levels(
    price_levels: tuple[tuple[Decimal, Decimal], ...],
) -> None:
    with pytest.raises(ValidationError, match="price_levels"):
        ShadowFill(
            leg_index=0,
            side="buy",
            price_levels=price_levels,
            quantity=Decimal("1"),
        )


def test_shadow_event_fills_are_machine_readable_evidence() -> None:
    fill = ShadowFill(
        leg_index=0,
        side="buy",
        price_levels=((Decimal("0.40"), Decimal("2")),),
        quantity=Decimal("2"),
    )

    event = _event(
        sequence=1,
        from_state=ShadowState.DISCOVERED,
        to_state=ShadowState.PROOF_VALIDATED,
        fills=(fill,),
    )

    assert event.fills == (fill,)


def test_derive_current_state_returns_the_last_state_for_a_contiguous_event_chain() -> None:
    events = (
        _event(),
        _event(
            event_id=UUID("00000000-0000-0000-0000-000000001006"),
            sequence=1,
            from_state=ShadowState.DISCOVERED,
            to_state=ShadowState.PROOF_VALIDATED,
        ),
        _event(
            event_id=UUID("00000000-0000-0000-0000-000000001007"),
            sequence=2,
            from_state=ShadowState.PROOF_VALIDATED,
            to_state=ShadowState.ECONOMICS_VALIDATED,
        ),
    )

    assert derive_current_state(events) is ShadowState.ECONOMICS_VALIDATED


@pytest.mark.parametrize(
    "events",
    (
        (
            _event(
                sequence=1, from_state=ShadowState.DISCOVERED, to_state=ShadowState.PROOF_VALIDATED
            ),
        ),
        (
            _event(),
            _event(
                sequence=2,
                from_state=ShadowState.DISCOVERED,
                to_state=ShadowState.PROOF_VALIDATED,
            ),
        ),
        (
            _event(),
            _event(
                sequence=1,
                from_state=ShadowState.ECONOMICS_VALIDATED,
                to_state=ShadowState.SHADOW_PLANNED,
            ),
        ),
    ),
)
def test_derive_current_state_rejects_broken_event_chains(events: tuple[ShadowEvent, ...]) -> None:
    with pytest.raises(ValueError):
        derive_current_state(events)


def test_derive_current_state_rejects_an_empty_event_chain() -> None:
    with pytest.raises(ValueError):
        derive_current_state(())


def test_deterministic_proposal_id_is_key_order_invariant_and_changes_with_plan_content() -> None:
    first = deterministic_proposal_id(
        SCAN_REPORT_ID,
        {"policy": {"version": "1", "name": "research"}, "max_quantity": "10"},
    )
    reordered = deterministic_proposal_id(
        SCAN_REPORT_ID,
        {"max_quantity": "10", "policy": {"name": "research", "version": "1"}},
    )
    changed = deterministic_proposal_id(
        SCAN_REPORT_ID,
        {"policy": {"version": "2", "name": "research"}, "max_quantity": "10"},
    )

    assert first == reordered
    assert changed != first
    assert isinstance(first, UUID)
