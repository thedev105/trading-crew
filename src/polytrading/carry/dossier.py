from __future__ import annotations

from importlib.resources import files

from pydantic import ValidationError

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

BUNDLED_DOSSIER_IDS = (
    "hyperliquid-dydx-core-v1",
    "lighter-dydx-core-v1",
)


def load_bundled_dossier(
    dossier_id: str = "hyperliquid-dydx-core-v1",
) -> ContractCompatibilityDossier:
    """Load and validate the immutable contract dossier packaged with the application."""
    if dossier_id not in BUNDLED_DOSSIER_IDS:
        raise ValueError(f"unknown bundled dossier: {dossier_id}")
    try:
        payload = (
            files("polytrading.carry.dossiers")
            .joinpath(f"{dossier_id}.json")
            .read_text(encoding="utf-8")
        )
        dossier = ContractCompatibilityDossier.model_validate_json(payload)
    except (OSError, UnicodeError, ValidationError) as error:
        raise ValueError(f"invalid bundled dossier: {dossier_id}") from error
    if dossier.dossier_id != dossier_id:
        raise ValueError(f"invalid bundled dossier: {dossier_id}")
    return dossier


def load_bundled_dossiers() -> tuple[ContractCompatibilityDossier, ...]:
    """Load the explicit research catalog and reject ambiguous catalog identity."""
    dossiers = tuple(load_bundled_dossier(dossier_id) for dossier_id in BUNDLED_DOSSIER_IDS)
    dossier_ids = tuple(dossier.dossier_id for dossier in dossiers)
    pairs = tuple((dossier.left_venue, dossier.right_venue) for dossier in dossiers)
    if len(set(dossier_ids)) != len(dossier_ids):
        raise ValueError("invalid bundled dossier catalog: duplicate dossier ID")
    if len(set(pairs)) != len(pairs):
        raise ValueError("invalid bundled dossier catalog: duplicate venue pair")
    return dossiers


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
