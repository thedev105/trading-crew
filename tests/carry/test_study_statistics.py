from datetime import timedelta
from decimal import Decimal

import pytest

from polytrading.carry.study import (
    _build_report,
    _longest_adverse_run,
    _maximum_drawdown,
    _nearest_rank,
    _sign_behavior,
    _summarize_holding_windows,
)
from polytrading.carry.study_models import (
    AvailabilityClass,
    PairedFundingBlock,
    StudyDecision,
)
from polytrading.domain.models import Asset, Venue
from tests.carry.study_helpers import at, history_from_spreads

START = at("2025-01-01T00:00:00Z")


def report_for(
    spreads: tuple[Decimal, ...],
    *,
    observation_lag: timedelta,
    missing_hyperliquid_block_indexes: frozenset[int] = frozenset(),
):
    rows = history_from_spreads(START, spreads, observation_lag=observation_lag)
    bybit_rows = tuple(row for row in rows if row.venue is Venue.BYBIT)
    hyperliquid_rows = tuple(
        row
        for row in rows
        if row.venue is Venue.HYPERLIQUID
        and ((row.effective_at - START - timedelta(microseconds=1)) // timedelta(hours=8))
        not in missing_hyperliquid_block_indexes
    )
    end = START + timedelta(hours=8 * len(spreads))
    return _build_report(
        asset=Asset.BTC,
        start=START,
        end=end,
        known_as_of=end + max(observation_lag, timedelta(minutes=5)),
        bybit_rows=bybit_rows,
        hyperliquid_rows=hyperliquid_rows,
    )


def test_nearest_rank_uses_exact_decimal_ceiling_without_interpolation() -> None:
    values = (Decimal("1"), Decimal("2"), Decimal("100"))

    assert _nearest_rank(values, Decimal("0.05")) == Decimal("1")
    assert _nearest_rank(values, Decimal("0.50")) == Decimal("2")
    assert _nearest_rank(values, Decimal("0.95")) == Decimal("100")


def test_path_statistics_include_initial_zero_and_zeros_break_sign_runs() -> None:
    values = (Decimal("2"), Decimal("-1"), Decimal("-3"), Decimal("4"))

    assert _maximum_drawdown(values) == Decimal("4")
    assert _longest_adverse_run((Decimal("-1"), Decimal("-2"), Decimal("0"), Decimal("-3"))) == 2
    assert _sign_behavior(
        (Decimal("1"), Decimal("1"), Decimal("-1"), Decimal("0"), Decimal("-1"))
    ) == (Decimal("0.5"), 1)
    assert _sign_behavior((Decimal("1"), Decimal("0"), Decimal("-1"))) == (None, 0)


def test_holding_windows_never_bridge_a_missing_eight_hour_block() -> None:
    blocks = tuple(
        PairedFundingBlock(
            schema_version=1,
            block_start=START + timedelta(hours=8 * index),
            block_end=START + timedelta(hours=8 * (index + 1)),
            bybit_rate=Decimal(0),
            hyperliquid_rate=Decimal(1),
            spread=Decimal(1),
        )
        for index in (*range(10), *range(11, 32))
    )

    summary = _summarize_holding_windows(blocks, holding_days=7)

    assert summary.block_count == 21
    assert summary.distribution.count == 1
    assert summary.distribution.minimum == Decimal(21)


def test_insufficient_coverage_withholds_all_economic_statistics() -> None:
    report = report_for(
        (Decimal(1),) * (90 * 3),
        observation_lag=timedelta(minutes=1),
        missing_hyperliquid_block_indexes=frozenset({0, 100, 200}),
    )

    assert report.coverage.coverage_ratio < Decimal("0.99")
    assert report.availability is AvailabilityClass.POINT_IN_TIME
    assert report.statistics is None
    assert report.decision is StudyDecision.INSUFFICIENT_DATA
    assert report.decision_reasons == ("PAIRED_COVERAGE_BELOW_99_PERCENT",)


@pytest.mark.parametrize(
    ("days", "observation_lag", "reason"),
    [
        (89, timedelta(minutes=1), "POINT_IN_TIME_WINDOW_BELOW_90_DAYS"),
        (364, timedelta(days=1), "HISTORICAL_WINDOW_BELOW_365_DAYS"),
    ],
)
def test_minimum_evidence_window_depends_on_availability_class(
    days: int, observation_lag: timedelta, reason: str
) -> None:
    report = report_for(
        (Decimal(1),) * (days * 3),
        observation_lag=observation_lag,
    )

    assert report.statistics is None
    assert report.decision is StudyDecision.INSUFFICIENT_DATA
    assert report.decision_reasons == (reason,)


def test_positive_reconstruction_requires_a_new_forward_test() -> None:
    report = report_for(
        (Decimal("0.00008"),) * (365 * 3),
        observation_lag=timedelta(days=1),
    )

    assert report.availability is AvailabilityClass.HISTORICAL_RECONSTRUCTION
    assert report.decision is StudyDecision.FORWARD_TEST_REQUIRED
    assert report.statistics is not None
    assert report.statistics.block_distribution.median == Decimal("0.00008")
    assert report.statistics.gross_annualized_mean == Decimal("0.08760000")
    assert tuple(row.holding_days for row in report.statistics.holding_windows) == (7, 14, 28)
    assert report.decision_reasons == ()


def test_positive_point_in_time_study_still_requires_the_net_forward_gate() -> None:
    report = report_for(
        (Decimal("0.00008"),) * (90 * 3),
        observation_lag=timedelta(minutes=1),
    )

    assert report.availability is AvailabilityClass.POINT_IN_TIME
    assert report.decision is StudyDecision.NET_FORWARD_GATE_REQUIRED
    assert report.statistics is not None
    assert report.decision_reasons == ()


@pytest.mark.parametrize(
    ("spreads", "expected_reason"),
    [
        (
            (Decimal("-0.00008"),) * (365 * 3),
            "BLOCK_MEDIAN_NON_POSITIVE",
        ),
        (
            (Decimal("0.00008"),) * (365 * 3 - 84) + (Decimal("-1"),) * 84,
            "HOLDING_WINDOW_28_DAY_LOWER_TAIL_NON_POSITIVE",
        ),
    ],
)
def test_non_positive_distribution_gate_fails_replication(
    spreads: tuple[Decimal, ...], expected_reason: str
) -> None:
    report = report_for(spreads, observation_lag=timedelta(days=1))

    assert report.decision is StudyDecision.REPLICATION_FAILED
    assert expected_reason in report.decision_reasons


def test_best_month_cannot_be_the_only_source_of_cumulative_funding() -> None:
    spreads = [Decimal(1)] * (365 * 3)
    spreads[0] = Decimal(-700)
    spreads[-1] = Decimal(-700)
    spreads[500] = Decimal(2000)

    report = report_for(tuple(spreads), observation_lag=timedelta(days=1))

    assert report.statistics is not None
    assert all(
        summary.distribution.percentile_05 > 0 for summary in report.statistics.holding_windows
    )
    assert report.statistics.cumulative_without_best_month < 0
    assert report.decision is StudyDecision.REPLICATION_FAILED
    assert report.decision_reasons == ("BEST_MONTH_CONCENTRATION",)


def test_gross_report_discloses_every_unmodeled_cost_in_stable_order() -> None:
    report = report_for(
        (Decimal(1),) * (90 * 3),
        observation_lag=timedelta(minutes=1),
    )

    assert report.economic_basis == "gross_funding_only"
    assert report.omitted_costs == (
        "basis_pnl",
        "collateral_effects",
        "failure_reserve",
        "fees",
        "financing",
        "slippage",
        "taxes",
    )
