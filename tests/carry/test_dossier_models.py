from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import ValidationError

from polytrading.carry.dossier import evaluate_dossier
from polytrading.carry.dossier_models import (
    CANONICAL_DOSSIER_CHECKS,
    ContractCompatibilityDossier,
    DossierCheck,
    DossierCheckKind,
    DossierJudgment,
    DossierSource,
    DossierStatus,
)
from polytrading.domain.models import Asset, Venue

DOSSIER_AT = datetime(2026, 8, 13, 12, tzinfo=UTC)
SOURCE_ID = "hyperliquid_contract_specifications"
EXCERPT = "these contracts are technically quanto contracts"
SOURCE_URL = "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications"


def dossier_source(**overrides: object) -> DossierSource:
    values: dict[str, object] = {
        "schema_version": 1,
        "source_id": SOURCE_ID,
        "venue": Venue.HYPERLIQUID,
        "url": SOURCE_URL,
        "title": "Contract specifications",
        "observed_at": DOSSIER_AT,
        "evidence_excerpt": EXCERPT,
        "excerpt_sha256": sha256(EXCERPT.encode()).hexdigest(),
    }
    values.update(overrides)
    return DossierSource(**values)


def dossier_check(
    kind: DossierCheckKind,
    *,
    judgment: DossierJudgment = DossierJudgment.MATCHED,
    reason_code: str | None = None,
    source_ids: tuple[str, ...] = (SOURCE_ID,),
    **overrides: object,
) -> DossierCheck:
    values: dict[str, object] = {
        "schema_version": 1,
        "kind": kind,
        "judgment": judgment,
        "reason_code": reason_code or f"{kind.value}_reason",
        "left_summary": f"Left evidence for {kind.value}",
        "right_summary": f"Right evidence for {kind.value}",
        "source_ids": source_ids,
    }
    values.update(overrides)
    return DossierCheck(**values)


def contract_dossier(
    *,
    sources: tuple[DossierSource, ...] | None = None,
    checks: tuple[DossierCheck, ...] | None = None,
    **overrides: object,
) -> ContractCompatibilityDossier:
    values: dict[str, object] = {
        "schema_version": 1,
        "dossier_id": "hyperliquid-dydx-core-v1",
        "left_venue": Venue.HYPERLIQUID,
        "right_venue": Venue.DYDX,
        "assets": (Asset.BTC, Asset.ETH, Asset.SOL),
        "observed_at": DOSSIER_AT,
        "decision_scope": "research_only",
        "warning": "Research only — no trading authority.",
        "sources": sources or (dossier_source(),),
        "checks": checks or tuple(dossier_check(kind) for kind in CANONICAL_DOSSIER_CHECKS),
    }
    values.update(overrides)
    return ContractCompatibilityDossier(**values)


def test_source_accepts_only_the_hash_of_the_exact_stored_excerpt() -> None:
    source = dossier_source()

    assert source.excerpt_sha256 == sha256(EXCERPT.encode()).hexdigest()
    with pytest.raises(ValidationError, match="excerpt hash"):
        dossier_source(evidence_excerpt=f"{EXCERPT} changed")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"url": "http://example.test/source"}, "HTTPS"),
        ({"url": "https://example.test/source"}, "official source"),
        ({"source_id": "Upper Case"}, "source ID"),
        ({"title": "   "}, "title"),
        ({"evidence_excerpt": "   ", "excerpt_sha256": "0" * 64}, "excerpt"),
    ],
)
def test_source_rejects_unverifiable_or_noncanonical_evidence(
    change: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        dossier_source(**change)


def test_dossier_rejects_source_observed_after_its_cutoff() -> None:
    future = dossier_source(observed_at=DOSSIER_AT + timedelta(microseconds=1))

    with pytest.raises(ValidationError, match="dossier observation"):
        contract_dossier(sources=(future,))


def test_dossier_requires_every_canonical_check_once_in_exact_order() -> None:
    checks = list(contract_dossier().checks)
    checks[0], checks[1] = checks[1], checks[0]

    with pytest.raises(ValidationError, match="canonical order"):
        contract_dossier(checks=tuple(checks))

    with pytest.raises(ValidationError, match="canonical order"):
        contract_dossier(checks=tuple(checks[1:]))


def test_dossier_rejects_unknown_and_uncited_sources() -> None:
    checks = list(contract_dossier().checks)
    checks[0] = dossier_check(checks[0].kind, source_ids=("unknown_source",))
    with pytest.raises(ValidationError, match="unknown source"):
        contract_dossier(checks=tuple(checks))

    extra = dossier_source(
        source_id="hyperliquid_funding",
        url="https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding",
    )
    with pytest.raises(ValidationError, match="uncited source"):
        contract_dossier(sources=(dossier_source(), extra))


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"right_venue": Venue.HYPERLIQUID}, "distinct"),
        ({"assets": (Asset.BTC, Asset.BTC)}, "unique canonical order"),
        ({"assets": (Asset.ETH, Asset.BTC)}, "unique canonical order"),
        ({"dossier_id": "Upper Case"}, "dossier ID"),
    ],
)
def test_dossier_rejects_ambiguous_identity_or_scope(
    overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        contract_dossier(**overrides)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"reason_code": "Upper Case"}, "reason code"),
        ({"left_summary": "  "}, "left summary"),
        ({"right_summary": "  "}, "right summary"),
        ({"source_ids": (SOURCE_ID, SOURCE_ID)}, "source IDs"),
        ({"source_ids": ()}, "source IDs"),
    ],
)
def test_check_rejects_ambiguous_or_duplicate_evidence_links(
    overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        dossier_check(DossierCheckKind.ASSET_AND_QUANTITY, **overrides)


@pytest.mark.parametrize(
    ("judgments", "expected_status", "primary_reason"),
    [
        (
            (DossierJudgment.MATCHED,) * 14,
            DossierStatus.COMPATIBLE,
            None,
        ),
        (
            (DossierJudgment.MODEL_REQUIRED,) + (DossierJudgment.MATCHED,) * 13,
            DossierStatus.MODEL_REQUIRED,
            "model_gap",
        ),
        (
            (DossierJudgment.MISSING_EVIDENCE,) + (DossierJudgment.MODEL_REQUIRED,) * 13,
            DossierStatus.EVIDENCE_INCOMPLETE,
            "missing_gap",
        ),
        (
            (DossierJudgment.MODEL_REQUIRED, DossierJudgment.BLOCKING)
            + (DossierJudgment.MISSING_EVIDENCE,) * 12,
            DossierStatus.INELIGIBLE,
            "blocking_gap",
        ),
    ],
)
def test_evaluator_uses_fail_closed_status_precedence(
    judgments: tuple[DossierJudgment, ...],
    expected_status: DossierStatus,
    primary_reason: str | None,
) -> None:
    checks = tuple(
        dossier_check(
            kind,
            judgment=judgment,
            reason_code=(
                "blocking_gap"
                if judgment is DossierJudgment.BLOCKING
                else "missing_gap"
                if judgment is DossierJudgment.MISSING_EVIDENCE
                else "model_gap"
                if judgment is DossierJudgment.MODEL_REQUIRED
                else "matched_fact"
            ),
        )
        for kind, judgment in zip(CANONICAL_DOSSIER_CHECKS, judgments, strict=True)
    )

    report = evaluate_dossier(contract_dossier(checks=checks))

    assert report.status is expected_status
    assert report.primary_reason_code == primary_reason
    assert sum(report.counts.model_dump().values()) == 14
    assert report.sources == contract_dossier(checks=checks).sources
    assert report.checks == checks
    assert report.activation_status == "not_authorized"
