from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid5

from pydantic import Field, StringConstraints

from polytrading.predictions.candidates_models import CandidateRelationship
from polytrading.predictions.domain import (
    PredictionBookLevel,
    PredictionBookSnapshot,
    PredictionFeeRate,
    PredictionRecord,
    normalize_utc_timestamp,
)
from polytrading.predictions.economics import evaluate_basket_economics
from polytrading.predictions.economics_models import PredictionEconomicsPolicy
from polytrading.predictions.proofs_models import ProofArtifact
from polytrading.predictions.shadow_models import (
    ShadowEvent,
    ShadowFill,
    ShadowLegPlan,
    ShadowPlan,
    ShadowState,
)

SimulatedBookProvider = Callable[[int, datetime], PredictionBookSnapshot | None]

_EVENT_IDENTITY_NAMESPACE = UUID("78d54095-e80a-4c69-aa4e-e64687377a09")


class StressScenario(PredictionRecord):
    scenario_id: Annotated[str, StringConstraints(min_length=1)]
    latency_seconds: Annotated[int, Field(ge=0)]
    fill_fraction: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
    failing_leg_index: Annotated[int, Field(ge=0)] | None
    unknown_after_leg: Annotated[int, Field(ge=0)] | None


BASELINE = StressScenario(
    scenario_id="baseline",
    latency_seconds=0,
    fill_fraction=Decimal("1"),
    failing_leg_index=None,
    unknown_after_leg=None,
)
LATENCY_1S = StressScenario(
    scenario_id="latency_1s",
    latency_seconds=1,
    fill_fraction=Decimal("1"),
    failing_leg_index=None,
    unknown_after_leg=None,
)
LATENCY_5S = StressScenario(
    scenario_id="latency_5s",
    latency_seconds=5,
    fill_fraction=Decimal("1"),
    failing_leg_index=None,
    unknown_after_leg=None,
)
PARTIAL_FILL_50 = StressScenario(
    scenario_id="partial_fill_50",
    latency_seconds=0,
    fill_fraction=Decimal("0.5"),
    failing_leg_index=None,
    unknown_after_leg=None,
)
SECOND_LEG_REJECT = StressScenario(
    scenario_id="second_leg_reject",
    latency_seconds=0,
    fill_fraction=Decimal("1"),
    failing_leg_index=1,
    unknown_after_leg=None,
)
UNKNOWN_AFTER_FIRST = StressScenario(
    scenario_id="unknown_after_first",
    latency_seconds=0,
    fill_fraction=Decimal("1"),
    failing_leg_index=None,
    unknown_after_leg=0,
)


def simulate_shadow_proposal(
    plan: ShadowPlan,
    *,
    proof: ProofArtifact,
    candidate: CandidateRelationship,
    fees: Mapping[int, PredictionFeeRate],
    economics_policy: PredictionEconomicsPolicy,
    books: SimulatedBookProvider,
    scenario: StressScenario,
    started_at: datetime,
) -> tuple[ShadowEvent, ...]:
    """Replay one frozen shadow plan against caller-supplied point-in-time books."""
    plan = _revalidate_record(plan, ShadowPlan)
    proof = _revalidate_record(proof, ProofArtifact)
    candidate = _revalidate_record(candidate, CandidateRelationship)
    economics_policy = _revalidate_record(economics_policy, PredictionEconomicsPolicy)
    scenario = _revalidate_record(scenario, StressScenario)
    fees = _revalidate_fees(fees)
    started_at = normalize_utc_timestamp(started_at)
    if started_at < plan.information_cutoff:
        raise ValueError("simulation cannot start before the plan information cutoff")
    ordered_legs = tuple(sorted(plan.legs, key=lambda leg: leg.sequence_position))
    _validate_frozen_inputs(plan, proof, candidate, fees, economics_policy, ordered_legs)
    _validate_scenario_indices(scenario, len(ordered_legs))

    events = _provenance_events(plan)
    book_cache: dict[tuple[int, datetime], PredictionBookSnapshot | None] = {}

    def read_book(leg: ShadowLegPlan, at: datetime) -> PredictionBookSnapshot | None:
        key = (leg.leg_index, at)
        if key not in book_cache:
            supplied = books(leg.leg_index, at)
            if supplied is None:
                book_cache[key] = None
            else:
                validated = _revalidate_record(supplied, PredictionBookSnapshot)
                _validate_book(plan, candidate, leg, validated, at)
                if at - validated.observed_at > timedelta(
                    seconds=economics_policy.max_book_age_seconds
                ):
                    book_cache[key] = None
                else:
                    book_cache[key] = validated
        return book_cache[key]

    legs_by_index = {leg.leg_index: leg for leg in ordered_legs}
    acquisitions: list[ShadowFill] = []
    terminal_buys: list[ShadowFill] = []
    first_leg = ordered_legs[0]
    first_submit_at = _submit_at(started_at, 0, scenario)
    first_fill_at = _fill_at(started_at, 0, scenario)

    if scenario.unknown_after_leg == 0:
        _append_first_attempt(events, plan, scenario, first_leg, first_submit_at, None)
        return _append_terminal(
            events,
            plan=plan,
            scenario=scenario,
            state=ShadowState.UNKNOWN,
            occurred_at=first_submit_at,
            detail="first order state became unknown after submission",
            quantity_filled=None,
            leg_index=first_leg.leg_index,
            fills=(),
        )
    if first_fill_at > plan.expires_at:
        _append_first_attempt(events, plan, scenario, first_leg, first_fill_at, None)
        return _append_terminal(
            events,
            plan=plan,
            scenario=scenario,
            state=ShadowState.EXPIRED,
            occurred_at=first_fill_at,
            detail="proposal expired before the first fill could be confirmed",
            quantity_filled=None,
            leg_index=first_leg.leg_index,
            fills=(),
        )
    if scenario.failing_leg_index == 0:
        _append_first_attempt(events, plan, scenario, first_leg, first_fill_at, None)
        return _append_terminal(
            events,
            plan=plan,
            scenario=scenario,
            state=ShadowState.UNWOUND,
            occurred_at=first_fill_at,
            detail="venue rejected the first planned leg; unwind_loss_usd=0",
            quantity_filled=Decimal("0"),
            leg_index=first_leg.leg_index,
            fills=(),
        )

    first_book = read_book(first_leg, first_fill_at)
    if first_book is None:
        _append_first_attempt(events, plan, scenario, first_leg, first_fill_at, None)
        return _append_terminal(
            events,
            plan=plan,
            scenario=scenario,
            state=ShadowState.UNKNOWN,
            occurred_at=first_fill_at,
            detail="point-in-time book evidence was unavailable for the first leg",
            quantity_filled=None,
            leg_index=first_leg.leg_index,
            fills=(),
        )
    first_levels = _walk_current_asks(
        first_leg,
        first_book,
        target_quantity=plan.max_quantity,
        fill_fraction=scenario.fill_fraction,
    )
    common_quantity = sum((size for _, size in first_levels), Decimal("0"))
    if common_quantity <= 0:
        _append_first_attempt(events, plan, scenario, first_leg, first_fill_at, None)
        return _append_terminal(
            events,
            plan=plan,
            scenario=scenario,
            state=ShadowState.UNWOUND,
            occurred_at=first_fill_at,
            detail="first leg had no executable quantity; no exposure was acquired",
            quantity_filled=Decimal("0"),
            leg_index=first_leg.leg_index,
            fills=(),
        )
    first_fill = ShadowFill(
        leg_index=first_leg.leg_index,
        side="buy",
        price_levels=first_levels,
        quantity=common_quantity,
    )
    acquisitions.append(first_fill)
    _append_first_attempt(events, plan, scenario, first_leg, first_fill_at, first_fill)

    if common_quantity < plan.max_quantity:
        economics_books: dict[int, PredictionBookSnapshot] = {
            first_leg.leg_index: _book_with_asks(first_book, first_levels)
        }
        for position, leg in enumerate(ordered_legs[1:], start=1):
            fill_at = _fill_at(started_at, position, scenario)
            submit_at = _submit_at(started_at, position, scenario)
            if fill_at > plan.expires_at:
                return _append_terminal(
                    events,
                    plan=plan,
                    scenario=scenario,
                    state=ShadowState.EXPIRED,
                    occurred_at=fill_at,
                    detail="proposal expired before reduced-quantity continuation",
                    quantity_filled=None,
                    leg_index=leg.leg_index,
                    fills=tuple(terminal_buys),
                )
            if scenario.unknown_after_leg == position:
                return _append_terminal(
                    events,
                    plan=plan,
                    scenario=scenario,
                    state=ShadowState.UNKNOWN,
                    occurred_at=submit_at,
                    detail="order state became unknown during reduced-quantity continuation",
                    quantity_filled=None,
                    leg_index=leg.leg_index,
                    fills=tuple(terminal_buys),
                )
            if scenario.failing_leg_index == position:
                return _unwind_or_unknown(
                    events,
                    plan=plan,
                    scenario=scenario,
                    at=fill_at,
                    failed_leg_index=leg.leg_index,
                    failed_quantity=None,
                    acquisitions=acquisitions,
                    terminal_buys=terminal_buys,
                    legs_by_index=legs_by_index,
                    read_book=read_book,
                    reason="venue rejected the reduced-quantity continuation",
                )
            book = read_book(leg, fill_at)
            if book is None:
                return _append_terminal(
                    events,
                    plan=plan,
                    scenario=scenario,
                    state=ShadowState.UNKNOWN,
                    occurred_at=fill_at,
                    detail="later point-in-time book evidence was unavailable",
                    quantity_filled=None,
                    leg_index=leg.leg_index,
                    fills=tuple(terminal_buys),
                )
            levels = _walk_current_asks(
                leg,
                book,
                target_quantity=common_quantity,
                fill_fraction=scenario.fill_fraction,
            )
            if sum((size for _, size in levels), Decimal("0")) != common_quantity:
                return _unwind_or_unknown(
                    events,
                    plan=plan,
                    scenario=scenario,
                    at=fill_at,
                    failed_leg_index=leg.leg_index,
                    failed_quantity=None,
                    acquisitions=acquisitions,
                    terminal_buys=terminal_buys,
                    legs_by_index=legs_by_index,
                    read_book=read_book,
                    reason="later leg could not fill the reduced common quantity",
                )
            economics_books[leg.leg_index] = _book_with_asks(book, levels)
        continuation_at = _fill_at(started_at, len(ordered_legs) - 1, scenario)
        continuation = evaluate_basket_economics(
            proof,
            candidate,
            books=economics_books,
            fees=fees,
            policy=economics_policy,
            as_of=continuation_at,
        )
        if continuation.status != "evaluated" or continuation.conservative_surplus_usd <= 0:
            return _unwind_or_unknown(
                events,
                plan=plan,
                scenario=scenario,
                at=continuation_at,
                failed_leg_index=ordered_legs[1].leg_index,
                failed_quantity=None,
                acquisitions=acquisitions,
                terminal_buys=terminal_buys,
                legs_by_index=legs_by_index,
                read_book=read_book,
                reason="recomputed reduced-quantity economics were not positive",
            )

    terminal_at = first_fill_at
    last_leg_index: int = first_leg.leg_index
    for position, leg in enumerate(ordered_legs[1:], start=1):
        terminal_at = _fill_at(started_at, position, scenario)
        last_leg_index = leg.leg_index
        if terminal_at > plan.expires_at:
            return _append_terminal(
                events,
                plan=plan,
                scenario=scenario,
                state=ShadowState.EXPIRED,
                occurred_at=terminal_at,
                detail="proposal expired before the next leg could fill",
                quantity_filled=None,
                leg_index=leg.leg_index,
                fills=tuple(terminal_buys),
            )
        if scenario.unknown_after_leg == position:
            return _append_terminal(
                events,
                plan=plan,
                scenario=scenario,
                state=ShadowState.UNKNOWN,
                occurred_at=_submit_at(started_at, position, scenario),
                detail="order state became unknown after submission; simulation halted",
                quantity_filled=None,
                leg_index=leg.leg_index,
                fills=tuple(terminal_buys),
            )
        if scenario.failing_leg_index == position:
            return _unwind_or_unknown(
                events,
                plan=plan,
                scenario=scenario,
                at=terminal_at,
                failed_leg_index=leg.leg_index,
                failed_quantity=None,
                acquisitions=acquisitions,
                terminal_buys=terminal_buys,
                legs_by_index=legs_by_index,
                read_book=read_book,
                reason="venue rejected the planned leg",
            )
        book = read_book(leg, terminal_at)
        if book is None:
            return _append_terminal(
                events,
                plan=plan,
                scenario=scenario,
                state=ShadowState.UNKNOWN,
                occurred_at=terminal_at,
                detail="point-in-time book evidence was unavailable; simulation halted",
                quantity_filled=None,
                leg_index=leg.leg_index,
                fills=tuple(terminal_buys),
            )
        levels = _walk_current_asks(
            leg,
            book,
            target_quantity=common_quantity,
            fill_fraction=scenario.fill_fraction,
        )
        quantity = sum((size for _, size in levels), Decimal("0"))
        if quantity > 0:
            fill = ShadowFill(
                leg_index=leg.leg_index,
                side="buy",
                price_levels=levels,
                quantity=quantity,
            )
            acquisitions.append(fill)
            terminal_buys.append(fill)
        if quantity != common_quantity:
            return _unwind_or_unknown(
                events,
                plan=plan,
                scenario=scenario,
                at=terminal_at,
                failed_leg_index=leg.leg_index,
                failed_quantity=quantity,
                acquisitions=acquisitions,
                terminal_buys=terminal_buys,
                legs_by_index=legs_by_index,
                read_book=read_book,
                reason="planned leg filled short of the common quantity",
            )

    return _append_terminal(
        events,
        plan=plan,
        scenario=scenario,
        state=ShadowState.COMPLETE,
        occurred_at=terminal_at,
        detail="every planned leg filled at a common quantity",
        quantity_filled=common_quantity,
        leg_index=last_leg_index,
        fills=tuple(terminal_buys),
    )


def _revalidate_record[RecordT: PredictionRecord](
    value: object, record_type: type[RecordT]
) -> RecordT:
    if not isinstance(value, record_type):
        raise TypeError(f"expected {record_type.__name__}")
    return record_type.model_validate(value.model_dump())


def _revalidate_fees(value: object) -> dict[int, PredictionFeeRate]:
    if not isinstance(value, Mapping):
        raise TypeError("fees must be an index mapping")
    result: dict[int, PredictionFeeRate] = {}
    for index, fee in value.items():
        if not isinstance(index, int) or isinstance(index, bool):
            raise TypeError("fee indices must be integers")
        result[index] = _revalidate_record(fee, PredictionFeeRate)
    return result


def _validate_frozen_inputs(
    plan: ShadowPlan,
    proof: ProofArtifact,
    candidate: CandidateRelationship,
    fees: Mapping[int, PredictionFeeRate],
    economics_policy: PredictionEconomicsPolicy,
    ordered_legs: Sequence[ShadowLegPlan],
) -> None:
    if plan.proof_id != proof.proof_id or plan.candidate_id != candidate.candidate_id:
        raise ValueError("plan, proof, and candidate identities must agree")
    if proof.candidate_id != candidate.candidate_id or proof.status != "proof_ready":
        raise ValueError("simulator requires the plan's proof-ready artifact")
    if plan.minimum_basket_payout != proof.minimum_basket_payout:
        raise ValueError("plan payout floor does not match its proof")
    if (
        plan.policy_id != economics_policy.policy_id
        or plan.policy_version != economics_policy.policy_version
    ):
        raise ValueError("plan economics policy identity does not match")

    expected_indices = set(range(len(candidate.legs)))
    if {leg.leg_index for leg in ordered_legs} != expected_indices or set(fees) != expected_indices:
        raise ValueError("plan legs and fees must exactly cover candidate legs")
    for leg in ordered_legs:
        candidate_leg = candidate.legs[leg.leg_index]
        fee = fees[leg.leg_index]
        if (
            leg.venue != candidate_leg.venue
            or leg.market_id != candidate_leg.market_id
            or leg.outcome_token_id != candidate_leg.outcome_token_id
            or fee.venue != candidate_leg.venue
            or (fee.market_id is not None and fee.market_id != candidate_leg.market_id)
        ):
            raise ValueError("plan, candidate, and fee leg evidence must agree")
        if leg.max_quantity != plan.max_quantity:
            raise ValueError("every planned leg must use the plan common quantity")
        if sum((size for _, size in leg.limit_price_levels), Decimal("0")) != leg.max_quantity:
            raise ValueError("planned limit capacities must equal the leg maximum quantity")
        if any(
            left[0] >= right[0]
            for left, right in zip(
                leg.limit_price_levels[:-1], leg.limit_price_levels[1:], strict=True
            )
        ):
            raise ValueError("planned limit prices must be strictly ascending")

    frozen = set(plan.frozen_hashes)
    required_hashes = set(proof.source_hashes)
    required_hashes.update(leg.rule_source_hash for leg in candidate.legs)
    required_hashes.update(fee.source_hash for fee in fees.values())
    required_hashes.add(_policy_hash(economics_policy))
    if not required_hashes <= frozen:
        raise ValueError("proof, candidate, fee, and policy evidence must be frozen in the plan")


def _validate_scenario_indices(scenario: StressScenario, leg_count: int) -> None:
    for index in (scenario.failing_leg_index, scenario.unknown_after_leg):
        if index is not None and index >= leg_count:
            raise ValueError("scenario leg positions must identify a planned execution position")


def _validate_book(
    plan: ShadowPlan,
    candidate: CandidateRelationship,
    leg: ShadowLegPlan,
    book: PredictionBookSnapshot,
    requested_at: datetime,
) -> None:
    candidate_leg = candidate.legs[leg.leg_index]
    if (
        book.venue != candidate_leg.venue
        or book.market_id != candidate_leg.market_id
        or book.outcome_token_id != candidate_leg.outcome_token_id
    ):
        raise ValueError("provider book does not match the requested candidate leg")
    if book.source_hash not in plan.frozen_hashes:
        raise ValueError("provider book hash is not frozen in the plan")
    if book.observed_at > requested_at or book.effective_at > requested_at:
        raise ValueError("provider book contains evidence from after its requested time")


def _walk_current_asks(
    leg: ShadowLegPlan,
    book: PredictionBookSnapshot,
    *,
    target_quantity: Decimal,
    fill_fraction: Decimal,
) -> tuple[tuple[Decimal, Decimal], ...]:
    remaining_book = [level.size * fill_fraction for level in book.asks]
    remaining_target = min(target_quantity, leg.max_quantity)
    filled: list[tuple[Decimal, Decimal]] = []

    for limit_price, limit_capacity in leg.limit_price_levels:
        remaining_limit = min(limit_capacity, remaining_target)
        for index, level in enumerate(book.asks):
            if remaining_limit <= 0 or remaining_target <= 0:
                break
            if level.price > limit_price:
                break
            take = min(remaining_book[index], remaining_limit, remaining_target)
            if take <= 0:
                continue
            _append_level(filled, level.price, take)
            remaining_book[index] -= take
            remaining_limit -= take
            remaining_target -= take
        if remaining_target <= 0:
            break
    return tuple(filled)


def _fill_at(started_at: datetime, position: int, scenario: StressScenario) -> datetime:
    return started_at + timedelta(seconds=(position + 1) * scenario.latency_seconds)


def _submit_at(started_at: datetime, position: int, scenario: StressScenario) -> datetime:
    return started_at + timedelta(seconds=position * scenario.latency_seconds)


def _append_first_attempt(
    events: list[ShadowEvent],
    plan: ShadowPlan,
    scenario: StressScenario,
    leg: ShadowLegPlan,
    occurred_at: datetime,
    fill: ShadowFill | None,
) -> None:
    events.append(
        _event(
            plan=plan,
            sequence=4,
            from_state=ShadowState.SHADOW_PLANNED,
            to_state=ShadowState.FIRST_LEG_SIMULATED,
            occurred_at=occurred_at,
            detail=(
                "first planned leg filled against point-in-time book evidence"
                if fill is not None
                else "first planned leg submission produced no confirmed fill"
            ),
            quantity_filled=fill.quantity if fill is not None else None,
            leg_index=leg.leg_index,
            scenario_id=scenario.scenario_id,
            fills=(fill,) if fill is not None else (),
        )
    )


def _append_terminal(
    events: list[ShadowEvent],
    *,
    plan: ShadowPlan,
    scenario: StressScenario,
    state: ShadowState,
    occurred_at: datetime,
    detail: str,
    quantity_filled: Decimal | None,
    leg_index: int | None,
    fills: tuple[ShadowFill, ...],
) -> tuple[ShadowEvent, ...]:
    events.append(
        _event(
            plan=plan,
            sequence=5,
            from_state=ShadowState.FIRST_LEG_SIMULATED,
            to_state=state,
            occurred_at=occurred_at,
            detail=detail,
            quantity_filled=quantity_filled,
            leg_index=leg_index,
            scenario_id=scenario.scenario_id,
            fills=fills,
        )
    )
    return tuple(events)


def _unwind_or_unknown(
    events: list[ShadowEvent],
    *,
    plan: ShadowPlan,
    scenario: StressScenario,
    at: datetime,
    failed_leg_index: int,
    failed_quantity: Decimal | None,
    acquisitions: Sequence[ShadowFill],
    terminal_buys: Sequence[ShadowFill],
    legs_by_index: Mapping[int, ShadowLegPlan],
    read_book: Callable[[ShadowLegPlan, datetime], PredictionBookSnapshot | None],
    reason: str,
) -> tuple[ShadowEvent, ...]:
    sells: list[ShadowFill] = []
    acquisition_cost = sum(
        (price * quantity for fill in acquisitions for price, quantity in fill.price_levels),
        Decimal("0"),
    )
    unwind_proceeds = Decimal("0")

    for acquisition in reversed(acquisitions):
        leg = legs_by_index[acquisition.leg_index]
        book = read_book(leg, at)
        if book is None:
            return _append_terminal(
                events,
                plan=plan,
                scenario=scenario,
                state=ShadowState.UNKNOWN,
                occurred_at=at,
                detail=f"{reason}; unwind evidence was unavailable",
                quantity_filled=failed_quantity,
                leg_index=failed_leg_index,
                fills=tuple((*terminal_buys, *sells)),
            )
        levels = _walk_current_bids(
            book,
            target_quantity=acquisition.quantity,
            fill_fraction=scenario.fill_fraction,
        )
        quantity = sum((size for _, size in levels), Decimal("0"))
        if quantity > 0:
            sell = ShadowFill(
                leg_index=acquisition.leg_index,
                side="sell",
                price_levels=levels,
                quantity=quantity,
            )
            sells.append(sell)
            unwind_proceeds += sum(
                (price * level_quantity for price, level_quantity in levels), Decimal("0")
            )
        if quantity != acquisition.quantity:
            return _append_terminal(
                events,
                plan=plan,
                scenario=scenario,
                state=ShadowState.UNKNOWN,
                occurred_at=at,
                detail=f"{reason}; unwind bid depth was insufficient",
                quantity_filled=failed_quantity,
                leg_index=failed_leg_index,
                fills=tuple((*terminal_buys, *sells)),
            )

    loss = max(acquisition_cost - unwind_proceeds, Decimal("0"))
    return _append_terminal(
        events,
        plan=plan,
        scenario=scenario,
        state=ShadowState.UNWOUND,
        occurred_at=at,
        detail=f"{reason}; unwind_loss_usd={loss}",
        quantity_filled=failed_quantity,
        leg_index=failed_leg_index,
        fills=tuple((*terminal_buys, *sells)),
    )


def _walk_current_bids(
    book: PredictionBookSnapshot,
    *,
    target_quantity: Decimal,
    fill_fraction: Decimal,
) -> tuple[tuple[Decimal, Decimal], ...]:
    remaining = target_quantity
    filled: list[tuple[Decimal, Decimal]] = []
    for level in book.bids:
        if remaining <= 0:
            break
        take = min(level.size * fill_fraction, remaining)
        if take > 0:
            filled.append((level.price, take))
            remaining -= take
    return tuple(filled)


def _book_with_asks(
    book: PredictionBookSnapshot,
    levels: Sequence[tuple[Decimal, Decimal]],
) -> PredictionBookSnapshot:
    values = book.model_dump()
    values["asks"] = tuple(PredictionBookLevel(price=price, size=size) for price, size in levels)
    return PredictionBookSnapshot.model_validate(values)


def _append_level(levels: list[tuple[Decimal, Decimal]], price: Decimal, size: Decimal) -> None:
    if levels and levels[-1][0] == price:
        previous_price, previous_size = levels[-1]
        levels[-1] = (previous_price, previous_size + size)
    else:
        levels.append((price, size))


def _provenance_events(plan: ShadowPlan) -> list[ShadowEvent]:
    specifications = (
        (None, ShadowState.DISCOVERED, "proposal discovered"),
        (ShadowState.DISCOVERED, ShadowState.PROOF_VALIDATED, "proof artifact validated"),
        (
            ShadowState.PROOF_VALIDATED,
            ShadowState.ECONOMICS_VALIDATED,
            "frozen economics validated",
        ),
        (
            ShadowState.ECONOMICS_VALIDATED,
            ShadowState.SHADOW_PLANNED,
            "shadow execution plan frozen",
        ),
    )
    return [
        _event(
            plan=plan,
            sequence=sequence,
            from_state=from_state,
            to_state=to_state,
            occurred_at=plan.information_cutoff,
            detail=detail,
            quantity_filled=None,
            leg_index=None,
            scenario_id=None,
            fills=(),
        )
        for sequence, (from_state, to_state, detail) in enumerate(specifications)
    ]


def _event(
    *,
    plan: ShadowPlan,
    sequence: int,
    from_state: ShadowState | None,
    to_state: ShadowState,
    occurred_at: datetime,
    detail: str,
    quantity_filled: Decimal | None,
    leg_index: int | None,
    scenario_id: str | None,
    fills: tuple[ShadowFill, ...],
) -> ShadowEvent:
    fields: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": plan.proposal_id,
        "sequence": sequence,
        "from_state": from_state,
        "to_state": to_state,
        "occurred_at": occurred_at,
        "detail": detail,
        "quantity_filled": quantity_filled,
        "leg_index": leg_index,
        "scenario_id": scenario_id,
        "fills": fills,
    }
    provisional = ShadowEvent(event_id=UUID(int=0), **fields)
    canonical = json.dumps(
        [
            str(plan.proposal_id),
            sequence,
            provisional.model_dump(mode="json", exclude={"event_id"}),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ShadowEvent(event_id=uuid5(_EVENT_IDENTITY_NAMESPACE, canonical), **fields)


def _policy_hash(policy: PredictionRecord) -> str:
    canonical = json.dumps(
        policy.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
