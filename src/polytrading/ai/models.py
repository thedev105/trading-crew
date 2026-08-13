from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.domain.models import StrictRecord, normalize_utc_timestamp

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REQUIRED_PROHIBITED_USES = frozenset(
    {"trade_approval", "order_submission", "risk_limit_changes", "credential_access"}
)


class AIRecord(StrictRecord):
    @field_validator(
        "source_retrieved_at",
        "information_cutoff",
        "training_cutoff",
        "approved_at",
        "expires_at",
        "created_at",
        check_fields=False,
    )
    @classmethod
    def require_utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        return normalize_utc_timestamp(value)


def _require_sha256(value: str, label: str = "hash") -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must contain 64 lowercase hexadecimal characters")
    return value


class SourceSpan(AIRecord):
    start_char: int
    end_char: int
    exact_text: NonEmptyString
    canonical_text_hash: str

    @field_validator("canonical_text_hash")
    @classmethod
    def require_canonical_text_hash(cls, value: str) -> str:
        return _require_sha256(value, "canonical text hash")

    @model_validator(mode="after")
    def require_valid_bounds(self) -> SourceSpan:
        if self.start_char < 0 or self.end_char <= self.start_char:
            raise ValueError("source span must have nonempty nonnegative bounds")
        return self


class CriticalField(AIRecord):
    status: Literal["known", "unknown"]
    value: str | None
    supporting_spans: tuple[SourceSpan, ...]

    @model_validator(mode="after")
    def require_evidence_for_known_values(self) -> CriticalField:
        if self.status == "known":
            if self.value is None or not self.supporting_spans:
                raise ValueError("known field requires supporting spans")
        elif self.value is not None:
            raise ValueError("unknown field cannot have a value")
        elif self.supporting_spans:
            raise ValueError("unknown field cannot have supporting spans")
        return self


class RuleFieldSet(AIRecord):
    subject: CriticalField
    scope: CriticalField
    oracle: CriticalField
    source_instrument: CriticalField
    observation_date: CriticalField
    observation_time: CriticalField
    timezone: CriticalField
    observation_window: CriticalField
    operator: CriticalField
    inclusivity: CriticalField
    threshold: CriticalField
    unit: CriticalField
    precision: CriticalField
    rounding: CriticalField
    set_membership: CriticalField
    cancellation_clause: CriticalField
    postponement_clause: CriticalField
    substitution_clause: CriticalField
    dispute_clause: CriticalField
    clarification_clause: CriticalField
    fallback_clause: CriticalField
    payout_asset: CriticalField
    collateral_asset: CriticalField
    document_version: CriticalField
    rule_hash: CriticalField

    @field_validator("rule_hash")
    @classmethod
    def require_valid_known_rule_hash(cls, value: CriticalField) -> CriticalField:
        if value.status == "known":
            if value.value is None:
                raise ValueError("known field requires supporting spans")
            _require_sha256(value.value, "rule hash")
        return value


class GoldContract(AIRecord):
    schema_version: Literal[1]
    contract_id: NonEmptyString
    source_url: NonEmptyString
    source_retrieved_at: datetime
    information_cutoff: datetime
    raw_text: str
    raw_text_hash: str
    canonical_text: str
    canonical_text_hash: str
    event_family: NonEmptyString
    sampling_stratum: NonEmptyString
    split: Literal["train", "validation", "test"]

    @field_validator("raw_text_hash", "canonical_text_hash")
    @classmethod
    def require_text_hashes(cls, value: str) -> str:
        return _require_sha256(value)


class GoldRelationship(AIRecord):
    schema_version: Literal[1]
    relationship_id: NonEmptyString
    member_contract_ids: tuple[NonEmptyString, ...]
    split: Literal["train", "validation", "test"]

    @field_validator("member_contract_ids")
    @classmethod
    def require_distinct_members(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) < 2:
            raise ValueError("relationship must contain at least two members")
        if len(value) != len(set(value)):
            raise ValueError("relationship members must be unique")
        return value


class _GoldLabel(AIRecord):
    label_version: int
    adversarial_tags: tuple[NonEmptyString, ...]
    review_ids: tuple[NonEmptyString, ...]
    adjudication_id: NonEmptyString | None

    @field_validator("label_version")
    @classmethod
    def require_positive_label_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("label version must be positive")
        return value

    @field_validator("review_ids")
    @classmethod
    def require_at_most_two_unique_reviews(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 2:
            raise ValueError("label may contain at most two review IDs")
        if len(value) != len(set(value)):
            raise ValueError("review IDs must be unique")
        return value


class GoldContractLabel(_GoldLabel):
    schema_version: Literal[1]
    contract_id: NonEmptyString
    rule_template: NonEmptyString
    fields: RuleFieldSet


class GoldRelationshipLabel(_GoldLabel):
    schema_version: Literal[1]
    relationship_id: NonEmptyString
    label: Literal[
        "complement",
        "exhaustive_set",
        "implication",
        "nested_threshold",
        "nested_deadline",
        "range_identity",
        "non_equivalent",
    ]
    supported_template: bool


class ModelCard(AIRecord):
    schema_version: Literal[1]
    model_id: NonEmptyString
    version: NonEmptyString
    owner: NonEmptyString
    intended_use: NonEmptyString
    prohibited_uses: tuple[NonEmptyString, ...]
    authority: Literal["research_only"]
    implementation_kind: Literal["deterministic_baseline", "external_artifact_import"]
    training_cutoff: datetime | None
    prompt_version: NonEmptyString | None
    feature_version: NonEmptyString
    validation_dataset_hash: str | None
    status: Literal["draft", "validated", "revoked", "expired"]
    approved_at: datetime | None
    expires_at: datetime | None

    @field_validator("validation_dataset_hash")
    @classmethod
    def require_validation_dataset_hash(cls, value: str | None) -> str | None:
        if value is not None:
            return _require_sha256(value, "validation dataset hash")
        return value

    @model_validator(mode="after")
    def require_safety_boundary_and_valid_lifetime(self) -> ModelCard:
        missing = _REQUIRED_PROHIBITED_USES.difference(self.prohibited_uses)
        if missing:
            raise ValueError("prohibited uses must include " + ", ".join(sorted(missing)))
        if len(self.prohibited_uses) != len(set(self.prohibited_uses)):
            raise ValueError("prohibited uses must be unique")
        if (
            self.approved_at is not None
            and self.expires_at is not None
            and self.expires_at <= self.approved_at
        ):
            raise ValueError("model card must expire after approval")
        return self


class RuleExtractionArtifact(AIRecord):
    schema_version: Literal[1]
    artifact_id: UUID
    contract_id: NonEmptyString
    information_cutoff: datetime
    source_hashes: tuple[str, ...]
    model_id: NonEmptyString
    model_version: NonEmptyString
    prompt_version: NonEmptyString
    inference_parameters_hash: str
    extracted_fields: RuleFieldSet
    uncertainty: FiniteDecimal
    abstention_reason: str | None
    inference_latency_ms: FiniteDecimal
    inference_cost_usd: FiniteDecimal
    created_at: datetime
    expires_at: datetime
    invalidation_conditions: tuple[NonEmptyString, ...]

    @field_validator("source_hashes")
    @classmethod
    def require_source_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("artifact must contain at least one source hash")
        for source_hash in value:
            _require_sha256(source_hash, "source hash")
        return value

    @field_validator("inference_parameters_hash")
    @classmethod
    def require_inference_parameters_hash(cls, value: str) -> str:
        return _require_sha256(value, "inference parameters hash")

    @model_validator(mode="after")
    def require_safe_artifact_values(self) -> RuleExtractionArtifact:
        if self.uncertainty < 0:
            raise ValueError("uncertainty must be nonnegative")
        if self.inference_latency_ms < 0:
            raise ValueError("inference latency must be nonnegative")
        if self.inference_cost_usd < 0:
            raise ValueError("inference cost must be nonnegative")
        if self.expires_at <= self.created_at:
            raise ValueError("artifact must expire after creation")
        return self


class ContractSpanEvidence(AIRecord):
    contract_id: NonEmptyString
    supporting_spans: tuple[SourceSpan, ...]

    @field_validator("supporting_spans")
    @classmethod
    def require_supporting_spans(cls, value: tuple[SourceSpan, ...]) -> tuple[SourceSpan, ...]:
        if not value:
            raise ValueError("contract evidence requires supporting spans")
        return value


class RelationshipCandidateArtifact(AIRecord):
    schema_version: Literal[1]
    artifact_id: UUID
    member_contract_ids: tuple[NonEmptyString, ...]
    proposed_relationship: NonEmptyString
    supporting_evidence: tuple[ContractSpanEvidence, ...]
    model_id: NonEmptyString
    model_version: NonEmptyString
    information_cutoff: datetime
    uncertainty: FiniteDecimal
    abstention_reason: str | None
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def require_safe_artifact_values(self) -> RelationshipCandidateArtifact:
        if len(self.member_contract_ids) < 2:
            raise ValueError("relationship artifact must contain at least two members")
        if len(self.member_contract_ids) != len(set(self.member_contract_ids)):
            raise ValueError("relationship artifact members must be unique")
        evidence_ids = tuple(evidence.contract_id for evidence in self.supporting_evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("relationship evidence contract IDs must be unique")
        unexpected = set(evidence_ids).difference(self.member_contract_ids)
        if unexpected:
            raise ValueError(
                "relationship evidence contains non-member contract IDs: "
                + ", ".join(sorted(unexpected))
            )
        missing = set(self.member_contract_ids).difference(evidence_ids)
        if missing:
            raise ValueError(
                "relationship evidence is missing member contract IDs: "
                + ", ".join(sorted(missing))
            )
        if self.uncertainty < 0:
            raise ValueError("uncertainty must be nonnegative")
        if self.expires_at <= self.created_at:
            raise ValueError("artifact must expire after creation")
        return self
