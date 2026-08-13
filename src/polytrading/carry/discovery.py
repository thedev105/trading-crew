from __future__ import annotations

from datetime import UTC, datetime

from polytrading.carry.discovery_models import (
    DISCOVERY_STATUS_RANK,
    DiscoveryStatusCounts,
    VenueDiscoveryReport,
)
from polytrading.carry.dossier_models import (
    RESEARCH_ONLY_WARNING,
    ContractDossierReport,
    DossierStatus,
)

_ADVANCEABLE_STATUSES = frozenset({DossierStatus.COMPATIBLE, DossierStatus.MODEL_REQUIRED})


def evaluate_discovery(
    reports: tuple[ContractDossierReport, ...],
) -> VenueDiscoveryReport:
    """Rank immutable dossier reports without reinterpreting venue evidence."""
    candidates = tuple(
        sorted(
            reports,
            key=lambda report: (DISCOVERY_STATUS_RANK[report.status], report.dossier_id),
        )
    )
    selected = next(
        (candidate for candidate in candidates if candidate.status in _ADVANCEABLE_STATUSES),
        None,
    )
    return VenueDiscoveryReport(
        schema_version=1,
        observed_at=max(
            (candidate.observed_at for candidate in candidates),
            default=datetime(1970, 1, 1, tzinfo=UTC),
        ),
        warning=RESEARCH_ONLY_WARNING,
        candidates=candidates,
        counts=DiscoveryStatusCounts(
            compatible=sum(
                candidate.status is DossierStatus.COMPATIBLE for candidate in candidates
            ),
            model_required=sum(
                candidate.status is DossierStatus.MODEL_REQUIRED for candidate in candidates
            ),
            evidence_incomplete=sum(
                candidate.status is DossierStatus.EVIDENCE_INCOMPLETE for candidate in candidates
            ),
            ineligible=sum(
                candidate.status is DossierStatus.INELIGIBLE for candidate in candidates
            ),
        ),
        selected_dossier_id=None if selected is None else selected.dossier_id,
        selection_reason_code=(
            "no_advanceable_candidate" if selected is None else "best_nonblocking_complete_evidence"
        ),
        activation_status="not_authorized",
    )
