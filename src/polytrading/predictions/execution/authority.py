"""Fail-closed authority snapshots for Polymarket venue mutations.

This module deliberately defines verification contracts but no production capability issuer,
signature scheme, bundle parser, or verification-key configuration.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from polytrading.predictions.domain import PredictionRecord, PredictionVenue, Sha256
from polytrading.predictions.execution.models import ExecutionOperation, canonical_execution_hash
from polytrading.predictions.manifest import VenueManifest, evaluate_execution_gate

NonEmptyString = Annotated[str, Field(min_length=1)]
NonEmptyBytes = Annotated[bytes, Field(min_length=1, repr=False, exclude=True)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
NonNegativeDuration = Annotated[timedelta, Field(ge=timedelta(0))]

AuthorityReason = Literal[
    "MANIFEST_NOT_FOUND",
    "COLLECTION_NOT_PERMITTED",
    "AUTOMATED_USE_RESTRICTED",
    "JURISDICTION_BLOCKED",
    "JURISDICTION_UNREVIEWED",
    "LIVE_NOT_ELIGIBLE",
    "CAPABILITY_VERIFIER_NOT_CONFIGURED",
    "CAPABILITY_MISSING",
    "CAPABILITY_SIGNATURE_INVALID",
    "CAPABILITY_CANONICAL_BYTES_INVALID",
    "CAPABILITY_NOT_YET_VALID",
    "CAPABILITY_EXPIRED",
    "CAPABILITY_CLOCK_SKEW",
    "CAPABILITY_VENUE_MISMATCH",
    "CAPABILITY_ACCOUNT_MISMATCH",
    "CAPABILITY_MANIFEST_MISMATCH",
    "CAPABILITY_SOURCE_HASH_MISMATCH",
    "CAPABILITY_STRATEGY_POLICY_MISMATCH",
    "CAPABILITY_PROOF_POLICY_MISMATCH",
    "CAPABILITY_ECONOMICS_POLICY_MISMATCH",
    "CAPABILITY_PROTOCOL_MISMATCH",
    "CAPABILITY_ROUTE_SET_MISMATCH",
    "CAPABILITY_OPERATION_NOT_ALLOWED",
    "CAPABILITY_MODE_MISMATCH",
    "CAPABILITY_GRANT_KIND_MISMATCH",
    "CAPABILITY_ACTION_MISMATCH",
    "CAPABILITY_SESSION_MISMATCH",
    "CAPABILITY_CEILING_MISMATCH",
    "CAPABILITY_REQUESTED_POLICY_MISMATCH",
    "CAPABILITY_PLAN_MISMATCH",
    "CAPABILITY_RECOVERY_POLICY_MISMATCH",
    "CAPABILITY_CREDENTIAL_ROUTE_NOT_ALLOWED",
    "OPERATOR_PRESENCE_LOST",
    "CAPABILITY_CAPITAL_LIMIT_EXCEEDED",
    "CAPABILITY_NOTIONAL_LIMIT_EXCEEDED",
    "CAPABILITY_POSITION_LIMIT_EXCEEDED",
    "CAPABILITY_LOSS_LIMIT_EXCEEDED",
    "CAPABILITY_NONCE_MISMATCH",
    "CAPABILITY_NONCE_REPLAYED",
    "CAPABILITY_REVOKED",
    "GEOBLOCK_EVIDENCE_MISSING",
    "GEOBLOCK_EVIDENCE_STALE",
    "GEOBLOCK_BLOCKED",
    "ACCOUNT_SCOPE_EVIDENCE_MISSING",
    "ACCOUNT_SCOPE_EVIDENCE_STALE",
    "ACCOUNT_SCOPE_MISMATCH",
    "EXECUTION_KILL_ENGAGED",
    "EXECUTION_UNAVAILABLE",
]

# Mode and grant kind travel as their pilot string values so this module stays independent of the
# pilot package: the coordinator and signer both compare them without importing pilot code.
AuthorizationModeValue = Literal["EXACT_ORDER", "COMPLETE_STRATEGY", "AUTOMATION_SESSION"]
GrantKindValue = Literal["PRIMARY", "RECOVERY", "CREDENTIAL_PROVISIONING"]
# Recovery may inspect account state or cancel a known bound order; it may never place one.
_RECOVERY_MUTATIONS = frozenset({ExecutionOperation.CANCEL_ORDER})

_MUTATING_OPERATIONS = frozenset(
    {
        ExecutionOperation.SIGN_ORDER,
        ExecutionOperation.SUBMIT_ORDER,
        ExecutionOperation.CANCEL_ORDER,
        ExecutionOperation.HEARTBEAT,
    }
)


def _require_sorted_unique(values: tuple[object, ...], field_name: str) -> tuple[object, ...]:
    if len(values) != len(set(values)) or values != tuple(sorted(values, key=str)):
        raise ValueError(f"{field_name} must be sorted and unique")
    return values


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


class _CapabilityFields(PredictionRecord):
    capability_version: Literal[1]
    capability_id: UUID
    venue: PredictionVenue
    account_fingerprint: Sha256
    manifest_record_hash: Sha256
    manifest_source_hashes: tuple[Sha256, ...]
    eligibility_evidence_hashes: tuple[Sha256, ...]
    strategy_policy_hash: Sha256
    proof_policy_hash: Sha256
    economics_policy_hash: Sha256
    protocol_fixture_hash: Sha256
    allowed_operations: tuple[ExecutionOperation, ...]
    route_set_version: NonEmptyString
    route_set_hash: Sha256
    maximum_capital: NonNegativeDecimal
    maximum_per_intent_notional: NonNegativeDecimal
    maximum_position: NonNegativeDecimal
    maximum_loss: NonNegativeDecimal
    not_before: datetime
    expires_at: datetime
    activation_nonce: NonEmptyString
    issuer_key_id: NonEmptyString
    # Pilot grant bindings. A capability minted before the local pilot leaves them unset; a pilot
    # grant sets them and then every boundary must state matching expectations or fail closed.
    mode: AuthorizationModeValue | None = None
    grant_kind: GrantKindValue | None = None
    parent_action_id: UUID | None = None
    session_id: UUID | None = None
    requested_limits_hash: Sha256 | None = None
    ceiling_hash: Sha256 | None = None
    plan_hash: Sha256 | None = None
    strategy_hash: Sha256 | None = None
    proof_family_hash: Sha256 | None = None
    recovery_policy_hash: Sha256 | None = None
    presence_deadline: datetime | None = None
    single_use: bool | None = None

    @field_validator("presence_deadline")
    @classmethod
    def _presence_deadline_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("manifest_source_hashes", "eligibility_evidence_hashes")
    @classmethod
    def _hashes_sorted_unique(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if not value:
            raise ValueError("capability evidence hashes must not be empty")
        return _require_sorted_unique(value, "capability evidence hashes")  # type: ignore[return-value]

    @field_validator("allowed_operations")
    @classmethod
    def _operations_sorted_unique(
        cls, value: tuple[ExecutionOperation, ...]
    ) -> tuple[ExecutionOperation, ...]:
        if not value:
            raise ValueError("allowed_operations must not be empty")
        if any(operation not in _MUTATING_OPERATIONS for operation in value):
            raise ValueError("allowed_operations contains a non-mutating operation")
        return _require_sorted_unique(value, "allowed_operations")  # type: ignore[return-value]

    @field_validator("not_before", "expires_at", "verified_at", check_fields=False)
    @classmethod
    def _timestamps_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _valid_window(self) -> _CapabilityFields:
        if self.expires_at <= self.not_before:
            raise ValueError("capability expiration must be after not-before")
        return self


class ExecutionCapability(_CapabilityFields):
    """Ephemeral public detached-signature capability.

    The signature is intentionally excluded from representations. Callers must never persist or
    log this model or the raw signed bundle.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    detached_signature: NonEmptyBytes

    @model_validator(mode="before")
    @classmethod
    def _redact_malformed_signature_input(cls, value: object) -> object:
        if not isinstance(value, Mapping) or "detached_signature" not in value:
            return value
        signature = value["detached_signature"]
        if isinstance(signature, bytes) and signature:
            return value
        sanitized = dict(value)
        sanitized["detached_signature"] = b""
        return sanitized

    @property
    def canonical_unsigned_bundle(self) -> bytes:
        payload: Mapping[str, object] = self.model_dump(mode="json", exclude={"detached_signature"})
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


class VerifiedExecutionCapability(_CapabilityFields):
    """Sanitized verifier projection; contains neither bundle bytes nor signature material."""

    capability_digest: Sha256
    verified_at: datetime
    signature_valid: bool
    canonical_bytes_valid: bool


class AuthorityDecision(PredictionRecord):
    allowed: bool
    reason: AuthorityReason | None
    evidence_hashes: tuple[Sha256, ...]

    def __init__(
        self,
        allowed: bool,
        reason: AuthorityReason | None,
        evidence_hashes: tuple[Sha256, ...],
    ) -> None:
        super().__init__(allowed=allowed, reason=reason, evidence_hashes=evidence_hashes)

    @field_validator("evidence_hashes")
    @classmethod
    def _evidence_sorted_unique(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        return _require_sorted_unique(value, "evidence_hashes")  # type: ignore[return-value]

    @model_validator(mode="after")
    def _reason_matches_result(self) -> AuthorityDecision:
        if self.allowed == (self.reason is not None):
            raise ValueError("allowed authority decisions must omit a reason")
        return self


class CapabilityVerifier(Protocol):
    def verify(
        self, *, capability_bundle: bytes, now: datetime
    ) -> AuthorityDecision | VerifiedExecutionCapability: ...


class UnavailableProductionCapabilityVerifier:
    """Production posture for this increment: no key exists, so verification is unavailable."""

    def verify(self, *, capability_bundle: bytes, now: datetime) -> AuthorityDecision:
        del capability_bundle, now
        return AuthorityDecision(
            allowed=False,
            reason="CAPABILITY_VERIFIER_NOT_CONFIGURED",
            evidence_hashes=(),
        )


class AuthorityContext(PredictionRecord):
    """Immutable inputs independently assembled at one mutation boundary."""

    manifest: VenueManifest | None
    verified_capability: VerifiedExecutionCapability | None
    now: datetime
    venue: PredictionVenue
    account_fingerprint: Sha256
    manifest_record_hash: Sha256
    manifest_source_hashes: tuple[Sha256, ...]
    strategy_policy_hash: Sha256
    proof_policy_hash: Sha256
    economics_policy_hash: Sha256
    protocol_fixture_hash: Sha256
    route_set_version: NonEmptyString
    route_set_hash: Sha256
    requested_notional: NonNegativeDecimal
    capital_after: NonNegativeDecimal
    position_after: NonNegativeDecimal
    loss_after: NonNegativeDecimal
    activation_nonce: NonEmptyString
    used_activation_nonces: frozenset[NonEmptyString]
    revoked_capability_ids: frozenset[UUID]
    observed_clock_skew: timedelta
    permitted_clock_skew: NonNegativeDuration
    geoblock_allowed: bool | None
    geoblock_evidence_hash: Sha256 | None
    geoblock_expires_at: datetime | None
    account_scope_account_fingerprint: Sha256 | None
    account_scope_evidence_hash: Sha256 | None
    account_scope_expires_at: datetime | None
    kill_engaged: bool
    evidence_hashes: tuple[Sha256, ...]
    # Pilot expectations assembled independently at this boundary.
    expected_mode: AuthorizationModeValue | None = None
    expected_grant_kind: GrantKindValue | None = None
    action_id: UUID | None = None
    session_id: UUID | None = None
    requested_limits_hash: Sha256 | None = None
    ceiling_hash: Sha256 | None = None
    plan_hash: Sha256 | None = None
    strategy_hash: Sha256 | None = None
    proof_family_hash: Sha256 | None = None
    recovery_policy_hash: Sha256 | None = None
    operator_present: bool = True
    credential_route_requested: bool = False

    @field_validator("now")
    @classmethod
    def _now_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("geoblock_expires_at", "account_scope_expires_at")
    @classmethod
    def _optional_timestamp_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @field_validator("manifest_source_hashes", "evidence_hashes")
    @classmethod
    def _context_hashes_sorted_unique(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        return _require_sorted_unique(value, "authority context hashes")  # type: ignore[return-value]


def _deny(context: AuthorityContext, reason: AuthorityReason) -> AuthorityDecision:
    return AuthorityDecision(False, reason, context.evidence_hashes)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _verify_capability_fields(
    context: AuthorityContext, operation: ExecutionOperation
) -> AuthorityDecision:
    capability = context.verified_capability
    if capability is None:
        return _deny(context, "CAPABILITY_MISSING")
    if capability.signature_valid is not True:
        return _deny(context, "CAPABILITY_SIGNATURE_INVALID")
    if capability.canonical_bytes_valid is not True:
        return _deny(context, "CAPABILITY_CANONICAL_BYTES_INVALID")
    if context.now < capability.not_before:
        return _deny(context, "CAPABILITY_NOT_YET_VALID")
    if context.now >= capability.expires_at:
        return _deny(context, "CAPABILITY_EXPIRED")
    if abs(context.observed_clock_skew) > context.permitted_clock_skew:
        return _deny(context, "CAPABILITY_CLOCK_SKEW")
    if (
        capability.venue != context.venue
        or context.venue is not PredictionVenue.POLYMARKET
        or context.manifest is None
        or context.manifest.venue is not PredictionVenue.POLYMARKET
    ):
        return _deny(context, "CAPABILITY_VENUE_MISMATCH")
    if capability.account_fingerprint != context.account_fingerprint:
        return _deny(context, "CAPABILITY_ACCOUNT_MISMATCH")
    current_manifest_hash = (
        canonical_execution_hash(context.manifest) if context.manifest is not None else None
    )
    if (
        capability.manifest_record_hash != context.manifest_record_hash
        or context.manifest_record_hash != current_manifest_hash
    ):
        return _deny(context, "CAPABILITY_MANIFEST_MISMATCH")
    if (
        capability.manifest_source_hashes != context.manifest_source_hashes
        or context.manifest is None
        or context.manifest.source_hashes != context.manifest_source_hashes
    ):
        return _deny(context, "CAPABILITY_SOURCE_HASH_MISMATCH")
    if capability.strategy_policy_hash != context.strategy_policy_hash:
        return _deny(context, "CAPABILITY_STRATEGY_POLICY_MISMATCH")
    if capability.proof_policy_hash != context.proof_policy_hash:
        return _deny(context, "CAPABILITY_PROOF_POLICY_MISMATCH")
    if capability.economics_policy_hash != context.economics_policy_hash:
        return _deny(context, "CAPABILITY_ECONOMICS_POLICY_MISMATCH")
    if capability.protocol_fixture_hash != context.protocol_fixture_hash:
        return _deny(context, "CAPABILITY_PROTOCOL_MISMATCH")
    if (
        capability.route_set_version != context.route_set_version
        or capability.route_set_hash != context.route_set_hash
    ):
        return _deny(context, "CAPABILITY_ROUTE_SET_MISMATCH")
    if operation not in _MUTATING_OPERATIONS or operation not in capability.allowed_operations:
        return _deny(context, "CAPABILITY_OPERATION_NOT_ALLOWED")
    pilot_denial = _verify_pilot_grant_fields(context, operation)
    if pilot_denial is not None:
        return pilot_denial
    if context.capital_after > capability.maximum_capital:
        return _deny(context, "CAPABILITY_CAPITAL_LIMIT_EXCEEDED")
    if context.requested_notional > capability.maximum_per_intent_notional:
        return _deny(context, "CAPABILITY_NOTIONAL_LIMIT_EXCEEDED")
    if abs(context.position_after) > capability.maximum_position:
        return _deny(context, "CAPABILITY_POSITION_LIMIT_EXCEEDED")
    if context.loss_after > capability.maximum_loss:
        return _deny(context, "CAPABILITY_LOSS_LIMIT_EXCEEDED")
    if capability.activation_nonce != context.activation_nonce:
        return _deny(context, "CAPABILITY_NONCE_MISMATCH")
    if capability.activation_nonce in context.used_activation_nonces:
        return _deny(context, "CAPABILITY_NONCE_REPLAYED")
    if capability.capability_id in context.revoked_capability_ids:
        return _deny(context, "CAPABILITY_REVOKED")
    if (
        not isinstance(context.geoblock_allowed, bool)
        or not _is_sha256(context.geoblock_evidence_hash)
        or context.geoblock_expires_at is None
    ):
        return _deny(context, "GEOBLOCK_EVIDENCE_MISSING")
    if context.now >= context.geoblock_expires_at:
        return _deny(context, "GEOBLOCK_EVIDENCE_STALE")
    if not context.geoblock_allowed:
        return _deny(context, "GEOBLOCK_BLOCKED")
    if (
        not _is_sha256(context.account_scope_account_fingerprint)
        or not _is_sha256(context.account_scope_evidence_hash)
        or context.account_scope_expires_at is None
    ):
        return _deny(context, "ACCOUNT_SCOPE_EVIDENCE_MISSING")
    if context.now >= context.account_scope_expires_at:
        return _deny(context, "ACCOUNT_SCOPE_EVIDENCE_STALE")
    if context.account_scope_account_fingerprint != context.account_fingerprint:
        return _deny(context, "ACCOUNT_SCOPE_MISMATCH")
    if context.kill_engaged:
        return _deny(context, "EXECUTION_KILL_ENGAGED")
    return AuthorityDecision(True, None, context.evidence_hashes)


def _verify_pilot_grant_fields(
    context: AuthorityContext, operation: ExecutionOperation
) -> AuthorityDecision | None:
    """Compare the pilot bindings of one grant against this boundary's own expectations.

    A grant that declares pilot bindings is only usable where the boundary states matching
    expectations, so a primary grant can never satisfy a recovery operation, an automation
    session's grant can never authorize an exact order, and a credential-provisioning grant can
    never authorize a mutation at all.
    """

    capability = context.verified_capability
    assert capability is not None  # narrowed by the caller
    if context.credential_route_requested:
        return _deny(context, "CAPABILITY_CREDENTIAL_ROUTE_NOT_ALLOWED")
    if capability.grant_kind == "CREDENTIAL_PROVISIONING":
        return _deny(context, "CAPABILITY_GRANT_KIND_MISMATCH")
    if capability.grant_kind != context.expected_grant_kind:
        return _deny(context, "CAPABILITY_GRANT_KIND_MISMATCH")
    if capability.mode != context.expected_mode:
        return _deny(context, "CAPABILITY_MODE_MISMATCH")
    if capability.grant_kind is None:
        return None
    if capability.grant_kind == "RECOVERY" and operation not in _RECOVERY_MUTATIONS:
        return _deny(context, "CAPABILITY_GRANT_KIND_MISMATCH")
    if capability.parent_action_id != context.action_id:
        return _deny(context, "CAPABILITY_ACTION_MISMATCH")
    if capability.session_id != context.session_id:
        return _deny(context, "CAPABILITY_SESSION_MISMATCH")
    if capability.mode == "AUTOMATION_SESSION" and capability.session_id is None:
        return _deny(context, "CAPABILITY_SESSION_MISMATCH")
    if capability.ceiling_hash != context.ceiling_hash:
        return _deny(context, "CAPABILITY_CEILING_MISMATCH")
    if capability.requested_limits_hash != context.requested_limits_hash:
        return _deny(context, "CAPABILITY_REQUESTED_POLICY_MISMATCH")
    if (
        capability.plan_hash != context.plan_hash
        or capability.strategy_hash != context.strategy_hash
        or capability.proof_family_hash != context.proof_family_hash
    ):
        return _deny(context, "CAPABILITY_PLAN_MISMATCH")
    if capability.recovery_policy_hash != context.recovery_policy_hash:
        return _deny(context, "CAPABILITY_RECOVERY_POLICY_MISMATCH")
    if (
        capability.presence_deadline is None
        or not context.operator_present
        or context.now > capability.presence_deadline
    ):
        return _deny(context, "OPERATOR_PRESENCE_LOST")
    return None


def verify_mutation_authority(
    context: AuthorityContext, operation: ExecutionOperation
) -> AuthorityDecision:
    """Evaluate one boundary's fresh snapshot without caching or mutating nonce state."""

    manifest = evaluate_execution_gate(context.manifest, venue=PredictionVenue.POLYMARKET)
    if not manifest.allowed:
        assert manifest.reason is not None
        return _deny(context, manifest.reason)
    return _verify_capability_fields(context, operation)
