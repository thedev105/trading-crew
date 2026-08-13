from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from polytrading.corpus_intake.source_policy import (
    IntendedUseScope,
    SourceEvidence,
    SourceUseApproval,
    SourceUseAssessment,
    canonical_sha256,
    evaluate_source_gate,
)

NOW = datetime(2026, 8, 12, 16, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def scope(**changes: object) -> IntendedUseScope:
    values: dict[str, object] = {
        "schema_version": 1,
        "source": "polymarket",
        "maximum_records": 1_000,
        "local_retention": True,
        "derived_semantic_labels": True,
        "offline_model_evaluation": True,
        "proprietary_trading_research": True,
        "redistribution": False,
        "generative_model_training": False,
    }
    values.update(changes)
    return IntendedUseScope(**values)


def assessment(**changes: object) -> SourceUseAssessment:
    intended_use = scope()
    values: dict[str, object] = {
        "schema_version": 1,
        "source": "polymarket",
        "assessed_at": NOW,
        "status": "requires_external_confirmation",
        "reason_code": "source_consultation_notice",
        "scope": intended_use,
        "scope_sha256": canonical_sha256(intended_use),
        "evidence_sha256s": (HASH_A,),
    }
    values.update(changes)
    return SourceUseAssessment(**values)


def approval(**changes: object) -> SourceUseApproval:
    values: dict[str, object] = {
        "schema_version": 1,
        "source": "polymarket",
        "approver_id": "human-legal-reviewer-001",
        "approver_role": "qualified_legal_review",
        "approval_reference": "internal-matter-2026-001",
        "approved_at": NOW - timedelta(days=1),
        "effective_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(days=30),
        "scope_sha256": canonical_sha256(scope()),
        "evidence_sha256s": (HASH_A,),
        "intake_manifest_sha256s": (HASH_B,),
    }
    values.update(changes)
    return SourceUseApproval(**values)


def test_automated_assessment_cannot_claim_approval() -> None:
    with pytest.raises(ValidationError):
        assessment(status="approved")


def test_scope_is_narrow_and_bounded() -> None:
    assert scope().maximum_records == 1_000
    with pytest.raises(ValidationError, match="less than or equal to 5000"):
        scope(maximum_records=5_001)
    with pytest.raises(ValidationError):
        scope(redistribution=True)
    with pytest.raises(ValidationError):
        scope(generative_model_training=True)


def test_evidence_rejects_long_or_hash_mismatched_excerpt() -> None:
    common = {
        "schema_version": 1,
        "source": "polymarket",
        "url": "https://institutional.polymarket.com/",
        "retrieved_at": NOW,
        "status_code": 200,
        "content_type": "text/html; charset=utf-8",
        "body_byte_count": 100,
        "body_sha256": HASH_A,
        "etag": None,
        "last_modified": None,
        "locator": "consultation with Polymarket and ICE",
        "full_body_retained": False,
    }
    excerpt = "Consultation is required for this intended data use."
    record = SourceEvidence(
        **common,
        excerpt=excerpt,
        excerpt_sha256=canonical_sha256(excerpt),
    )
    assert record.excerpt == excerpt

    with pytest.raises(ValidationError, match="25 words"):
        SourceEvidence(
            **common,
            excerpt=" ".join(f"word{number}" for number in range(26)),
            excerpt_sha256=HASH_B,
        )
    with pytest.raises(ValidationError, match="excerpt hash"):
        SourceEvidence(**common, excerpt=excerpt, excerpt_sha256=HASH_B)


def test_assessment_binds_exact_scope_and_sorted_unique_evidence() -> None:
    with pytest.raises(ValidationError, match="scope hash"):
        assessment(scope_sha256=HASH_C)
    with pytest.raises(ValidationError, match="sorted and unique"):
        assessment(evidence_sha256s=(HASH_B, HASH_A))
    with pytest.raises(ValidationError, match="sorted and unique"):
        assessment(evidence_sha256s=(HASH_A, HASH_A))
    with pytest.raises(ValidationError, match="reason code"):
        assessment(status="rejected")


def test_approval_requires_human_role_exact_hash_sets_and_valid_dates() -> None:
    with pytest.raises(ValidationError):
        approval(approver_role="automated_agent")
    with pytest.raises(ValidationError, match="sorted and unique"):
        approval(intake_manifest_sha256s=(HASH_C, HASH_B))
    with pytest.raises(ValidationError, match="expiry"):
        approval(expires_at=NOW - timedelta(hours=2))


def test_gate_accepts_only_exact_effective_human_approval() -> None:
    decision = evaluate_source_gate(
        assessment=assessment(),
        approval=approval(),
        scope=scope(),
        evidence_sha256s=(HASH_A,),
        intake_manifest_sha256s=(HASH_B,),
        as_of=NOW,
    )

    assert decision.allowed is True
    assert decision.reason_code == "exact_human_approval"
    assert decision.scope_sha256 == canonical_sha256(scope())
    assert decision.evidence_sha256s == (HASH_A,)
    assert decision.intake_manifest_sha256s == (HASH_B,)


@pytest.mark.parametrize(
    ("changed_approval", "as_of", "reason"),
    [
        ({"scope_sha256": HASH_C}, NOW, "approval_scope_mismatch"),
        ({"evidence_sha256s": (HASH_C,)}, NOW, "approval_evidence_mismatch"),
        ({"intake_manifest_sha256s": (HASH_C,)}, NOW, "approval_manifest_mismatch"),
        (
            {"effective_at": NOW + timedelta(hours=1), "expires_at": NOW + timedelta(days=1)},
            NOW,
            "approval_not_effective",
        ),
        ({"expires_at": NOW}, NOW, "approval_expired"),
    ],
)
def test_gate_fails_closed_for_nonexact_or_inactive_approval(
    changed_approval: dict[str, object], as_of: datetime, reason: str
) -> None:
    decision = evaluate_source_gate(
        assessment=assessment(),
        approval=approval(**changed_approval),
        scope=scope(),
        evidence_sha256s=(HASH_A,),
        intake_manifest_sha256s=(HASH_B,),
        as_of=as_of,
    )

    assert decision.allowed is False
    assert decision.reason_code == reason


def test_gate_rejects_missing_approval_and_assessment_binding_mismatch() -> None:
    missing = evaluate_source_gate(
        assessment=assessment(),
        approval=None,
        scope=scope(),
        evidence_sha256s=(HASH_A,),
        intake_manifest_sha256s=(HASH_B,),
        as_of=NOW,
    )
    wrong_evidence = evaluate_source_gate(
        assessment=assessment(),
        approval=approval(),
        scope=scope(),
        evidence_sha256s=(HASH_C,),
        intake_manifest_sha256s=(HASH_B,),
        as_of=NOW,
    )

    assert missing.reason_code == "external_confirmation_required"
    assert wrong_evidence.reason_code == "assessment_evidence_mismatch"


def test_rejected_assessment_cannot_be_overridden_by_approval() -> None:
    rejected = assessment(status="rejected", reason_code="human_rejected")

    decision = evaluate_source_gate(
        assessment=rejected,
        approval=approval(),
        scope=scope(),
        evidence_sha256s=(HASH_A,),
        intake_manifest_sha256s=(HASH_B,),
        as_of=NOW,
    )

    assert decision.reason_code == "source_use_rejected"
    assert decision.allowed is False
