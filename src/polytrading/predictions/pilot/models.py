"""Immutable, append-only records for the local Polymarket live pilot.

Every record here is operator-facing evidence: it stores stable identities, hashes, and
sanitized public fields only. Raw eligibility documents, capability bundles, passkey
assertion bytes, wallet keys, and API credentials never reach these models -- ``PilotRecord``
rejects a secret-looking field name at class-definition time so a later record cannot quietly
introduce one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, StringConstraints, field_validator, model_validator

from polytrading.predictions.domain import (
    PredictionRecord,
    PredictionVenue,
    Sha256,
    normalize_utc_timestamp,
)
from polytrading.predictions.execution.models import ExecutionOperation, canonical_execution_hash

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
NonNegativeInt = Annotated[int, Field(ge=0)]
UtcTimestamp = Annotated[datetime, AfterValidator(normalize_utc_timestamp)]

# A declared field name may name sensitive material only as a one-way derivation of it.
_SECRET_FIELD_TOKENS = frozenset(
    {
        "apikey",
        "assertion",
        "authorization",
        "bearer",
        "bundle",
        "cookie",
        "credential",
        "credentials",
        "header",
        "headers",
        "key",
        "keys",
        "mnemonic",
        "passphrase",
        "password",
        "private",
        "secret",
        "secrets",
        "seed",
        "signature",
    }
)
_SAFE_FIELD_SUFFIXES = (
    "_digest",
    "_digests",
    "_fingerprint",
    "_fingerprints",
    "_hash",
    "_hashes",
    "_id",
    "_ids",
)

# Recovery may only inspect account state or cancel a known bound order (spec section 4.5).
_RECOVERY_OPERATIONS = frozenset(
    {
        ExecutionOperation.CANCEL_ORDER,
        ExecutionOperation.READ_ACCOUNT,
        ExecutionOperation.READ_ORDERS,
        ExecutionOperation.READ_TRADES,
    }
)


def _require_safe_field_name(name: str) -> None:
    if name.endswith(_SAFE_FIELD_SUFFIXES):
        return
    if any(part in _SECRET_FIELD_TOKENS for part in name.split("_")):
        raise TypeError(
            f"pilot record field {name!r} names secret material; store a digest or fingerprint"
        )


def _sorted_unique[ItemT](values: tuple[ItemT, ...], field_name: str) -> tuple[ItemT, ...]:
    if values != tuple(sorted(set(values), key=str)):
        raise ValueError(f"{field_name} must be sorted and unique")
    return values


class PilotRecord(PredictionRecord):
    """Frozen, strict base for every pilot record."""

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        for name in cls.model_fields:
            _require_safe_field_name(name)


class AuthorizationMode(StrEnum):
    EXACT_ORDER = "EXACT_ORDER"
    COMPLETE_STRATEGY = "COMPLETE_STRATEGY"
    AUTOMATION_SESSION = "AUTOMATION_SESSION"


class GrantKind(StrEnum):
    PRIMARY = "PRIMARY"
    RECOVERY = "RECOVERY"
    CREDENTIAL_PROVISIONING = "CREDENTIAL_PROVISIONING"


class PilotProofFamily(StrEnum):
    """The deterministic single-venue proof templates the pilot may consume."""

    BINARY_COMPLEMENT = "binary_complement@1"
    EXHAUSTIVE_OUTCOME_SET = "exhaustive_outcome_set@1"
    LOGICAL_IMPLICATION = "logical_implication@1"
    WITHIN_VENUE_EQUIVALENCE = "cross_venue_equivalence@1"


class EligibilityReviewerCategory(StrEnum):
    OPERATOR_SELF = "OPERATOR_SELF"
    EXTERNAL_ADVISER = "EXTERNAL_ADVISER"
    VENUE_SUPPORT = "VENUE_SUPPORT"


class ChallengeState(StrEnum):
    ISSUED = "ISSUED"
    USED = "USED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class CapabilityEventType(StrEnum):
    ISSUED = "ISSUED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class NonceScope(StrEnum):
    CHALLENGE = "CHALLENGE"
    CAPABILITY = "CAPABILITY"
    PRIMARY_OPERATION = "PRIMARY_OPERATION"
    RECOVERY_OPERATION = "RECOVERY_OPERATION"


class PresenceEventType(StrEnum):
    STARTED = "STARTED"
    HEARTBEAT_LOST = "HEARTBEAT_LOST"
    SLEEP = "SLEEP"
    SCREEN_LOCK = "SCREEN_LOCK"
    RECONNECTED = "RECONNECTED"
    TERMINAL = "TERMINAL"


class PresenceState(StrEnum):
    PRESENT = "PRESENT"
    LOST = "LOST"
    TERMINAL = "TERMINAL"


class PilotSessionState(StrEnum):
    ARMED = "ARMED"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    KILLED = "KILLED"


class PilotSessionResult(StrEnum):
    COMPLETED = "COMPLETED"
    STOPPED_BY_OPERATOR = "STOPPED_BY_OPERATOR"
    EXPIRED = "EXPIRED"
    KILLED = "KILLED"
    FAILED = "FAILED"


class LossStatus(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class CredentialProvisioningResult(StrEnum):
    CREATED = "CREATED"
    DERIVED = "DERIVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ActivationResult(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ABANDONED = "ABANDONED"


class KillClearanceResult(StrEnum):
    CLEARED = "CLEARED"
    REJECTED = "REJECTED"


_TERMINAL_SESSION_STATES = frozenset({PilotSessionState.STOPPED, PilotSessionState.KILLED})


class PilotLimits(PilotRecord):
    """Requested pilot bounds. Each field's constraint is the immutable approved ceiling.

    A request may only lower a ceiling; an attempted increase is rejected here rather than
    clamped, so a client can never widen authority by supplying a larger number.
    """

    wallet_trading_equity: Annotated[Decimal, Field(gt=0, le=250, allow_inf_nan=False)]
    order_notional: Annotated[Decimal, Field(gt=0, le=10, allow_inf_nan=False)]
    strategy_gross_notional: Annotated[Decimal, Field(gt=0, le=25, allow_inf_nan=False)]
    session_duration: Annotated[timedelta, Field(gt=timedelta(0), le=timedelta(minutes=15))]
    session_deployed_capital: Annotated[Decimal, Field(gt=0, le=50, allow_inf_nan=False)]
    concurrent_strategies: Literal[1]
    session_loss: Annotated[Decimal, Field(gt=0, le=5, allow_inf_nan=False)]
    utc_day_loss: Annotated[Decimal, Field(gt=0, le=10, allow_inf_nan=False)]


PILOT_CEILINGS = PilotLimits(
    wallet_trading_equity=Decimal("250"),
    order_notional=Decimal("10"),
    strategy_gross_notional=Decimal("25"),
    session_duration=timedelta(minutes=15),
    session_deployed_capital=Decimal("50"),
    concurrent_strategies=1,
    session_loss=Decimal("5"),
    utc_day_loss=Decimal("10"),
)
PILOT_CEILING_HASH: Sha256 = canonical_execution_hash(PILOT_CEILINGS)


class PilotLossState(PilotRecord):
    """Conservative session/day loss accounting, or an explicit UNKNOWN that stops execution."""

    status: LossStatus
    session_start_equity: NonNegativeDecimal | None
    realized_loss: NonNegativeDecimal | None
    unrealized_loss: NonNegativeDecimal | None
    evidence_hashes: tuple[Sha256, ...]
    evaluated_at: UtcTimestamp

    @field_validator("evidence_hashes")
    @classmethod
    def _validate_evidence(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        return _sorted_unique(value, "evidence_hashes")

    @model_validator(mode="after")
    def _require_consistent_status(self) -> PilotLossState:
        amounts = (self.session_start_equity, self.realized_loss, self.unrealized_loss)
        if self.status is LossStatus.UNKNOWN and any(amount is not None for amount in amounts):
            raise ValueError("UNKNOWN loss state must not carry marked amounts")
        if self.status is LossStatus.KNOWN and any(amount is None for amount in amounts):
            raise ValueError("KNOWN loss state requires start equity and both loss components")
        return self


class EligibilityAttestationRef(PilotRecord):
    """An operator-supplied eligibility gate, referenced by hash -- never the document itself."""

    schema_version: Literal[1]
    attestation_id: UUID
    operator_reference: NonEmptyString
    document_hash: Sha256
    venue: Literal[PredictionVenue.POLYMARKET]
    account_holder_type: Literal["INDIVIDUAL"]
    physical_jurisdiction: Literal["PH"]
    wallet_fingerprint: Sha256
    account_fingerprint: Sha256
    reviewer_category: EligibilityReviewerCategory
    scoped_assertions: tuple[NonEmptyString, ...]
    operator_supplied_gate: Literal[True]
    superseded_attestation_id: UUID | None
    reviewed_at: UtcTimestamp
    expires_at: UtcTimestamp

    @field_validator("scoped_assertions")
    @classmethod
    def _validate_assertions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("scoped_assertions must not be empty")
        return _sorted_unique(value, "scoped_assertions")

    @model_validator(mode="after")
    def _require_forward_expiry(self) -> EligibilityAttestationRef:
        if self.expires_at <= self.reviewed_at:
            raise ValueError("eligibility expiry must follow its review date")
        if self.wallet_fingerprint == self.account_fingerprint:
            raise ValueError("wallet and account fingerprints must be distinct")
        return self


class PilotPolicyProfile(PilotRecord):
    schema_version: Literal[1]
    policy_id: UUID
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    requested_limits: PilotLimits
    ceiling_hash: Sha256
    enabled_proof_families: tuple[PilotProofFamily, ...]
    eligibility_attestation_id: UUID
    eligibility_attestation_hash: Sha256
    created_at: UtcTimestamp

    @field_validator("enabled_proof_families")
    @classmethod
    def _validate_families(
        cls, value: tuple[PilotProofFamily, ...]
    ) -> tuple[PilotProofFamily, ...]:
        if not value:
            raise ValueError("enabled_proof_families must name at least one qualified family")
        return _sorted_unique(value, "enabled_proof_families")

    @model_validator(mode="after")
    def _require_compiled_ceilings(self) -> PilotPolicyProfile:
        if self.ceiling_hash != PILOT_CEILING_HASH:
            raise ValueError("ceiling_hash must bind the compiled immutable ceilings")
        return self


class PilotActivationCeremony(PilotRecord):
    schema_version: Literal[1]
    ceremony_id: UUID
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    stage: Annotated[int, Field(ge=0, le=4)]
    readiness_digest: Sha256
    passkey_assertion_digest: Sha256
    policy_hash: Sha256
    protocol_fixture_hash: Sha256
    manifest_record_hash: Sha256 | None
    evidence_hashes: tuple[Sha256, ...]
    result: ActivationResult
    first_strategy_reconciliation_hash: Sha256 | None
    occurred_at: UtcTimestamp

    @field_validator("evidence_hashes")
    @classmethod
    def _validate_evidence(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if not value:
            raise ValueError("evidence_hashes must not be empty")
        return _sorted_unique(value, "evidence_hashes")

    @model_validator(mode="after")
    def _require_promoted_manifest(self) -> PilotActivationCeremony:
        if (
            self.result is ActivationResult.APPROVED
            and self.stage >= 3
            and self.manifest_record_hash is None
        ):
            raise ValueError("an approved activation must bind its manifest_record_hash")
        return self


class CredentialProvisioningEvent(PilotRecord):
    """One create-or-derive CLOB credential ceremony, recorded as fingerprints only."""

    schema_version: Literal[1]
    event_id: UUID
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    protocol_fixture_hash: Sha256
    source_hashes: tuple[Sha256, ...]
    grant_digest: Sha256
    result: CredentialProvisioningResult
    credential_fingerprint: Sha256 | None
    occurred_at: UtcTimestamp

    @field_validator("source_hashes")
    @classmethod
    def _validate_sources(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if not value:
            raise ValueError("source_hashes must not be empty")
        return _sorted_unique(value, "source_hashes")

    @model_validator(mode="after")
    def _require_consistent_result(self) -> CredentialProvisioningEvent:
        provisioned = self.result in (
            CredentialProvisioningResult.CREATED,
            CredentialProvisioningResult.DERIVED,
        )
        if provisioned and self.credential_fingerprint is None:
            raise ValueError("a provisioned credential must record its credential_fingerprint")
        if not provisioned and self.credential_fingerprint is not None:
            raise ValueError("a failed ceremony must not record a credential_fingerprint")
        return self


class AuthorizationChallenge(PilotRecord):
    """The exact action a passkey assertion is bound to, in safe public fields only."""

    schema_version: Literal[1]
    challenge_id: UUID
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    browser_session_hash: Sha256
    credential_id_hash: Sha256
    mode: AuthorizationMode
    grant_kind: GrantKind
    target_id: UUID
    policy_id: UUID
    evidence_hashes: tuple[Sha256, ...]
    requested_limits_hash: Sha256
    ceiling_hash: Sha256
    allowed_operations: tuple[ExecutionOperation, ...]
    recovery_operations: tuple[ExecutionOperation, ...]
    confirmation_text: NonEmptyString
    confirmation_text_hash: Sha256
    nonce: NonEmptyString
    state: ChallengeState
    not_before: UtcTimestamp
    expires_at: UtcTimestamp

    @field_validator("evidence_hashes")
    @classmethod
    def _validate_evidence(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if not value:
            raise ValueError("evidence_hashes must not be empty")
        return _sorted_unique(value, "evidence_hashes")

    @field_validator("allowed_operations", "recovery_operations")
    @classmethod
    def _validate_operations(
        cls, value: tuple[ExecutionOperation, ...], info: Any
    ) -> tuple[ExecutionOperation, ...]:
        return _sorted_unique(value, info.field_name)

    @model_validator(mode="after")
    def _require_bounded_authority(self) -> AuthorizationChallenge:
        if self.expires_at <= self.not_before:
            raise ValueError("expires_at must follow not_before")
        if self.ceiling_hash != PILOT_CEILING_HASH:
            raise ValueError("ceiling_hash must bind the compiled immutable ceilings")
        if unsupported := set(self.recovery_operations) - _RECOVERY_OPERATIONS:
            raise ValueError(
                "recovery_operations may only inspect or cancel; "
                f"rejected {sorted(operation.value for operation in unsupported)}"
            )
        if self.grant_kind is GrantKind.CREDENTIAL_PROVISIONING and (
            self.allowed_operations or self.recovery_operations
        ):
            raise ValueError("a CREDENTIAL_PROVISIONING challenge grants no execution operation")
        if self.grant_kind is not GrantKind.CREDENTIAL_PROVISIONING and not self.allowed_operations:
            raise ValueError("a trading challenge must name its allowed_operations")
        return self


class PilotCapabilityEvent(PilotRecord):
    """Issuance, verification, rejection, revocation, or expiry of one capability."""

    schema_version: Literal[1]
    event_id: UUID
    capability_id: UUID
    challenge_id: UUID
    account_fingerprint: Sha256
    mode: AuthorizationMode
    grant_kind: GrantKind
    event_type: CapabilityEventType
    capability_digest: Sha256
    nonce: NonEmptyString
    reason: NonEmptyString | None
    expires_at: UtcTimestamp
    occurred_at: UtcTimestamp

    @model_validator(mode="after")
    def _require_reason_for_rejection(self) -> PilotCapabilityEvent:
        if self.event_type is CapabilityEventType.REJECTED and self.reason is None:
            raise ValueError("a REJECTED capability event must record its sanitized reason")
        return self


class PilotNonceClaim(PilotRecord):
    """One globally unique challenge, capability, or operation nonce."""

    schema_version: Literal[1]
    scope: NonceScope
    nonce: NonEmptyString
    account_fingerprint: Sha256
    payload_hash: Sha256
    claimed_at: UtcTimestamp


def pilot_nonce_claim_key(claim: PilotNonceClaim) -> Sha256:
    """Derive the durable uniqueness key of a nonce claim from its scope and nonce."""

    return canonical_execution_hash({"scope": claim.scope.value, "nonce": claim.nonce})


class PilotExecutionSession(PilotRecord):
    """One immutable transition of an authorized exact order, strategy, or session."""

    schema_version: Literal[1]
    event_id: UUID
    session_id: UUID
    sequence_number: NonNegativeInt
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    mode: AuthorizationMode
    capability_id: UUID
    policy_id: UUID
    effective_limits: PilotLimits
    state: PilotSessionState
    loss_state: PilotLossState
    presence_state: PresenceState
    strategies_started: NonNegativeInt
    deployed_capital: NonNegativeDecimal
    result: PilotSessionResult | None
    started_at: UtcTimestamp
    expires_at: UtcTimestamp
    occurred_at: UtcTimestamp
    ended_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def _require_bounded_lifecycle(self) -> PilotExecutionSession:
        if self.expires_at <= self.started_at:
            raise ValueError("expires_at must follow started_at")
        if self.expires_at - self.started_at > self.effective_limits.session_duration:
            raise ValueError("expires_at must stay inside the effective session_duration")
        if self.deployed_capital > self.effective_limits.session_deployed_capital:
            raise ValueError("deployed_capital must stay inside its effective limit")
        if self.strategies_started > 1 and self.mode is not AuthorizationMode.AUTOMATION_SESSION:
            raise ValueError("only an automation session may start more than one strategy")
        terminal = self.state in _TERMINAL_SESSION_STATES
        if terminal and (self.result is None or self.ended_at is None):
            raise ValueError("a terminal session transition requires a result and ended_at")
        if not terminal and (self.result is not None or self.ended_at is not None):
            raise ValueError("a live session transition must not declare a result or ended_at")
        return self


class PilotPresenceEvent(PilotRecord):
    """A presence transition -- never an individual two-second heartbeat."""

    schema_version: Literal[1]
    event_id: UUID
    session_id: UUID | None
    account_fingerprint: Sha256
    event_type: PresenceEventType
    monotonic_gap_ms: NonNegativeInt | None
    detail_code: NonEmptyString | None
    occurred_at: UtcTimestamp


class PilotKillClearanceEvent(PilotRecord):
    schema_version: Literal[1]
    clearance_event_id: UUID
    account_fingerprint: Sha256
    kill_event_id: UUID
    discrepancy_evidence_hashes: tuple[Sha256, ...]
    reconciliation_hash: Sha256
    passkey_assertion_digest: Sha256
    confirmation_phrase_hash: Sha256
    result: KillClearanceResult
    occurred_at: UtcTimestamp

    @field_validator("discrepancy_evidence_hashes")
    @classmethod
    def _validate_discrepancies(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if not value:
            raise ValueError("discrepancy_evidence_hashes must record the reviewed evidence")
        return _sorted_unique(value, "discrepancy_evidence_hashes")
