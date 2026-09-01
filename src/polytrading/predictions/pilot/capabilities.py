"""Per-launch Ed25519 capability issuance bound to one verified passkey assertion.

The signing key exists only in this process's memory for one launch: a restart invalidates every
outstanding capability. Each approval yields exactly two independently verifiable grants -- a
primary grant for the approved action and a bounded, risk-reducing recovery grant -- and neither
can be widened, renewed, or converted into the other.
"""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import Base64Bytes, Field, StringConstraints, field_validator, model_validator

from polytrading.predictions.domain import (
    PredictionRecord,
    PredictionVenue,
    Sha256,
    normalize_utc_timestamp,
)
from polytrading.predictions.execution.models import ExecutionOperation, canonical_execution_hash
from polytrading.predictions.manifest import VenueManifest
from polytrading.predictions.pilot.models import (
    PILOT_CEILING_HASH,
    AuthorizationChallenge,
    AuthorizationMode,
    GrantKind,
    PilotLimits,
    PilotRecord,
    UtcTimestamp,
)
from polytrading.predictions.pilot.passkeys import (
    VerifiedOperatorAssertion,
    action_challenge_digest,
)

# Recovery may outlive primary authority by at most this much, and only to reduce exposure.
MAXIMUM_RECOVERY_LIFETIME = timedelta(seconds=120)
# Mode lifetimes from spec section 7.2.
MAXIMUM_PRIMARY_LIFETIME = {
    AuthorizationMode.EXACT_ORDER: timedelta(seconds=60),
    AuthorizationMode.COMPLETE_STRATEGY: timedelta(minutes=5),
    AuthorizationMode.AUTOMATION_SESSION: timedelta(minutes=15),
}
_SINGLE_USE_MODES = frozenset({AuthorizationMode.EXACT_ORDER, AuthorizationMode.COMPLETE_STRATEGY})
_RECOVERY_OPERATIONS = frozenset(
    {
        ExecutionOperation.CANCEL_ORDER,
        ExecutionOperation.READ_ACCOUNT,
        ExecutionOperation.READ_ORDERS,
        ExecutionOperation.READ_TRADES,
    }
)

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
MAXIMUM_MUTATION_EVIDENCE_LIFETIME = timedelta(seconds=5)

CapabilityIssueCode = Literal[
    "ISSUER_CLOSED",
    "MODE_MISMATCH",
    "GRANT_KIND_MISMATCH",
    "CHALLENGE_MISMATCH",
    "ACTION_DIGEST_MISMATCH",
    "ACCOUNT_MISMATCH",
    "BROWSER_SESSION_MISMATCH",
    "CEILING_MISMATCH",
    "LIMITS_MISMATCH",
    "PRIMARY_LIFETIME_EXCEEDED",
    "RECOVERY_LIFETIME_EXCEEDED",
    "RECOVERY_OPERATION_NOT_REDUCING",
    "PRESENCE_DEADLINE_INVALID",
    "NONCE_REPLAYED",
    "CAPABILITY_ALREADY_ISSUED",
]


class CapabilityIssueError(ValueError):
    """An issuance the local authority refuses, named by a stable code."""

    def __init__(self, code: CapabilityIssueCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class VenueBinding(PilotRecord):
    """The venue-side identity a grant must carry so the signer can verify it independently."""

    venue: PredictionVenue
    manifest_record_hash: Sha256
    manifest_source_hashes: tuple[Sha256, ...]
    eligibility_evidence_hashes: tuple[Sha256, ...]
    strategy_policy_hash: Sha256
    proof_policy_hash: Sha256
    economics_policy_hash: Sha256
    protocol_fixture_hash: Sha256
    route_set_version: NonEmptyString
    route_set_hash: Sha256

    @field_validator("manifest_source_hashes", "eligibility_evidence_hashes")
    @classmethod
    def _sorted_unique_hashes(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if not value:
            raise ValueError("venue binding evidence must not be empty")
        if value != tuple(sorted(set(value))):
            raise ValueError("venue binding evidence must be sorted and unique")
        return value


class CapabilityRequest(PilotRecord):
    """Everything the server resolved for one approved action, before it is signed."""

    schema_version: Literal[1]
    capability_id: UUID
    recovery_capability_id: UUID
    challenge_id: UUID
    mode: AuthorizationMode
    venue_binding: VenueBinding
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    browser_session_hash: Sha256
    policy_id: UUID
    target_id: UUID
    session_id: UUID | None
    effective_limits: PilotLimits
    requested_limits_hash: Sha256
    ceiling_hash: Sha256
    plan_hash: Sha256
    strategy_hash: Sha256
    proof_family_hash: Sha256
    recovery_policy_hash: Sha256
    evidence_hashes: tuple[Sha256, ...]
    allowed_operations: tuple[ExecutionOperation, ...]
    recovery_operations: tuple[ExecutionOperation, ...]
    primary_nonce: NonEmptyString
    recovery_nonce: NonEmptyString
    not_before: UtcTimestamp
    expires_at: UtcTimestamp
    recovery_expires_at: UtcTimestamp
    presence_deadline: UtcTimestamp

    @field_validator("evidence_hashes", "allowed_operations", "recovery_operations")
    @classmethod
    def _sorted_unique[ItemT](cls, value: tuple[ItemT, ...], info: object) -> tuple[ItemT, ...]:
        if value != tuple(sorted(set(value), key=str)):
            raise ValueError(f"{getattr(info, 'field_name', 'field')} must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _require_distinct_grants(self) -> CapabilityRequest:
        if self.capability_id == self.recovery_capability_id:
            raise ValueError("primary and recovery grants must have distinct identities")
        if self.primary_nonce == self.recovery_nonce:
            raise ValueError("primary and recovery grants must have distinct nonces")
        return self


class CapabilityGrant(PilotRecord):
    """One signed grant. The bundle bytes stay ephemeral; only digests are ever persisted."""

    schema_version: Literal[1]
    capability_id: UUID
    challenge_id: UUID
    grant_kind: GrantKind
    mode: AuthorizationMode
    venue_binding: VenueBinding
    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    parent_action_id: UUID
    session_id: UUID | None
    effective_limits: PilotLimits
    requested_limits_hash: Sha256
    ceiling_hash: Sha256
    plan_hash: Sha256
    strategy_hash: Sha256
    proof_family_hash: Sha256
    recovery_policy_hash: Sha256
    browser_session_hash: Sha256
    passkey_assertion_digest: Sha256
    evidence_hashes: tuple[Sha256, ...]
    allowed_operations: tuple[ExecutionOperation, ...]
    single_use: bool
    nonce: NonEmptyString
    issuer_key_id: NonEmptyString
    not_before: UtcTimestamp
    expires_at: UtcTimestamp
    presence_deadline: UtcTimestamp

    @property
    def digest(self) -> Sha256:
        return canonical_execution_hash(self)


class SignerKillDirective(PredictionRecord):
    """A public, launch-bound directive that revokes the listed signer capabilities."""

    capability_ids: tuple[UUID, ...]
    issued_at: UtcTimestamp
    signature: Base64Bytes

    @field_validator("capability_ids")
    @classmethod
    def _sorted_unique_capability_ids(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if value != tuple(sorted(set(value), key=str)):
            raise ValueError("capability ids must be sorted and unique")
        return value

    @property
    def signature_digest(self) -> Sha256:
        return canonical_execution_hash(
            {
                "kind": "signer-kill-v1",
                "capability_ids": self.capability_ids,
                "issued_at": self.issued_at,
            }
        )


class MutationEvidence(PilotRecord):
    """One short-lived, public mutation snapshot signed by the launch issuer."""

    schema_version: Literal[1]
    manifest: VenueManifest
    manifest_record_hash: Sha256
    account_fingerprint: Sha256
    reconciliation_hash: Sha256
    reconciliation_observed_at: UtcTimestamp
    geoblock_allowed: bool
    geoblock_evidence_hash: Sha256
    geoblock_expires_at: UtcTimestamp
    account_scope_evidence_hash: Sha256
    account_scope_expires_at: UtcTimestamp
    kill_engaged: bool
    operator_present: bool
    plan_digest: Sha256
    authority_digest: Sha256
    requested_notional: NonNegativeDecimal
    capital_after: NonNegativeDecimal
    position_after: NonNegativeDecimal
    loss_after: NonNegativeDecimal
    issued_at: UtcTimestamp
    expires_at: UtcTimestamp

    @model_validator(mode="after")
    def _bind_manifest_and_lifetime(self) -> MutationEvidence:
        if canonical_execution_hash(self.manifest) != self.manifest_record_hash:
            raise ValueError("mutation evidence manifest hash mismatch")
        lifetime = self.expires_at - self.issued_at
        if lifetime <= timedelta(0) or lifetime > MAXIMUM_MUTATION_EVIDENCE_LIFETIME:
            raise ValueError("mutation evidence lifetime invalid")
        return self

    @property
    def digest(self) -> Sha256:
        return canonical_execution_hash(
            {
                "kind": "pilot-mutation-evidence-v1",
                "evidence": self.model_dump(mode="json"),
            }
        )


class SignedMutationEvidence(PredictionRecord):
    """A public evidence snapshot plus its detached launch signature."""

    evidence: MutationEvidence
    signature: Base64Bytes


@dataclass(frozen=True, slots=True)
class SignedCapability:
    """A grant plus its detached Ed25519 signature. Never persisted: bundles stay ephemeral."""

    grant: CapabilityGrant
    signature: bytes = field(repr=False)
    public_verification_key: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class IssuedGrantPair:
    primary: SignedCapability
    recovery: SignedCapability


def verify_capability_signature(capability: SignedCapability, public_key: bytes) -> bool:
    """Verify one grant against the launch's public verification key."""
    if capability.public_verification_key != public_key:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            capability.signature, capability.grant.digest.encode("ascii")
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def verify_kill_directive(directive: SignerKillDirective, public_key: bytes) -> bool:
    """Verify a kill directive using only this launch's public verification key."""
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            directive.signature, directive.signature_digest.encode("ascii")
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def verify_mutation_evidence(evidence: SignedMutationEvidence, public_key: bytes) -> bool:
    """Verify one public mutation snapshot against this launch and no other."""
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            evidence.signature, evidence.evidence.digest.encode("ascii")
        )
    except (InvalidSignature, ValueError):
        return False
    return True


class PilotCapabilityIssuer:
    """The one local authority allowed to mint execution capabilities, for one launch only."""

    def __init__(self, *, key_id: str) -> None:
        self._private_key: Ed25519PrivateKey | None = Ed25519PrivateKey.generate()
        self._public_key = self._private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        self._key_id = key_id
        self._closed = False
        self._claimed_nonces: set[str] = set()
        self._issued_capabilities: set[UUID] = set()

    @property
    def public_verification_key(self) -> bytes:
        """The only issuer material that may leave this process."""
        return self._public_key

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def issued_capability_ids(self) -> tuple[UUID, ...]:
        """Return the public capability identities this launch must kill on shutdown."""
        return tuple(sorted(self._issued_capabilities, key=str))

    def close(self) -> None:
        self._private_key = None
        self._closed = True

    def issue(
        self,
        request: CapabilityRequest,
        assertion: VerifiedOperatorAssertion,
        challenge: AuthorizationChallenge,
    ) -> IssuedGrantPair:
        self._require_open()
        self._verify_assertion_binding(request, assertion, challenge)
        return self._sign_primary_and_recovery(request, assertion)

    def issue_kill_directive(
        self,
        capability_ids: Iterable[UUID],
        *,
        issued_at: datetime,
    ) -> SignerKillDirective:
        """Sign a canonical, non-replayable kill directive for this launch."""
        self._require_open()
        normalized_ids = tuple(sorted(set(capability_ids), key=str))
        normalized_issued_at = normalize_utc_timestamp(issued_at)
        unsigned = {
            "kind": "signer-kill-v1",
            "capability_ids": normalized_ids,
            "issued_at": normalized_issued_at,
        }
        assert self._private_key is not None  # narrowed by _require_open
        signature = self._private_key.sign(canonical_execution_hash(unsigned).encode("ascii"))
        return SignerKillDirective(
            capability_ids=normalized_ids,
            issued_at=normalized_issued_at,
            signature=b64encode(signature),
        )

    def issue_mutation_evidence(
        self,
        evidence: MutationEvidence,
    ) -> SignedMutationEvidence:
        """Sign one already-validated public snapshot for immediate signer use."""
        self._require_open()
        assert self._private_key is not None  # narrowed by _require_open
        signature = self._private_key.sign(evidence.digest.encode("ascii"))
        return SignedMutationEvidence(evidence=evidence, signature=b64encode(signature))

    def _require_open(self) -> None:
        if self._closed or self._private_key is None:
            raise CapabilityIssueError(
                "ISSUER_CLOSED", "this launch's capability authority is closed"
            )

    def _verify_assertion_binding(
        self,
        request: CapabilityRequest,
        assertion: VerifiedOperatorAssertion,
        challenge: AuthorizationChallenge,
    ) -> None:
        if challenge.challenge_id != request.challenge_id:
            raise CapabilityIssueError(
                "CHALLENGE_MISMATCH", "the challenge does not belong to this request"
            )
        if assertion.challenge_id != request.challenge_id:
            raise CapabilityIssueError(
                "CHALLENGE_MISMATCH", "the assertion approved another challenge"
            )
        if challenge.mode is not request.mode:
            raise CapabilityIssueError(
                "MODE_MISMATCH", f"{challenge.mode.value} cannot issue {request.mode.value}"
            )
        if challenge.grant_kind is not GrantKind.PRIMARY:
            raise CapabilityIssueError(
                "GRANT_KIND_MISMATCH", "only a PRIMARY challenge issues execution grants"
            )
        if assertion.action_digest != action_challenge_digest(challenge):
            raise CapabilityIssueError(
                "ACTION_DIGEST_MISMATCH", "the assertion signed a different action"
            )
        if assertion.account_fingerprint != request.account_fingerprint:
            raise CapabilityIssueError("ACCOUNT_MISMATCH", "the assertion is for another account")
        if assertion.browser_session_hash != request.browser_session_hash:
            raise CapabilityIssueError(
                "BROWSER_SESSION_MISMATCH", "the assertion is from another browser session"
            )
        if request.ceiling_hash != PILOT_CEILING_HASH:
            raise CapabilityIssueError(
                "CEILING_MISMATCH", "the request does not bind the compiled ceilings"
            )
        if challenge.requested_limits_hash != request.requested_limits_hash:
            raise CapabilityIssueError(
                "LIMITS_MISMATCH", "the approved limits differ from the requested limits"
            )
        if set(challenge.allowed_operations) != set(request.allowed_operations) or set(
            challenge.recovery_operations
        ) != set(request.recovery_operations):
            raise CapabilityIssueError(
                "LIMITS_MISMATCH", "the approved operations differ from the requested operations"
            )
        self._require_bounded_lifetimes(request)

    def _require_bounded_lifetimes(self, request: CapabilityRequest) -> None:
        primary_lifetime = request.expires_at - request.not_before
        if primary_lifetime <= timedelta(0):
            raise CapabilityIssueError(
                "PRIMARY_LIFETIME_EXCEEDED", "a capability must expire after it becomes valid"
            )
        if primary_lifetime > MAXIMUM_PRIMARY_LIFETIME[request.mode]:
            raise CapabilityIssueError(
                "PRIMARY_LIFETIME_EXCEEDED",
                f"{request.mode.value} may not live longer than "
                f"{MAXIMUM_PRIMARY_LIFETIME[request.mode]}",
            )
        if request.recovery_expires_at > request.expires_at + MAXIMUM_RECOVERY_LIFETIME:
            raise CapabilityIssueError(
                "RECOVERY_LIFETIME_EXCEEDED",
                "recovery may outlive primary authority by at most 120 seconds",
            )
        if request.recovery_expires_at <= request.not_before:
            raise CapabilityIssueError(
                "RECOVERY_LIFETIME_EXCEEDED", "recovery must expire after it becomes valid"
            )
        if unsupported := set(request.recovery_operations) - _RECOVERY_OPERATIONS:
            raise CapabilityIssueError(
                "RECOVERY_OPERATION_NOT_REDUCING",
                f"recovery cannot perform {sorted(item.value for item in unsupported)}",
            )
        if not request.not_before <= request.presence_deadline <= request.expires_at:
            raise CapabilityIssueError(
                "PRESENCE_DEADLINE_INVALID",
                "operator presence must be required for the whole capability lifetime",
            )

    def _sign_primary_and_recovery(
        self, request: CapabilityRequest, assertion: VerifiedOperatorAssertion
    ) -> IssuedGrantPair:
        for nonce in (request.primary_nonce, request.recovery_nonce):
            if nonce in self._claimed_nonces:
                raise CapabilityIssueError("NONCE_REPLAYED", "this nonce was already issued")
        for capability_id in (request.capability_id, request.recovery_capability_id):
            if capability_id in self._issued_capabilities:
                raise CapabilityIssueError(
                    "CAPABILITY_ALREADY_ISSUED", "this capability identity was already issued"
                )
        primary = self._sign(
            self._grant(
                request,
                assertion,
                grant_kind=GrantKind.PRIMARY,
                capability_id=request.capability_id,
                operations=request.allowed_operations,
                nonce=request.primary_nonce,
                expires_at=request.expires_at,
            )
        )
        recovery = self._sign(
            self._grant(
                request,
                assertion,
                grant_kind=GrantKind.RECOVERY,
                capability_id=request.recovery_capability_id,
                operations=request.recovery_operations,
                nonce=request.recovery_nonce,
                expires_at=request.recovery_expires_at,
            )
        )
        self._claimed_nonces.update({request.primary_nonce, request.recovery_nonce})
        self._issued_capabilities.update({request.capability_id, request.recovery_capability_id})
        return IssuedGrantPair(primary=primary, recovery=recovery)

    def _grant(
        self,
        request: CapabilityRequest,
        assertion: VerifiedOperatorAssertion,
        *,
        grant_kind: GrantKind,
        capability_id: UUID,
        operations: tuple[ExecutionOperation, ...],
        nonce: str,
        expires_at: datetime,
    ) -> CapabilityGrant:
        return CapabilityGrant(
            schema_version=1,
            capability_id=capability_id,
            challenge_id=request.challenge_id,
            grant_kind=grant_kind,
            mode=request.mode,
            venue_binding=request.venue_binding,
            account_fingerprint=request.account_fingerprint,
            wallet_fingerprint=request.wallet_fingerprint,
            parent_action_id=request.target_id,
            session_id=request.session_id,
            effective_limits=request.effective_limits,
            requested_limits_hash=request.requested_limits_hash,
            ceiling_hash=request.ceiling_hash,
            plan_hash=request.plan_hash,
            strategy_hash=request.strategy_hash,
            proof_family_hash=request.proof_family_hash,
            recovery_policy_hash=request.recovery_policy_hash,
            browser_session_hash=request.browser_session_hash,
            passkey_assertion_digest=assertion.assertion_digest,
            evidence_hashes=request.evidence_hashes,
            allowed_operations=operations,
            single_use=request.mode in _SINGLE_USE_MODES,
            nonce=nonce,
            issuer_key_id=self._key_id,
            not_before=request.not_before,
            expires_at=expires_at,
            presence_deadline=request.presence_deadline,
        )

    def _sign(self, grant: CapabilityGrant) -> SignedCapability:
        self._require_open()
        assert self._private_key is not None  # narrowed by _require_open
        return SignedCapability(
            grant=grant,
            signature=self._private_key.sign(grant.digest.encode("ascii")),
            public_verification_key=self._public_key,
        )
