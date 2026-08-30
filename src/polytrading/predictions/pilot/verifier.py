"""The adapter that lets the coordinator and signer verify one pilot grant independently.

A signed grant is not authority by itself. This module checks its Ed25519 signature against the
launch's public verification key, then projects it into the sanitized
:class:`VerifiedExecutionCapability` the existing authority layer already knows how to refuse.
Every field the authority compares comes from the grant; nothing is inferred here.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from polytrading.predictions.domain import PredictionVenue, Sha256
from polytrading.predictions.execution.authority import (
    AuthorityContext,
    AuthorityDecision,
    VerifiedExecutionCapability,
)
from polytrading.predictions.execution.models import ExecutionOperation
from polytrading.predictions.manifest import VenueManifest
from polytrading.predictions.pilot.capabilities import (
    CapabilityGrant,
    SignedCapability,
    verify_capability_signature,
)
from polytrading.predictions.pilot.models import GrantKind


class PilotCapabilityVerifier:
    """Verify grants minted by one launch's issuer, and nothing else."""

    __slots__ = ("_public_key", "_revoked")

    def __init__(self, public_verification_key: bytes) -> None:
        self._public_key = public_verification_key
        self._revoked: set[UUID] = set()

    def revoke(self, capability_id: UUID) -> None:
        """Revocation is local, append-only in effect, and never reversed in this launch."""
        self._revoked.add(capability_id)

    @property
    def revoked_capability_ids(self) -> frozenset[UUID]:
        return frozenset(self._revoked)

    def verify(
        self, *, capability: SignedCapability, now: datetime
    ) -> VerifiedExecutionCapability | AuthorityDecision:
        """Return a sanitized projection, or a fail-closed denial with a stable reason."""

        if not verify_capability_signature(capability, self._public_key):
            return AuthorityDecision(False, "CAPABILITY_SIGNATURE_INVALID", ())
        grant = capability.grant
        if grant.capability_id in self._revoked:
            return AuthorityDecision(False, "CAPABILITY_REVOKED", ())
        if now < grant.not_before:
            return AuthorityDecision(False, "CAPABILITY_NOT_YET_VALID", ())
        if now >= grant.expires_at:
            return AuthorityDecision(False, "CAPABILITY_EXPIRED", ())
        return verified_capability_from_grant(grant, verified_at=now)


def verified_capability_from_grant(
    grant: CapabilityGrant, *, verified_at: datetime
) -> VerifiedExecutionCapability:
    """Project one verified grant into the authority layer's sanitized capability."""

    binding = grant.venue_binding
    limits = grant.effective_limits
    return VerifiedExecutionCapability(
        capability_version=1,
        capability_id=grant.capability_id,
        venue=binding.venue,
        account_fingerprint=grant.account_fingerprint,
        manifest_record_hash=binding.manifest_record_hash,
        manifest_source_hashes=binding.manifest_source_hashes,
        eligibility_evidence_hashes=binding.eligibility_evidence_hashes,
        strategy_policy_hash=binding.strategy_policy_hash,
        proof_policy_hash=binding.proof_policy_hash,
        economics_policy_hash=binding.economics_policy_hash,
        protocol_fixture_hash=binding.protocol_fixture_hash,
        allowed_operations=grant.allowed_operations,
        route_set_version=binding.route_set_version,
        route_set_hash=binding.route_set_hash,
        # The pilot's own bounds are the capability's bounds: the session deployment ceiling caps
        # capital, one order caps per-intent notional, and the strategy ceiling caps position.
        maximum_capital=limits.session_deployed_capital,
        maximum_per_intent_notional=limits.order_notional,
        maximum_position=limits.strategy_gross_notional,
        maximum_loss=limits.session_loss,
        not_before=grant.not_before,
        expires_at=grant.expires_at,
        activation_nonce=grant.nonce,
        issuer_key_id=grant.issuer_key_id,
        mode=grant.mode.value,
        grant_kind=grant.grant_kind.value,
        parent_action_id=grant.parent_action_id,
        session_id=grant.session_id,
        requested_limits_hash=grant.requested_limits_hash,
        ceiling_hash=grant.ceiling_hash,
        plan_hash=grant.plan_hash,
        strategy_hash=grant.strategy_hash,
        proof_family_hash=grant.proof_family_hash,
        recovery_policy_hash=grant.recovery_policy_hash,
        presence_deadline=grant.presence_deadline,
        single_use=grant.single_use,
        capability_digest=grant.digest,
        verified_at=verified_at,
        signature_valid=True,
        canonical_bytes_valid=True,
    )


def build_authority_context(
    *,
    capability: VerifiedExecutionCapability | None,
    manifest: VenueManifest | None,
    now: datetime,
    account_fingerprint: Sha256,
    action_id: UUID | None,
    requested_notional: Decimal,
    capital_after: Decimal,
    position_after: Decimal,
    loss_after: Decimal,
    used_activation_nonces: frozenset[str],
    revoked_capability_ids: frozenset[UUID],
    geoblock_allowed: bool | None,
    geoblock_evidence_hash: Sha256 | None,
    geoblock_expires_at: datetime | None,
    account_scope_evidence_hash: Sha256 | None,
    account_scope_expires_at: datetime | None,
    kill_engaged: bool,
    operator_present: bool,
    evidence_hashes: Sequence[Sha256],
    permitted_clock_skew: timedelta = timedelta(seconds=2),
    observed_clock_skew: timedelta = timedelta(0),
    credential_route_requested: bool = False,
) -> AuthorityContext:
    """Assemble one boundary's expectations from the grant it is about to check.

    The expectations are copied from the capability deliberately: the authority layer's job is to
    prove the *rest* of the world still matches the grant, and every hash it cannot corroborate
    locally is checked again against the manifest, protocol, and route set it reads itself.
    """

    return AuthorityContext(
        manifest=manifest,
        verified_capability=capability,
        now=now,
        venue=PredictionVenue.POLYMARKET,
        account_fingerprint=account_fingerprint,
        manifest_record_hash=_field(capability, "manifest_record_hash", ""),
        manifest_source_hashes=_field(capability, "manifest_source_hashes", ()),
        strategy_policy_hash=_field(capability, "strategy_policy_hash", ""),
        proof_policy_hash=_field(capability, "proof_policy_hash", ""),
        economics_policy_hash=_field(capability, "economics_policy_hash", ""),
        protocol_fixture_hash=_field(capability, "protocol_fixture_hash", ""),
        route_set_version=_field(capability, "route_set_version", "unset"),
        route_set_hash=_field(capability, "route_set_hash", ""),
        requested_notional=requested_notional,
        capital_after=capital_after,
        position_after=position_after,
        loss_after=loss_after,
        activation_nonce=_field(capability, "activation_nonce", "unset"),
        used_activation_nonces=used_activation_nonces,
        revoked_capability_ids=revoked_capability_ids,
        observed_clock_skew=observed_clock_skew,
        permitted_clock_skew=permitted_clock_skew,
        geoblock_allowed=geoblock_allowed,
        geoblock_evidence_hash=geoblock_evidence_hash,
        geoblock_expires_at=geoblock_expires_at,
        account_scope_account_fingerprint=account_fingerprint,
        account_scope_evidence_hash=account_scope_evidence_hash,
        account_scope_expires_at=account_scope_expires_at,
        kill_engaged=kill_engaged,
        evidence_hashes=tuple(sorted(set(evidence_hashes))),
        expected_mode=_field(capability, "mode", None),
        expected_grant_kind=_field(capability, "grant_kind", None),
        action_id=action_id,
        session_id=_field(capability, "session_id", None),
        requested_limits_hash=_field(capability, "requested_limits_hash", None),
        ceiling_hash=_field(capability, "ceiling_hash", None),
        plan_hash=_field(capability, "plan_hash", None),
        strategy_hash=_field(capability, "strategy_hash", None),
        proof_family_hash=_field(capability, "proof_family_hash", None),
        recovery_policy_hash=_field(capability, "recovery_policy_hash", None),
        operator_present=operator_present,
        credential_route_requested=credential_route_requested,
    )


def _field(capability: VerifiedExecutionCapability | None, name: str, default: object) -> object:
    return default if capability is None else getattr(capability, name)


def recovery_operations(grant: CapabilityGrant) -> frozenset[ExecutionOperation]:
    return (
        frozenset(grant.allowed_operations)
        if grant.grant_kind is GrantKind.RECOVERY
        else frozenset()
    )


__all__ = [
    "PilotCapabilityVerifier",
    "build_authority_context",
    "recovery_operations",
    "verified_capability_from_grant",
]
