from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.predictions.domain import PredictionFeeRate, PredictionVenue
from polytrading.predictions.shadow_models import (
    ShadowEvent,
    ShadowFill,
    ShadowLegPlan,
    ShadowPlan,
    ShadowState,
)
from polytrading.predictions.storage.store import ConflictingRecordError, PredictionMarketStore

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
PROPOSAL_ID = UUID("70000000-0000-0000-0000-000000000001")
HASH_A = "a" * 64
HASH_B = "b" * 64


def _ledger() -> object:
    return importlib.import_module("polytrading.predictions.shadow_ledger")


def _plan(**overrides: object) -> ShadowPlan:
    values: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": PROPOSAL_ID,
        "candidate_id": UUID("70000000-0000-0000-0000-000000000002"),
        "proof_id": UUID("70000000-0000-0000-0000-000000000003"),
        "scan_report_id": UUID("70000000-0000-0000-0000-000000000004"),
        "legs": (
            ShadowLegPlan(
                leg_index=0,
                venue=PredictionVenue.POLYMARKET,
                market_id="market-a",
                outcome_token_id="token-a",
                sequence_position=0,
                limit_price_levels=((Decimal("0.40"), Decimal("2")),),
                max_quantity=Decimal("2"),
            ),
            ShadowLegPlan(
                leg_index=1,
                venue=PredictionVenue.KALSHI,
                market_id="market-b",
                outcome_token_id=None,
                sequence_position=1,
                limit_price_levels=((Decimal("0.50"), Decimal("2")),),
                max_quantity=Decimal("2"),
            ),
        ),
        "bottleneck_leg_index": 0,
        "max_quantity": Decimal("2"),
        "order_policy": "taker_cross_only",
        "expires_at": NOW + timedelta(minutes=5),
        "completion_path": "buy every remaining leg",
        "cancellation_path": "cancel unfilled orders",
        "unwind_path": "sell confirmed inventory",
        "max_incomplete_exposure_usd": Decimal("1"),
        "max_incomplete_loss_usd": Decimal("1"),
        "frozen_hashes": (HASH_A, HASH_B),
        "policy_id": "ledger-test",
        "policy_version": "1",
        "risk_policy_version": "1",
        "minimum_basket_payout": Decimal("1"),
        "kill_conditions": ("book unavailable",),
        "information_cutoff": NOW,
        "observed_at": NOW,
    }
    values.update(overrides)
    return ShadowPlan(**values)


def _fees() -> dict[int, PredictionFeeRate]:
    return {
        0: PredictionFeeRate(
            schema_version=1,
            venue=PredictionVenue.POLYMARKET,
            market_id="market-a",
            maker_rate=Decimal("0"),
            taker_rate=Decimal("0.01"),
            observed_at=NOW,
            source_hash=HASH_A,
        ),
        1: PredictionFeeRate(
            schema_version=1,
            venue=PredictionVenue.KALSHI,
            market_id="market-b",
            maker_rate=Decimal("0"),
            taker_rate=Decimal("0.02"),
            observed_at=NOW,
            source_hash=HASH_B,
        ),
    }


def _fill(leg_index: int, side: str, price: str, quantity: str = "2") -> ShadowFill:
    return ShadowFill(
        leg_index=leg_index,
        side=side,
        price_levels=((Decimal(price), Decimal(quantity)),),
        quantity=Decimal(quantity),
    )


def _event(
    sequence: int,
    from_state: ShadowState | None,
    to_state: ShadowState,
    *,
    fills: tuple[ShadowFill, ...] = (),
    event_id_suffix: int | None = None,
) -> ShadowEvent:
    suffix = sequence + 1 if event_id_suffix is None else event_id_suffix
    return ShadowEvent(
        schema_version=1,
        event_id=UUID(f"70000000-0000-0000-0000-{suffix:012d}"),
        proposal_id=PROPOSAL_ID,
        sequence=sequence,
        from_state=from_state,
        to_state=to_state,
        occurred_at=NOW + timedelta(seconds=sequence),
        detail="untrusted free-form detail that accounting must ignore",
        quantity_filled=fills[0].quantity if fills else None,
        leg_index=fills[0].leg_index if fills else None,
        scenario_id="ledger-test",
        fills=fills,
    )


def _events(
    terminal: ShadowState,
    *,
    first_fills: tuple[ShadowFill, ...] = (),
    terminal_fills: tuple[ShadowFill, ...] = (),
) -> tuple[ShadowEvent, ...]:
    return (
        _event(0, None, ShadowState.DISCOVERED),
        _event(1, ShadowState.DISCOVERED, ShadowState.PROOF_VALIDATED),
        _event(2, ShadowState.PROOF_VALIDATED, ShadowState.ECONOMICS_VALIDATED),
        _event(3, ShadowState.ECONOMICS_VALIDATED, ShadowState.SHADOW_PLANNED),
        _event(
            4,
            ShadowState.SHADOW_PLANNED,
            ShadowState.FIRST_LEG_SIMULATED,
            fills=first_fills,
        ),
        _event(5, ShadowState.FIRST_LEG_SIMULATED, terminal, fills=terminal_fills),
    )


def test_complete_lifecycle_posts_exact_fees_floor_and_hand_computed_pnl() -> None:
    """Wrong fill, fee, or floor arithmetic changes the hand-derived USD 0.172 result."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )

    postings = ledger.postings_for_events(plan, events, fees)
    ledger.verify_conservation(postings)

    by_event: dict[UUID, list[object]] = {}
    for posting in postings:
        by_event.setdefault(posting.event_id, []).append(posting)
    assert sum((item.debit_usd for item in by_event[events[4].event_id]), Decimal("0")) == Decimal(
        "0.808"
    )
    assert sum((item.credit_usd for item in by_event[events[4].event_id]), Decimal("0")) == Decimal(
        "0.808"
    )
    assert sum((item.debit_usd for item in by_event[events[5].event_id]), Decimal("0")) == Decimal(
        "3.02"
    )
    assert sum((item.credit_usd for item in by_event[events[5].event_id]), Decimal("0")) == Decimal(
        "3.02"
    )

    payout = [
        item
        for item in postings
        if item.event_id == events[-1].event_id
        and item.account == "venue_cash"
        and item.venue is None
    ]
    assert len(payout) == 1
    assert payout[0].debit_usd == Decimal("2")
    assert payout[0].credit_usd == Decimal("0")

    reconciliation = ledger.reconcile_proposal(plan, events, postings, fees)
    assert reconciliation.complete is True
    assert reconciliation.venues_reconciled == (
        PredictionVenue.KALSHI,
        PredictionVenue.POLYMARKET,
    )
    assert reconciliation.unexplained_difference_usd == Decimal("0")
    assert ledger.proposal_paper_pnl(postings, reconciliation) == Decimal("0.172")


def test_deterministic_identities_ignore_detail_and_event_evidence_hash_changes() -> None:
    """Accounting identity must follow structured monetary content, not mutable prose/lineage."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    changed = tuple(
        event.model_copy(update={"detail": "different", "evidence_hashes": ("c" * 64,)})
        for event in events
    )

    first = ledger.postings_for_events(plan, events, fees)
    second = ledger.postings_for_events(plan, events, fees)
    changed_postings = ledger.postings_for_events(plan, changed, fees)

    assert first == second
    assert tuple(item.posting_id for item in first) == tuple(
        item.posting_id for item in changed_postings
    )
    assert tuple((item.debit_usd, item.credit_usd) for item in first) == tuple(
        (item.debit_usd, item.credit_usd) for item in changed_postings
    )
    assert (
        ledger.reconcile_proposal(plan, events, first, fees).reconciliation_id
        == ledger.reconcile_proposal(plan, events, first, fees).reconciliation_id
    )


def test_reconciliation_is_stable_when_its_derived_state_event_is_replayed() -> None:
    """Appending RECONCILED after success must not mint a second reconciliation identity."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    postings = ledger.postings_for_events(plan, events, fees)
    reconciled_event = _event(
        6,
        ShadowState.COMPLETE,
        ShadowState.RECONCILED,
        event_id_suffix=90,
    )

    before = ledger.reconcile_proposal(plan, events, postings, fees)
    after = ledger.reconcile_proposal(plan, (*events, reconciled_event), postings, fees)

    assert after == before


def test_ledger_rejects_unchecked_unknown_to_reconciled_chain() -> None:
    """Unknown order state remains unreconciled until external evidence changes the model."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.UNKNOWN,
        first_fills=(_fill(0, "buy", "0.40"),),
    )
    forged = _event(
        6,
        ShadowState.COMPLETE,
        ShadowState.RECONCILED,
        event_id_suffix=89,
    ).model_copy(update={"from_state": ShadowState.UNKNOWN})

    with pytest.raises(ValidationError, match="transition"):
        ledger.postings_for_events(plan, (*events, forged), fees)


def test_unwound_lifecycle_closes_realized_loss_and_charges_both_taker_fees() -> None:
    """Dropping exit fees or leaving the cost/proceeds residual open overstates unwind P&L."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.UNWOUND,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(0, "sell", "0.30"),),
    )

    postings = ledger.postings_for_events(plan, events, fees)
    terminal = [item for item in postings if item.event_id == events[-1].event_id]
    ledger.verify_conservation(postings)

    assert sum((item.debit_usd for item in terminal), Decimal("0")) == Decimal("0.806")
    assert sum((item.credit_usd for item in terminal), Decimal("0")) == Decimal("0.806")
    assert sum(
        (item.debit_usd for item in terminal if item.account == "opportunity_cost"),
        Decimal("0"),
    ) == Decimal("0.20")
    opportunity = [item for item in terminal if item.account == "opportunity_cost"]
    assert len(opportunity) == 1
    assert opportunity[0].venue is None
    reconciliation = ledger.reconcile_proposal(plan, events, postings, fees)
    assert reconciliation.complete is True
    assert ledger.proposal_paper_pnl(postings, reconciliation) == Decimal("-0.214")


def test_expired_exposure_is_conservatively_closed_at_zero_payout() -> None:
    """An expiration that strands a confirmed buy must not retain fictitious position value."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.EXPIRED,
        first_fills=(_fill(0, "buy", "0.40"),),
    )

    postings = ledger.postings_for_events(plan, events, fees)
    reconciliation = ledger.reconcile_proposal(plan, events, postings, fees)

    assert sum(
        (item.debit_usd for item in postings if item.account == "opportunity_cost"),
        Decimal("0"),
    ) == Decimal("0.80")
    assert reconciliation.complete is True
    assert ledger.proposal_paper_pnl(postings, reconciliation) == Decimal("-0.808")


def test_expired_without_exposure_reconciles_to_zero_without_zero_sided_postings() -> None:
    """A no-fill expiry is a known zero result, but must not manufacture invalid zero entries."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(ShadowState.EXPIRED)

    postings = ledger.postings_for_events(plan, events, fees)
    reconciliation = ledger.reconcile_proposal(plan, events, postings, fees)

    assert postings == ()
    assert reconciliation.complete is True
    assert reconciliation.venues_reconciled == (
        PredictionVenue.KALSHI,
        PredictionVenue.POLYMARKET,
    )
    assert ledger.proposal_paper_pnl(postings, reconciliation) == Decimal("0")


def test_unknown_retains_confirmed_exposure_but_never_yields_paper_pnl() -> None:
    """A balanced acquisition ledger cannot make unknown order state a valid paper result."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.UNKNOWN,
        first_fills=(_fill(0, "buy", "0.40"),),
    )

    postings = ledger.postings_for_events(plan, events, fees)
    reconciliation = ledger.reconcile_proposal(plan, events, postings, fees)

    assert all(item.account not in {"reserve", "opportunity_cost"} for item in postings)
    assert reconciliation.complete is False
    assert reconciliation.venues_reconciled == ()
    assert reconciliation.unexplained_difference_usd == Decimal("0")
    assert ledger.proposal_paper_pnl(postings, reconciliation) is None


def test_conservation_is_checked_per_event_instead_of_only_globally() -> None:
    """Opposite event imbalances must not cancel across an entire proposal ledger."""
    ledger = _ledger()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    postings = list(ledger.postings_for_events(_plan(), events, _fees()))
    first_credit = next(
        index
        for index, item in enumerate(postings)
        if item.event_id == events[4].event_id and item.credit_usd > 0
    )
    terminal_debit = next(
        index
        for index, item in enumerate(postings)
        if item.event_id == events[5].event_id and item.debit_usd > 0
    )
    postings[first_credit] = postings[first_credit].model_copy(
        update={"event_id": events[5].event_id}
    )
    postings[terminal_debit] = postings[terminal_debit].model_copy(
        update={"event_id": events[4].event_id}
    )

    with pytest.raises(ValueError, match="not conserved"):
        ledger.verify_conservation(postings)


def test_ledger_posting_rejects_zero_dual_and_inexact_float_sides() -> None:
    """Malformed journal entries must fail at the immutable model boundary."""
    ledger = _ledger()
    values = {
        "posting_id": UUID("70000000-0000-0000-0000-000000000099"),
        "proposal_id": PROPOSAL_ID,
        "event_id": UUID("70000000-0000-0000-0000-000000000098"),
        "venue": PredictionVenue.POLYMARKET,
        "account": "venue_cash",
        "debit_usd": Decimal("1"),
        "credit_usd": Decimal("0"),
        "occurred_at": NOW,
        "detail": "test entry",
    }

    with pytest.raises(ValidationError, match="exactly one"):
        ledger.LedgerPosting(**{**values, "debit_usd": Decimal("0")})
    with pytest.raises(ValidationError, match="exactly one"):
        ledger.LedgerPosting(**{**values, "credit_usd": Decimal("1")})
    with pytest.raises(ValidationError):
        ledger.LedgerPosting(**{**values, "debit_usd": 1.0})


def test_ledger_posting_requires_aware_time_and_normalizes_offsets_to_utc() -> None:
    """Posting identity and cutoff ordering require one canonical UTC timestamp basis."""
    ledger = _ledger()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    posting = ledger.postings_for_events(_plan(), events, _fees())[0]
    values = posting.model_dump()

    with pytest.raises(ValidationError, match="timezone-aware"):
        ledger.LedgerPosting(**{**values, "occurred_at": posting.occurred_at.replace(tzinfo=None)})

    offset = timezone(timedelta(hours=-5))
    normalized = ledger.LedgerPosting(
        **{**values, "occurred_at": posting.occurred_at.astimezone(offset)}
    )
    assert normalized.occurred_at == posting.occurred_at
    assert normalized.occurred_at.tzinfo is UTC


def test_conservation_revalidates_unchecked_naive_posting_copy() -> None:
    """Pydantic model_copy cannot bypass the posting timestamp boundary."""
    ledger = _ledger()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    postings = ledger.postings_for_events(_plan(), events, _fees())
    unchecked = postings[0].model_copy(
        update={"occurred_at": postings[0].occurred_at.replace(tzinfo=None)}
    )

    with pytest.raises(ValidationError, match="timezone-aware"):
        ledger.verify_conservation((unchecked, *postings[1:]))


def test_complete_reconciliation_model_requires_zero_unexplained_difference() -> None:
    """A caller cannot flip an unexplained reconciliation to complete via an unchecked copy."""
    ledger = _ledger()
    values = {
        "reconciliation_id": UUID("70000000-0000-0000-0000-000000000093"),
        "proposal_id": PROPOSAL_ID,
        "venues_reconciled": (PredictionVenue.POLYMARKET,),
        "complete": True,
        "unexplained_difference_usd": Decimal("1"),
        "observed_at": NOW,
    }

    with pytest.raises(ValidationError, match="unexplained"):
        ledger.ShadowReconciliation(**values)

    with pytest.raises(ValidationError, match="venues"):
        ledger.ShadowReconciliation(
            **{**values, "unexplained_difference_usd": Decimal("0"), "venues_reconciled": ()}
        )


@pytest.mark.parametrize(
    "update",
    (
        {"complete": False},
        {"venues_reconciled": (PredictionVenue.POLYMARKET,)},
        {"unexplained_difference_usd": Decimal("1")},
        {"observed_at": NOW + timedelta(minutes=2)},
        {"proposal_id": UUID("70000000-0000-0000-0000-000000000092")},
        {"reconciliation_id": UUID("70000000-0000-0000-0000-000000000091")},
    ),
)
def test_paper_pnl_revalidates_every_reconciliation_identity_field(
    update: dict[str, object],
) -> None:
    """Unchecked reconciliation copies cannot authorize or alter a paper result."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    postings = ledger.postings_for_events(plan, events, fees)
    reconciliation = ledger.reconcile_proposal(plan, events, postings, fees)

    with pytest.raises((ValueError, ValidationError)):
        ledger.proposal_paper_pnl(postings, reconciliation.model_copy(update=update))


def test_paper_pnl_rejects_valid_balanced_postings_not_bound_to_reconciliation() -> None:
    """Balanced canonical posting content must still match the reconciliation identity."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    postings = ledger.postings_for_events(plan, events, fees)
    reconciliation = ledger.reconcile_proposal(plan, events, postings, fees)
    changed_fees = {
        **fees,
        0: fees[0].model_copy(update={"taker_rate": Decimal("0.03")}),
    }
    balanced_but_different = ledger.postings_for_events(plan, events, changed_fees)

    ledger.verify_conservation(balanced_but_different)
    with pytest.raises(ValueError, match="reconciliation identity"):
        ledger.proposal_paper_pnl(balanced_but_different, reconciliation)


def test_paper_pnl_rejects_unchecked_posting_content_and_accepts_order_independence() -> None:
    """Posting mutation must fail, while storage/read ordering must not affect identity."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    postings = ledger.postings_for_events(plan, events, fees)
    reconciliation = ledger.reconcile_proposal(plan, events, postings, fees)

    assert ledger.proposal_paper_pnl(tuple(reversed(postings)), reconciliation) == Decimal("0.172")
    with pytest.raises(ValueError):
        ledger.proposal_paper_pnl(
            (postings[0].model_copy(update={"detail": "tampered"}), *postings[1:]),
            reconciliation,
        )


def test_empty_mixed_proposal_and_identity_tampered_posting_groups_are_rejected() -> None:
    """Conservation must not bless empty, cross-proposal, or content-mutated journals."""
    ledger = _ledger()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    postings = ledger.postings_for_events(_plan(), events, _fees())

    with pytest.raises(ValueError, match="at least one"):
        ledger.verify_conservation(())
    with pytest.raises(ValueError, match="one proposal"):
        ledger.verify_conservation(
            (
                *postings,
                postings[0].model_copy(
                    update={
                        "posting_id": UUID("70000000-0000-0000-0000-000000000097"),
                        "proposal_id": UUID("70000000-0000-0000-0000-000000000096"),
                    }
                ),
            )
        )
    with pytest.raises(ValueError, match="identity"):
        ledger.verify_conservation(
            (postings[0].model_copy(update={"detail": "content was changed"}), *postings[1:])
        )


def test_balanced_fee_tampering_does_not_reconcile_against_frozen_fee_evidence() -> None:
    """A balanced arbitrary fee journal must not substitute for the event-implied fee rate."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    changed_fees = {
        **fees,
        0: fees[0].model_copy(update={"taker_rate": Decimal("0.03")}),
    }
    balanced_but_wrong = ledger.postings_for_events(plan, events, changed_fees)

    ledger.verify_conservation(balanced_but_wrong)
    reconciliation = ledger.reconcile_proposal(plan, events, balanced_but_wrong, fees)

    assert reconciliation.complete is False
    assert reconciliation.unexplained_difference_usd == Decimal("0.016")
    assert ledger.proposal_paper_pnl(balanced_but_wrong, reconciliation) is None


@pytest.mark.parametrize(
    "events",
    (
        _events(
            ShadowState.COMPLETE,
            first_fills=(_fill(0, "buy", "0.40"),),
            terminal_fills=(_fill(1, "buy", "0.50", "1"),),
        ),
        _events(
            ShadowState.COMPLETE,
            first_fills=(_fill(0, "buy", "0.40"),),
            terminal_fills=(_fill(0, "buy", "0.40"), _fill(1, "buy", "0.50")),
        ),
        _events(
            ShadowState.UNWOUND,
            first_fills=(_fill(0, "buy", "0.40"),),
            terminal_fills=(_fill(0, "sell", "0.30", "1"),),
        ),
        _events(
            ShadowState.EXPIRED,
            terminal_fills=(_fill(1, "buy", "0.50"),),
        ),
    ),
)
def test_mismatched_duplicate_and_out_of_sequence_fill_streams_are_rejected(
    events: tuple[ShadowEvent, ...],
) -> None:
    """Ledger translation must reject fill topology the simulator could not legally emit."""
    ledger = _ledger()

    with pytest.raises(ValueError):
        ledger.postings_for_events(_plan(), events, _fees())


def test_unknown_leg_proposal_mismatch_and_unfrozen_fee_inputs_are_rejected() -> None:
    """Unchecked copies and mappings must not bypass plan/evidence identity boundaries."""
    ledger = _ledger()
    events = _events(
        ShadowState.UNKNOWN,
        first_fills=(_fill(0, "buy", "0.40"),),
    )
    unknown_leg = (
        *events[:-1],
        events[-1].model_copy(update={"fills": (_fill(2, "buy", "0.10"),)}),
    )
    wrong_proposal = (
        *events[:-1],
        events[-1].model_copy(update={"proposal_id": UUID("70000000-0000-0000-0000-000000000095")}),
    )
    unfrozen = {
        **_fees(),
        1: _fees()[1].model_copy(update={"source_hash": "c" * 64}),
    }

    with pytest.raises(ValueError, match="unknown"):
        ledger.postings_for_events(_plan(), unknown_leg, _fees())
    with pytest.raises(ValueError, match="proposal"):
        ledger.postings_for_events(_plan(), wrong_proposal, _fees())
    with pytest.raises(ValueError, match="frozen"):
        ledger.postings_for_events(_plan(), events, unfrozen)


@pytest.mark.parametrize(
    "events",
    (
        _events(
            ShadowState.COMPLETE,
            first_fills=(_fill(0, "buy", "0.41"),),
            terminal_fills=(_fill(1, "buy", "0.50"),),
        ),
        _events(
            ShadowState.COMPLETE,
            first_fills=(_fill(0, "buy", "0.40", "3"),),
            terminal_fills=(_fill(1, "buy", "0.50", "3"),),
        ),
    ),
)
def test_buy_fills_cannot_exceed_frozen_price_or_quantity_limits(
    events: tuple[ShadowEvent, ...],
) -> None:
    """A ledger must not book execution the immutable plan did not authorize."""
    ledger = _ledger()

    with pytest.raises(ValueError, match="limit"):
        ledger.postings_for_events(_plan(), events, _fees())


def test_ledger_revalidates_global_plan_quantity_against_every_leg_cap() -> None:
    """An unchecked plan copy cannot broaden a leg beyond the proposal-wide quantity cap."""
    ledger = _ledger()
    plan = _plan().model_copy(update={"max_quantity": Decimal("1")})
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )

    with pytest.raises(ValidationError, match="max_quantity"):
        ledger.postings_for_events(plan, events, _fees())


@pytest.mark.parametrize(("leg_index", "quantity"), ((1, Decimal("2")), (0, Decimal("1"))))
def test_first_fill_event_metadata_must_match_its_structured_fill(
    leg_index: int, quantity: Decimal
) -> None:
    """Contradictory structured summary fields signal a malformed event boundary."""
    ledger = _ledger()
    events = list(
        _events(
            ShadowState.UNKNOWN,
            first_fills=(_fill(0, "buy", "0.40"),),
        )
    )
    events[4] = events[4].model_copy(update={"leg_index": leg_index, "quantity_filled": quantity})

    with pytest.raises(ValueError, match="metadata"):
        ledger.postings_for_events(_plan(), tuple(events), _fees())


def test_empty_first_leg_event_cannot_defer_the_first_acquisition_to_terminal() -> None:
    """A later first-leg buy would contradict the simulator's confirmed execution order."""
    ledger = _ledger()
    events = _events(
        ShadowState.UNKNOWN,
        terminal_fills=(_fill(0, "buy", "0.40"),),
    )

    with pytest.raises(ValueError, match="first-leg"):
        ledger.postings_for_events(_plan(), events, _fees())


def test_empty_first_leg_event_requires_absent_quantity_metadata() -> None:
    """A no-fill first attempt cannot claim a scalar confirmed quantity."""
    ledger = _ledger()
    events = list(_events(ShadowState.EXPIRED))
    events[4] = events[4].model_copy(update={"quantity_filled": Decimal("0")})

    with pytest.raises(ValueError, match="metadata"):
        ledger.postings_for_events(_plan(), tuple(events), _fees())


@pytest.mark.parametrize(
    "events",
    (
        _events(
            ShadowState.UNKNOWN,
            terminal_fills=(_fill(0, "sell", "0.30"),),
        ),
        _events(
            ShadowState.UNKNOWN,
            first_fills=(_fill(0, "buy", "0.40"),),
            terminal_fills=(_fill(1, "sell", "0.30"),),
        ),
    ),
)
def test_unknown_terminal_cannot_introduce_naked_sells(
    events: tuple[ShadowEvent, ...],
) -> None:
    """UNKNOWN may retain confirmed exits, but may never manufacture inventory to sell."""
    ledger = _ledger()

    with pytest.raises(ValueError, match="sell"):
        ledger.postings_for_events(_plan(), events, _fees())


def test_sell_fill_cannot_appear_before_the_execution_terminal() -> None:
    """Only the terminal unwind/unknown evidence may contain an exit fill."""
    ledger = _ledger()
    events = list(
        _events(
            ShadowState.UNKNOWN,
            first_fills=(_fill(0, "buy", "0.40"),),
        )
    )
    events[3] = events[3].model_copy(update={"fills": (_fill(0, "sell", "0.30"),)})

    with pytest.raises(ValueError, match="terminal"):
        ledger.postings_for_events(_plan(), tuple(events), _fees())


def test_ledger_posting_store_round_trip_is_cutoff_safe_ordered_and_conflict_safe(
    tmp_path: Path,
) -> None:
    """Immutable posting retries must not overwrite content or leak after the read cutoff."""
    ledger = _ledger()
    plan = _plan()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    postings = ledger.postings_for_events(plan, events, _fees())
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")

    for posting in reversed(postings):
        assert store.append_ledger_posting(posting) is True
    assert store.append_ledger_posting(postings[0]) is False

    cutoff = events[4].occurred_at
    early = store.ledger_postings_for_proposal(plan.proposal_id, cutoff)
    assert set(early) == {item for item in postings if item.occurred_at <= cutoff}
    assert store.ledger_postings_for_proposal(plan.proposal_id, NOW) == ()
    assert store.ledger_postings_for_proposal(plan.proposal_id, events[-1].occurred_at) == tuple(
        sorted(
            postings, key=lambda item: (item.occurred_at, str(item.event_id), str(item.posting_id))
        )
    )

    with pytest.raises(ConflictingRecordError):
        store.append_ledger_posting(postings[0].model_copy(update={"detail": "different"}))


def test_reconciliation_store_returns_latest_at_cutoff_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    """A later reconciliation must not leak into an earlier point-in-time replay."""
    ledger = _ledger()
    plan = _plan()
    fees = _fees()
    events = _events(
        ShadowState.COMPLETE,
        first_fills=(_fill(0, "buy", "0.40"),),
        terminal_fills=(_fill(1, "buy", "0.50"),),
    )
    postings = ledger.postings_for_events(plan, events, fees)
    early = ledger.reconcile_proposal(plan, events, postings, fees)
    late = early.model_copy(
        update={
            "reconciliation_id": UUID("70000000-0000-0000-0000-000000000094"),
            "observed_at": early.observed_at + timedelta(minutes=1),
        }
    )
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")

    assert store.append_reconciliation(late) is True
    assert store.append_reconciliation(early) is True
    assert store.append_reconciliation(early) is False
    assert (
        store.latest_reconciliation_for_proposal(
            plan.proposal_id, early.observed_at - timedelta(microseconds=1)
        )
        is None
    )
    assert store.latest_reconciliation_for_proposal(plan.proposal_id, early.observed_at) == early
    assert store.latest_reconciliation_for_proposal(plan.proposal_id, late.observed_at) == late
    with pytest.raises(ConflictingRecordError):
        store.append_reconciliation(
            early.model_copy(update={"complete": False, "unexplained_difference_usd": Decimal("1")})
        )
