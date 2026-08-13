from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from polytrading.carry.discovery import evaluate_discovery
from polytrading.carry.dossier import evaluate_dossier, load_bundled_dossier
from polytrading.carry.dossier_models import (
    ContractDossierReport,
    DossierJudgmentCounts,
    DossierStatus,
    ResearchVenue,
)

BASE_AT = datetime(2026, 8, 13, 12, tzinfo=UTC)


def _counts(status: DossierStatus) -> DossierJudgmentCounts:
    values = {
        DossierStatus.COMPATIBLE: (14, 0, 0, 0),
        DossierStatus.MODEL_REQUIRED: (13, 0, 1, 0),
        DossierStatus.EVIDENCE_INCOMPLETE: (13, 0, 0, 1),
        DossierStatus.INELIGIBLE: (13, 1, 0, 0),
    }[status]
    return DossierJudgmentCounts(
        matched=values[0],
        blocking=values[1],
        model_required=values[2],
        missing_evidence=values[3],
    )


def _report(
    status: DossierStatus,
    dossier_id: str,
    left: ResearchVenue,
    right: ResearchVenue,
    *,
    observed_at: datetime = BASE_AT,
) -> ContractDossierReport:
    base = evaluate_dossier(load_bundled_dossier())
    return ContractDossierReport(
        **(
            base.model_dump()
            | {
                "dossier_id": dossier_id,
                "left_venue": left,
                "right_venue": right,
                "observed_at": observed_at,
                "status": status,
                "primary_reason_code": (
                    None if status is DossierStatus.COMPATIBLE else f"{status.value}_reason"
                ),
                "counts": _counts(status),
            }
        )
    )


def test_discovery_ranks_statuses_and_selects_best_complete_candidate() -> None:
    compatible = _report(
        DossierStatus.COMPATIBLE,
        "compatible-pair-v1",
        ResearchVenue.LIGHTER,
        ResearchVenue.DYDX,
    )
    model_required = _report(
        DossierStatus.MODEL_REQUIRED,
        "model-pair-v1",
        ResearchVenue.DYDX,
        ResearchVenue.LIGHTER,
    )
    incomplete = _report(
        DossierStatus.EVIDENCE_INCOMPLETE,
        "incomplete-pair-v1",
        ResearchVenue.HYPERLIQUID,
        ResearchVenue.LIGHTER,
    )
    ineligible = _report(
        DossierStatus.INELIGIBLE,
        "ineligible-pair-v1",
        ResearchVenue.HYPERLIQUID,
        ResearchVenue.DYDX,
    )

    report = evaluate_discovery((ineligible, incomplete, model_required, compatible))

    assert tuple(item.status for item in report.candidates) == (
        DossierStatus.COMPATIBLE,
        DossierStatus.MODEL_REQUIRED,
        DossierStatus.EVIDENCE_INCOMPLETE,
        DossierStatus.INELIGIBLE,
    )
    assert report.selected_dossier_id == "compatible-pair-v1"
    assert report.selection_reason_code == "best_nonblocking_complete_evidence"
    assert report.counts.model_dump() == {
        "compatible": 1,
        "model_required": 1,
        "evidence_incomplete": 1,
        "ineligible": 1,
    }
    assert report.activation_status == "not_authorized"


def test_discovery_uses_dossier_id_as_stable_status_tie_breaker() -> None:
    zulu = _report(
        DossierStatus.MODEL_REQUIRED,
        "zulu-pair-v1",
        ResearchVenue.DYDX,
        ResearchVenue.LIGHTER,
    )
    alpha = _report(
        DossierStatus.MODEL_REQUIRED,
        "alpha-pair-v1",
        ResearchVenue.LIGHTER,
        ResearchVenue.DYDX,
    )

    report = evaluate_discovery((zulu, alpha))

    assert tuple(item.dossier_id for item in report.candidates) == (
        "alpha-pair-v1",
        "zulu-pair-v1",
    )
    assert report.selected_dossier_id == "alpha-pair-v1"


def test_discovery_keeps_rejected_and_incomplete_catalog_without_selection() -> None:
    ineligible = _report(
        DossierStatus.INELIGIBLE,
        "ineligible-pair-v1",
        ResearchVenue.HYPERLIQUID,
        ResearchVenue.DYDX,
    )
    incomplete = _report(
        DossierStatus.EVIDENCE_INCOMPLETE,
        "incomplete-pair-v1",
        ResearchVenue.HYPERLIQUID,
        ResearchVenue.LIGHTER,
    )

    report = evaluate_discovery((ineligible, incomplete))

    assert report.selected_dossier_id is None
    assert report.selection_reason_code == "no_advanceable_candidate"


def test_discovery_rejects_empty_duplicate_or_unsafe_catalogs() -> None:
    with pytest.raises(ValidationError, match="at least one candidate"):
        evaluate_discovery(())

    first = _report(
        DossierStatus.MODEL_REQUIRED,
        "same-pair-v1",
        ResearchVenue.LIGHTER,
        ResearchVenue.DYDX,
    )
    duplicate_id = _report(
        DossierStatus.MODEL_REQUIRED,
        "same-pair-v1",
        ResearchVenue.DYDX,
        ResearchVenue.LIGHTER,
    )
    with pytest.raises(ValidationError, match="candidate IDs must be unique"):
        evaluate_discovery((first, duplicate_id))

    duplicate_pair = _report(
        DossierStatus.MODEL_REQUIRED,
        "different-id-v1",
        ResearchVenue.LIGHTER,
        ResearchVenue.DYDX,
    )
    with pytest.raises(ValidationError, match="candidate venue pairs must be unique"):
        evaluate_discovery((first, duplicate_pair))

    unsafe = first.model_copy(
        update={
            "counts": DossierJudgmentCounts(
                matched=12,
                blocking=1,
                model_required=1,
                missing_evidence=0,
            )
        }
    )
    with pytest.raises(
        ValidationError, match="selected candidate must have no blocking or missing"
    ):
        evaluate_discovery((unsafe,))


def test_discovery_observation_is_newest_candidate_timestamp() -> None:
    older = _report(
        DossierStatus.INELIGIBLE,
        "older-pair-v1",
        ResearchVenue.HYPERLIQUID,
        ResearchVenue.DYDX,
    )
    newer_at = BASE_AT + timedelta(minutes=3)
    newer = _report(
        DossierStatus.MODEL_REQUIRED,
        "newer-pair-v1",
        ResearchVenue.LIGHTER,
        ResearchVenue.DYDX,
        observed_at=newer_at,
    )

    assert evaluate_discovery((older, newer)).observed_at == newer_at
