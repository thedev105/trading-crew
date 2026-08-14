from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from polytrading.carry.economics_models import CandidateEconomicsReport


def _value(value: object) -> str:
    member_value = getattr(value, "value", value)
    return str(member_value)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _decimal(value: Decimal) -> str:
    return str(value)


def render_economics_json(report: CandidateEconomicsReport) -> str:
    """Render a stable, machine-readable report without lossy numeric conversion."""
    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def render_economics_text(report: CandidateEconomicsReport) -> str:
    """Render the full research decision in stable, non-action-oriented text."""
    direction = "unavailable" if report.direction is None else _value(report.direction)
    coverage = report.coverage
    lines = [
        report.warning,
        f"Decision: {_value(report.decision)}",
        f"Direction: {direction}",
        f"Asset: {_value(report.asset)}",
        f"Evidence cutoff: {_timestamp(report.known_as_of)}",
        f"Evaluation time: {_timestamp(report.evaluated_at)}",
        "Coverage: "
        f"training={_decimal(coverage.training_funding_coverage)}, "
        f"evaluation={_decimal(coverage.evaluation_funding_coverage)}, "
        f"funding={_decimal(coverage.funding_coverage)}, "
        f"books={_decimal(coverage.book_coverage)}",
        "Freshness: "
        f"book_age_seconds={coverage.latest_book_age_seconds}, "
        f"pair_skew_ms={coverage.latest_pair_skew_ms}, "
        f"latency_samples={coverage.latency_sample_count}",
    ]
    economics = report.economics
    if economics is None:
        lines.extend(
            (
                "Assigned capital: unavailable",
                "Economic components: unavailable",
            )
        )
    else:
        costs = economics.costs
        lines.extend(
            (
                f"Assigned capital: {_decimal(economics.assigned_capital_usd)} USD",
                f"Unused cash: {_decimal(economics.unused_cash_usd)} USD",
                f"Base quantity: {_decimal(economics.base_quantity)}",
                f"Entry slippage: {_decimal(costs.entry_slippage_usd)} USD",
                f"Forced exit cost: {_decimal(costs.forced_exit_cost_usd)} USD",
                f"Taker fees: {_decimal(costs.taker_fee_cost_usd)} USD",
                f"Operational cost: {_decimal(costs.operational_cost_usd)} USD",
                f"Latency reserve: {_decimal(costs.latency_reserve_usd)} USD",
            )
        )
        for horizon in economics.horizons:
            lines.append(
                f"{horizon.holding_days} days: "
                f"lighter_rate_sum={_decimal(horizon.lighter_funding_rate_sum)}, "
                f"dydx_rate_sum={_decimal(horizon.dydx_funding_rate_sum)}, "
                f"lighter_funding={_decimal(horizon.lighter_funding_usd)} USD, "
                f"dydx_funding={_decimal(horizon.dydx_funding_usd)} USD, "
                f"gross={_decimal(horizon.gross_funding_usd)} USD, "
                f"funding_reversal={_decimal(horizon.funding_reversal_reserve_usd)} USD, "
                f"basis_rate={_decimal(horizon.basis_divergence_rate)}, "
                f"basis={_decimal(horizon.basis_divergence_reserve_usd)} USD, "
                f"net={_decimal(horizon.conservative_net_usd)} USD, "
                f"assigned_return={_decimal(horizon.assigned_capital_return)}, "
                f"account_return={_decimal(horizon.account_return)}, "
                f"annualized={_decimal(horizon.annualized_conservative_return)}"
            )
        lines.extend(
            (
                f"Doubled-cost 28-day net: {_decimal(economics.doubled_cost_28d_net_usd)} USD",
                f"Stress loss: {_decimal(economics.funding_and_forced_exit_loss_rate)}",
                f"Modeled drawdown: {_decimal(economics.modeled_drawdown_rate)}",
                f"Modeled liquidation: {'yes' if economics.modeled_liquidation else 'no'}",
                "Quote observations: "
                f"normal={economics.normal_quote_observations}, "
                f"five_second={economics.stress_quote_observations}",
            )
        )
    reasons = ", ".join(sorted(report.reason_codes)) if report.reason_codes else "none"
    lines.append(f"Reasons: {reasons}")
    return "\n".join(lines) + "\n"
