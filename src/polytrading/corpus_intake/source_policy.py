from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.domain.models import StrictRecord, normalize_utc_timestamp

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GateReason = Literal[
    "source_use_rejected",
    "external_confirmation_required",
    "assessment_scope_mismatch",
    "assessment_evidence_mismatch",
    "approval_source_mismatch",
    "approval_scope_mismatch",
    "approval_evidence_mismatch",
    "approval_manifest_mismatch",
    "approval_not_issued",
    "approval_not_effective",
    "approval_expired",
    "exact_human_approval",
]


def canonical_sha256(value: object) -> str:
    if isinstance(value, StrictRecord):
        serializable: Any = value.model_dump(mode="json")
    else:
        serializable = value
    encoded = json.dumps(
        serializable,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IntendedUseScope(StrictRecord):
    schema_version: Literal[1]
    source: Literal["polymarket"]
    maximum_records: int = Field(ge=1, le=5_000)
    local_retention: Literal[True]
    derived_semantic_labels: Literal[True]
    offline_model_evaluation: Literal[True]
    proprietary_trading_research: Literal[True]
    redistribution: Literal[False]
    generative_model_training: Literal[False]


class SourceEvidence(StrictRecord):
    schema_version: Literal[1]
    source: Literal["polymarket"]
    url: NonEmptyString
    retrieved_at: datetime
    status_code: Literal[200]
    content_type: NonEmptyString
    body_byte_count: int = Field(ge=1, le=64 * 1024 * 1024)
    body_sha256: Sha256
    etag: str | None
    last_modified: str | None
    locator: NonEmptyString
    excerpt: NonEmptyString
    excerpt_sha256: Sha256
    full_body_retained: Literal[False]

    @field_validator("retrieved_at")
    @classmethod
    def require_retrieved_at_utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_short_exact_excerpt(self) -> SourceEvidence:
        if len(self.excerpt.split()) > 25:
            raise ValueError("source evidence excerpt must contain at most 25 words")
        if self.excerpt_sha256 != canonical_sha256(self.excerpt):
            raise ValueError("source evidence excerpt hash does not match excerpt")
        return self


class SourceUseAssessment(StrictRecord):
    schema_version: Literal[1]
    source: Literal["polymarket"]
    assessed_at: datetime
    status: Literal["requires_external_confirmation", "rejected"]
    reason_code: Literal["source_consultation_notice", "human_rejected"]
    scope: IntendedUseScope
    scope_sha256: Sha256
    evidence_sha256s: tuple[Sha256, ...]

    @field_validator("assessed_at")
    @classmethod
    def require_assessed_at_utc(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("evidence_sha256s")
    @classmethod
    def require_ordered_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_sorted_unique_hashes(value, "assessment evidence")

    @model_validator(mode="after")
    def require_bound_scope_and_reason(self) -> SourceUseAssessment:
        if self.source != self.scope.source:
            raise ValueError("assessment source must match intended-use scope")
        if self.scope_sha256 != canonical_sha256(self.scope):
            raise ValueError("assessment scope hash does not match exact scope")
        expected_reason = (
            "human_rejected" if self.status == "rejected" else "source_consultation_notice"
        )
        if self.reason_code != expected_reason:
            raise ValueError("assessment reason code does not match status")
        return self


class SourceUseApproval(StrictRecord):
    schema_version: Literal[1]
    source: Literal["polymarket"]
    approver_id: NonEmptyString
    approver_role: Literal["source_owner_authorization", "qualified_legal_review"]
    approval_reference: NonEmptyString
    approved_at: datetime
    effective_at: datetime
    expires_at: datetime | None
    scope_sha256: Sha256
    evidence_sha256s: tuple[Sha256, ...]
    intake_manifest_sha256s: tuple[Sha256, ...]

    @field_validator("approved_at", "effective_at", "expires_at")
    @classmethod
    def require_approval_times_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else normalize_utc_timestamp(value)

    @field_validator("evidence_sha256s")
    @classmethod
    def require_ordered_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_sorted_unique_hashes(value, "approval evidence")

    @field_validator("intake_manifest_sha256s")
    @classmethod
    def require_ordered_manifests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _require_sorted_unique_hashes(value, "approval intake manifests")

    @model_validator(mode="after")
    def require_valid_approval_window(self) -> SourceUseApproval:
        if self.expires_at is not None and self.expires_at <= self.effective_at:
            raise ValueError("approval expiry must follow its effective time")
        return self


class GateDecision(StrictRecord):
    allowed: bool
    reason_code: GateReason
    scope_sha256: Sha256
    evidence_sha256s: tuple[Sha256, ...]
    intake_manifest_sha256s: tuple[Sha256, ...]


def evaluate_source_gate(
    *,
    assessment: SourceUseAssessment,
    approval: SourceUseApproval | None,
    scope: IntendedUseScope,
    evidence_sha256s: tuple[str, ...],
    intake_manifest_sha256s: tuple[str, ...],
    as_of: datetime,
) -> GateDecision:
    as_of = normalize_utc_timestamp(as_of)
    evidence_hashes = _require_sorted_unique_hashes(evidence_sha256s, "gate evidence")
    manifest_hashes = _require_sorted_unique_hashes(
        intake_manifest_sha256s, "gate intake manifests"
    )
    scope_hash = canonical_sha256(scope)

    def decision(allowed: bool, reason: GateReason) -> GateDecision:
        return GateDecision(
            allowed=allowed,
            reason_code=reason,
            scope_sha256=scope_hash,
            evidence_sha256s=evidence_hashes,
            intake_manifest_sha256s=manifest_hashes,
        )

    if assessment.scope_sha256 != scope_hash or assessment.source != scope.source:
        return decision(False, "assessment_scope_mismatch")
    if assessment.evidence_sha256s != evidence_hashes:
        return decision(False, "assessment_evidence_mismatch")
    if assessment.status == "rejected":
        return decision(False, "source_use_rejected")
    if approval is None:
        return decision(False, "external_confirmation_required")
    if approval.source != scope.source:
        return decision(False, "approval_source_mismatch")
    if approval.scope_sha256 != scope_hash:
        return decision(False, "approval_scope_mismatch")
    if approval.evidence_sha256s != evidence_hashes:
        return decision(False, "approval_evidence_mismatch")
    if approval.intake_manifest_sha256s != manifest_hashes:
        return decision(False, "approval_manifest_mismatch")
    if as_of < approval.approved_at:
        return decision(False, "approval_not_issued")
    if as_of < approval.effective_at:
        return decision(False, "approval_not_effective")
    if approval.expires_at is not None and as_of >= approval.expires_at:
        return decision(False, "approval_expired")
    return decision(True, "exact_human_approval")


def _require_sorted_unique_hashes(value: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not value:
        raise ValueError(f"{label} hashes must not be empty")
    if tuple(sorted(set(value))) != value:
        raise ValueError(f"{label} hashes must be sorted and unique")
    for item in value:
        if re.fullmatch(r"[0-9a-f]{64}", item) is None:
            raise ValueError(f"{label} hashes must contain lowercase SHA-256 values")
    return value
