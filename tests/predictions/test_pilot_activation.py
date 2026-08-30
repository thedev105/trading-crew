from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from polytrading.predictions.execution.kill_switch import derive_kill_state
from polytrading.predictions.execution.models import KillSwitchEvent
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.pilot.activation import (
    KILL_CLEARANCE_PHRASE,
    ActivationError,
    ActivationInputs,
    KillClearanceError,
    KillClearanceRequest,
    PilotReconciliationState,
    clear_pilot_kill,
    invalidate_pilot_manifest,
    promote_pilot_manifest,
)
from polytrading.predictions.pilot.models import (
    AuthorizationChallenge,
    KillClearanceResult,
    PilotProofFamily,
)
from polytrading.predictions.pilot.passkeys import (
    RP_ID,
    FakePasskeyService,
    action_challenge_digest,
)
from polytrading.predictions.pilot.qualification import QualificationGate, QualificationReport
from tests.predictions.pilot_helpers import (
    ACCOUNT_FINGERPRINT,
    BROWSER_SESSION_HASH,
    EVIDENCE_HASH,
    PROTOCOL_FIXTURE_HASH,
    WALLET_FINGERPRINT,
    challenge_fields,
)

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
PORT = 8788
CREDENTIAL_ID = "pilot-credential"
CREDENTIAL_ID_HASH = __import__("hashlib").sha256(CREDENTIAL_ID.encode()).hexdigest()
KILL_EVENT_ID = UUID("00000000-0000-0000-0000-0000000000f1")
CLEARANCE_ID = UUID("00000000-0000-0000-0000-0000000000f2")


def assertion(**overrides: Any):
    target = AuthorizationChallenge.model_validate(
        challenge_fields(credential_id_hash=CREDENTIAL_ID_HASH, **overrides), strict=True
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
    return service.verify(
        credential={"action_digest": action_challenge_digest(target), "sign_count": 1},
        challenge=target,
        browser_session_hash=BROWSER_SESSION_HASH,
        origin=f"http://localhost:{PORT}",
        rp_id=RP_ID,
        verified_at=target.not_before,
    )


def qualification(*, qualified: bool = True) -> QualificationReport:
    gate = QualificationGate(
        code="EVIDENCE_DAYS_INSUFFICIENT",
        satisfied=qualified,
        observed=Decimal(45 if qualified else 44),
        threshold=Decimal(45),
    )
    return QualificationReport(
        schema_version=1,
        proof_family=PilotProofFamily.BINARY_COMPLEMENT,
        as_of=NOW,
        evidence_window_start=NOW - timedelta(days=45),
        shadow_window_start=NOW - timedelta(days=30),
        qualified=qualified,
        gates=(gate,),
        failed_codes=() if qualified else ("EVIDENCE_DAYS_INSUFFICIENT",),
        evidence_hashes=(EVIDENCE_HASH,),
        policy_identities=("research-v1@1",),
        protocol_fixture_hashes=(PROTOCOL_FIXTURE_HASH,),
    )


def inputs(**overrides: Any) -> ActivationInputs:
    fields: dict[str, Any] = {
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "stages_passed": (0, 1, 2),
        "eligibility_expires_at": NOW + timedelta(days=30),
        "geoblock_allowed": True,
        "protocol_readiness": "CURRENT",
        "qualifications": (qualification(),),
        "reviewed_source_hashes": (PROTOCOL_FIXTURE_HASH,),
        "review_identity": "operator@example.test",
        "policy_hash": "d" * 64,
        "protocol_fixture_hash": PROTOCOL_FIXTURE_HASH,
        "readiness_digest": "9" * 64,
    }
    fields.update(overrides)
    return ActivationInputs(**fields)


def reconciliation(**overrides: Any) -> PilotReconciliationState:
    fields: dict[str, Any] = {
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "active_submissions": 0,
        "unknown_outcomes": 0,
        "reconciliation_complete": True,
        "unexplained_difference_usd": Decimal("0"),
        "reconciliation_hash": "8" * 64,
        "observed_at": NOW,
    }
    fields.update(overrides)
    return PilotReconciliationState.model_validate(fields, strict=True)


def clearance_request(**overrides: Any) -> KillClearanceRequest:
    fields: dict[str, Any] = {
        "clearance_event_id": CLEARANCE_ID,
        "kill_event_id": KILL_EVENT_ID,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "state": reconciliation(),
        "discrepancy_evidence_hashes": (EVIDENCE_HASH,),
        "confirmation_phrase": KILL_CLEARANCE_PHRASE,
        "assertion": assertion(),
    }
    fields.update(overrides)
    return KillClearanceRequest(**fields)


def test_promotion_appends_a_live_eligible_manifest_and_no_capability() -> None:
    manifest, ceremony = promote_pilot_manifest(inputs(), assertion(), now=NOW)

    assert manifest.implementation_state is AdapterImplementationState.LIVE_ELIGIBLE
    assert manifest.jurisdiction_review_status == "ELIGIBILITY_REVIEWED"
    assert manifest.authenticated_live_capability is True
    assert ceremony.stage == 3
    assert ceremony.manifest_record_hash is not None
    assert not hasattr(ceremony, "capability_id")


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"stages_passed": (0, 1)}, "STAGES_INCOMPLETE"),
        ({"eligibility_expires_at": NOW}, "ELIGIBILITY_EXPIRED"),
        ({"geoblock_allowed": False}, "GEOBLOCK_NOT_CONFIRMED"),
        ({"protocol_readiness": "PROTOCOL_REVIEW_REQUIRED"}, "PROTOCOL_REVIEW_REQUIRED"),
        ({"qualifications": ()}, "QUALIFICATION_INCOMPLETE"),
        ({"qualifications": (qualification(qualified=False),)}, "QUALIFICATION_INCOMPLETE"),
    ],
)
def test_every_promotion_gate_is_recomputed(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(ActivationError) as raised:
        promote_pilot_manifest(inputs(**overrides), assertion(), now=NOW)
    assert raised.value.code == code


def test_promotion_requires_a_fresh_assertion_for_this_account() -> None:
    with pytest.raises(ActivationError) as missing:
        promote_pilot_manifest(inputs(), None, now=NOW)
    assert missing.value.code == "ASSERTION_MISSING"

    other_account = replace(inputs(), account_fingerprint="9" * 64)
    with pytest.raises(ActivationError) as mismatched:
        promote_pilot_manifest(other_account, assertion(), now=NOW)
    assert mismatched.value.code == "ACCOUNT_MISMATCH"


def test_invalidation_appends_a_live_disabled_version_with_its_reason() -> None:
    manifest, ceremony = invalidate_pilot_manifest("attestation expiry", inputs(), now=NOW)

    assert manifest.implementation_state is AdapterImplementationState.LIVE_DISABLED
    assert manifest.authenticated_live_capability is False
    assert manifest.invalidation_conditions == ("attestation expiry",)
    assert ceremony.result.value == "REJECTED"


def test_a_reviewed_clearance_produces_an_append_only_event() -> None:
    event = clear_pilot_kill(clearance_request(), now=NOW)

    assert event.result is KillClearanceResult.CLEARED
    assert event.kill_event_id == KILL_EVENT_ID
    assert event.reconciliation_hash == "8" * 64
    assert event.discrepancy_evidence_hashes == (EVIDENCE_HASH,)
    assert not hasattr(event, "capability_id")


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"state": {"active_submissions": 1}}, "ACTIVE_SUBMISSIONS_PRESENT"),
        ({"state": {"unknown_outcomes": 1}}, "UNKNOWN_OUTCOME_PRESENT"),
        ({"state": {"reconciliation_complete": False}}, "RECONCILIATION_INCOMPLETE"),
        ({"state": {"unexplained_difference_usd": Decimal("0.01")}}, "RECONCILIATION_INCOMPLETE"),
        ({"discrepancy_evidence_hashes": ()}, "DISCREPANCY_REVIEW_MISSING"),
        ({"confirmation_phrase": "clear the kill"}, "CONFIRMATION_PHRASE_INVALID"),
        ({"assertion": None}, "ASSERTION_MISSING"),
        ({"account_fingerprint": "9" * 64}, "ACCOUNT_MISMATCH"),
    ],
)
def test_every_clearance_gate_is_required(overrides: dict[str, Any], code: str) -> None:
    prepared: dict[str, Any] = dict(overrides)
    if "state" in prepared:
        prepared["state"] = reconciliation(**prepared["state"])
    with pytest.raises(KillClearanceError) as raised:
        clear_pilot_kill(clearance_request(**prepared), now=NOW)
    assert raised.value.code == code


def kill_event(**overrides: Any) -> KillSwitchEvent:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "kill_event_id": KILL_EVENT_ID,
        "trigger": "UNKNOWN_OUTCOME",
        "scope": ACCOUNT_FINGERPRINT,
        "source_intent_id": None,
        "source_order_id": None,
        "prior_state": False,
        "occurred_at": NOW - timedelta(minutes=5),
    }
    fields.update(overrides)
    return KillSwitchEvent.model_validate(fields)


def test_an_empty_production_history_stays_killed() -> None:
    assert derive_kill_state((), production=True).engaged is True


def test_a_kill_without_a_clearance_stays_engaged() -> None:
    assert derive_kill_state((kill_event(),), production=True).engaged is True


def test_a_matching_reviewed_clearance_releases_the_kill() -> None:
    event = clear_pilot_kill(clearance_request(), now=NOW)

    state = derive_kill_state((kill_event(),), production=True, clearances=(event,))

    assert state.engaged is False
    assert state.latest_event is not None


def test_a_clearance_for_another_kill_or_an_older_one_clears_nothing() -> None:
    event = clear_pilot_kill(clearance_request(), now=NOW)
    later_kill = kill_event(
        kill_event_id=UUID("00000000-0000-0000-0000-0000000000f9"),
        occurred_at=NOW + timedelta(minutes=1),
    )

    assert (
        derive_kill_state((kill_event(), later_kill), production=True, clearances=(event,)).engaged
        is True
    )
