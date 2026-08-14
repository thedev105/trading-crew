from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from itertools import pairwise
from math import gcd
from typing import Literal, cast

from polytrading.carry.economics_funding import nearest_rank
from polytrading.carry.economics_models import (
    EconomicsPolicy,
    FundingDirection,
    VenueMarginAssumption,
)
from polytrading.domain.models import (
    BookLevel,
    InstrumentSpec,
    Level2BookSnapshot,
    Venue,
    normalize_utc_timestamp,
)

HoldingDays = Literal[7, 14, 28]


class InsufficientDepthError(ValueError):
    """Raised when a deterministic book walk cannot satisfy the whole request."""


@dataclass(frozen=True)
class WalkedQuote:
    quantity: Decimal
    notional: Decimal
    weighted_average_price: Decimal


@dataclass(frozen=True)
class ShadowPosition:
    direction: FundingDirection
    base_quantity: Decimal
    lighter_entry: WalkedQuote
    dydx_entry: WalkedQuote
    lighter_entry_notional_usd: Decimal
    dydx_entry_notional_usd: Decimal
    lighter_contract_multiplier: Decimal
    dydx_contract_multiplier: Decimal
    assigned_capital_usd: Decimal
    unused_cash_usd: Decimal
    incomplete_leg_loss_usd: Decimal


@dataclass(frozen=True)
class PairedBookObservation:
    effective_at: datetime
    lighter: Level2BookSnapshot
    dydx: Level2BookSnapshot

    def __post_init__(self) -> None:
        normalized = normalize_utc_timestamp(self.effective_at)
        object.__setattr__(self, "effective_at", normalized)
        if self.lighter.venue is not Venue.LIGHTER or self.dydx.venue is not Venue.DYDX:
            raise ValueError("paired books must use Lighter and dYdX order")
        if self.lighter.asset is not self.dydx.asset:
            raise ValueError("paired books must use one asset")


@dataclass(frozen=True)
class ExecutableQuoteObservation:
    observed_at: datetime
    quote_cost_usd: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", normalize_utc_timestamp(self.observed_at))
        _require_decimal(self.quote_cost_usd, "quote cost")


@dataclass(frozen=True)
class MarginStressResult:
    venue: Venue
    is_long: bool
    stressed_exit_price: Decimal
    stressed_exit_notional_usd: Decimal
    stressed_pnl_usd: Decimal
    stressed_fee_usd: Decimal
    liquidation_penalty_usd: Decimal
    remaining_collateral_usd: Decimal
    required_collateral_usd: Decimal
    modeled_liquidation: bool


@dataclass(frozen=True)
class QuoteObservationCounts:
    total: int
    normal_positive: int
    stress_positive: int


def _require_decimal(value: Decimal, label: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{label} must be a Decimal instance")
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def available_quantity(levels: tuple[BookLevel, ...]) -> Decimal:
    return sum((level.quantity for level in levels), Decimal(0))


def walk_book(levels: tuple[BookLevel, ...], quantity: Decimal) -> WalkedQuote:
    _require_decimal(quantity, "quantity", positive=True)
    remaining = quantity
    notional = Decimal(0)
    for level in levels:
        consumed = min(remaining, level.quantity)
        notional += consumed * level.price
        remaining -= consumed
        if remaining == 0:
            break
    if remaining != 0:
        raise InsufficientDepthError("requested quantity exceeds available book depth")
    return WalkedQuote(
        quantity=quantity,
        notional=notional,
        weighted_average_price=notional / quantity,
    )


def _decimal_scale(values: tuple[Decimal, ...]) -> int:
    return max(0, *(max(0, -value.as_tuple().exponent) for value in values))


def _common_base_step(lighter: InstrumentSpec, dydx: InstrumentSpec) -> Decimal | None:
    if lighter.quantity_step is None or dydx.quantity_step is None:
        return None
    steps = (
        lighter.quantity_step * lighter.contract_multiplier,
        dydx.quantity_step * dydx.contract_multiplier,
    )
    if any(step <= 0 or not step.is_finite() for step in steps):
        return None
    scale = Decimal(10) ** _decimal_scale(steps)
    integers = tuple(int(step * scale) for step in steps)
    common_integer = abs(integers[0] * integers[1]) // gcd(*integers)
    return Decimal(common_integer) / scale


def compatible_base_quantity(
    lighter: InstrumentSpec,
    dydx: InstrumentSpec,
    maximum_quantity: Decimal,
) -> Decimal | None:
    _require_decimal(maximum_quantity, "maximum quantity")
    if maximum_quantity <= 0:
        return None
    common_step = _common_base_step(lighter, dydx)
    if common_step is None:
        return None
    multiples = (maximum_quantity / common_step).to_integral_value(rounding=ROUND_FLOOR)
    quantity = multiples * common_step
    return quantity if quantity > 0 else None


def _base_walk(
    levels: tuple[BookLevel, ...], base_quantity: Decimal, instrument: InstrumentSpec
) -> WalkedQuote:
    venue_quantity = base_quantity / instrument.contract_multiplier
    walked = walk_book(levels, venue_quantity)
    return WalkedQuote(
        quantity=base_quantity,
        notional=walked.notional * instrument.contract_multiplier,
        weighted_average_price=walked.weighted_average_price,
    )


def _entry_levels(
    direction: FundingDirection,
    lighter_book: Level2BookSnapshot,
    dydx_book: Level2BookSnapshot,
) -> tuple[tuple[BookLevel, ...], tuple[BookLevel, ...]]:
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        return lighter_book.bids[:20], dydx_book.asks[:20]
    if direction is FundingDirection.SHORT_DYDX_LONG_LIGHTER:
        return lighter_book.asks[:20], dydx_book.bids[:20]
    raise ValueError("unsupported funding direction")


def _exit_levels(
    direction: FundingDirection,
    lighter_book: Level2BookSnapshot,
    dydx_book: Level2BookSnapshot,
) -> tuple[tuple[BookLevel, ...], tuple[BookLevel, ...]]:
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        return lighter_book.asks[:20], dydx_book.bids[:20]
    if direction is FundingDirection.SHORT_DYDX_LONG_LIGHTER:
        return lighter_book.bids[:20], dydx_book.asks[:20]
    raise ValueError("unsupported funding direction")


def _validate_position_inputs(
    lighter_book: Level2BookSnapshot,
    dydx_book: Level2BookSnapshot,
    lighter_instrument: InstrumentSpec,
    dydx_instrument: InstrumentSpec,
) -> None:
    if lighter_book.venue is not Venue.LIGHTER or lighter_instrument.venue is not Venue.LIGHTER:
        raise ValueError("Lighter inputs must use the Lighter venue")
    if dydx_book.venue is not Venue.DYDX or dydx_instrument.venue is not Venue.DYDX:
        raise ValueError("dYdX inputs must use the dYdX venue")
    identities = {
        (lighter_book.asset, lighter_book.symbol),
        (lighter_instrument.asset, lighter_instrument.symbol),
    }
    if len(identities) != 1:
        raise ValueError("Lighter book and instrument identities must match")
    identities = {
        (dydx_book.asset, dydx_book.symbol),
        (dydx_instrument.asset, dydx_instrument.symbol),
    }
    if len(identities) != 1:
        raise ValueError("dYdX book and instrument identities must match")
    if lighter_book.asset is not dydx_book.asset:
        raise ValueError("position legs must use one asset")


def size_shadow_position(
    *,
    policy: EconomicsPolicy,
    direction: FundingDirection,
    lighter_book: Level2BookSnapshot,
    dydx_book: Level2BookSnapshot,
    lighter_instrument: InstrumentSpec,
    dydx_instrument: InstrumentSpec,
) -> ShadowPosition | None:
    _validate_position_inputs(lighter_book, dydx_book, lighter_instrument, dydx_instrument)
    lighter_levels, dydx_levels = _entry_levels(direction, lighter_book, dydx_book)
    maximum_base = min(
        available_quantity(lighter_levels) * lighter_instrument.contract_multiplier,
        available_quantity(dydx_levels) * dydx_instrument.contract_multiplier,
    )
    maximum_compatible = compatible_base_quantity(lighter_instrument, dydx_instrument, maximum_base)
    common_step = _common_base_step(lighter_instrument, dydx_instrument)
    if maximum_compatible is None or common_step is None:
        return None
    maximum_units = int(maximum_compatible / common_step)
    capital_cap = min(
        policy.account_equity_usd * policy.maximum_assigned_equity_fraction,
        policy.maximum_assigned_usd,
    )
    incomplete_loss_cap = policy.account_equity_usd * policy.maximum_incomplete_loss_equity_fraction

    def quote_for_units(units: int) -> tuple[WalkedQuote, WalkedQuote] | None:
        if units <= 0:
            return None
        base_quantity = common_step * units
        try:
            lighter_quote = _base_walk(lighter_levels, base_quantity, lighter_instrument)
            dydx_quote = _base_walk(dydx_levels, base_quantity, dydx_instrument)
            lighter_exit_levels, dydx_exit_levels = _exit_levels(direction, lighter_book, dydx_book)
            stressed_quantity = base_quantity * policy.forced_exit_depth_multiplier
            _base_walk(lighter_exit_levels, stressed_quantity, lighter_instrument)
            _base_walk(dydx_exit_levels, stressed_quantity, dydx_instrument)
        except InsufficientDepthError:
            return None
        assigned = lighter_quote.notional + dydx_quote.notional
        incomplete_loss = max(lighter_quote.notional, dydx_quote.notional) * (
            policy.incomplete_leg_shock
        )
        if assigned > capital_cap or incomplete_loss > incomplete_loss_cap:
            return None
        return lighter_quote, dydx_quote

    lower = 1
    upper = maximum_units
    best: tuple[int, WalkedQuote, WalkedQuote] | None = None
    while lower <= upper:
        middle = (lower + upper) // 2
        quotes = quote_for_units(middle)
        if quotes is None:
            upper = middle - 1
        else:
            best = (middle, *quotes)
            lower = middle + 1
    if best is None:
        return None
    units, lighter_quote, dydx_quote = best
    if lighter_instrument.min_notional is None or dydx_instrument.min_notional is None:
        return None
    if (
        lighter_quote.notional < lighter_instrument.min_notional
        or dydx_quote.notional < dydx_instrument.min_notional
    ):
        return None
    assigned = lighter_quote.notional + dydx_quote.notional
    incomplete_loss = max(lighter_quote.notional, dydx_quote.notional) * (
        policy.incomplete_leg_shock
    )
    return ShadowPosition(
        direction=direction,
        base_quantity=common_step * units,
        lighter_entry=lighter_quote,
        dydx_entry=dydx_quote,
        lighter_entry_notional_usd=lighter_quote.notional,
        dydx_entry_notional_usd=dydx_quote.notional,
        lighter_contract_multiplier=lighter_instrument.contract_multiplier,
        dydx_contract_multiplier=dydx_instrument.contract_multiplier,
        assigned_capital_usd=assigned,
        unused_cash_usd=policy.account_equity_usd - assigned,
        incomplete_leg_loss_usd=incomplete_loss,
    )


def entry_slippage_cost(
    position: ShadowPosition,
    lighter_book: Level2BookSnapshot,
    dydx_book: Level2BookSnapshot,
) -> Decimal:
    lighter_mid = _midpoint(lighter_book)
    dydx_mid = _midpoint(dydx_book)
    if position.direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        lighter_adverse = lighter_mid - position.lighter_entry.weighted_average_price
        dydx_adverse = position.dydx_entry.weighted_average_price - dydx_mid
    else:
        lighter_adverse = position.lighter_entry.weighted_average_price - lighter_mid
        dydx_adverse = dydx_mid - position.dydx_entry.weighted_average_price
    return position.base_quantity * (
        max(Decimal(0), lighter_adverse) + max(Decimal(0), dydx_adverse)
    )


def _walk_base_with_multiplier(
    levels: tuple[BookLevel, ...],
    base_quantity: Decimal,
    contract_multiplier: Decimal,
) -> WalkedQuote:
    walked = walk_book(levels[:20], base_quantity / contract_multiplier)
    return WalkedQuote(
        quantity=base_quantity,
        notional=walked.notional * contract_multiplier,
        weighted_average_price=walked.weighted_average_price,
    )


def forced_exit_cost(
    position: ShadowPosition,
    lighter_book: Level2BookSnapshot,
    dydx_book: Level2BookSnapshot,
    depth_multiplier: Decimal,
) -> Decimal:
    _require_decimal(depth_multiplier, "depth multiplier", positive=True)
    stressed_quantity = position.base_quantity * depth_multiplier
    if position.direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        lighter_exit = _walk_base_with_multiplier(
            lighter_book.asks,
            stressed_quantity,
            position.lighter_contract_multiplier,
        )
        dydx_exit = _walk_base_with_multiplier(
            dydx_book.bids,
            stressed_quantity,
            position.dydx_contract_multiplier,
        )
        lighter_adverse = (
            lighter_exit.weighted_average_price - position.lighter_entry.weighted_average_price
        )
        dydx_adverse = position.dydx_entry.weighted_average_price - dydx_exit.weighted_average_price
    else:
        lighter_exit = _walk_base_with_multiplier(
            lighter_book.bids,
            stressed_quantity,
            position.lighter_contract_multiplier,
        )
        dydx_exit = _walk_base_with_multiplier(
            dydx_book.asks,
            stressed_quantity,
            position.dydx_contract_multiplier,
        )
        lighter_adverse = (
            position.lighter_entry.weighted_average_price - lighter_exit.weighted_average_price
        )
        dydx_adverse = dydx_exit.weighted_average_price - position.dydx_entry.weighted_average_price
    return position.base_quantity * (
        max(Decimal(0), lighter_adverse) + max(Decimal(0), dydx_adverse)
    )


def _validate_holding_days(holding_days: int) -> HoldingDays:
    if holding_days not in (7, 14, 28):
        raise ValueError("holding days must be 7, 14, or 28")
    return cast(HoldingDays, holding_days)


def _midpoint(book: Level2BookSnapshot) -> Decimal:
    return (book.bids[0].price + book.asks[0].price) / Decimal(2)


def _fixed_direction_basis(row: PairedBookObservation, direction: FundingDirection) -> Decimal:
    lighter_mid = _midpoint(row.lighter)
    dydx_mid = _midpoint(row.dydx)
    pair_mid = (lighter_mid + dydx_mid) / Decimal(2)
    differential = lighter_mid - dydx_mid
    if direction is FundingDirection.SHORT_DYDX_LONG_LIGHTER:
        differential = -differential
    return differential / pair_mid


def basis_reserve(
    rows: tuple[PairedBookObservation, ...],
    direction: FundingDirection,
    holding_days: HoldingDays,
) -> Decimal:
    if not rows:
        raise ValueError("paired book observations must not be empty")
    if any(right.effective_at <= left.effective_at for left, right in pairwise(rows)):
        raise ValueError("paired book observations must use strict timestamp order")
    window_size = _validate_holding_days(holding_days) * 24
    adverse_changes: list[Decimal] = []
    chunk_start = 0
    boundaries = (
        *(
            index
            for index, (left, right) in enumerate(pairwise(rows), start=1)
            if right.effective_at - left.effective_at != timedelta(hours=1)
        ),
        len(rows),
    )
    for chunk_end in boundaries:
        chunk = rows[chunk_start:chunk_end]
        for start in range(0, len(chunk) - window_size + 1):
            first = _fixed_direction_basis(chunk[start], direction)
            last = _fixed_direction_basis(chunk[start + window_size - 1], direction)
            adverse_changes.append(max(Decimal(0), last - first))
        chunk_start = chunk_end
    if not adverse_changes:
        raise ValueError(f"at least one complete {holding_days}-day book window is required")
    return nearest_rank(tuple(adverse_changes), Decimal("0.99"))


def latency_reserve(
    observations: tuple[ExecutableQuoteObservation, ...],
    documented_latency_ms: Decimal,
) -> Decimal | None:
    _require_decimal(documented_latency_ms, "documented latency")
    if documented_latency_ms < 0:
        raise ValueError("documented latency must be nonnegative")
    if any(right.observed_at <= left.observed_at for left, right in pairwise(observations)):
        raise ValueError("quote observations must use strict timestamp order")
    samples: list[tuple[Decimal, Decimal]] = []
    for left, right in pairwise(observations):
        elapsed = right.observed_at - left.observed_at
        if elapsed <= timedelta(0) or elapsed > timedelta(seconds=5):
            continue
        elapsed_microseconds = (
            elapsed.days * 86_400_000_000 + elapsed.seconds * 1_000_000 + elapsed.microseconds
        )
        elapsed_ms = Decimal(elapsed_microseconds) / Decimal(1000)
        adverse = max(Decimal(0), right.quote_cost_usd - left.quote_cost_usd)
        samples.append((elapsed_ms, adverse))
    if not samples:
        return None
    eligible_for_floor = tuple(sample for sample in samples if sample[0] >= documented_latency_ms)
    if not eligible_for_floor:
        return None
    floor_duration = min(duration for duration, _ in eligible_for_floor)
    floor_observation = max(
        adverse for duration, adverse in eligible_for_floor if duration == floor_duration
    )
    empirical = nearest_rank(tuple(adverse for _, adverse in samples), Decimal("0.99"))
    return max(floor_observation, empirical)


def margin_stress(
    *,
    is_long: bool,
    base_quantity: Decimal,
    entry_price: Decimal,
    collateral_usd: Decimal,
    taker_fee_rate: Decimal,
    shock: Decimal,
    assumption: VenueMarginAssumption,
) -> MarginStressResult:
    for value, label in (
        (base_quantity, "base quantity"),
        (entry_price, "entry price"),
        (collateral_usd, "collateral"),
    ):
        _require_decimal(value, label, positive=True)
    for value, label in ((taker_fee_rate, "taker fee"), (shock, "shock")):
        _require_decimal(value, label)
        if value < 0:
            raise ValueError(f"{label} must be nonnegative")
    if shock >= 1:
        raise ValueError("shock must be less than one")
    direction_multiplier = Decimal(-1) if is_long else Decimal(1)
    stressed_exit_price = entry_price * (Decimal(1) + direction_multiplier * shock)
    entry_notional = base_quantity * entry_price
    stressed_exit_notional = base_quantity * stressed_exit_price
    stressed_pnl = (
        stressed_exit_notional - entry_notional
        if is_long
        else entry_notional - stressed_exit_notional
    )
    stressed_fee = stressed_exit_notional * taker_fee_rate
    penalty = stressed_exit_notional * assumption.liquidation_penalty_fraction
    remaining = collateral_usd + stressed_pnl - stressed_fee - penalty
    required = stressed_exit_notional * max(
        assumption.maintenance_margin_fraction,
        assumption.close_out_margin_fraction,
    )
    initial_required = entry_notional * assumption.initial_margin_fraction
    modeled_liquidation = collateral_usd < initial_required or remaining <= required
    return MarginStressResult(
        venue=assumption.venue,
        is_long=is_long,
        stressed_exit_price=stressed_exit_price,
        stressed_exit_notional_usd=stressed_exit_notional,
        stressed_pnl_usd=stressed_pnl,
        stressed_fee_usd=stressed_fee,
        liquidation_penalty_usd=penalty,
        remaining_collateral_usd=remaining,
        required_collateral_usd=required,
        modeled_liquidation=modeled_liquidation,
    )


def quote_observation_counts(
    *,
    normal_net_values: tuple[Decimal, ...],
    stress_net_values: tuple[Decimal, ...],
) -> QuoteObservationCounts:
    if len(normal_net_values) != len(stress_net_values):
        raise ValueError("normal and stress observations must have equal counts")
    for value in (*normal_net_values, *stress_net_values):
        _require_decimal(value, "quoted net value")
    return QuoteObservationCounts(
        total=len(normal_net_values),
        normal_positive=sum(value > 0 for value in normal_net_values),
        stress_positive=sum(value > 0 for value in stress_net_values),
    )


def quoted_net_usd(
    *,
    gross_funding_usd: Decimal,
    entry_cost_usd: Decimal,
    exit_cost_usd: Decimal,
    fee_cost_usd: Decimal,
    operational_cost_usd: Decimal,
    latency_reserve_usd: Decimal,
    funding_reversal_reserve_usd: Decimal,
    basis_reserve_usd: Decimal,
) -> Decimal:
    _require_decimal(gross_funding_usd, "gross funding")
    costs = (
        entry_cost_usd,
        exit_cost_usd,
        fee_cost_usd,
        operational_cost_usd,
        latency_reserve_usd,
        funding_reversal_reserve_usd,
        basis_reserve_usd,
    )
    for cost in costs:
        _require_decimal(cost, "cost")
        if cost < 0:
            raise ValueError("costs must be nonnegative")
    return gross_funding_usd - sum(costs, Decimal(0))
