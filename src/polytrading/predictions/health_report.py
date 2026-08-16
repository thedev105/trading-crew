from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from polytrading.predictions.health import PredictionHealthReport


def render_prediction_health_json(report: PredictionHealthReport) -> str:
    return json.dumps(_json_value(report), ensure_ascii=False, indent=2, sort_keys=True)


def render_prediction_health_text(report: PredictionHealthReport) -> str:
    lines = [f"Prediction-market health v1 | {_timestamp(report.as_of)}"]
    for venue in report.venues:
        age = (
            "none" if venue.latest_book_age_seconds is None else str(venue.latest_book_age_seconds)
        )
        reasons = ",".join(venue.reason_codes) if venue.reason_codes else "none"
        lines.append(
            f"{venue.venue.value} | {venue.status.value} | "
            f"gate_allowed={venue.collection_gate.allowed} | "
            f"markets={venue.market_count} | latest_book_age_seconds={age} | "
            f"reasons={reasons}"
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
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value
