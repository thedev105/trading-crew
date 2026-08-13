from datetime import UTC, datetime
from hashlib import sha256
from importlib import resources

from polytrading.carry.dossier import evaluate_dossier, load_bundled_dossier
from polytrading.carry.dossier_models import (
    CANONICAL_DOSSIER_CHECKS,
    DossierStatus,
)
from polytrading.domain.models import Asset, Venue


def test_bundled_dossier_is_complete_and_fail_closed() -> None:
    dossier = load_bundled_dossier()
    report = evaluate_dossier(dossier)

    assert dossier.dossier_id == "hyperliquid-dydx-core-v1"
    assert dossier.left_venue is Venue.HYPERLIQUID
    assert dossier.right_venue is Venue.DYDX
    assert dossier.assets == (Asset.BTC, Asset.ETH, Asset.SOL)
    assert dossier.observed_at == datetime(2026, 8, 13, 12, tzinfo=UTC)
    assert len(dossier.sources) == 13
    assert tuple(check.kind for check in dossier.checks) == CANONICAL_DOSSIER_CHECKS
    assert report.status is DossierStatus.INELIGIBLE
    assert report.primary_reason_code == "quanto_structure_excluded"
    assert report.counts.model_dump() == {
        "matched": 3,
        "blocking": 1,
        "model_required": 6,
        "missing_evidence": 4,
    }
    assert report.activation_status == "not_authorized"


def test_bundled_dossier_preserves_exact_repeatable_excerpt_evidence() -> None:
    resource = resources.files("polytrading.carry.dossiers").joinpath(
        "hyperliquid-dydx-core-v1.json"
    )

    assert resource.read_bytes() == resource.read_bytes()
    dossier = load_bundled_dossier()
    assert all(
        sha256(source.evidence_excerpt.encode("utf-8")).hexdigest() == source.excerpt_sha256
        for source in dossier.sources
    )
    assert {source.source_id for source in dossier.sources} == {
        source_id for check in dossier.checks for source_id in check.source_ids
    }
