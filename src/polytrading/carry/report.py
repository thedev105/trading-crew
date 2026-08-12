from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from polytrading.carry.audit import CarryAuditReport

_WARNINGS = (
    "RESEARCH ONLY — NOT A TRADE RECOMMENDATION",
    "No credentials, balances, positions, or orders were accessed.",
    "Instantaneous annualization is diagnostic, not a funding forecast.",
)
_MISSING_ACTIVATION_EVIDENCE = (
    "12 months point-in-time history",
    "45 continuous days of synchronized books",
    "fee and slippage models",
    "reversal/forced-exit reserve",
    "complete stress suite",
    "90 forward days",
    "ledger reconciliation",
    "eligibility review",
)


def render_json(report: CarryAuditReport) -> str:
    return json.dumps(_json_value(report), ensure_ascii=False, indent=2, sort_keys=True)


def render_text(report: CarryAuditReport) -> str:
    lines = [*_WARNINGS, ""]
    for row in report.assets:
        spread = row.diagnostic.hourly_spread if row.diagnostic is not None else "unavailable"
        reasons = ",".join(row.reason_codes) if row.reason_codes else "none"
        lines.append(
            f"{row.asset.value} | status={row.status.value} | hourly_spread={spread} | "
            f"funding_ready={str(row.funding_ready).lower()} | "
            f"book_ready={str(row.book_ready).lower()} | reasons={reasons}"
        )
    lines.extend(("", "Missing activation evidence:"))
    lines.extend(f"- {item}" for item in _MISSING_ACTIVATION_EVIDENCE)
    return "\n".join(lines)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump())
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
