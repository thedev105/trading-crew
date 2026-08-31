from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from polytrading.predictions.execution.models import ExecutionOperation
from polytrading.predictions.pilot.capabilities import (
    MAXIMUM_RECOVERY_LIFETIME,
    AuthorizationMode,
    CapabilityIssueError,
    CapabilityRequest,
    GrantKind,
    PilotCapabilityIssuer,
    VenueBinding,
    verify_capability_signature,
    verify_kill_directive,
)
from polytrading.predictions.pilot.models import (
    PILOT_CEILING_HASH,
    AuthorizationChallenge,
    PilotLimits,
)
from polytrading.predictions.pilot.passkeys import (
    RP_ID,
    FakePasskeyService,
    action_challenge_digest,
)
from tests.predictions.pilot_helpers import (
    ACCOUNT_FINGERPRINT,
    BROWSER_SESSION_HASH,
    CAPABILITY_ID,
    CHALLENGE_ID,
    EVIDENCE_HASH,
    NOW,
    POLICY_HASH,
    POLICY_ID,
    PROTOCOL_FIXTURE_HASH,
    TARGET_ID,
    WALLET_FINGERPRINT,
    challenge_fields,
    limits_fields,
    venue_binding_fields,
)

PORT = 8788
ORIGIN = f"http://localhost:{PORT}"
CREDENTIAL_ID = "pilot-credential"
CREDENTIAL_ID_HASH = __import__("hashlib").sha256(CREDENTIAL_ID.encode()).hexdigest()
RECOVERY_CAPABILITY_ID = UUID("00000000-0000-0000-0000-00000000e001")


def challenge(**overrides: Any) -> AuthorizationChallenge:
    fields = challenge_fields(credential_id_hash=CREDENTIAL_ID_HASH, **overrides)
    return AuthorizationChallenge.model_validate(fields, strict=True)


def request_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "capability_id": CAPABILITY_ID,
        "recovery_capability_id": RECOVERY_CAPABILITY_ID,
        "challenge_id": CHALLENGE_ID,
        "mode": AuthorizationMode.COMPLETE_STRATEGY,
        "venue_binding": VenueBinding.model_validate(venue_binding_fields(), strict=True),
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "browser_session_hash": BROWSER_SESSION_HASH,
        "policy_id": POLICY_ID,
        "target_id": TARGET_ID,
        "session_id": None,
        "effective_limits": PilotLimits.model_validate(limits_fields(), strict=True),
        "requested_limits_hash": POLICY_HASH,
        "ceiling_hash": PILOT_CEILING_HASH,
        "plan_hash": PROTOCOL_FIXTURE_HASH,
        "strategy_hash": EVIDENCE_HASH,
        "proof_family_hash": "5" * 64,
        "recovery_policy_hash": "6" * 64,
        "evidence_hashes": (PROTOCOL_FIXTURE_HASH, EVIDENCE_HASH),
        "allowed_operations": (ExecutionOperation.SIGN_ORDER, ExecutionOperation.SUBMIT_ORDER),
        "recovery_operations": (ExecutionOperation.CANCEL_ORDER,),
        "primary_nonce": "primary-nonce-1",
        "recovery_nonce": "recovery-nonce-1",
        "not_before": NOW,
        "expires_at": NOW + timedelta(minutes=5),
        "recovery_expires_at": NOW + timedelta(minutes=5, seconds=120),
        "presence_deadline": NOW + timedelta(minutes=5),
    }
    fields.update(overrides)
    return fields


def capability_request(**overrides: Any) -> CapabilityRequest:
    return CapabilityRequest.model_validate(request_fields(**overrides), strict=True)


def approved(target: AuthorizationChallenge):
    service = FakePasskeyService(port=PORT)
    service.registration_options(account_fingerprint=ACCOUNT_FINGERPRINT, wallet_unlocked=True)
    service.complete_registration(
        credential={"id": CREDENTIAL_ID, "public_key": "cHVibGlj", "sign_count": 0},
        account_fingerprint=ACCOUNT_FINGERPRINT,
        wallet_fingerprint=WALLET_FINGERPRINT,
        registered_at=NOW,
    )
    service.authentication_options(target)
    return service.verify(
        credential={"action_digest": action_challenge_digest(target), "sign_count": 1},
        challenge=target,
        browser_session_hash=BROWSER_SESSION_HASH,
        origin=ORIGIN,
        rp_id=RP_ID,
        verified_at=target.not_before,
    )


def issuer() -> PilotCapabilityIssuer:
    return PilotCapabilityIssuer(key_id="launch-1")


def test_issued_grants_are_signed_by_this_launch_only() -> None:
    target = challenge()
    authority = issuer()
    pair = authority.issue(capability_request(), approved(target), target)

    assert verify_capability_signature(pair.primary, authority.public_verification_key)
    assert verify_capability_signature(pair.recovery, authority.public_verification_key)
    assert not verify_capability_signature(pair.primary, issuer().public_verification_key)
    assert pair.primary.grant.grant_kind is GrantKind.PRIMARY
    assert pair.recovery.grant.grant_kind is GrantKind.RECOVERY
    assert pair.primary.grant.single_use is True
    assert pair.primary.grant.passkey_assertion_digest == approved(challenge()).assertion_digest


def test_kill_directive_verifies_only_for_the_launch_issuer() -> None:
    authority = PilotCapabilityIssuer(key_id="test")
    directive = authority.issue_kill_directive([CAPABILITY_ID], issued_at=NOW)

    assert verify_kill_directive(directive, authority.public_verification_key)
    assert not verify_kill_directive(
        directive,
        Ed25519PrivateKey.generate().public_key().public_bytes_raw(),
    )


def test_a_closed_issuer_drops_its_private_key_and_refuses_to_sign() -> None:
    target = challenge()
    authority = issuer()
    assertion = approved(target)
    authority.close()

    assert authority.closed is True
    assert authority._private_key is None
    with pytest.raises(CapabilityIssueError, match="ISSUER_CLOSED"):
        authority.issue(capability_request(), assertion, target)


def test_strategy_assertion_cannot_issue_a_session_capability() -> None:
    target = challenge()
    with pytest.raises(CapabilityIssueError, match="MODE_MISMATCH"):
        issuer().issue(
            capability_request(mode=AuthorizationMode.AUTOMATION_SESSION),
            approved(target),
            target,
        )


def test_an_assertion_for_another_action_cannot_issue() -> None:
    other = challenge(challenge_id=UUID("00000000-0000-0000-0000-00000000e002"))
    with pytest.raises(CapabilityIssueError, match="CHALLENGE_MISMATCH"):
        issuer().issue(capability_request(), approved(other), other)


def test_a_credential_provisioning_challenge_cannot_issue_execution_authority() -> None:
    target = challenge(
        grant_kind=GrantKind.CREDENTIAL_PROVISIONING,
        allowed_operations=(),
        recovery_operations=(),
    )
    with pytest.raises(CapabilityIssueError, match="GRANT_KIND_MISMATCH"):
        issuer().issue(
            capability_request(allowed_operations=(), recovery_operations=()),
            approved(target),
            target,
        )


def test_a_request_that_widens_the_approved_operations_is_refused() -> None:
    target = challenge()
    with pytest.raises(CapabilityIssueError, match="LIMITS_MISMATCH"):
        issuer().issue(
            capability_request(
                allowed_operations=(
                    ExecutionOperation.CANCEL_ORDER,
                    ExecutionOperation.SIGN_ORDER,
                    ExecutionOperation.SUBMIT_ORDER,
                )
            ),
            approved(target),
            target,
        )


def test_a_request_that_does_not_bind_the_compiled_ceilings_is_refused() -> None:
    target = challenge()
    with pytest.raises(CapabilityIssueError, match="CEILING_MISMATCH"):
        issuer().issue(capability_request(ceiling_hash="f" * 64), approved(target), target)


@pytest.mark.parametrize(
    ("mode", "lifetime"),
    [
        (AuthorizationMode.EXACT_ORDER, timedelta(seconds=61)),
        (AuthorizationMode.COMPLETE_STRATEGY, timedelta(minutes=5, seconds=1)),
        (AuthorizationMode.AUTOMATION_SESSION, timedelta(minutes=15, seconds=1)),
    ],
)
def test_each_mode_has_its_own_maximum_lifetime(
    mode: AuthorizationMode, lifetime: timedelta
) -> None:
    target = challenge(mode=mode)
    with pytest.raises(CapabilityIssueError, match="PRIMARY_LIFETIME_EXCEEDED"):
        issuer().issue(
            capability_request(
                mode=mode,
                expires_at=NOW + lifetime,
                presence_deadline=NOW + lifetime,
                recovery_expires_at=NOW + lifetime + MAXIMUM_RECOVERY_LIFETIME,
            ),
            approved(target),
            target,
        )


def test_recovery_never_outlives_primary_by_more_than_120_seconds() -> None:
    target = challenge()
    with pytest.raises(CapabilityIssueError, match="RECOVERY_LIFETIME_EXCEEDED"):
        issuer().issue(
            capability_request(recovery_expires_at=NOW + timedelta(minutes=5, seconds=121)),
            approved(target),
            target,
        )


def test_recovery_may_only_inspect_or_cancel() -> None:
    widened = (ExecutionOperation.CANCEL_ORDER, ExecutionOperation.SUBMIT_ORDER)

    # The challenge refuses a submitting recovery set, so no assertion can ever approve one ...
    with pytest.raises(ValidationError, match="recovery_operations"):
        challenge(recovery_operations=widened)
    # ... and the issuer refuses it independently rather than trusting that first check.
    with pytest.raises(CapabilityIssueError, match="RECOVERY_OPERATION_NOT_REDUCING"):
        issuer()._require_bounded_lifetimes(capability_request(recovery_operations=widened))


def test_presence_must_be_required_for_the_whole_capability() -> None:
    target = challenge()
    with pytest.raises(CapabilityIssueError, match="PRESENCE_DEADLINE_INVALID"):
        issuer().issue(
            capability_request(presence_deadline=NOW + timedelta(minutes=6)),
            approved(target),
            target,
        )


def test_a_nonce_is_never_issued_twice_in_one_launch() -> None:
    authority = issuer()
    first = challenge()
    authority.issue(capability_request(), approved(first), first)

    second = challenge(challenge_id=UUID("00000000-0000-0000-0000-00000000e003"))
    with pytest.raises(CapabilityIssueError, match="NONCE_REPLAYED"):
        authority.issue(
            capability_request(
                challenge_id=second.challenge_id,
                capability_id=UUID("00000000-0000-0000-0000-00000000e004"),
                recovery_capability_id=UUID("00000000-0000-0000-0000-00000000e005"),
            ),
            approved(second),
            second,
        )


def test_a_capability_identity_is_never_reissued() -> None:
    authority = issuer()
    first = challenge()
    authority.issue(capability_request(), approved(first), first)

    second = challenge(challenge_id=UUID("00000000-0000-0000-0000-00000000e006"))
    with pytest.raises(CapabilityIssueError, match="CAPABILITY_ALREADY_ISSUED"):
        authority.issue(
            capability_request(
                challenge_id=second.challenge_id,
                primary_nonce="primary-nonce-2",
                recovery_nonce="recovery-nonce-2",
            ),
            approved(second),
            second,
        )


def test_automation_sessions_are_not_single_use() -> None:
    target = challenge(mode=AuthorizationMode.AUTOMATION_SESSION)
    pair = issuer().issue(
        capability_request(
            mode=AuthorizationMode.AUTOMATION_SESSION,
            session_id=UUID("00000000-0000-0000-0000-00000000e007"),
            expires_at=NOW + timedelta(minutes=15),
            presence_deadline=NOW + timedelta(minutes=15),
            recovery_expires_at=NOW + timedelta(minutes=15, seconds=120),
        ),
        approved(target),
        target,
    )

    assert pair.primary.grant.single_use is False
    assert pair.primary.grant.session_id == UUID("00000000-0000-0000-0000-00000000e007")


def test_a_grant_binds_every_approved_hash_and_limit() -> None:
    target = challenge()
    grant = issuer().issue(capability_request(), approved(target), target).primary.grant

    assert grant.ceiling_hash == PILOT_CEILING_HASH
    assert grant.requested_limits_hash == POLICY_HASH
    assert grant.plan_hash == PROTOCOL_FIXTURE_HASH
    assert grant.strategy_hash == EVIDENCE_HASH
    assert grant.recovery_policy_hash == "6" * 64
    assert grant.effective_limits == PilotLimits.model_validate(limits_fields(), strict=True)
    assert grant.issuer_key_id == "launch-1"
    assert grant.digest == grant.model_copy().digest


def test_a_tampered_grant_fails_verification() -> None:
    target = challenge()
    authority = issuer()
    pair = authority.issue(capability_request(), approved(target), target)
    widened = pair.primary.__class__(
        grant=pair.primary.grant.model_copy(
            update={"allowed_operations": (ExecutionOperation.CANCEL_ORDER,)}
        ),
        signature=pair.primary.signature,
        public_verification_key=pair.primary.public_verification_key,
    )

    assert not verify_capability_signature(widened, authority.public_verification_key)
