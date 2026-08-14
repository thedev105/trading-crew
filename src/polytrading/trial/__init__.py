"""Prospective, research-only evidence operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from polytrading.trial.book_evidence import (
        EligibleTrialBookPair,
        eligible_lighter_dydx_book_pair,
        select_hourly_trial_books,
    )
    from polytrading.trial.health import (
        LighterDydxTrialHealthAuditor,
        ProjectedAssetEvidence,
        project_earliest_evaluation_end,
    )
    from polytrading.trial.health_models import (
        LighterDydxTrialHealthReport,
        TrialCollectionStatus,
        TrialEvidenceStatus,
    )
    from polytrading.trial.health_report import (
        render_trial_health_json,
        render_trial_health_text,
    )

__all__ = [
    "EligibleTrialBookPair",
    "LighterDydxTrialHealthAuditor",
    "LighterDydxTrialHealthReport",
    "ProjectedAssetEvidence",
    "TrialCollectionStatus",
    "TrialEvidenceStatus",
    "eligible_lighter_dydx_book_pair",
    "project_earliest_evaluation_end",
    "render_trial_health_json",
    "render_trial_health_text",
    "select_hourly_trial_books",
]


def __getattr__(name: str) -> Any:
    if name in {
        "EligibleTrialBookPair",
        "eligible_lighter_dydx_book_pair",
        "select_hourly_trial_books",
    }:
        from polytrading.trial import book_evidence

        return getattr(book_evidence, name)
    if name in {
        "LighterDydxTrialHealthAuditor",
        "ProjectedAssetEvidence",
        "project_earliest_evaluation_end",
    }:
        from polytrading.trial import health

        return getattr(health, name)
    if name in {
        "LighterDydxTrialHealthReport",
        "TrialCollectionStatus",
        "TrialEvidenceStatus",
    }:
        from polytrading.trial import health_models

        return getattr(health_models, name)
    if name in {"render_trial_health_json", "render_trial_health_text"}:
        from polytrading.trial import health_report

        return getattr(health_report, name)
    raise AttributeError(name)
