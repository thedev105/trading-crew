from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from polytrading.carry.economics_execution import (
    ExecutableQuoteObservation,
    InsufficientDepthError,
    PairedBookObservation,
    available_quantity,
    basis_reserve,
    compatible_base_quantity,
    entry_slippage_cost,
    forced_exit_cost,
    latency_reserve,
    margin_stress,
    quote_observation_counts,
    quoted_net_usd,
    size_shadow_position,
    walk_book,
)
from polytrading.carry.economics_models import FundingDirection
from polytrading.domain.models import Asset, BookLevel, InstrumentSpec, Venue
from tests.carry.test_economics_models import margin_assumption, policy
from tests.domain.factories import book_snapshot, instrument_spec

NOW = datetime(2026, 8, 13, 16, tzinfo=UTC)


def instrument(venue: Venue, step: str = "0.1", minimum: str = "5") -> InstrumentSpec:
    return instrument_spec(
        instrument_id=f"{venue.value}:BTC:linear_perpetual",
        venue=venue,
        symbol="BTC" if venue is Venue.LIGHTER else "BTC-USD",
        asset=Asset.BTC,
        contract_multiplier=Decimal("1"),
        min_notional=Decimal(minimum),
        quantity_step=Decimal(step),
        funding_interval_hours=Decimal("1"),
    )


def book(
    venue: Venue,
    *,
    bid: str = "100",
    ask: str = "101",
    quantity: str = "10",
    effective_at: datetime = NOW,
):
    bid_value = Decimal(bid)
    ask_value = Decimal(ask)
    return book_snapshot(
        venue=venue,
        symbol="BTC" if venue is Venue.LIGHTER else "BTC-USD",
        asset=Asset.BTC,
        bids=(BookLevel(price=bid_value, quantity=Decimal(quantity), order_count=1),),
        asks=(BookLevel(price=ask_value, quantity=Decimal(quantity), order_count=1),),
        effective_at=effective_at,
        observed_at=effective_at,
    )


def book_at_basis(at: datetime, fixed_direction_basis: Decimal) -> PairedBookObservation:
    lighter_mid = Decimal("100") * (Decimal(1) + fixed_direction_basis / Decimal(2))
    dydx_mid = Decimal("100") * (Decimal(1) - fixed_direction_basis / Decimal(2))
    return PairedBookObservation(
        effective_at=at,
        lighter=book(
            Venue.LIGHTER,
            bid=str(lighter_mid - Decimal("0.1")),
            ask=str(lighter_mid + Decimal("0.1")),
            effective_at=at,
        ),
        dydx=book(
            Venue.DYDX,
            bid=str(dydx_mid - Decimal("0.1")),
            ask=str(dydx_mid + Decimal("0.1")),
            effective_at=at,
        ),
    )


def test_walk_book_consumes_levels_in_side_order_with_exact_wap() -> None:
    quote = walk_book(
        levels=(
            BookLevel(price=Decimal("100"), quantity=Decimal("1"), order_count=1),
            BookLevel(price=Decimal("101"), quantity=Decimal("2"), order_count=1),
        ),
        quantity=Decimal("2"),
    )

    assert quote.quantity == Decimal("2")
    assert quote.notional == Decimal("201")
    assert quote.weighted_average_price == Decimal("100.5")
    with pytest.raises(InsufficientDepthError, match="requested quantity exceeds"):
        walk_book(
            levels=(BookLevel(price=Decimal("100"), quantity=Decimal("1"), order_count=1),),
            quantity=Decimal("2"),
        )


def test_compatible_quantity_uses_exact_common_decimal_multiple() -> None:
    assert compatible_base_quantity(
        instrument(Venue.LIGHTER, "0.003"),
        instrument(Venue.DYDX, "0.002"),
        Decimal("0.019"),
    ) == Decimal("0.018")
    assert (
        compatible_base_quantity(
            instrument(Venue.LIGHTER, "0.003"),
            instrument(Venue.DYDX, "0.002"),
            Decimal("0.005"),
        )
        is None
    )
    assert (
        compatible_base_quantity(
            instrument(Venue.LIGHTER).model_copy(update={"quantity_step": None}),
            instrument(Venue.DYDX),
            Decimal("1"),
        )
        is None
    )


def test_sizing_uses_equal_base_and_every_cap_without_calling_it_a_fill() -> None:
    item = size_shadow_position(
        policy=policy(account_equity_usd=Decimal("10000")),
        direction=FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        lighter_book=book(Venue.LIGHTER),
        dydx_book=book(Venue.DYDX),
        lighter_instrument=instrument(Venue.LIGHTER),
        dydx_instrument=instrument(Venue.DYDX),
    )

    assert item is not None
    assert item.base_quantity == Decimal("2.4")
    assert item.lighter_entry.quantity == item.dydx_entry.quantity == Decimal("2.4")
    assert item.assigned_capital_usd == Decimal("482.4")
    assert item.assigned_capital_usd <= Decimal("500")
    assert item.incomplete_leg_loss_usd == Decimal("24.24")
    assert item.incomplete_leg_loss_usd <= Decimal("25")
    assert item.unused_cash_usd == Decimal("9517.6")


def test_sizing_chooses_the_fixed_direction_entry_sides_and_rejects_minimums() -> None:
    inverse = size_shadow_position(
        policy=policy(account_equity_usd=Decimal("10000")),
        direction=FundingDirection.SHORT_DYDX_LONG_LIGHTER,
        lighter_book=book(Venue.LIGHTER, bid="99", ask="100"),
        dydx_book=book(Venue.DYDX, bid="102", ask="103"),
        lighter_instrument=instrument(Venue.LIGHTER),
        dydx_instrument=instrument(Venue.DYDX),
    )

    assert inverse is not None
    assert inverse.lighter_entry.weighted_average_price == Decimal("100")
    assert inverse.dydx_entry.weighted_average_price == Decimal("102")

    assert (
        size_shadow_position(
            policy=policy(account_equity_usd=Decimal("3000")),
            direction=FundingDirection.SHORT_LIGHTER_LONG_DYDX,
            lighter_book=book(Venue.LIGHTER, quantity="0.01"),
            dydx_book=book(Venue.DYDX, quantity="0.01"),
            lighter_instrument=instrument(Venue.LIGHTER, minimum="5"),
            dydx_instrument=instrument(Venue.DYDX, minimum="5"),
        )
        is None
    )


def test_sizing_uses_only_the_first_twenty_levels() -> None:
    bids = tuple(
        BookLevel(
            price=Decimal("100") - Decimal(index),
            quantity=Decimal("0.001") if index < 20 else Decimal("100"),
            order_count=1,
        )
        for index in range(21)
    )
    asks = tuple(
        BookLevel(
            price=Decimal("101") + Decimal(index),
            quantity=Decimal("0.001") if index < 20 else Decimal("100"),
            order_count=1,
        )
        for index in range(21)
    )

    result = size_shadow_position(
        policy=policy(account_equity_usd=Decimal("10000")),
        direction=FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        lighter_book=book_snapshot(venue=Venue.LIGHTER, symbol="BTC", bids=bids, asks=asks),
        dydx_book=book_snapshot(venue=Venue.DYDX, symbol="BTC-USD", bids=bids, asks=asks),
        lighter_instrument=instrument(Venue.LIGHTER, step="0.001"),
        dydx_instrument=instrument(Venue.DYDX, step="0.001"),
    )

    assert result is None


def test_entry_and_forced_exit_costs_walk_the_correct_opposite_sides() -> None:
    lighter = book(Venue.LIGHTER, bid="100", ask="102")
    dydx = book(Venue.DYDX, bid="99", ask="101")
    item = size_shadow_position(
        policy=policy(account_equity_usd=Decimal("10000")),
        direction=FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        lighter_book=lighter,
        dydx_book=dydx,
        lighter_instrument=instrument(Venue.LIGHTER),
        dydx_instrument=instrument(Venue.DYDX),
    )

    assert item is not None
    assert entry_slippage_cost(item, lighter, dydx) == item.base_quantity * Decimal("2")
    assert forced_exit_cost(item, lighter, dydx, Decimal("2")) == (
        item.base_quantity * Decimal("4")
    )


def test_lower_equity_cannot_increase_assigned_capital() -> None:
    common = {
        "direction": FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        "lighter_book": book(Venue.LIGHTER),
        "dydx_book": book(Venue.DYDX),
        "lighter_instrument": instrument(Venue.LIGHTER),
        "dydx_instrument": instrument(Venue.DYDX),
    }
    lower = size_shadow_position(policy=policy(account_equity_usd=Decimal("3000")), **common)
    higher = size_shadow_position(policy=policy(account_equity_usd=Decimal("10000")), **common)

    assert lower is not None and higher is not None
    assert lower.assigned_capital_usd <= higher.assigned_capital_usd


def test_basis_reserve_charges_only_fixed_direction_adverse_widening() -> None:
    rows = tuple(
        book_at_basis(
            NOW + timedelta(hours=index),
            Decimal("0.01") if index < 167 else Decimal("0.03"),
        )
        for index in range(168)
    )

    assert basis_reserve(rows, FundingDirection.SHORT_LIGHTER_LONG_DYDX, 7) == Decimal("0.02")
    assert basis_reserve(rows, FundingDirection.SHORT_DYDX_LONG_LIGHTER, 7) == 0


def test_basis_windows_do_not_bridge_timestamp_gaps() -> None:
    rows = tuple(
        book_at_basis(
            NOW + timedelta(hours=index + (1 if index >= 168 else 0)),
            Decimal("0.01") if index < 168 else Decimal("0.03"),
        )
        for index in range(336)
    )

    assert basis_reserve(rows, FundingDirection.SHORT_LIGHTER_LONG_DYDX, 7) == 0


def test_latency_reserve_uses_only_positive_gaps_through_five_seconds() -> None:
    observations = (
        ExecutableQuoteObservation(observed_at=NOW, quote_cost_usd=Decimal("0")),
        ExecutableQuoteObservation(
            observed_at=NOW + timedelta(milliseconds=500), quote_cost_usd=Decimal("1")
        ),
        ExecutableQuoteObservation(
            observed_at=NOW + timedelta(milliseconds=1500), quote_cost_usd=Decimal("0.5")
        ),
        ExecutableQuoteObservation(
            observed_at=NOW + timedelta(milliseconds=6500), quote_cost_usd=Decimal("3")
        ),
        ExecutableQuoteObservation(
            observed_at=NOW + timedelta(milliseconds=12501), quote_cost_usd=Decimal("100")
        ),
    )

    assert latency_reserve(observations, documented_latency_ms=Decimal("700")) == Decimal("2.5")
    assert latency_reserve(observations[:1], documented_latency_ms=Decimal("300")) is None
    with pytest.raises(ValueError, match="strict timestamp order"):
        latency_reserve(tuple(reversed(observations[:3])), Decimal("300"))


def test_documented_latency_floor_can_dominate_empirical_99th_percentile() -> None:
    observations = [
        ExecutableQuoteObservation(observed_at=NOW, quote_cost_usd=Decimal("0")),
        ExecutableQuoteObservation(
            observed_at=NOW + timedelta(milliseconds=500), quote_cost_usd=Decimal("10")
        ),
    ]
    for index in range(2, 101):
        observations.append(
            ExecutableQuoteObservation(
                observed_at=NOW + timedelta(milliseconds=500 + (index - 1) * 1000),
                quote_cost_usd=Decimal("10"),
            )
        )

    assert latency_reserve(tuple(observations), Decimal("500")) == Decimal("10")


def test_margin_stress_uses_strict_requirement_and_all_documented_charges() -> None:
    safe = margin_stress(
        is_long=True,
        base_quantity=Decimal("1"),
        entry_price=Decimal("100"),
        collateral_usd=Decimal("100"),
        taker_fee_rate=Decimal("0.001"),
        shock=Decimal("0.10"),
        assumption=margin_assumption(Venue.DYDX),
    )

    assert safe.stressed_exit_notional_usd == Decimal("90")
    assert safe.remaining_collateral_usd == Decimal("89.01")
    assert safe.required_collateral_usd == Decimal("4.50")
    assert safe.modeled_liquidation is False

    strict_assumption = margin_assumption(Venue.DYDX).model_copy(
        update={
            "maintenance_margin_fraction": Decimal("0.99"),
            "close_out_margin_fraction": Decimal("0.98"),
        }
    )
    unsafe = margin_stress(
        is_long=True,
        base_quantity=Decimal("1"),
        entry_price=Decimal("100"),
        collateral_usd=Decimal("100"),
        taker_fee_rate=Decimal("0.001"),
        shock=Decimal("0.10"),
        assumption=strict_assumption,
    )
    assert unsafe.required_collateral_usd == Decimal("89.10")
    assert unsafe.modeled_liquidation is True


def test_quote_counts_and_net_math_never_create_execution_state() -> None:
    counts = quote_observation_counts(
        normal_net_values=(Decimal("1"), Decimal("0"), Decimal("-1"), Decimal("2")),
        stress_net_values=(Decimal("0.5"), Decimal("0"), Decimal("-2"), Decimal("-1")),
    )

    assert counts.normal_positive == 2
    assert counts.stress_positive == 1
    assert quoted_net_usd(
        gross_funding_usd=Decimal("10"),
        entry_cost_usd=Decimal("1"),
        exit_cost_usd=Decimal("1"),
        fee_cost_usd=Decimal("1"),
        operational_cost_usd=Decimal("1"),
        latency_reserve_usd=Decimal("1"),
        funding_reversal_reserve_usd=Decimal("1"),
        basis_reserve_usd=Decimal("1"),
    ) == Decimal("3")


@given(tail_quantity=st.integers(min_value=1, max_value=100))
def test_removing_a_tail_level_cannot_increase_available_quantity(tail_quantity: int) -> None:
    head = BookLevel(price=Decimal("100"), quantity=Decimal("1"), order_count=1)
    tail = BookLevel(price=Decimal("99"), quantity=Decimal(tail_quantity), order_count=1)

    assert available_quantity((head,)) <= available_quantity((head, tail))


@given(
    cost_name=st.sampled_from(
        (
            "entry_cost_usd",
            "exit_cost_usd",
            "fee_cost_usd",
            "operational_cost_usd",
            "latency_reserve_usd",
            "funding_reversal_reserve_usd",
            "basis_reserve_usd",
        )
    ),
    extra_cost=st.integers(min_value=0, max_value=1000),
)
def test_increasing_any_cost_cannot_improve_quoted_net(cost_name: str, extra_cost: int) -> None:
    values = {
        "gross_funding_usd": Decimal("100"),
        "entry_cost_usd": Decimal("1"),
        "exit_cost_usd": Decimal("1"),
        "fee_cost_usd": Decimal("1"),
        "operational_cost_usd": Decimal("1"),
        "latency_reserve_usd": Decimal("1"),
        "funding_reversal_reserve_usd": Decimal("1"),
        "basis_reserve_usd": Decimal("1"),
    }
    baseline = quoted_net_usd(**values)
    values[cost_name] += Decimal(extra_cost)
    worse = quoted_net_usd(**values)

    assert worse <= baseline
