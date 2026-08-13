from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from polytrading.venues.funding_health_models import FundingCollectionHealthReport


def render_funding_health_json(report: FundingCollectionHealthReport) -> str:
    return json.dumps(_json_value(report), ensure_ascii=False, indent=2, sort_keys=True)


def render_funding_health_text(report: FundingCollectionHealthReport) -> str:
    lines = [
        f"Funding collection health v1 | {_timestamp(report.as_of)} | {report.status.value}",
        (
            f"Boundaries: {_timestamp(report.first_boundary)}.."
            f"{_timestamp(report.last_boundary)} | hours={report.requested_hours}"
        ),
        (
            f"Coverage: {report.complete_boundary_count}/{report.requested_hours} "
            f"({report.complete_coverage}) | "
            f"current_complete_streak={report.current_complete_streak}"
        ),
    ]
    for boundary in report.boundaries:
        selected = "none" if boundary.selected_cycle_id is None else str(boundary.selected_cycle_id)
        reasons = ",".join(boundary.reason_codes) if boundary.reason_codes else "none"
        lines.append(
            f"{_timestamp(boundary.cycle_end)} | {boundary.status.value} | "
            f"attempts={boundary.attempt_count} | "
            "complete/degraded/late="
            f"{boundary.complete_attempt_count}/"
            f"{boundary.degraded_attempt_count}/"
            f"{boundary.late_attempt_count} | "
            f"selected={selected} | reasons={reasons}"
        )
    lines.append("")
    lines.extend(report.warnings)
    return "\n".join(lines)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump())
    if isinstance(value, datetime):
        return _timestamp(value)
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
