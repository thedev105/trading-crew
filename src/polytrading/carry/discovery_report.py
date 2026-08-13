from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from polytrading.carry.discovery_models import VenueDiscoveryReport


def render_discovery_json(report: VenueDiscoveryReport) -> str:
    return json.dumps(_json_value(report), ensure_ascii=False, indent=2, sort_keys=True)


def render_discovery_text(report: VenueDiscoveryReport) -> str:
    lines = [
        "RESEARCH ONLY — NOT A TRADE RECOMMENDATION",
        (
            f"selected={report.selected_dossier_id or 'none'} | "
            f"reason={report.selection_reason_code} | "
            f"activation={report.activation_status}"
        ),
        "",
    ]
    for rank, candidate in enumerate(report.candidates, start=1):
        assets = ",".join(asset.value for asset in candidate.assets)
        lines.append(
            f"rank={rank} | dossier={candidate.dossier_id} | "
            f"pair={candidate.left_venue.value}->{candidate.right_venue.value} | "
            f"assets={assets} | status={candidate.status.value} | "
            f"matched={candidate.counts.matched} | "
            f"model_required={candidate.counts.model_required} | "
            f"blocking={candidate.counts.blocking} | "
            f"missing_evidence={candidate.counts.missing_evidence} | "
            f"primary={candidate.primary_reason_code or 'none'}"
        )
    lines.extend(
        (
            "",
            (
                "Next gate: collect public Lighter evidence and model costs; "
                "no trading authority exists."
            ),
        )
    )
    return "\n".join(lines)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump())
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
