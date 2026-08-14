from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from polytrading.trial.funding_models import LighterDydxFundingCycle


def render_trial_funding_text(cycle: LighterDydxFundingCycle) -> str:
    lines = [
        "Lighter-dYdX prospective funding cycle v1 | "
        f"boundary={_timestamp(cycle.cycle_end)} | status={cycle.status.value}",
        f"Attempt: started={_timestamp(cycle.request_started_at)} | "
        f"completed={_timestamp(cycle.request_completed_at)}",
    ]
    for item in cycle.items:
        reasons = ",".join(item.reason_codes) if item.reason_codes else "none"
        lines.append(
            f"{item.venue.value} {item.asset.value} | "
            f"instrument={item.instrument_outcome.value} | "
            f"funding={item.funding_outcome.value} | reasons={reasons}"
        )
    lines.append("")
    lines.extend(cycle.warnings)
    return "\n".join(lines)


def render_trial_funding_json(cycle: LighterDydxFundingCycle) -> str:
    return json.dumps(_json_value(cycle), ensure_ascii=False, indent=2, sort_keys=True)


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
