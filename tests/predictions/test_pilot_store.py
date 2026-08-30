from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from polytrading.predictions.pilot.models import (
    AuthorizationChallenge,
    CredentialProvisioningEvent,
    EligibilityAttestationRef,
    PilotActivationCeremony,
    PilotCapabilityEvent,
    PilotExecutionSession,
    PilotKillClearanceEvent,
    PilotNonceClaim,
    PilotPolicyProfile,
    PilotPresenceEvent,
    PilotSessionResult,
    PilotSessionState,
)
from polytrading.predictions.storage.store import ConflictingRecordError, PredictionMarketStore
from tests.predictions.pilot_helpers import (
    ACCOUNT_FINGERPRINT,
    NOW,
    SESSION_ID,
    activation_ceremony_fields,
    capability_event_fields,
    challenge_fields,
    credential_provisioning_fields,
    eligibility_fields,
    kill_clearance_fields,
    nonce_claim_fields,
    policy_fields,
    presence_event_fields,
    session_fields,
)

_PILOT_TABLES = (
    "pilot_eligibility_attestation_refs",
    "pilot_policy_profiles",
    "pilot_activation_ceremonies",
    "pilot_credential_provisioning_events",
    "pilot_authorization_challenges",
    "pilot_capability_events",
    "pilot_nonce_claims",
    "pilot_execution_sessions",
    "pilot_presence_events",
    "pilot_kill_clearance_events",
)


@pytest.fixture
def store(tmp_path: Path) -> PredictionMarketStore:
    opened = PredictionMarketStore(tmp_path / "pilot.duckdb")
    try:
        yield opened
    finally:
        opened.close()


def attestation() -> EligibilityAttestationRef:
    return EligibilityAttestationRef.model_validate(eligibility_fields(), strict=True)


def policy() -> PilotPolicyProfile:
    return PilotPolicyProfile.model_validate(policy_fields(), strict=True)


def ceremony() -> PilotActivationCeremony:
    return PilotActivationCeremony.model_validate(activation_ceremony_fields(), strict=True)


def credential_event() -> CredentialProvisioningEvent:
    return CredentialProvisioningEvent.model_validate(credential_provisioning_fields(), strict=True)


def challenge() -> AuthorizationChallenge:
    return AuthorizationChallenge.model_validate(challenge_fields(), strict=True)


def capability_event() -> PilotCapabilityEvent:
    return PilotCapabilityEvent.model_validate(capability_event_fields(), strict=True)


def nonce_claim() -> PilotNonceClaim:
    return PilotNonceClaim.model_validate(nonce_claim_fields(), strict=True)


def session() -> PilotExecutionSession:
    return PilotExecutionSession.model_validate(session_fields(), strict=True)


def presence_event() -> PilotPresenceEvent:
    return PilotPresenceEvent.model_validate(presence_event_fields(), strict=True)


def kill_clearance() -> PilotKillClearanceEvent:
    return PilotKillClearanceEvent.model_validate(kill_clearance_fields(), strict=True)


def test_migration_011_creates_every_pilot_table(store: PredictionMarketStore) -> None:
    tables = {
        row[0]
        for row in store._connection.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert set(_PILOT_TABLES) <= tables


def test_pilot_records_round_trip_through_verified_reads(store: PredictionMarketStore) -> None:
    assert store.append_pilot_eligibility_attestation(attestation()) is True
    assert store.append_pilot_policy_profile(policy()) is True
    assert store.append_pilot_activation_ceremony(ceremony()) is True
    assert store.append_pilot_credential_provisioning_event(credential_event()) is True
    assert store.append_pilot_authorization_challenge(challenge()) is True
    assert store.append_pilot_capability_event(capability_event()) is True
    assert store.append_pilot_execution_session(session()) is True
    assert store.append_pilot_presence_event(presence_event()) is True
    assert store.append_pilot_kill_clearance_event(kill_clearance()) is True

    account = ACCOUNT_FINGERPRINT
    assert store.verified_pilot_eligibility_attestations(account) == (attestation(),)
    assert store.verified_pilot_policy_profiles(account) == (policy(),)
    assert store.verified_pilot_activation_ceremonies(account) == (ceremony(),)
    assert store.verified_pilot_credential_provisioning_events(account) == (credential_event(),)
    assert store.verified_pilot_authorization_challenges(account) == (challenge(),)
    assert store.verified_pilot_capability_events(account) == (capability_event(),)
    assert store.verified_pilot_execution_session_history(SESSION_ID) == (session(),)
    assert store.verified_pilot_presence_events(account) == (presence_event(),)
    assert store.verified_pilot_kill_clearance_events(account) == (kill_clearance(),)


def test_repeated_pilot_appends_are_idempotent_and_conflicts_are_rejected(
    store: PredictionMarketStore,
) -> None:
    assert store.append_pilot_policy_profile(policy()) is True
    assert store.append_pilot_policy_profile(policy()) is False
    conflicting = PilotPolicyProfile.model_validate(policy_fields(order_notional="4"), strict=True)
    with pytest.raises(ConflictingRecordError):
        store.append_pilot_policy_profile(conflicting)


def test_pilot_session_transitions_are_appended_in_sequence(store: PredictionMarketStore) -> None:
    armed = session()
    stopped = PilotExecutionSession.model_validate(
        session_fields(
            event_id=UUID("00000000-0000-0000-0000-00000000c001"),
            sequence_number=1,
            state=PilotSessionState.STOPPED,
            result=PilotSessionResult.STOPPED_BY_OPERATOR,
            ended_at=NOW + timedelta(minutes=2),
            occurred_at=NOW + timedelta(minutes=2),
        ),
        strict=True,
    )
    assert store.append_pilot_execution_session(armed) is True
    assert store.append_pilot_execution_session(stopped) is True
    assert store.verified_pilot_execution_session_history(SESSION_ID) == (armed, stopped)

    reused_sequence = stopped.model_copy(
        update={"event_id": UUID("00000000-0000-0000-0000-00000000c002")}
    )
    with pytest.raises(ConflictingRecordError):
        store.append_pilot_execution_session(reused_sequence)


def test_pilot_nonce_claim_is_atomic_and_replay_safe(store: PredictionMarketStore) -> None:
    claim = nonce_claim()
    assert store.claim_pilot_nonce(claim) is True
    assert store.claim_pilot_nonce(claim) is False
    with pytest.raises(ConflictingRecordError):
        store.claim_pilot_nonce(claim.model_copy(update={"payload_hash": "f" * 64}))


def test_pilot_nonce_claim_persists_its_capability_event_in_one_transaction(
    store: PredictionMarketStore,
) -> None:
    event = capability_event()
    assert store.claim_pilot_nonce(nonce_claim(), capability_event=event) is True
    assert store.verified_pilot_capability_events(ACCOUNT_FINGERPRINT) == (event,)

    replayed = store.claim_pilot_nonce(nonce_claim(), capability_event=event)
    assert replayed is False
    assert store.verified_pilot_capability_events(ACCOUNT_FINGERPRINT) == (event,)


def test_pilot_nonce_claim_rolls_back_a_failed_companion_append(
    store: PredictionMarketStore,
) -> None:
    conflicting = capability_event()
    store.append_pilot_capability_event(conflicting)
    diverged = conflicting.model_copy(update={"capability_digest": "b" * 64})

    with pytest.raises(ConflictingRecordError):
        store.claim_pilot_nonce(nonce_claim(), capability_event=diverged)
    assert store.claim_pilot_nonce(nonce_claim()) is True


def test_pilot_nonce_scopes_are_independent(store: PredictionMarketStore) -> None:
    from polytrading.predictions.pilot.models import NonceScope

    assert store.claim_pilot_nonce(nonce_claim()) is True
    other_scope = PilotNonceClaim.model_validate(
        nonce_claim_fields(scope=NonceScope.CHALLENGE), strict=True
    )
    assert store.claim_pilot_nonce(other_scope) is True


def test_pilot_records_survive_reopening_the_store(tmp_path: Path) -> None:
    path = tmp_path / "reopen.duckdb"
    writer = PredictionMarketStore(path)
    writer.append_pilot_policy_profile(policy())
    writer.close()

    reader = PredictionMarketStore(path, read_only=True)
    try:
        assert reader.verified_pilot_policy_profiles(ACCOUNT_FINGERPRINT) == (policy(),)
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("account_fingerprint", "9" * 64),
        ("record_hash", "0" * 64),
        ("record_json", "{}"),
    ],
)
def test_tampered_pilot_rows_are_rejected(column: str, value: object, tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / f"tampered-{column}.duckdb")
    try:
        store.append_pilot_policy_profile(policy())
        store._connection.execute(f"UPDATE pilot_policy_profiles SET {column} = ?", [value])
        with pytest.raises(ConflictingRecordError):
            store.verified_pilot_policy_profiles(ACCOUNT_FINGERPRINT)
    finally:
        store.close()


def test_tampered_pilot_nonce_claims_are_rejected(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "tampered-nonce.duckdb")
    try:
        store.claim_pilot_nonce(nonce_claim())
        store._connection.execute("UPDATE pilot_nonce_claims SET record_json = ?", ["{}"])
        with pytest.raises(ConflictingRecordError):
            store.claim_pilot_nonce(nonce_claim())
    finally:
        store.close()
