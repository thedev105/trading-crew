from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

from polytrading.carry.study_models import CarryPersistenceReport

_FOOTER = (
    "Research only: fees, slippage, basis P&L, collateral effects, financing, taxes, "
    "and failure reserves are omitted."
)


def render_study_json(report: CarryPersistenceReport) -> str:
    payload = _json_value(report)
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def render_study_text(report: CarryPersistenceReport) -> str:
    lines = [
        f"Carry persistence study v1 | {report.asset.value} | {report.decision.value}",
        f"Evidence: {report.availability.value} | economics={report.economic_basis}",
        (
            f"Coverage: paired={report.coverage.paired_complete_blocks}/"
            f"{report.coverage.requested_blocks} ({report.coverage.coverage_ratio:.6f})"
        ),
        f"Window: ({_timestamp(report.start)}, {_timestamp(report.end)}]",
    ]
    reasons = ",".join(report.decision_reasons) if report.decision_reasons else "none"
    lines.append(f"Reasons: {reasons}")
    if report.statistics is None:
        lines.append("Statistics: withheld")
    else:
        statistics = report.statistics
        lines.extend(
            (
                (f"Block median gross funding: {_decimal(statistics.block_distribution.median)}"),
                (
                    "Block fifth percentile: "
                    f"{_decimal(statistics.block_distribution.percentile_05)}"
                ),
                f"Cumulative gross funding: {_decimal(statistics.cumulative_gross_funding)}",
                (
                    "Gross annualized mean per matched leg notional: "
                    f"{_decimal(statistics.gross_annualized_mean)}"
                ),
                (f"Maximum gross-funding drawdown: {_decimal(statistics.maximum_drawdown)}"),
                (
                    "Cumulative gross funding without best month: "
                    f"{_decimal(statistics.cumulative_without_best_month)}"
                ),
            )
        )
        for holding in statistics.holding_windows:
            lines.append(
                f"Holding {holding.holding_days}d | samples={holding.distribution.count} | "
                f"median={_decimal(holding.distribution.median)} | "
                f"p05={_decimal(holding.distribution.percentile_05)}"
            )
    lines.extend((f"Source hashes: {len(report.source_hashes)}", _FOOTER))
    return "\n".join(lines)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump())
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
