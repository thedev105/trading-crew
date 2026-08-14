from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from polytrading.trial.health_models import LighterDydxTrialHealthReport, TrialEvidenceStatus


def render_trial_health_json(report: LighterDydxTrialHealthReport) -> str:
    """Render canonical, lossless trial health JSON."""
    return json.dumps(_json_value(report), ensure_ascii=False, indent=2, sort_keys=True)


def render_trial_health_text(report: LighterDydxTrialHealthReport) -> str:
    projection = report.assets[0].projected_earliest_evaluation_end
    lines = [
        "Lighter-dYdX prospective evidence trial health v1",
        f"status: {report.status.value}",
        f"cutoff: {_timestamp(report.as_of)}",
        f"latest auditable boundary: {_timestamp(report.latest_auditable_boundary)}",
        "trial start: "
        + (
            "not started"
            if report.trial_started_at is None
            else _timestamp(report.trial_started_at)
        ),
        f"elapsed/target hours: {report.elapsed_auditable_hours}/2160",
        "projection: "
        + ("unavailable" if projection is None else _timestamp(projection))
        + " — collection-only projection assuming complete future boundaries",
    ]
    for asset in report.assets:
        lines.extend(
            (
                f"{asset.asset.value} coverage: "
                f"training={_decimal(asset.training_funding_coverage)} "
                f"evaluation={_decimal(asset.evaluation_funding_coverage)} "
                f"total={_decimal(asset.total_funding_coverage)} "
                f"books={_decimal(asset.book_coverage)}",
                f"{asset.asset.value} current 168: "
                f"paired={asset.current_funding_paired_hours}/168 "
                f"consecutive={'yes' if asset.current_funding_consecutive else 'no'} "
                f"dense_pairs={asset.dense_book_pair_count} "
                f"consecutive_samples={asset.consecutive_dense_sample_count}",
                f"{asset.asset.value} depth: "
                f"completed={_optional_timestamp(asset.latest_book_completed_at)} "
                f"age_seconds={_optional_decimal(asset.latest_book_age_seconds)} "
                f"skew_ms={_optional_decimal(asset.latest_book_skew_ms)} "
                f"fresh={'yes' if asset.fresh_book_ready else 'no'}",
            )
        )
    gaps = tuple(
        item for item in report.recent_boundaries if item.status is not TrialEvidenceStatus.COMPLETE
    )
    if gaps:
        lines.append("recent gaps:")
        lines.extend(
            f"- {_timestamp(item.cycle_end)}: {','.join(item.reason_codes)}" for item in gaps
        )
    else:
        lines.append("recent gaps: none")
    lines.extend(
        (
            "dossier evidence: " + ("available" if report.dossier_available else "unavailable"),
            f"fee evidence: reviewed schedules={len(report.reviewed_fees)}; "
            "no fee tier selected or recommended",
            "operator policy: not assessed",
            "warnings:",
            *(f"- {warning}" for warning in report.warnings),
        )
    )
    return "\n".join(lines) + "\n"


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_timestamp(value: datetime | None) -> str:
    return "unavailable" if value is None else _timestamp(value)


def _decimal(value: Decimal) -> str:
    return str(value)


def _optional_decimal(value: Decimal | None) -> str:
    return "unavailable" if value is None else _decimal(value)


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
