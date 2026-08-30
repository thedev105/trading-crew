from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from polytrading.predictions.execution.models import ExecutionOperation, canonical_execution_hash
from polytrading.predictions.pilot.models import (
    PILOT_CEILING_HASH,
    PILOT_CEILINGS,
    AuthorizationChallenge,
    AuthorizationMode,
    CapabilityEventType,
    CredentialProvisioningEvent,
    CredentialProvisioningResult,
    EligibilityAttestationRef,
    GrantKind,
    LossStatus,
    NonceScope,
    PilotActivationCeremony,
    PilotCapabilityEvent,
    PilotExecutionSession,
    PilotKillClearanceEvent,
    PilotLimits,
    PilotLossState,
    PilotNonceClaim,
    PilotPolicyProfile,
    PilotPresenceEvent,
    PilotProofFamily,
    PilotRecord,
    PilotSessionResult,
    PilotSessionState,
    pilot_nonce_claim_key,
)
from polytrading.predictions.proofs_models import APPROVED_PROOF_TEMPLATES
from tests.predictions.pilot_helpers import (
    CAPABILITY_DIGEST,
    NOW,
    activation_ceremony_fields,
    capability_event_fields,
    challenge_fields,
    credential_provisioning_fields,
    eligibility_fields,
    kill_clearance_fields,
    limits_fields,
    loss_state_fields,
    nonce_claim_fields,
    policy_fields,
    presence_event_fields,
    session_fields,
)


def test_requested_policy_cannot_encode_a_limit_above_compiled_ceiling() -> None:
    with pytest.raises(ValidationError, match="order_notional"):
        PilotPolicyProfile.model_validate(policy_fields(order_notional="10.01"), strict=True)


def test_eligibility_reference_contains_no_document_body() -> None:
    fields = eligibility_fields()
    assert "document" not in EligibilityAttestationRef.model_validate(fields).model_dump()


def test_compiled_ceilings_are_the_approved_envelope() -> None:
    assert PILOT_CEILINGS.wallet_trading_equity == Decimal("250")
    assert PILOT_CEILINGS.order_notional == Decimal("10")
    assert PILOT_CEILINGS.strategy_gross_notional == Decimal("25")
    assert PILOT_CEILINGS.session_duration == timedelta(minutes=15)
    assert PILOT_CEILINGS.session_deployed_capital == Decimal("50")
    assert PILOT_CEILINGS.concurrent_strategies == 1
    assert PILOT_CEILINGS.session_loss == Decimal("5")
    assert PILOT_CEILINGS.utc_day_loss == Decimal("10")
    assert canonical_execution_hash(PILOT_CEILINGS) == PILOT_CEILING_HASH


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wallet_trading_equity", "250.01"),
        ("order_notional", "10.01"),
        ("strategy_gross_notional", "25.01"),
        ("session_deployed_capital", "50.01"),
        ("session_loss", "5.01"),
        ("utc_day_loss", "10.01"),
    ],
)
def test_requested_limits_reject_every_raised_money_ceiling(field: str, value: str) -> None:
    with pytest.raises(ValidationError, match=field):
        PilotLimits.model_validate(limits_fields(**{field: value}), strict=True)


def test_requested_limits_reject_a_longer_session_and_extra_strategies() -> None:
    with pytest.raises(ValidationError, match="session_duration"):
        PilotLimits.model_validate(
            limits_fields(session_duration=timedelta(minutes=15, seconds=1)), strict=True
        )
    with pytest.raises(ValidationError, match="concurrent_strategies"):
        PilotLimits.model_validate(limits_fields(concurrent_strategies=2), strict=True)


def test_requested_limits_accept_lower_values() -> None:
    lowered = PilotLimits.model_validate(
        limits_fields(order_notional="4", session_duration=timedelta(minutes=5)), strict=True
    )
    assert lowered.order_notional == Decimal("4")
    assert lowered.session_duration == timedelta(minutes=5)
    assert canonical_execution_hash(lowered) != PILOT_CEILING_HASH


def test_requested_limits_reject_non_positive_money() -> None:
    with pytest.raises(ValidationError, match="order_notional"):
        PilotLimits.model_validate(limits_fields(order_notional="0"), strict=True)


def test_eligibility_requires_an_individual_philippines_operator() -> None:
    with pytest.raises(ValidationError, match="physical_jurisdiction"):
        EligibilityAttestationRef.model_validate(
            eligibility_fields(physical_jurisdiction="US"), strict=True
        )
    with pytest.raises(ValidationError, match="account_holder_type"):
        EligibilityAttestationRef.model_validate(
            eligibility_fields(account_holder_type="ENTITY"), strict=True
        )


def test_eligibility_requires_a_review_expiry_after_review() -> None:
    with pytest.raises(ValidationError, match="expiry"):
        EligibilityAttestationRef.model_validate(eligibility_fields(expires_at=NOW), strict=True)


def test_eligibility_is_only_an_operator_supplied_gate() -> None:
    with pytest.raises(ValidationError, match="operator_supplied_gate"):
        EligibilityAttestationRef.model_validate(
            eligibility_fields(operator_supplied_gate=False), strict=True
        )


def test_pilot_records_are_frozen_and_reject_unknown_fields() -> None:
    attestation = EligibilityAttestationRef.model_validate(eligibility_fields(), strict=True)
    with pytest.raises(ValidationError):
        attestation.operator_reference = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="document_body"):
        EligibilityAttestationRef.model_validate(
            eligibility_fields(document_body="scan.pdf"), strict=True
        )


def test_pilot_records_reject_naive_timestamps() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        EligibilityAttestationRef.model_validate(
            eligibility_fields(reviewed_at=datetime(2026, 8, 27, 16)), strict=True
        )


def test_pilot_record_subclasses_cannot_declare_secret_looking_fields() -> None:
    with pytest.raises(TypeError, match="api_secret"):

        class _Leaky(PilotRecord):
            api_secret: str


def test_pilot_record_subclasses_may_declare_digests_of_sensitive_material() -> None:
    class _Safe(PilotRecord):
        passkey_assertion_digest: str
        credential_fingerprint: str

    assert set(_Safe.model_fields) == {"passkey_assertion_digest", "credential_fingerprint"}


def test_enabled_proof_families_are_approved_single_venue_templates() -> None:
    assert {family.value for family in PilotProofFamily} <= APPROVED_PROOF_TEMPLATES
    with pytest.raises(ValidationError, match="enabled_proof_families"):
        PilotPolicyProfile.model_validate(policy_fields(enabled_proof_families=()), strict=True)
    with pytest.raises(ValidationError, match="enabled_proof_families"):
        PilotPolicyProfile.model_validate(
            policy_fields(
                enabled_proof_families=(
                    PilotProofFamily.EXHAUSTIVE_OUTCOME_SET,
                    PilotProofFamily.BINARY_COMPLEMENT,
                )
            ),
            strict=True,
        )


def test_policy_profile_binds_the_compiled_ceiling_hash() -> None:
    with pytest.raises(ValidationError, match="ceiling_hash"):
        PilotPolicyProfile.model_validate(policy_fields(ceiling_hash="f" * 64), strict=True)
    policy = PilotPolicyProfile.model_validate(policy_fields(), strict=True)
    assert policy.requested_limits == PILOT_CEILINGS


def test_unknown_loss_state_carries_no_amounts() -> None:
    with pytest.raises(ValidationError, match="UNKNOWN"):
        PilotLossState.model_validate(
            loss_state_fields(status=LossStatus.UNKNOWN), strict=True
        )
    unknown = PilotLossState.model_validate(
        loss_state_fields(
            status=LossStatus.UNKNOWN,
            session_start_equity=None,
            realized_loss=None,
            unrealized_loss=None,
        ),
        strict=True,
    )
    assert unknown.realized_loss is None


def test_known_loss_state_requires_every_amount() -> None:
    with pytest.raises(ValidationError, match="KNOWN"):
        PilotLossState.model_validate(loss_state_fields(unrealized_loss=None), strict=True)


def test_challenge_must_expire_after_it_becomes_valid() -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        AuthorizationChallenge.model_validate(challenge_fields(expires_at=NOW), strict=True)


def test_challenge_operations_are_sorted_unique_and_mutation_free_for_recovery() -> None:
    with pytest.raises(ValidationError, match="allowed_operations"):
        AuthorizationChallenge.model_validate(
            challenge_fields(
                allowed_operations=(
                    ExecutionOperation.SUBMIT_ORDER,
                    ExecutionOperation.SIGN_ORDER,
                )
            ),
            strict=True,
        )
    with pytest.raises(ValidationError, match="recovery_operations"):
        AuthorizationChallenge.model_validate(
            challenge_fields(
                recovery_operations=(
                    ExecutionOperation.SUBMIT_ORDER,
                    ExecutionOperation.CANCEL_ORDER,
                )
            ),
            strict=True,
        )


def test_credential_provisioning_challenge_allows_no_trading_operation() -> None:
    with pytest.raises(ValidationError, match="CREDENTIAL_PROVISIONING"):
        AuthorizationChallenge.model_validate(
            challenge_fields(grant_kind=GrantKind.CREDENTIAL_PROVISIONING), strict=True
        )
    challenge = AuthorizationChallenge.model_validate(
        challenge_fields(
            grant_kind=GrantKind.CREDENTIAL_PROVISIONING,
            allowed_operations=(),
            recovery_operations=(),
        ),
        strict=True,
    )
    assert challenge.grant_kind is GrantKind.CREDENTIAL_PROVISIONING


def test_credential_provisioning_event_records_only_fingerprints() -> None:
    event = CredentialProvisioningEvent.model_validate(
        credential_provisioning_fields(), strict=True
    )
    dumped = event.model_dump()
    assert "credential_fingerprint" in dumped
    assert not {"api_key", "api_secret", "passphrase"} & set(dumped)
    with pytest.raises(ValidationError, match="credential_fingerprint"):
        CredentialProvisioningEvent.model_validate(
            credential_provisioning_fields(
                result=CredentialProvisioningResult.REJECTED,
                credential_fingerprint=CAPABILITY_DIGEST,
            ),
            strict=True,
        )


def test_capability_event_rejects_a_raw_bundle() -> None:
    with pytest.raises(ValidationError, match="capability_bundle"):
        PilotCapabilityEvent.model_validate(
            capability_event_fields(capability_bundle="signed-bytes"), strict=True
        )
    rejected = PilotCapabilityEvent.model_validate(
        capability_event_fields(
            event_type=CapabilityEventType.REJECTED, reason="CAPABILITY_EXPIRED"
        ),
        strict=True,
    )
    assert rejected.reason == "CAPABILITY_EXPIRED"


def test_nonce_claim_key_is_scoped_and_stable() -> None:
    claim = PilotNonceClaim.model_validate(nonce_claim_fields(), strict=True)
    other_scope = PilotNonceClaim.model_validate(
        nonce_claim_fields(scope=NonceScope.CHALLENGE), strict=True
    )
    assert pilot_nonce_claim_key(claim) == pilot_nonce_claim_key(
        PilotNonceClaim.model_validate(nonce_claim_fields(payload_hash="cd" * 32), strict=True)
    )
    assert pilot_nonce_claim_key(claim) != pilot_nonce_claim_key(other_scope)


def test_nonce_claim_requires_a_sha256_payload_hash() -> None:
    with pytest.raises(ValidationError, match="payload_hash"):
        PilotNonceClaim.model_validate(nonce_claim_fields(payload_hash="short"), strict=True)


def test_session_records_are_ordered_immutable_transitions() -> None:
    session = PilotExecutionSession.model_validate(session_fields(), strict=True)
    assert session.mode is AuthorizationMode.AUTOMATION_SESSION
    assert session.sequence_number == 0
    with pytest.raises(ValidationError, match="sequence_number"):
        PilotExecutionSession.model_validate(session_fields(sequence_number=-1), strict=True)
    with pytest.raises(ValidationError, match="expires_at"):
        PilotExecutionSession.model_validate(
            session_fields(expires_at=NOW - timedelta(seconds=1)), strict=True
        )


def test_session_deployment_stays_inside_its_effective_limits() -> None:
    with pytest.raises(ValidationError, match="deployed_capital"):
        PilotExecutionSession.model_validate(
            session_fields(deployed_capital=Decimal("50.01")), strict=True
        )


def test_exact_order_and_strategy_sessions_may_not_declare_a_session_result_early() -> None:
    ended = PilotExecutionSession.model_validate(
        session_fields(
            state=PilotSessionState.STOPPED,
            result=PilotSessionResult.COMPLETED,
            ended_at=NOW + timedelta(minutes=1),
        ),
        strict=True,
    )
    assert ended.ended_at == NOW + timedelta(minutes=1)
    with pytest.raises(ValidationError, match="result"):
        PilotExecutionSession.model_validate(
            session_fields(result=PilotSessionResult.COMPLETED), strict=True
        )


def test_presence_event_records_transitions_not_heartbeats() -> None:
    event = PilotPresenceEvent.model_validate(presence_event_fields(), strict=True)
    assert event.event_type.value == "STARTED"
    with pytest.raises(ValidationError, match="event_type"):
        PilotPresenceEvent.model_validate(
            presence_event_fields(event_type="HEARTBEAT"), strict=True
        )


def test_activation_ceremony_binds_its_readiness_and_assertion_digests() -> None:
    ceremony = PilotActivationCeremony.model_validate(activation_ceremony_fields(), strict=True)
    assert ceremony.stage == 3
    with pytest.raises(ValidationError, match="stage"):
        PilotActivationCeremony.model_validate(activation_ceremony_fields(stage=5), strict=True)
    with pytest.raises(ValidationError, match="manifest_record_hash"):
        PilotActivationCeremony.model_validate(
            activation_ceremony_fields(manifest_record_hash=None), strict=True
        )


def test_kill_clearance_requires_reconciliation_and_a_new_assertion() -> None:
    clearance = PilotKillClearanceEvent.model_validate(kill_clearance_fields(), strict=True)
    assert clearance.result.value == "CLEARED"
    with pytest.raises(ValidationError, match="discrepancy_evidence_hashes"):
        PilotKillClearanceEvent.model_validate(
            kill_clearance_fields(discrepancy_evidence_hashes=()), strict=True
        )


def test_pilot_records_normalize_offset_timestamps_to_utc() -> None:
    shifted = NOW.astimezone(UTC)
    attestation = EligibilityAttestationRef.model_validate(
        eligibility_fields(reviewed_at=shifted), strict=True
    )
    assert attestation.reviewed_at.tzinfo is UTC
