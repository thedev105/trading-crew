from __future__ import annotations

from polytrading.carry.dossier_models import (
    ContractCompatibilityDossier,
    ContractDossierReport,
    DossierJudgment,
    DossierJudgmentCounts,
    DossierStatus,
)

_STATUS_PRECEDENCE = (
    (DossierJudgment.BLOCKING, DossierStatus.INELIGIBLE),
    (DossierJudgment.MISSING_EVIDENCE, DossierStatus.EVIDENCE_INCOMPLETE),
    (DossierJudgment.MODEL_REQUIRED, DossierStatus.MODEL_REQUIRED),
)


def evaluate_dossier(dossier: ContractCompatibilityDossier) -> ContractDossierReport:
    """Summarize a complete dossier without interpreting venue-specific facts."""
    counts = DossierJudgmentCounts(
        matched=sum(check.judgment is DossierJudgment.MATCHED for check in dossier.checks),
        blocking=sum(check.judgment is DossierJudgment.BLOCKING for check in dossier.checks),
        model_required=sum(
            check.judgment is DossierJudgment.MODEL_REQUIRED for check in dossier.checks
        ),
        missing_evidence=sum(
            check.judgment is DossierJudgment.MISSING_EVIDENCE for check in dossier.checks
        ),
    )
    status = DossierStatus.COMPATIBLE
    primary_reason_code: str | None = None
    for judgment, candidate_status in _STATUS_PRECEDENCE:
        winner = next(
            (check for check in dossier.checks if check.judgment is judgment),
            None,
        )
        if winner is not None:
            status = candidate_status
            primary_reason_code = winner.reason_code
            break

    return ContractDossierReport(
        schema_version=1,
        dossier_id=dossier.dossier_id,
        left_venue=dossier.left_venue,
        right_venue=dossier.right_venue,
        assets=dossier.assets,
        observed_at=dossier.observed_at,
        warning=dossier.warning,
        status=status,
        primary_reason_code=primary_reason_code,
        counts=counts,
        sources=dossier.sources,
        checks=dossier.checks,
        activation_status="not_authorized",
    )
