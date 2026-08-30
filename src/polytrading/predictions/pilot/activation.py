"""Operator ceremonies: manifest promotion, invalidation, and kill clearance.

Each ceremony is append-only evidence and creates no trading authority by itself. Promotion binds
the reviewed sources and evidence; invalidation revokes; clearance requires exact reconciliation,
an explicit review, the exact phrase, and a fresh passkey assertion — and still leaves the account
without a standing grant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from polytrading.predictions.domain import PredictionVenue, Sha256
from polytrading.predictions.execution.models import canonical_execution_hash
from polytrading.predictions.manifest import (
    AdapterImplementationState,
    VenueManifest,
)
from polytrading.predictions.pilot.models import (
    ActivationResult,
    KillClearanceResult,
    PilotActivationCeremony,
    PilotKillClearanceEvent,
    PilotRecord,
    UtcTimestamp,
)
from polytrading.predictions.pilot.passkeys import VerifiedOperatorAssertion
from polytrading.predictions.pilot.qualification import QualificationReport

# The operator must type this exactly; nothing shorter clears a kill.
KILL_CLEARANCE_PHRASE = "CLEAR POLYMARKET PILOT KILL"

ActivationCode = Literal[
    "STAGES_INCOMPLETE",
    "ELIGIBILITY_EXPIRED",
    "GEOBLOCK_NOT_CONFIRMED",
    "PROTOCOL_REVIEW_REQUIRED",
    "QUALIFICATION_INCOMPLETE",
    "ACCOUNT_MISMATCH",
    "ASSERTION_MISSING",
]
ClearanceCode = Literal[
    "ACTIVE_SUBMISSIONS_PRESENT",
    "UNKNOWN_OUTCOME_PRESENT",
    "RECONCILIATION_INCOMPLETE",
    "DISCREPANCY_REVIEW_MISSING",
    "CONFIRMATION_PHRASE_INVALID",
    "ASSERTION_MISSING",
    "ACCOUNT_MISMATCH",
]


class ActivationError(ValueError):
    def __init__(self, code: ActivationCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class KillClearanceError(ValueError):
    def __init__(self, code: ClearanceCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class ActivationInputs:
    """Everything Stage 3 recomputes before it may promote a manifest."""

    account_fingerprint: str
    wallet_fingerprint: str
    stages_passed: tuple[int, ...]
    eligibility_expires_at: datetime
    geoblock_allowed: bool
    protocol_readiness: str
    qualifications: tuple[QualificationReport, ...]
    reviewed_source_hashes: tuple[str, ...]
    review_identity: str
    policy_hash: str
    protocol_fixture_hash: str
    readiness_digest: str


class PilotReconciliationState(PilotRecord):
    """The authoritative closure a clearance is allowed to rely on."""

    account_fingerprint: Sha256
    active_submissions: int
    unknown_outcomes: int
    reconciliation_complete: bool
    unexplained_difference_usd: Decimal
    reconciliation_hash: Sha256
    observed_at: UtcTimestamp


def promote_pilot_manifest(
    inputs: ActivationInputs,
    assertion: VerifiedOperatorAssertion | None,
    *,
    now: datetime,
) -> tuple[VenueManifest, PilotActivationCeremony]:
    """Append a LIVE_ELIGIBLE manifest version. This creates no capability and submits nothing."""

    if tuple(sorted(inputs.stages_passed)) != (0, 1, 2):
        raise ActivationError("STAGES_INCOMPLETE", "stages 0-2 must all have passed")
    if inputs.eligibility_expires_at <= now:
        raise ActivationError("ELIGIBILITY_EXPIRED", "the operator attestation has expired")
    if not inputs.geoblock_allowed:
        raise ActivationError("GEOBLOCK_NOT_CONFIRMED", "the venue geoblock check did not pass")
    if inputs.protocol_readiness != "CURRENT":
        raise ActivationError("PROTOCOL_REVIEW_REQUIRED", "the protocol checkpoint needs review")
    if not inputs.qualifications or not all(report.qualified for report in inputs.qualifications):
        raise ActivationError(
            "QUALIFICATION_INCOMPLETE", "every enabled proof family must be qualified"
        )
    if assertion is None:
        raise ActivationError("ASSERTION_MISSING", "stage 3 requires a fresh passkey assertion")
    if assertion.account_fingerprint != inputs.account_fingerprint:
        raise ActivationError("ACCOUNT_MISMATCH", "the assertion is for another account")

    manifest = VenueManifest(
        schema_version=1,
        venue=PredictionVenue.POLYMARKET,
        underlying_exchange=None,
        is_independent_liquidity=True,
        official_sources=("https://docs.polymarket.com/llms.txt",),
        public_capability=True,
        authenticated_demo_capability=False,
        authenticated_live_capability=True,
        data_retention_status="permitted",
        automated_use_status="permitted",
        commercial_use_status="restricted",
        redistribution_status="restricted",
        model_training_status="restricted",
        implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
        jurisdiction_review_status="ELIGIBILITY_REVIEWED",
        review_identity=inputs.review_identity,
        reviewed_at=now,
        source_hashes=tuple(sorted(set(inputs.reviewed_source_hashes))),
        invalidation_conditions=(
            "attestation expiry",
            "evidence failure",
            "operator deactivation",
            "official source change",
        ),
    )
    ceremony = PilotActivationCeremony(
        schema_version=1,
        ceremony_id=_ceremony_id(inputs, now),
        account_fingerprint=inputs.account_fingerprint,
        wallet_fingerprint=inputs.wallet_fingerprint,
        stage=3,
        readiness_digest=inputs.readiness_digest,
        passkey_assertion_digest=assertion.assertion_digest,
        policy_hash=inputs.policy_hash,
        protocol_fixture_hash=inputs.protocol_fixture_hash,
        manifest_record_hash=canonical_execution_hash(manifest),
        evidence_hashes=tuple(
            sorted({hash_ for report in inputs.qualifications for hash_ in report.evidence_hashes})
        ),
        result=ActivationResult.APPROVED,
        first_strategy_reconciliation_hash=None,
        occurred_at=now,
    )
    return manifest, ceremony


def invalidate_pilot_manifest(
    reason: str, inputs: ActivationInputs, *, now: datetime
) -> tuple[VenueManifest, PilotActivationCeremony]:
    """Append a LIVE_DISABLED manifest version; outstanding grants are revoked by the caller."""

    manifest = VenueManifest(
        schema_version=1,
        venue=PredictionVenue.POLYMARKET,
        underlying_exchange=None,
        is_independent_liquidity=True,
        official_sources=("https://docs.polymarket.com/llms.txt",),
        public_capability=True,
        authenticated_demo_capability=False,
        authenticated_live_capability=False,
        data_retention_status="permitted",
        automated_use_status="permitted",
        commercial_use_status="restricted",
        redistribution_status="restricted",
        model_training_status="restricted",
        implementation_state=AdapterImplementationState.LIVE_DISABLED,
        jurisdiction_review_status="UNREVIEWED",
        review_identity=inputs.review_identity,
        reviewed_at=now,
        source_hashes=tuple(sorted(set(inputs.reviewed_source_hashes))),
        invalidation_conditions=(reason,),
    )
    ceremony = PilotActivationCeremony(
        schema_version=1,
        ceremony_id=_ceremony_id(inputs, now),
        account_fingerprint=inputs.account_fingerprint,
        wallet_fingerprint=inputs.wallet_fingerprint,
        stage=3,
        readiness_digest=inputs.readiness_digest,
        passkey_assertion_digest="0" * 64,
        policy_hash=inputs.policy_hash,
        protocol_fixture_hash=inputs.protocol_fixture_hash,
        manifest_record_hash=canonical_execution_hash(manifest),
        evidence_hashes=(canonical_execution_hash({"invalidation_reason": reason}),),
        result=ActivationResult.REJECTED,
        first_strategy_reconciliation_hash=None,
        occurred_at=now,
    )
    return manifest, ceremony


@dataclass(frozen=True, slots=True)
class KillClearanceRequest:
    """One operator clearance attempt, reviewed rather than automatic."""

    clearance_event_id: UUID
    kill_event_id: UUID
    account_fingerprint: str
    state: PilotReconciliationState
    discrepancy_evidence_hashes: tuple[str, ...]
    confirmation_phrase: str
    assertion: VerifiedOperatorAssertion | None


def clear_pilot_kill(request: KillClearanceRequest, *, now: datetime) -> PilotKillClearanceEvent:
    """Record one reviewed clearance. It grants nothing: a new action still needs a new passkey."""

    state = request.state
    if state.active_submissions:
        raise KillClearanceError(
            "ACTIVE_SUBMISSIONS_PRESENT", "clearance requires no in-flight submission"
        )
    if state.unknown_outcomes:
        raise KillClearanceError(
            "UNKNOWN_OUTCOME_PRESENT", "every outcome must be authoritative first"
        )
    if not state.reconciliation_complete or state.unexplained_difference_usd != 0:
        raise KillClearanceError(
            "RECONCILIATION_INCOMPLETE", "venue, ledger, and balances must reconcile exactly"
        )
    if not request.discrepancy_evidence_hashes:
        raise KillClearanceError(
            "DISCREPANCY_REVIEW_MISSING", "the operator must cite the reviewed evidence"
        )
    if request.confirmation_phrase != KILL_CLEARANCE_PHRASE:
        raise KillClearanceError(
            "CONFIRMATION_PHRASE_INVALID", "the exact confirmation phrase is required"
        )
    if request.assertion is None:
        raise KillClearanceError("ASSERTION_MISSING", "clearance requires a fresh assertion")
    if (
        request.assertion.account_fingerprint != request.account_fingerprint
        or state.account_fingerprint != request.account_fingerprint
    ):
        raise KillClearanceError("ACCOUNT_MISMATCH", "the evidence is for another account")
    return PilotKillClearanceEvent(
        schema_version=1,
        clearance_event_id=request.clearance_event_id,
        account_fingerprint=request.account_fingerprint,
        kill_event_id=request.kill_event_id,
        discrepancy_evidence_hashes=tuple(sorted(set(request.discrepancy_evidence_hashes))),
        reconciliation_hash=state.reconciliation_hash,
        passkey_assertion_digest=request.assertion.assertion_digest,
        confirmation_phrase_hash=canonical_execution_hash(
            {"confirmation_phrase": request.confirmation_phrase}
        ),
        result=KillClearanceResult.CLEARED,
        occurred_at=now,
    )


def _ceremony_id(inputs: ActivationInputs, now: datetime) -> UUID:
    from uuid import uuid5

    namespace = UUID("6f2f2c3a-6c22-4e0e-9a4e-6a9c8c9a2a11")
    return uuid5(
        namespace,
        canonical_execution_hash(
            {
                "account_fingerprint": inputs.account_fingerprint,
                "occurred_at": now.isoformat(),
                "readiness_digest": inputs.readiness_digest,
            }
        ),
    )


def qualified_families(reports: Sequence[QualificationReport]) -> tuple[str, ...]:
    return tuple(sorted(report.proof_family.value for report in reports if report.qualified))


__all__ = [
    "KILL_CLEARANCE_PHRASE",
    "ActivationError",
    "ActivationInputs",
    "KillClearanceError",
    "KillClearanceRequest",
    "PilotReconciliationState",
    "clear_pilot_kill",
    "invalidate_pilot_manifest",
    "promote_pilot_manifest",
    "qualified_families",
]
