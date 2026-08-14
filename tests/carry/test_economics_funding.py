from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from polytrading.carry.economics_funding import (
    FundingCashflowObservation,
    exact_median,
    funding_cashflow_horizon_statistics,
    funding_horizon_statistics,
    maximum_funding_drawdown,
    nearest_rank,
    orient_funding,
    rolling_funding_sums,
    select_direction,
)
from polytrading.carry.economics_models import FundingDirection

START = datetime(2026, 6, 1, tzinfo=UTC)


def hourly_rows(values: tuple[Decimal, ...]) -> tuple[tuple[datetime, Decimal], ...]:
    return tuple((START + timedelta(hours=index), value) for index, value in enumerate(values))


def test_training_median_selects_one_direction_and_evaluation_does_not_change_it() -> None:
    training = (Decimal("0.0002"), Decimal("0.0001"), Decimal("-0.0001"))
    evaluation = (Decimal("-1"),) * 10

    direction = select_direction(training)

    assert direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX
    assert orient_funding(evaluation, direction) == (Decimal("-1"),) * 10
    assert select_direction(tuple(-value for value in training)) is (
        FundingDirection.SHORT_DYDX_LONG_LIGHTER
    )
    assert select_direction((Decimal("-1"), Decimal("1"))) is None


def test_even_median_uses_exact_central_mean_and_nearest_rank_never_interpolates() -> None:
    assert exact_median((Decimal("1"), Decimal("2"), Decimal("9"), Decimal("10"))) == Decimal("5.5")
    assert nearest_rank(tuple(Decimal(value) for value in range(1, 21)), Decimal("0.05")) == 1
    assert nearest_rank(tuple(Decimal(value) for value in range(1, 21)), Decimal("0.99")) == 20


@pytest.mark.parametrize("percentile", [Decimal("0"), Decimal("-0.1"), Decimal("1.1")])
def test_order_statistics_reject_empty_or_out_of_range_inputs(percentile: Decimal) -> None:
    with pytest.raises(ValueError, match="percentile"):
        nearest_rank((Decimal("1"),), percentile)
    with pytest.raises(ValueError, match="must not be empty"):
        exact_median(())
    with pytest.raises(ValueError, match="must not be empty"):
        select_direction(())


def test_rolling_windows_never_bridge_a_missing_hour() -> None:
    rows = list(hourly_rows((Decimal("1"),) * (14 * 24)))
    del rows[100]

    windows = rolling_funding_sums(tuple(rows), 7)

    assert len(windows) == (14 * 24 - 101 - 168 + 1)
    assert windows == (Decimal(168),) * len(windows)


def test_rolling_window_counts_are_exact_for_every_horizon() -> None:
    rows = hourly_rows((Decimal("0.001"),) * (60 * 24))

    assert len(rolling_funding_sums(rows, 7)) == 1440 - 168 + 1
    assert len(rolling_funding_sums(rows, 14)) == 1440 - 336 + 1
    assert len(rolling_funding_sums(rows, 28)) == 1440 - 672 + 1


def test_maximum_drawdown_uses_initial_zero_and_resets_at_gaps() -> None:
    contiguous = hourly_rows(
        (Decimal("2"), Decimal("-3"), Decimal("-4"), Decimal("10"), Decimal("-2"))
    )
    assert maximum_funding_drawdown(contiguous, 7) == Decimal("7")

    gapped = list(contiguous)
    gapped[2] = (gapped[2][0] + timedelta(hours=1), gapped[2][1])
    gapped[3] = (gapped[3][0] + timedelta(hours=1), gapped[3][1])
    gapped[4] = (gapped[4][0] + timedelta(hours=1), gapped[4][1])
    assert maximum_funding_drawdown(tuple(gapped), 7) == Decimal("4")

    all_negative = hourly_rows((Decimal("-1"), Decimal("-2")))
    assert maximum_funding_drawdown(all_negative, 7) == Decimal("3")


def test_horizon_statistics_return_exact_lower_tail_and_drawdown() -> None:
    rows = hourly_rows((Decimal("0.001"),) * (28 * 24))

    result = funding_horizon_statistics(rows, 7)

    assert result.holding_days == 7
    assert result.complete_window_count == 505
    assert result.percentile_05_sum == Decimal("0.168")
    assert result.maximum_drawdown == 0


def test_horizon_statistics_require_at_least_one_complete_window() -> None:
    with pytest.raises(ValueError, match="complete 7-day funding window"):
        funding_horizon_statistics(hourly_rows((Decimal("1"),) * 167), 7)


def test_cashflow_lower_tail_preserves_exact_venue_components() -> None:
    rows = tuple(
        FundingCashflowObservation(
            effective_at=START + timedelta(hours=index),
            lighter_rate=Decimal("0.001"),
            dydx_rate=Decimal("-0.0005"),
            lighter_funding_usd=Decimal("2"),
            dydx_funding_usd=Decimal("-1"),
        )
        for index in range(7 * 24)
    )

    result = funding_cashflow_horizon_statistics(rows, 7)

    assert result.complete_window_count == 1
    assert result.lighter_rate_sum == Decimal("0.168")
    assert result.dydx_rate_sum == Decimal("-0.0840")
    assert result.lighter_funding_usd == Decimal("336")
    assert result.dydx_funding_usd == Decimal("-168")
    assert result.gross_funding_usd == Decimal("168")
    assert result.maximum_drawdown_usd == 0


@pytest.mark.parametrize("bad_hour", [0, 1, 167, 168, 335])
def test_gap_at_each_boundary_prevents_bridged_complete_windows(bad_hour: int) -> None:
    rows = list(hourly_rows((Decimal("1"),) * 336))
    rows[bad_hour] = (rows[bad_hour][0] + timedelta(minutes=1), rows[bad_hour][1])

    windows = rolling_funding_sums(tuple(rows), 7)

    assert all(value == Decimal(168) for value in windows)
    assert len(windows) < 169


def test_rows_must_be_strictly_ordered_utc_hours() -> None:
    rows = hourly_rows((Decimal("1"), Decimal("2")))
    with pytest.raises(ValueError, match="strict timestamp order"):
        rolling_funding_sums(tuple(reversed(rows)), 7)
    with pytest.raises(ValueError, match="timezone-aware"):
        rolling_funding_sums(((START.replace(tzinfo=None), Decimal("1")),), 7)


decimal_values = st.lists(
    st.integers(min_value=-100, max_value=100), min_size=168, max_size=168
).map(lambda values: tuple(Decimal(value) / Decimal("10000") for value in values))


@given(values=decimal_values, worsening=st.integers(min_value=0, max_value=100))
def test_worsening_every_hour_cannot_improve_lower_tail_or_drawdown(
    values: tuple[Decimal, ...], worsening: int
) -> None:
    amount = Decimal(worsening) / Decimal("10000")
    worse = tuple(value - amount for value in values)
    original_rows = hourly_rows(values)
    worse_rows = hourly_rows(worse)

    assert rolling_funding_sums(worse_rows, 7)[0] <= rolling_funding_sums(original_rows, 7)[0]
    assert maximum_funding_drawdown(worse_rows, 7) >= maximum_funding_drawdown(original_rows, 7)
    if exact_median(values) <= 0:
        assert exact_median(worse) <= 0
