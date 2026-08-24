from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from polytrading.predictions.candidates_models import CandidateRelationship
from polytrading.predictions.domain import PredictionFeeRate
from polytrading.predictions.economics_models import (
    EconomicsResult,
    InsufficiencyReason,
    LegExecutionPlan,
    PredictionEconomicsPolicy,
)
from polytrading.predictions.proofs_models import ProofArtifact


class _BookLevelLike(Protocol):
    price: Decimal
    size: Decimal


class _BookLike(Protocol):
    """Structural shape ``evaluate_basket_economics`` needs from a book.

    Deliberately a duck-typed ``Protocol`` rather than ``PredictionBookSnapshot``
    itself: a *real* stored snapshot can never be crossed or carry zero ask depth
    (the domain validator in ``domain.PredictionBookSnapshot`` rejects both at
    construction), so the ``CROSSED_BOOK``/``ZERO_EXECUTABLE_DEPTH`` branches below
    are only reachable through a hand-built double satisfying this shape -- see
    ``tests/predictions/test_economics.py``. This function never constructs or
    mutates a book; it only reads these three attributes.
    """

    bids: tuple[_BookLevelLike, ...]
    asks: tuple[_BookLevelLike, ...]
    observed_at: datetime


def evaluate_basket_economics(
    proof: ProofArtifact,
    candidate: CandidateRelationship,
    *,
    books: Mapping[int, _BookLike | None],
    fees: Mapping[int, PredictionFeeRate | None],
    policy: PredictionEconomicsPolicy,
    as_of: datetime,
) -> EconomicsResult:
    """Spec section 7's conservative, depth-aware cost/reserve/surplus evaluation.

    Pure function: no I/O, no clock reads, no storage access -- the caller loads
    ``books``/``fees`` (each keyed by leg index into ``candidate.legs``) and passes
    them in already. Economics only ever runs over a ``proof_ready`` proof (a
    ``ValueError`` otherwise): the basket's guaranteed floor payout is only known
    once a proof has bounded it, and the two must agree on leg order/count since a
    proof's ``minimum_basket_payout`` is a per-basket-share figure and this function
    multiplies it by the depth-walked bottleneck quantity.

    Every leg must have a present, fresh (within ``policy.max_book_age_seconds`` of
    ``as_of``), uncrossed book and a present fee, else the whole basket is
    ``insufficient_evidence`` with the first-encountered typed reason (checked in
    fixed order across legs: every leg's book first, then every leg's fee, then the
    depth-derived bottleneck quantity) -- one missing/bad leg makes the entire
    basket unexecutable, since every leg must be bought together.

    The basket's executable quantity ``q`` is the bottleneck: the smallest total ask
    depth across all legs, so every leg's depth walk stays within its own book (no
    leg's walk ever needs more than its own total depth) and all legs fill the same
    quantity. Zero credit is given anywhere for maker rebates, points, incentives,
    or unproven early conversion; capital is assumed pre-positioned (no
    just-in-time transfer credit).
    """
    if proof.status != "proof_ready":
        raise ValueError(
            "evaluate_basket_economics requires a proof_ready proof artifact; got "
            f"status={proof.status!r}"
        )

    legs = candidate.legs
    max_age = timedelta(seconds=policy.max_book_age_seconds)

    for i in range(len(legs)):
        book = books.get(i)
        if book is None:
            return _insufficient(policy, "MISSING_BOOK")
        if as_of - book.observed_at > max_age:
            return _insufficient(policy, "STALE_BOOK")
        if book.bids and book.asks and book.bids[0].price >= book.asks[0].price:
            return _insufficient(policy, "CROSSED_BOOK")

    for i in range(len(legs)):
        if fees.get(i) is None:
            return _insufficient(policy, "MISSING_FEE")

    leg_depths = [
        sum((level.size for level in books[i].asks), start=Decimal("0")) for i in range(len(legs))
    ]
    quantity = min(leg_depths) if leg_depths else Decimal("0")
    if quantity <= 0:
        return _insufficient(policy, "ZERO_EXECUTABLE_DEPTH")

    leg_plans: list[LegExecutionPlan] = []
    for i, candidate_leg in enumerate(legs):
        book = books[i]
        assert book is not None  # narrowed by the missing-book check above
        depth_walked_levels, acquisition_cost = _walk_ask_depth(book.asks, quantity)
        leg_plans.append(
            LegExecutionPlan(
                leg_index=i,
                venue=candidate_leg.venue,
                market_id=candidate_leg.market_id,
                outcome_token_id=candidate_leg.outcome_token_id,
                depth_walked_levels=depth_walked_levels,
                filled_quantity=quantity,
                acquisition_cost_usd=acquisition_cost,
            )
        )

    acquisition_total = sum((plan.acquisition_cost_usd for plan in leg_plans), start=Decimal("0"))
    fee_total = sum(
        (plan.acquisition_cost_usd * fees[plan.leg_index].taker_rate for plan in leg_plans),
        start=Decimal("0"),
    )

    currency_basis_reserve = acquisition_total * policy.currency_basis_reserve_rate
    capital_lockup_reserve = (
        acquisition_total * policy.capital_lockup_rate_per_day * policy.assumed_capital_lock_days
    )

    all_in_cost_usd = (
        acquisition_total
        + fee_total
        + policy.gas_conversion_redemption_reserve_usd
        + currency_basis_reserve
        + policy.transfer_cost_usd
        + capital_lockup_reserve
        + policy.operational_cost_usd
    )

    failure_reserve_rate = (
        policy.partial_fill_reserve_rate
        + policy.latency_reserve_rate
        + policy.dispute_delay_reserve_rate
        + policy.venue_failure_reserve_rate
    )
    failure_reserve_usd = acquisition_total * failure_reserve_rate

    proven_floor_usd = quantity * proof.minimum_basket_payout

    conservative_surplus_usd = proven_floor_usd - all_in_cost_usd - failure_reserve_usd

    return_on_assigned_capital = (
        conservative_surplus_usd / all_in_cost_usd if all_in_cost_usd != 0 else Decimal("0")
    )

    stranded_collateral_by_venue: dict[str, Decimal] = {}
    for plan in leg_plans:
        key = plan.venue.value
        stranded_collateral_by_venue[key] = (
            stranded_collateral_by_venue.get(key, Decimal("0")) + plan.acquisition_cost_usd
        )

    doubled_cost_surplus_usd = proven_floor_usd - 2 * all_in_cost_usd - 2 * failure_reserve_usd

    return EconomicsResult(
        status="evaluated",
        insufficiency_reason=None,
        quantity=quantity,
        leg_plans=tuple(leg_plans),
        proven_floor_usd=proven_floor_usd,
        all_in_cost_usd=all_in_cost_usd,
        failure_reserve_usd=failure_reserve_usd,
        conservative_surplus_usd=conservative_surplus_usd,
        return_on_assigned_capital=return_on_assigned_capital,
        capacity_usd_at_current_depth=acquisition_total,
        stranded_collateral_by_venue=stranded_collateral_by_venue,
        max_capital_lock_days=policy.assumed_capital_lock_days,
        doubled_cost_surplus_usd=doubled_cost_surplus_usd,
    )


def _walk_ask_depth(
    asks: tuple[_BookLevelLike, ...], quantity: Decimal
) -> tuple[tuple[tuple[Decimal, Decimal], ...], Decimal]:
    """Walk an ask ladder to fill exactly ``quantity`` shares, level by level.

    ``quantity`` is always at most this book's own total ask depth (the caller only
    ever calls this with the bottleneck quantity across all legs), so the walk
    always completes without exhausting the ladder.
    """
    remaining = quantity
    levels: list[tuple[Decimal, Decimal]] = []
    cost = Decimal("0")
    for entry in asks:
        if remaining <= 0:
            break
        take = entry.size if entry.size <= remaining else remaining
        levels.append((entry.price, take))
        cost += entry.price * take
        remaining -= take
    return tuple(levels), cost


def _insufficient(
    policy: PredictionEconomicsPolicy, reason: InsufficiencyReason
) -> EconomicsResult:
    return EconomicsResult(
        status="insufficient_evidence",
        insufficiency_reason=reason,
        quantity=Decimal("0"),
        leg_plans=(),
        proven_floor_usd=Decimal("0"),
        all_in_cost_usd=Decimal("0"),
        failure_reserve_usd=Decimal("0"),
        conservative_surplus_usd=Decimal("0"),
        return_on_assigned_capital=Decimal("0"),
        capacity_usd_at_current_depth=Decimal("0"),
        stranded_collateral_by_venue={},
        max_capital_lock_days=policy.assumed_capital_lock_days,
        doubled_cost_surplus_usd=Decimal("0"),
    )
