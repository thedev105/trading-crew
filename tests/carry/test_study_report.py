import json
from datetime import timedelta
from decimal import Decimal

from polytrading.carry.study_report import _decimal, render_study_json, render_study_text
from tests.carry.test_study_statistics import report_for


def test_json_is_stable_typed_and_discloses_gross_economics() -> None:
    report = report_for(
        (Decimal("0.00008"),) * (365 * 3),
        observation_lag=timedelta(days=1),
    )

    rendered = render_study_json(report)
    payload = json.loads(rendered)

    assert render_study_json(report) == rendered
    assert payload["protocol_version"] == "hl-bybit-funding-persistence-v1"
    assert payload["statistics"]["block_distribution"]["median"] == "0.00008"
    assert payload["source_hashes"] == ["a" * 64]
    assert payload["economic_basis"] == "gross_funding_only"
    assert payload["omitted_costs"] == [
        "basis_pnl",
        "collateral_effects",
        "failure_reserve",
        "fees",
        "financing",
        "slippage",
        "taxes",
    ]
    for forbidden in ("TRADE", "APPROVED", "LIVE_ELIGIBLE", "expected profit", "recommended"):
        assert forbidden not in rendered

    equivalent = report_for(
        (Decimal("0.000080000000000000"),) * (365 * 3),
        observation_lag=timedelta(days=1),
    )
    assert render_study_json(equivalent) == rendered


def test_canonical_decimal_trims_scale_without_rounding_significant_digits() -> None:
    assert _decimal(Decimal("12345678901234567890.123456789012345678")) == (
        "12345678901234567890.123456789012345678"
    )
    assert _decimal(Decimal("1000.000000000000000000")) == "1000"
    assert _decimal(Decimal("-0.000000000000000000")) == "0"


def test_text_report_has_stable_header_metrics_and_research_footer() -> None:
    report = report_for(
        (Decimal("0.00008"),) * (365 * 3),
        observation_lag=timedelta(days=1),
    )

    rendered = render_study_text(report)

    assert rendered.splitlines()[:3] == [
        "Carry persistence study v1 | BTC | FORWARD_TEST_REQUIRED",
        "Evidence: historical_reconstruction | economics=gross_funding_only",
        "Coverage: paired=1095/1095 (1.000000)",
    ]
    assert "Gross annualized mean per matched leg notional: 0.0876" in rendered
    assert rendered.endswith(
        "Research only: fees, slippage, basis P&L, collateral effects, financing, taxes, "
        "and failure reserves are omitted."
    )


def test_insufficient_text_withholds_statistics_and_lists_reasons() -> None:
    report = report_for(
        (Decimal(1),) * (89 * 3),
        observation_lag=timedelta(minutes=1),
    )

    rendered = render_study_text(report)

    assert "Statistics: withheld" in rendered
    assert "Reasons: POINT_IN_TIME_WINDOW_BELOW_90_DAYS" in rendered
    assert "Gross annualized mean" not in rendered
