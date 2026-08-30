from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.authority import (
    AuthorityDecision,
    VerifiedExecutionCapability,
    verify_mutation_authority,
)
from polytrading.predictions.execution.models import ExecutionOperation, canonical_execution_hash
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.pilot.capabilities import (
    CapabilityRequest,
    PilotCapabilityIssuer,
    VenueBinding,
)
from polytrading.predictions.pilot.models import (
    PILOT_CEILING_HASH,
    PILOT_CEILINGS,
    AuthorizationChallenge,
    AuthorizationMode,
)
from polytrading.predictions.pilot.passkeys import (
    RP_ID,
    FakePasskeyService,
    action_challenge_digest,
)
from polytrading.predictions.pilot.verifier import (
    PilotCapabilityVerifier,
    build_authority_context,
    verified_capability_from_grant,
)
from tests.predictions.manifest_helpers import venue_manifest
from tests.predictions.pilot_helpers import (
    ACCOUNT_FINGERPRINT,
    BROWSER_SESSION_HASH,
    CAPABILITY_ID,
    CHALLENGE_ID,
    EVIDENCE_HASH,
    POLICY_HASH,
    POLICY_ID,
    PROTOCOL_FIXTURE_HASH,
    TARGET_ID,
    WALLET_FINGERPRINT,
    challenge_fields,
    venue_binding_fields,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
PORT = 8788
CREDENTIAL_ID = "pilot-credential"
CREDENTIAL_ID_HASH = __import__("hashlib").sha256(CREDENTIAL_ID.encode()).hexdigest()
RECOVERY_CAPABILITY_ID = UUID("00000000-0000-0000-0000-00000000e001")
ELIGIBLE_MANIFEST = venue_manifest(
    venue=PredictionVenue.POLYMARKET,
    implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
    jurisdiction_review_status="ELIGIBILITY_REVIEWED",
    source_hashes=(EVIDENCE_HASH,),
    reviewed_at=NOW - timedelta(days=1),
)
MANIFEST_HASH = canonical_execution_hash(ELIGIBLE_MANIFEST)


def binding(**overrides: Any) -> VenueBinding:
    fields = venue_binding_fields(manifest_record_hash=MANIFEST_HASH, **overrides)
    return VenueBinding.model_validate(fields, strict=True)


def issued(**overrides: Any):
    mode = overrides.pop("mode", AuthorizationMode.COMPLETE_STRATEGY)
    target = AuthorizationChallenge.model_validate(
        challenge_fields(
            credential_id_hash=CREDENTIAL_ID_HASH,
            mode=mode,
            not_before=NOW,
            expires_at=NOW + timedelta(minutes=5),
        ),
        strict=True,
    )
    service = FakePasskeyService(port=PORT)
    service.registration_options(account_fingerprint=ACCOUNT_FINGERPRINT, wallet_unlocked=True)
    service.complete_registration(
        credential={"id": CREDENTIAL_ID, "public_key": "cHVibGlj", "sign_count": 0},
        account_fingerprint=ACCOUNT_FINGERPRINT,
        wallet_fingerprint=WALLET_FINGERPRINT,
        registered_at=NOW,
    )
    service.authentication_options(target)
    assertion = service.verify(
        credential={"action_digest": action_challenge_digest(target), "sign_count": 1},
        challenge=target,
        browser_session_hash=BROWSER_SESSION_HASH,
        origin=f"http://localhost:{PORT}",
        rp_id=RP_ID,
        verified_at=NOW,
    )
    request = CapabilityRequest.model_validate(
        {
            "schema_version": 1,
            "capability_id": CAPABILITY_ID,
            "recovery_capability_id": RECOVERY_CAPABILITY_ID,
            "challenge_id": CHALLENGE_ID,
            "mode": mode,
            "venue_binding": overrides.pop("binding", binding()),
            "account_fingerprint": ACCOUNT_FINGERPRINT,
            "wallet_fingerprint": WALLET_FINGERPRINT,
            "browser_session_hash": BROWSER_SESSION_HASH,
            "policy_id": POLICY_ID,
            "target_id": TARGET_ID,
            "session_id": None,
            "effective_limits": PILOT_CEILINGS,
            "requested_limits_hash": POLICY_HASH,
            "ceiling_hash": PILOT_CEILING_HASH,
            "plan_hash": PROTOCOL_FIXTURE_HASH,
            "strategy_hash": EVIDENCE_HASH,
            "proof_family_hash": "5" * 64,
            "recovery_policy_hash": "6" * 64,
            "evidence_hashes": (PROTOCOL_FIXTURE_HASH, EVIDENCE_HASH),
            "allowed_operations": (
                ExecutionOperation.SIGN_ORDER,
                ExecutionOperation.SUBMIT_ORDER,
            ),
            "recovery_operations": (ExecutionOperation.CANCEL_ORDER,),
            "primary_nonce": "primary-nonce-1",
            "recovery_nonce": "recovery-nonce-1",
            "not_before": NOW,
            "expires_at": NOW + timedelta(minutes=5),
            "recovery_expires_at": NOW + timedelta(minutes=7),
            "presence_deadline": NOW + timedelta(minutes=5),
            **overrides,
        },
        strict=True,
    )
    issuer = PilotCapabilityIssuer(key_id="launch-1")
    return issuer, issuer.issue(request, assertion, target)


def context(capability: VerifiedExecutionCapability, **overrides: Any):
    fields: dict[str, Any] = {
        "capability": capability,
        "manifest": ELIGIBLE_MANIFEST,
        "now": NOW + timedelta(seconds=1),
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "action_id": TARGET_ID,
        "requested_notional": Decimal("9"),
        "capital_after": Decimal("20"),
        "position_after": Decimal("20"),
        "loss_after": Decimal("1"),
        "used_activation_nonces": frozenset(),
        "revoked_capability_ids": frozenset(),
        "geoblock_allowed": True,
        "geoblock_evidence_hash": "a" * 64,
        "geoblock_expires_at": NOW + timedelta(minutes=1),
        "account_scope_evidence_hash": "b" * 64,
        "account_scope_expires_at": NOW + timedelta(minutes=1),
        "kill_engaged": False,
        "operator_present": True,
        "evidence_hashes": (EVIDENCE_HASH,),
    }
    fields.update(overrides)
    return build_authority_context(**fields)


def test_a_verified_grant_projects_every_field_the_authority_compares() -> None:
    issuer, grants = issued()

    verified = PilotCapabilityVerifier(issuer.public_verification_key).verify(
        capability=grants.primary, now=NOW + timedelta(seconds=1)
    )

    assert isinstance(verified, VerifiedExecutionCapability)
    assert verified.capability_id == CAPABILITY_ID
    assert verified.venue is PredictionVenue.POLYMARKET
    assert verified.mode == "COMPLETE_STRATEGY"
    assert verified.grant_kind == "PRIMARY"
    assert verified.parent_action_id == TARGET_ID
    assert verified.maximum_per_intent_notional == PILOT_CEILINGS.order_notional
    assert verified.maximum_capital == PILOT_CEILINGS.session_deployed_capital
    assert verified.maximum_position == PILOT_CEILINGS.strategy_gross_notional
    assert verified.maximum_loss == PILOT_CEILINGS.session_loss
    assert verified.signature_valid is True
    assert verified.capability_digest == grants.primary.grant.digest


def test_a_grant_from_another_launch_is_refused() -> None:
    _issuer, grants = issued()
    other = PilotCapabilityIssuer(key_id="launch-2")

    decision = PilotCapabilityVerifier(other.public_verification_key).verify(
        capability=grants.primary, now=NOW
    )

    assert isinstance(decision, AuthorityDecision)
    assert decision.reason == "CAPABILITY_SIGNATURE_INVALID"


def test_a_tampered_grant_is_refused() -> None:
    issuer, grants = issued()
    widened = type(grants.primary)(
        grant=grants.primary.grant.model_copy(
            update={"effective_limits": PILOT_CEILINGS.model_copy(update={})}
        ),
        signature=grants.primary.signature,
        public_verification_key=grants.primary.public_verification_key,
    )
    forged = type(grants.primary)(
        grant=widened.grant.model_copy(update={"single_use": False}),
        signature=grants.primary.signature,
        public_verification_key=grants.primary.public_verification_key,
    )

    decision = PilotCapabilityVerifier(issuer.public_verification_key).verify(
        capability=forged, now=NOW
    )

    assert isinstance(decision, AuthorityDecision)
    assert decision.reason == "CAPABILITY_SIGNATURE_INVALID"


@pytest.mark.parametrize(
    ("now", "reason"),
    [
        (NOW - timedelta(seconds=1), "CAPABILITY_NOT_YET_VALID"),
        (NOW + timedelta(minutes=5), "CAPABILITY_EXPIRED"),
    ],
)
def test_the_verifier_enforces_the_grant_window(now: datetime, reason: str) -> None:
    issuer, grants = issued()

    decision = PilotCapabilityVerifier(issuer.public_verification_key).verify(
        capability=grants.primary, now=now
    )

    assert isinstance(decision, AuthorityDecision)
    assert decision.reason == reason


def test_revocation_takes_effect_at_the_verifier() -> None:
    issuer, grants = issued()
    verifier = PilotCapabilityVerifier(issuer.public_verification_key)
    verifier.revoke(grants.primary.grant.capability_id)

    decision = verifier.verify(capability=grants.primary, now=NOW + timedelta(seconds=1))

    assert isinstance(decision, AuthorityDecision)
    assert decision.reason == "CAPABILITY_REVOKED"
    assert grants.primary.grant.capability_id in verifier.revoked_capability_ids


def test_a_verified_grant_passes_the_full_authority_boundary() -> None:
    _issuer, grants = issued()
    verified = verified_capability_from_grant(grants.primary.grant, verified_at=NOW)

    decision = verify_mutation_authority(context(verified), ExecutionOperation.SUBMIT_ORDER)

    assert decision.allowed is True, decision.reason


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kill_engaged": True}, "EXECUTION_KILL_ENGAGED"),
        ({"operator_present": False}, "OPERATOR_PRESENCE_LOST"),
        ({"geoblock_allowed": False}, "GEOBLOCK_BLOCKED"),
        ({"action_id": UUID("00000000-0000-0000-0000-0000000000ff")}, "CAPABILITY_ACTION_MISMATCH"),
        ({"credential_route_requested": True}, "CAPABILITY_CREDENTIAL_ROUTE_NOT_ALLOWED"),
        ({"requested_notional": Decimal("10.01")}, "CAPABILITY_NOTIONAL_LIMIT_EXCEEDED"),
        ({"loss_after": Decimal("5.01")}, "CAPABILITY_LOSS_LIMIT_EXCEEDED"),
        ({"manifest": None}, "MANIFEST_NOT_FOUND"),
    ],
)
def test_the_boundary_still_refuses_everything_it_should(
    overrides: dict[str, Any], reason: str
) -> None:
    _issuer, grants = issued()
    verified = verified_capability_from_grant(grants.primary.grant, verified_at=NOW)

    decision = verify_mutation_authority(
        context(verified, **overrides), ExecutionOperation.SUBMIT_ORDER
    )

    assert decision.allowed is False
    assert decision.reason == reason


def test_a_recovery_grant_cannot_submit_through_the_boundary() -> None:
    _issuer, grants = issued()
    recovery = verified_capability_from_grant(grants.recovery.grant, verified_at=NOW)
    recovery_context = context(recovery)

    submitted = verify_mutation_authority(recovery_context, ExecutionOperation.SUBMIT_ORDER)
    cancelled = verify_mutation_authority(recovery_context, ExecutionOperation.CANCEL_ORDER)

    assert submitted.allowed is False
    assert cancelled.allowed is True, cancelled.reason


def test_a_replayed_activation_nonce_is_refused() -> None:
    _issuer, grants = issued()
    verified = verified_capability_from_grant(grants.primary.grant, verified_at=NOW)

    decision = verify_mutation_authority(
        context(verified, used_activation_nonces=frozenset({verified.activation_nonce})),
        ExecutionOperation.SUBMIT_ORDER,
    )

    assert decision.reason == "CAPABILITY_NONCE_REPLAYED"
