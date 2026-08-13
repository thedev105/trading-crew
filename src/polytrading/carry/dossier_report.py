from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from polytrading.carry.dossier_models import ContractDossierReport


def render_dossier_json(report: ContractDossierReport) -> str:
    return json.dumps(_json_value(report), ensure_ascii=False, indent=2, sort_keys=True)


def render_dossier_text(report: ContractDossierReport) -> str:
    assets = ",".join(asset.value for asset in report.assets)
    lines = [
        "RESEARCH ONLY — NOT A TRADE RECOMMENDATION",
        (
            f"status={report.status.value} | pair={report.left_venue.value}->"
            f"{report.right_venue.value} | assets={assets}"
        ),
        f"primary_blocker={report.primary_reason_code or 'none'}",
        "",
    ]
    lines.extend(
        (
            f"check={check.kind.value} | judgment={check.judgment.value} | "
            f"reason={check.reason_code} | left={check.left_summary} | "
            f"right={check.right_summary}"
        )
        for check in report.checks
    )
    lines.extend(("", "No cost model or trading authority exists."))
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
