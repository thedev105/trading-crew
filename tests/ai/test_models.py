from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.ai.models import (
    ContractSpanEvidence,
    CriticalField,
    GoldContract,
    ModelCard,
    RelationshipCandidateArtifact,
    RuleExtractionArtifact,
    RuleFieldSet,
    SourceSpan,
)

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
HASH = "a" * 64


def known(value: str = "value") -> CriticalField:
    return CriticalField(
        status="known",
        value=value,
        supporting_spans=(
            SourceSpan(
                start_char=0,
                end_char=5,
                exact_text="value",
                canonical_text_hash=HASH,
            ),
        ),
    )


def fields(**overrides: object) -> RuleFieldSet:
    values = {field: known() for field in RuleFieldSet.model_fields}
    values["rule_hash"] = known(HASH)
    values.update(overrides)
    return RuleFieldSet(**values)


def model_card(**overrides: object) -> ModelCard:
    values: dict[str, object] = {
        "schema_version": 1,
        "model_id": "rule-regex-baseline",
        "version": "1.0.0",
        "owner": "research",
        "intended_use": "offline rule extraction research",
        "prohibited_uses": (
            "trade_approval",
            "order_submission",
            "risk_limit_changes",
            "credential_access",
        ),
        "authority": "research_only",
        "implementation_kind": "deterministic_baseline",
        "training_cutoff": NOW,
        "prompt_version": "rules-v1",
        "feature_version": "features-v1",
        "validation_dataset_hash": HASH,
        "status": "validated",
        "approved_at": NOW,
        "expires_at": NOW.replace(year=2027),
    }
    values.update(overrides)
    return ModelCard(**values)


def artifact(**overrides: object) -> RuleExtractionArtifact:
    values: dict[str, object] = {
        "schema_version": 1,
        "artifact_id": UUID("00000000-0000-0000-0000-000000000111"),
        "contract_id": "contract-001",
        "information_cutoff": NOW,
        "source_hashes": (HASH,),
        "model_id": "rule-regex-baseline",
        "model_version": "1.0.0",
        "prompt_version": "rules-v1",
        "inference_parameters_hash": HASH,
        "extracted_fields": fields(),
        "uncertainty": Decimal("0.1"),
        "abstention_reason": None,
        "inference_latency_ms": Decimal("2.5"),
        "inference_cost_usd": Decimal("0"),
        "created_at": NOW,
        "expires_at": NOW.replace(year=2027),
        "invalidation_conditions": ("source revision",),
    }
    values.update(overrides)
    return RuleExtractionArtifact(**values)


def relationship_evidence(contract_id: str) -> ContractSpanEvidence:
    return ContractSpanEvidence(contract_id=contract_id, supporting_spans=known().supporting_spans)


def relationship_artifact(**overrides: object) -> RelationshipCandidateArtifact:
    values: dict[str, object] = {
        "schema_version": 1,
        "artifact_id": UUID("00000000-0000-0000-0000-000000000112"),
        "member_contract_ids": ("contract-001", "contract-002"),
        "proposed_relationship": "nested but verify oracle",
        "supporting_evidence": (
            relationship_evidence("contract-001"),
            relationship_evidence("contract-002"),
        ),
        "model_id": "relationship-baseline",
        "model_version": "1.0.0",
        "information_cutoff": NOW,
        "uncertainty": Decimal("0.2"),
        "abstention_reason": None,
        "created_at": NOW,
        "expires_at": NOW.replace(year=2027),
    }
    values.update(overrides)
    return RelationshipCandidateArtifact(**values)


def test_known_critical_field_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="known field requires supporting spans"):
        CriticalField(status="known", value=">=", supporting_spans=())


def test_unknown_critical_field_cannot_carry_a_guess() -> None:
    with pytest.raises(ValidationError, match="unknown field cannot have a value"):
        CriticalField(status="unknown", value="UTC", supporting_spans=())


def test_unknown_critical_field_cannot_carry_supporting_spans() -> None:
    with pytest.raises(ValidationError, match="unknown field cannot have supporting spans"):
        CriticalField(status="unknown", value=None, supporting_spans=(known().supporting_spans[0],))


def test_records_reject_unknown_fields_and_mutation() -> None:
    card = model_card()

    with pytest.raises(ValidationError, match="Extra inputs"):
        model_card(unrecognized=True)
    with pytest.raises(ValidationError, match="frozen"):
        card.owner = "changed"  # type: ignore[misc]


def test_gold_contract_rejects_naive_timestamps_and_invalid_hashes() -> None:
    values = {
        "schema_version": 1,
        "contract_id": "contract-001",
        "source_url": "https://example.test/contract-001",
        "source_retrieved_at": NOW,
        "information_cutoff": NOW,
        "raw_text": "rule text",
        "raw_text_hash": HASH,
        "canonical_text": "rule text",
        "canonical_text_hash": HASH,
        "event_family": "btc-price",
        "sampling_stratum": "inclusive threshold",
        "split": "train",
    }

    with pytest.raises(ValidationError, match="timezone-aware"):
        GoldContract(**(values | {"information_cutoff": NOW.replace(tzinfo=None)}))
    with pytest.raises(ValidationError, match="64 lowercase hexadecimal"):
        GoldContract(**(values | {"raw_text_hash": "invalid"}))


def test_model_card_rejects_non_research_authority_and_missing_safety_prohibitions() -> None:
    with pytest.raises(ValidationError):
        model_card(authority="execution")
    with pytest.raises(ValidationError, match="prohibited uses must include"):
        model_card(prohibited_uses=("trade_approval",))


def test_artifact_rejects_negative_or_nonfinite_cost_and_expired_lifetime() -> None:
    with pytest.raises(ValidationError, match="inference cost must be nonnegative"):
        artifact(inference_cost_usd=Decimal("-0.01"))
    with pytest.raises(ValidationError):
        artifact(inference_latency_ms=Decimal("NaN"))
    with pytest.raises(ValidationError, match="artifact must expire after creation"):
        artifact(expires_at=NOW)


def test_relationship_artifact_rejects_missing_member_evidence() -> None:
    with pytest.raises(
        ValidationError,
        match="relationship evidence is missing member contract IDs",
    ):
        relationship_artifact(supporting_evidence=(relationship_evidence("contract-001"),))


def test_relationship_artifact_rejects_duplicate_evidence_contract_ids() -> None:
    with pytest.raises(
        ValidationError,
        match="relationship evidence contract IDs must be unique",
    ):
        relationship_artifact(
            supporting_evidence=(
                relationship_evidence("contract-001"),
                relationship_evidence("contract-001"),
            )
        )


def test_relationship_artifact_rejects_non_member_evidence() -> None:
    with pytest.raises(
        ValidationError,
        match="relationship evidence contains non-member contract IDs",
    ):
        relationship_artifact(
            supporting_evidence=(
                relationship_evidence("contract-001"),
                relationship_evidence("contract-002"),
                relationship_evidence("contract-003"),
            )
        )
