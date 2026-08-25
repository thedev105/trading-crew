from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.predictions.domain import (
    PredictionFeeRate,
    PredictionRecord,
    PredictionVenue,
)
from polytrading.predictions.shadow_models import (
    ShadowEvent,
    ShadowFill,
    ShadowPlan,
    ShadowState,
    derive_current_state,
)

type LedgerAccount = Literal[
    "venue_cash", "venue_position", "fees_paid", "reserve", "opportunity_cost"
]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]

_POSTING_NAMESPACE = UUID("2b243b3d-1d94-4f28-a737-14bd593f59b1")
_RECONCILIATION_NAMESPACE = UUID("6bcb978a-c932-4136-a56c-1abaad7a1820")
_TERMINAL_STATES = frozenset(
    {ShadowState.COMPLETE, ShadowState.UNWOUND, ShadowState.EXPIRED, ShadowState.UNKNOWN}
)


class LedgerPosting(PredictionRecord):
    posting_id: UUID
    proposal_id: UUID
    event_id: UUID
    venue: PredictionVenue | None
    account: LedgerAccount
    debit_usd: NonNegativeDecimal
    credit_usd: NonNegativeDecimal
    occurred_at: datetime
    detail: NonEmptyString

    @model_validator(mode="after")
    def _require_exactly_one_nonzero_side(self) -> LedgerPosting:
        if (self.debit_usd > 0) == (self.credit_usd > 0):
            raise ValueError("exactly one posting side must be non-zero")
        return self


class ShadowReconciliation(PredictionRecord):
    reconciliation_id: UUID
    proposal_id: UUID
    venues_reconciled: tuple[PredictionVenue, ...]
    complete: bool
    unexplained_difference_usd: NonNegativeDecimal
    observed_at: datetime

    @field_validator("venues_reconciled")
    @classmethod
    def _require_sorted_unique_venues(
        cls, value: tuple[PredictionVenue, ...]
    ) -> tuple[PredictionVenue, ...]:
        if value != tuple(sorted(set(value), key=lambda venue: venue.value)):
            raise ValueError("venues_reconciled must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _require_explained_completion(self) -> ShadowReconciliation:
        if self.complete and self.unexplained_difference_usd != 0:
            raise ValueError("complete reconciliation cannot have an unexplained difference")
        return self


def postings_for_events(
    plan: ShadowPlan,
    events: Sequence[ShadowEvent],
    fees: Mapping[int, PredictionFeeRate],
) -> tuple[LedgerPosting, ...]:
    """Translate confirmed structured fills into an exact double-entry paper ledger.

    COMPLETE uses the proof-derived floor frozen on ``plan``. It deliberately gives no
    credit for a better eventual resolution because no such result is part of the
    event-time evidence supplied to this pure function.
    """
    plan = _revalidate_record(plan, ShadowPlan)
    events = _validated_events(plan, events)
    fees = _validated_fees(plan, fees)
    terminal = _terminal_event(events)

    postings: list[LedgerPosting] = []
    acquisition_costs: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    acquisition_quantities: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    sale_proceeds: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    sale_quantities: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    seen_fills: set[tuple[int, str]] = set()
    legs = {leg.leg_index: leg for leg in plan.legs}

    for event in events:
        for fill in event.fills:
            fill = _revalidate_record(fill, ShadowFill)
            key = (fill.leg_index, fill.side)
            if key in seen_fills:
                raise ValueError("duplicate fill side for a planned leg")
            seen_fills.add(key)
            leg = legs.get(fill.leg_index)
            if leg is None:
                raise ValueError("fill references an unknown planned leg")
            if fill.side == "buy":
                _validate_buy_against_limits(fill, leg.limit_price_levels, leg.max_quantity)
            notional = sum(
                (price * quantity for price, quantity in fill.price_levels), Decimal("0")
            )
            fee = notional * fees[fill.leg_index].taker_rate
            if fill.side == "buy":
                acquisition_costs[fill.leg_index] += notional
                acquisition_quantities[fill.leg_index] += fill.quantity
                _add_pair(
                    postings,
                    event,
                    venue=leg.venue,
                    debit_account="venue_position",
                    credit_account="venue_cash",
                    amount=notional,
                    detail=f"buy leg {fill.leg_index}",
                )
            else:
                sale_proceeds[fill.leg_index] += notional
                sale_quantities[fill.leg_index] += fill.quantity
                _add_pair(
                    postings,
                    event,
                    venue=leg.venue,
                    debit_account="venue_cash",
                    credit_account="venue_position",
                    amount=notional,
                    detail=f"sell leg {fill.leg_index}",
                )
            if fee > 0:
                _add_pair(
                    postings,
                    event,
                    venue=leg.venue,
                    debit_account="fees_paid",
                    credit_account="venue_cash",
                    amount=fee,
                    detail=f"taker fee leg {fill.leg_index} {fill.side}",
                )

    _validate_fill_lifecycle(
        plan,
        events,
        terminal,
        acquisition_quantities,
        sale_quantities,
    )
    if terminal.to_state is ShadowState.COMPLETE:
        quantity = next(iter(acquisition_quantities.values()))
        payout = quantity * plan.minimum_basket_payout
        total_cost = sum(acquisition_costs.values(), Decimal("0"))
        _add_posting(
            postings,
            terminal,
            venue=None,
            account="venue_cash",
            debit=payout,
            credit=Decimal("0"),
            detail="conservative proof-floor payout",
        )
        for leg_index in sorted(acquisition_costs):
            _add_posting(
                postings,
                terminal,
                venue=legs[leg_index].venue,
                account="venue_position",
                debit=Decimal("0"),
                credit=acquisition_costs[leg_index],
                detail=f"close completed position leg {leg_index}",
            )
        _post_residual(postings, terminal, payout - total_cost)
    elif terminal.to_state in {ShadowState.UNWOUND, ShadowState.EXPIRED}:
        aggregate_residual = Decimal("0")
        for leg_index in sorted(acquisition_costs):
            residual = acquisition_costs[leg_index] - sale_proceeds[leg_index]
            aggregate_residual += residual
            if residual > 0:
                _add_posting(
                    postings,
                    terminal,
                    venue=legs[leg_index].venue,
                    account="venue_position",
                    debit=Decimal("0"),
                    credit=residual,
                    detail=f"close loss leg {leg_index}",
                )
            elif residual < 0:
                _add_posting(
                    postings,
                    terminal,
                    venue=legs[leg_index].venue,
                    account="venue_position",
                    debit=-residual,
                    credit=Decimal("0"),
                    detail=f"close gain leg {leg_index}",
                )
        if aggregate_residual > 0:
            _add_posting(
                postings,
                terminal,
                venue=None,
                account="opportunity_cost",
                debit=aggregate_residual,
                credit=Decimal("0"),
                detail="aggregate acquisition-versus-unwind loss",
            )
        elif aggregate_residual < 0:
            _add_posting(
                postings,
                terminal,
                venue=None,
                account="reserve",
                debit=Decimal("0"),
                credit=-aggregate_residual,
                detail="aggregate acquisition-versus-unwind gain",
            )

    result = tuple(postings)
    if result:
        verify_conservation(result)
    return result


def verify_conservation(postings: Sequence[LedgerPosting]) -> None:
    if not isinstance(postings, Sequence) or isinstance(postings, (str, bytes)) or not postings:
        raise ValueError("postings must contain at least one balanced event group")
    validated = tuple(_revalidate_record(posting, LedgerPosting) for posting in postings)
    if len({posting.proposal_id for posting in validated}) != 1:
        raise ValueError("postings must belong to one proposal")
    if len({posting.posting_id for posting in validated}) != len(validated):
        raise ValueError("posting identities must be unique")
    groups: dict[UUID, list[LedgerPosting]] = defaultdict(list)
    for posting in validated:
        groups[posting.event_id].append(posting)
    for event_id, group in groups.items():
        debit = sum((posting.debit_usd for posting in group), Decimal("0"))
        credit = sum((posting.credit_usd for posting in group), Decimal("0"))
        if debit != credit:
            raise ValueError(f"ledger event {event_id} is not conserved")
    if any(posting.posting_id != _posting_id(posting) for posting in validated):
        raise ValueError("posting identity does not match canonical content")


def reconcile_proposal(
    plan: ShadowPlan,
    events: Sequence[ShadowEvent],
    postings: Sequence[LedgerPosting],
    fees: Mapping[int, PredictionFeeRate],
) -> ShadowReconciliation:
    plan = _revalidate_record(plan, ShadowPlan)
    events = _validated_events(plan, events)
    fees = _validated_fees(plan, fees)
    terminal_event = _terminal_event(events)
    execution_events = tuple(event for event in events if event.sequence <= terminal_event.sequence)
    actual = tuple(_revalidate_record(posting, LedgerPosting) for posting in postings)
    if actual:
        verify_conservation(actual)
        if any(posting.proposal_id != plan.proposal_id for posting in actual):
            raise ValueError("postings do not belong to the reconciled proposal")
    expected = postings_for_events(plan, execution_events, fees)
    exact = Counter(_posting_signature(item) for item in actual) == Counter(
        _posting_signature(item) for item in expected
    )
    unexplained = _unexplained_difference(expected, actual)
    terminal = terminal_event.to_state
    complete = exact and terminal in {
        ShadowState.COMPLETE,
        ShadowState.UNWOUND,
        ShadowState.EXPIRED,
    }
    venues = (
        tuple(sorted({leg.venue for leg in plan.legs}, key=lambda venue: venue.value))
        if exact and terminal is not ShadowState.UNKNOWN
        else ()
    )
    observed_at = terminal_event.occurred_at
    values = [
        str(plan.proposal_id),
        [str(event.event_id) for event in execution_events],
        [
            _posting_signature(posting)
            for posting in sorted(actual, key=lambda item: str(item.posting_id))
        ],
        [venue.value for venue in venues],
        complete,
        str(unexplained),
        observed_at.isoformat(),
    ]
    reconciliation_id = uuid5(_RECONCILIATION_NAMESPACE, _canonical(values))
    return ShadowReconciliation(
        reconciliation_id=reconciliation_id,
        proposal_id=plan.proposal_id,
        venues_reconciled=venues,
        complete=complete,
        unexplained_difference_usd=unexplained,
        observed_at=observed_at,
    )


def proposal_paper_pnl(
    postings: Sequence[LedgerPosting], reconciliation: ShadowReconciliation
) -> Decimal | None:
    reconciliation = _revalidate_record(reconciliation, ShadowReconciliation)
    if not reconciliation.complete:
        return None
    validated = tuple(_revalidate_record(posting, LedgerPosting) for posting in postings)
    if validated:
        verify_conservation(validated)
    if any(posting.proposal_id != reconciliation.proposal_id for posting in validated):
        raise ValueError("postings do not belong to the reconciliation")

    def net_credit(account: LedgerAccount) -> Decimal:
        return sum(
            (
                posting.credit_usd - posting.debit_usd
                for posting in validated
                if posting.account == account
            ),
            Decimal("0"),
        )

    return net_credit("reserve") + net_credit("opportunity_cost") + net_credit("fees_paid")


def _validated_events(plan: ShadowPlan, value: object) -> tuple[ShadowEvent, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError("events must be a non-empty sequence")
    events = tuple(_revalidate_record(event, ShadowEvent) for event in value)
    if any(event.proposal_id != plan.proposal_id for event in events):
        raise ValueError("events do not belong to the plan proposal")
    if len({event.event_id for event in events}) != len(events):
        raise ValueError("event identities must be unique")
    derive_current_state(events)
    _terminal_event(events)
    return events


def _terminal_event(events: Sequence[ShadowEvent]) -> ShadowEvent:
    terminals = [event for event in events if event.to_state in _TERMINAL_STATES]
    if len(terminals) != 1:
        raise ValueError("event chain must contain exactly one execution terminal")
    terminal = terminals[0]
    if events[-1] is not terminal and not (
        len(events) >= 2
        and events[-2] is terminal
        and events[-1].to_state is ShadowState.RECONCILED
        and not events[-1].fills
    ):
        raise ValueError("execution terminal must end the chain or precede reconciliation")
    return terminal


def _validated_fees(plan: ShadowPlan, value: object) -> dict[int, PredictionFeeRate]:
    if not isinstance(value, Mapping):
        raise TypeError("fees must be a leg-index mapping")
    legs = {leg.leg_index: leg for leg in plan.legs}
    if set(value) != set(legs) or any(
        not isinstance(index, int) or isinstance(index, bool) for index in value
    ):
        raise ValueError("fees must exactly cover planned leg indices")
    result: dict[int, PredictionFeeRate] = {}
    for index, supplied in value.items():
        fee = _revalidate_record(supplied, PredictionFeeRate)
        leg = legs[index]
        if fee.venue != leg.venue or (fee.market_id is not None and fee.market_id != leg.market_id):
            raise ValueError("fee evidence does not match its planned leg")
        if fee.source_hash not in plan.frozen_hashes:
            raise ValueError("fee evidence is not frozen on the plan")
        result[index] = fee
    return result


def _validate_fill_lifecycle(
    plan: ShadowPlan,
    events: Sequence[ShadowEvent],
    terminal: ShadowEvent,
    buys: Mapping[int, Decimal],
    sells: Mapping[int, Decimal],
) -> None:
    for event in events:
        if event.fills and event.to_state not in {
            ShadowState.FIRST_LEG_SIMULATED,
            *tuple(_TERMINAL_STATES),
        }:
            raise ValueError("fills may only appear on simulated or terminal events")
    first_event = next(
        (event for event in events if event.to_state is ShadowState.FIRST_LEG_SIMULATED), None
    )
    if first_event is None:
        raise ValueError("event chain must contain a first-leg simulation")
    first_leg_index = min(plan.legs, key=lambda leg: leg.sequence_position).leg_index
    if any(fill.side != "buy" or fill.leg_index != first_leg_index for fill in first_event.fills):
        raise ValueError("first-leg event may only confirm a buy of the first planned leg")
    if first_event.fills and (
        len(first_event.fills) != 1
        or first_event.leg_index != first_event.fills[0].leg_index
        or first_event.quantity_filled != first_event.fills[0].quantity
    ):
        raise ValueError("first-leg event metadata must match its structured fill")
    execution_positions = {leg.leg_index: leg.sequence_position for leg in plan.legs}
    acquired_positions = {execution_positions[index] for index in buys}
    if acquired_positions and acquired_positions != set(range(max(acquired_positions) + 1)):
        raise ValueError("confirmed buys must follow a contiguous planned execution prefix")
    if terminal.to_state is ShadowState.COMPLETE:
        if sells or set(buys) != {leg.leg_index for leg in plan.legs}:
            raise ValueError("complete event stream must buy every leg and contain no sells")
        quantities = set(buys.values())
        if len(quantities) != 1 or next(iter(quantities)) <= 0:
            raise ValueError("complete event stream must have one positive common quantity")
    elif terminal.to_state is ShadowState.UNWOUND:
        if any(sells.get(index, Decimal("0")) != quantity for index, quantity in buys.items()):
            raise ValueError("unwound event stream must sell every confirmed acquisition")
        if set(sells) - set(buys):
            raise ValueError("unwind cannot sell a leg that was not acquired")
    elif terminal.to_state is ShadowState.EXPIRED:
        if sells:
            raise ValueError("expired event stream cannot contain confirmed sells")
    elif any(sells.get(index, Decimal("0")) > quantity for index, quantity in buys.items()):
        raise ValueError("unknown event stream cannot sell more than confirmed acquisitions")


def _add_pair(
    postings: list[LedgerPosting],
    event: ShadowEvent,
    *,
    venue: PredictionVenue,
    debit_account: LedgerAccount,
    credit_account: LedgerAccount,
    amount: Decimal,
    detail: str,
) -> None:
    _add_posting(
        postings,
        event,
        venue=venue,
        account=debit_account,
        debit=amount,
        credit=Decimal("0"),
        detail=detail,
    )
    _add_posting(
        postings,
        event,
        venue=venue,
        account=credit_account,
        debit=Decimal("0"),
        credit=amount,
        detail=detail,
    )


def _validate_buy_against_limits(
    fill: ShadowFill,
    limits: Sequence[tuple[Decimal, Decimal]],
    max_quantity: Decimal,
) -> None:
    if fill.quantity > max_quantity or any(
        left[0] >= right[0]
        for left, right in zip(fill.price_levels[:-1], fill.price_levels[1:], strict=True)
    ):
        raise ValueError("buy fill exceeds its frozen limit schedule")
    remaining_capacity = [quantity for _, quantity in limits]
    for price, quantity in fill.price_levels:
        remaining = quantity
        for index, (limit_price, _) in enumerate(limits):
            if limit_price < price or remaining_capacity[index] <= 0:
                continue
            take = min(remaining, remaining_capacity[index])
            remaining -= take
            remaining_capacity[index] -= take
            if remaining == 0:
                break
        if remaining != 0:
            raise ValueError("buy fill exceeds its frozen limit schedule")


def _post_residual(postings: list[LedgerPosting], event: ShadowEvent, residual: Decimal) -> None:
    if residual > 0:
        _add_posting(
            postings,
            event,
            venue=None,
            account="reserve",
            debit=Decimal("0"),
            credit=residual,
            detail="conservative floor surplus",
        )
    elif residual < 0:
        _add_posting(
            postings,
            event,
            venue=None,
            account="opportunity_cost",
            debit=-residual,
            credit=Decimal("0"),
            detail="conservative floor deficit",
        )


def _add_posting(
    postings: list[LedgerPosting],
    event: ShadowEvent,
    *,
    venue: PredictionVenue | None,
    account: LedgerAccount,
    debit: Decimal,
    credit: Decimal,
    detail: str,
) -> None:
    if debit == 0 and credit == 0:
        return
    prototype = LedgerPosting(
        posting_id=UUID(int=0),
        proposal_id=event.proposal_id,
        event_id=event.event_id,
        venue=venue,
        account=account,
        debit_usd=debit,
        credit_usd=credit,
        occurred_at=event.occurred_at,
        detail=detail,
    )
    postings.append(prototype.model_copy(update={"posting_id": _posting_id(prototype)}))


def _posting_id(posting: LedgerPosting) -> UUID:
    values = [
        str(posting.proposal_id),
        str(posting.event_id),
        posting.venue.value if posting.venue is not None else None,
        posting.account,
        str(posting.debit_usd),
        str(posting.credit_usd),
        posting.occurred_at.isoformat(),
        posting.detail,
    ]
    return uuid5(_POSTING_NAMESPACE, _canonical(values))


def _posting_signature(posting: LedgerPosting) -> tuple[str, ...]:
    return (
        str(posting.posting_id),
        str(posting.proposal_id),
        str(posting.event_id),
        posting.venue.value if posting.venue is not None else "",
        posting.account,
        str(posting.debit_usd),
        str(posting.credit_usd),
        posting.occurred_at.isoformat(),
        posting.detail,
    )


def _unexplained_difference(
    expected: Sequence[LedgerPosting], actual: Sequence[LedgerPosting]
) -> Decimal:
    def balances(
        items: Sequence[LedgerPosting],
    ) -> dict[tuple[UUID, PredictionVenue | None, str], Decimal]:
        result: dict[tuple[UUID, PredictionVenue | None, str], Decimal] = defaultdict(
            lambda: Decimal("0")
        )
        for item in items:
            result[(item.event_id, item.venue, item.account)] += item.debit_usd - item.credit_usd
        return result

    expected_balances = balances(expected)
    actual_balances = balances(actual)
    keys = set(expected_balances) | set(actual_balances)
    return sum(
        (abs(expected_balances[key] - actual_balances[key]) for key in keys), Decimal("0")
    ) / Decimal("2")


def _revalidate_record[RecordT: PredictionRecord](value: object, kind: type[RecordT]) -> RecordT:
    if not isinstance(value, kind):
        raise TypeError(f"expected {kind.__name__}")
    return kind.model_validate(value.model_dump())


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
