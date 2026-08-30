from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.models import ExecutionOperation
from polytrading.predictions.pilot.models import (
    PILOT_CEILING_HASH,
    ActivationResult,
    AuthorizationMode,
    CapabilityEventType,
    ChallengeState,
    CredentialProvisioningResult,
    EligibilityReviewerCategory,
    GrantKind,
    KillClearanceResult,
    LossStatus,
    NonceScope,
    PilotProofFamily,
    PilotSessionState,
    PresenceEventType,
    PresenceState,
)

NOW = datetime(2026, 8, 27, 16, tzinfo=UTC)

ACCOUNT_FINGERPRINT = "a" * 64
WALLET_FINGERPRINT = "b" * 64
DOCUMENT_HASH = "c" * 64
POLICY_HASH = "d" * 64
EVIDENCE_HASH = "e" * 64
PROTOCOL_FIXTURE_HASH = "1" * 64
MANIFEST_RECORD_HASH = "2" * 64
BROWSER_SESSION_HASH = "3" * 64
CREDENTIAL_ID_HASH = "4" * 64
CONFIRMATION_TEXT_HASH = "5" * 64
PASSKEY_ASSERTION_DIGEST = "6" * 64
CAPABILITY_DIGEST = "7" * 64
RECONCILIATION_HASH = "8" * 64
READINESS_DIGEST = "9" * 64
GRANT_DIGEST = "0" * 64
PAYLOAD_HASH = "ab" * 32

ATTESTATION_ID = UUID("00000000-0000-0000-0000-00000000a001")
POLICY_ID = UUID("00000000-0000-0000-0000-00000000a002")
CHALLENGE_ID = UUID("00000000-0000-0000-0000-00000000a003")
CAPABILITY_ID = UUID("00000000-0000-0000-0000-00000000a004")
SESSION_ID = UUID("00000000-0000-0000-0000-00000000a005")
KILL_EVENT_ID = UUID("00000000-0000-0000-0000-00000000a006")
TARGET_ID = UUID("00000000-0000-0000-0000-00000000a007")

_LIMIT_MONEY_FIELDS = frozenset(
    {
        "wallet_trading_equity",
        "order_notional",
        "strategy_gross_notional",
        "session_deployed_capital",
        "session_loss",
        "utc_day_loss",
    }
)


def _money(value: Any) -> Any:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (str, int)):
        return Decimal(str(value))
    return value


def limits_fields(**overrides: Any) -> dict[str, Any]:
    """Ceiling-valued requested limits; string/int money overrides become ``Decimal``."""

    values: dict[str, Any] = {
        "wallet_trading_equity": Decimal("250"),
        "order_notional": Decimal("10"),
        "strategy_gross_notional": Decimal("25"),
        "session_duration": timedelta(minutes=15),
        "session_deployed_capital": Decimal("50"),
        "concurrent_strategies": 1,
        "session_loss": Decimal("5"),
        "utc_day_loss": Decimal("10"),
    }
    values.update(overrides)
    return {
        name: _money(value) if name in _LIMIT_MONEY_FIELDS else value
        for name, value in values.items()
    }


def eligibility_fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "attestation_id": ATTESTATION_ID,
        "operator_reference": "pilot-eligibility-2026-08",
        "document_hash": DOCUMENT_HASH,
        "venue": PredictionVenue.POLYMARKET,
        "account_holder_type": "INDIVIDUAL",
        "physical_jurisdiction": "PH",
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "reviewer_category": EligibilityReviewerCategory.OPERATOR_SELF,
        "scoped_assertions": ("individual-non-us-operator", "venue-terms-reviewed"),
        "operator_supplied_gate": True,
        "superseded_attestation_id": None,
        "reviewed_at": NOW,
        "expires_at": NOW + timedelta(days=90),
    }
    values.update(overrides)
    return values


def loss_state_fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "status": LossStatus.KNOWN,
        "session_start_equity": Decimal("200"),
        "realized_loss": Decimal("1"),
        "unrealized_loss": Decimal("0.5"),
        "evidence_hashes": (EVIDENCE_HASH,),
        "evaluated_at": NOW,
    }
    values.update(overrides)
    return values


def policy_fields(**overrides: Any) -> dict[str, Any]:
    limit_overrides = {name: overrides.pop(name) for name in list(overrides) if name in _LIMITS}
    values: dict[str, Any] = {
        "schema_version": 1,
        "policy_id": POLICY_ID,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "requested_limits": limits_fields(**limit_overrides),
        "ceiling_hash": PILOT_CEILING_HASH,
        "enabled_proof_families": (
            PilotProofFamily.BINARY_COMPLEMENT,
            PilotProofFamily.EXHAUSTIVE_OUTCOME_SET,
        ),
        "eligibility_attestation_id": ATTESTATION_ID,
        "eligibility_attestation_hash": EVIDENCE_HASH,
        "created_at": NOW,
    }
    values.update(overrides)
    return values


def activation_ceremony_fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "ceremony_id": UUID("00000000-0000-0000-0000-00000000b001"),
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "stage": 3,
        "readiness_digest": READINESS_DIGEST,
        "passkey_assertion_digest": PASSKEY_ASSERTION_DIGEST,
        "policy_hash": POLICY_HASH,
        "protocol_fixture_hash": PROTOCOL_FIXTURE_HASH,
        "manifest_record_hash": MANIFEST_RECORD_HASH,
        "evidence_hashes": (EVIDENCE_HASH,),
        "result": ActivationResult.APPROVED,
        "first_strategy_reconciliation_hash": None,
        "occurred_at": NOW,
    }
    values.update(overrides)
    return values


def credential_provisioning_fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "event_id": UUID("00000000-0000-0000-0000-00000000b002"),
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "protocol_fixture_hash": PROTOCOL_FIXTURE_HASH,
        "source_hashes": (EVIDENCE_HASH,),
        "grant_digest": GRANT_DIGEST,
        "result": CredentialProvisioningResult.CREATED,
        "credential_fingerprint": CAPABILITY_DIGEST,
        "occurred_at": NOW,
    }
    values.update(overrides)
    return values


def challenge_fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "challenge_id": CHALLENGE_ID,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "browser_session_hash": BROWSER_SESSION_HASH,
        "credential_id_hash": CREDENTIAL_ID_HASH,
        "mode": AuthorizationMode.COMPLETE_STRATEGY,
        "grant_kind": GrantKind.PRIMARY,
        "target_id": TARGET_ID,
        "policy_id": POLICY_ID,
        "evidence_hashes": (PROTOCOL_FIXTURE_HASH, EVIDENCE_HASH),
        "requested_limits_hash": POLICY_HASH,
        "ceiling_hash": PILOT_CEILING_HASH,
        "allowed_operations": (
            ExecutionOperation.SIGN_ORDER,
            ExecutionOperation.SUBMIT_ORDER,
        ),
        "recovery_operations": (ExecutionOperation.CANCEL_ORDER,),
        "confirmation_text": "Authorize one complete strategy on Polymarket",
        "confirmation_text_hash": CONFIRMATION_TEXT_HASH,
        "nonce": "challenge-nonce-1",
        "state": ChallengeState.ISSUED,
        "not_before": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(overrides)
    return values


def capability_event_fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "event_id": UUID("00000000-0000-0000-0000-00000000b003"),
        "capability_id": CAPABILITY_ID,
        "challenge_id": CHALLENGE_ID,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "mode": AuthorizationMode.COMPLETE_STRATEGY,
        "grant_kind": GrantKind.PRIMARY,
        "event_type": CapabilityEventType.ISSUED,
        "capability_digest": CAPABILITY_DIGEST,
        "nonce": "capability-nonce-1",
        "reason": None,
        "expires_at": NOW + timedelta(minutes=5),
        "occurred_at": NOW,
    }
    values.update(overrides)
    return values


def nonce_claim_fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "scope": NonceScope.CAPABILITY,
        "nonce": "capability-nonce-1",
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "payload_hash": PAYLOAD_HASH,
        "claimed_at": NOW,
    }
    values.update(overrides)
    return values


def session_fields(**overrides: Any) -> dict[str, Any]:
    limit_overrides = {name: overrides.pop(name) for name in list(overrides) if name in _LIMITS}
    values: dict[str, Any] = {
        "schema_version": 1,
        "event_id": UUID("00000000-0000-0000-0000-00000000b004"),
        "session_id": SESSION_ID,
        "sequence_number": 0,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "mode": AuthorizationMode.AUTOMATION_SESSION,
        "capability_id": CAPABILITY_ID,
        "policy_id": POLICY_ID,
        "effective_limits": limits_fields(**limit_overrides),
        "state": PilotSessionState.ARMED,
        "loss_state": loss_state_fields(),
        "presence_state": PresenceState.PRESENT,
        "strategies_started": 0,
        "deployed_capital": Decimal("0"),
        "result": None,
        "started_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
        "occurred_at": NOW,
    }
    values.update(overrides)
    return values


def presence_event_fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "event_id": UUID("00000000-0000-0000-0000-00000000b005"),
        "session_id": SESSION_ID,
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "event_type": PresenceEventType.STARTED,
        "monotonic_gap_ms": None,
        "detail_code": None,
        "occurred_at": NOW,
    }
    values.update(overrides)
    return values


def kill_clearance_fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "schema_version": 1,
        "clearance_event_id": UUID("00000000-0000-0000-0000-00000000b006"),
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "kill_event_id": KILL_EVENT_ID,
        "discrepancy_evidence_hashes": (EVIDENCE_HASH,),
        "reconciliation_hash": RECONCILIATION_HASH,
        "passkey_assertion_digest": PASSKEY_ASSERTION_DIGEST,
        "confirmation_phrase_hash": CONFIRMATION_TEXT_HASH,
        "result": KillClearanceResult.CLEARED,
        "occurred_at": NOW,
    }
    values.update(overrides)
    return values


_LIMITS = frozenset(limits_fields())
