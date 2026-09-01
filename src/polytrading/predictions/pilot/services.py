"""The live control-plane services: every route's behaviour, composed from the pilot's own parts.

Each handler resolves stable identifiers into server-side evidence, persists what it decides, and
returns a sanitized projection. The browser supplies identifiers, lowered limits, a typed
confirmation, and a passkey assertion — never a price, a route, a hash, or an order body.
"""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from typing import Any
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from polytrading.predictions.domain import Sha256
from polytrading.predictions.execution.authority import VerifiedExecutionCapability
from polytrading.predictions.execution.models import ExecutionOperation, canonical_execution_hash
from polytrading.predictions.manifest import VenueManifest
from polytrading.predictions.pilot.activation import (
    ActivationError,
    ActivationInputs,
    KillClearanceError,
    KillClearanceRequest,
    PilotReconciliationState,
    clear_pilot_kill,
    promote_pilot_manifest,
)
from polytrading.predictions.pilot.capabilities import (
    CapabilityIssueError,
    CapabilityRequest,
    IssuedGrantPair,
    PilotCapabilityIssuer,
    VenueBinding,
)
from polytrading.predictions.pilot.execution_port import ExecutionEvidence
from polytrading.predictions.pilot.models import (
    PILOT_CEILING_HASH,
    AuthorizationChallenge,
    AuthorizationMode,
    CapabilityEventType,
    ChallengeState,
    GrantKind,
    LossStatus,
    NonceScope,
    PilotCapabilityEvent,
    PilotExecutionSession,
    PilotLimits,
    PilotLossState,
    PilotNonceClaim,
    PilotPolicyProfile,
    PilotPresenceEvent,
    PilotProofFamily,
    PilotSessionResult,
    PilotSessionState,
    PresenceEventType,
    PresenceState,
)
from polytrading.predictions.pilot.passkeys import (
    PasskeyError,
    PasskeyService,
    VerifiedOperatorAssertion,
    action_challenge_digest,
)
from polytrading.predictions.pilot.policy import (
    COMPILED_PILOT_CEILINGS,
    MAXIMUM_EQUITY_EVIDENCE_AGE,
    PilotPolicyError,
    RequestedPilotLimits,
    effective_limits,
)
from polytrading.predictions.pilot.presence import PresenceMonitor
from polytrading.predictions.pilot.qualification import QualificationReport
from polytrading.predictions.pilot.read_models import (
    build_limits_view,
    build_opportunity_views,
    build_readiness_view,
    build_session_view,
)
from polytrading.predictions.pilot.selector import (
    PilotAccountState,
    PilotOpportunity,
    PilotSelectionError,
    compile_frozen_pilot_plan,
    eligible_opportunities,
)
from polytrading.predictions.pilot.server import PilotRequestError
from polytrading.predictions.pilot.sessions import ExecutionResult, PilotExecutor
from polytrading.predictions.pilot.verifier import PilotCapabilityVerifier
from polytrading.predictions.polymarket_execution.credentials import (
    CredentialProvisioner,
    CredentialProvisioningError,
    CredentialProvisioningGrant,
)
from polytrading.predictions.polymarket_execution.ipc import SignerCapabilityProof
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    AccountSignatureBinding,
)
from polytrading.predictions.storage.store import PredictionMarketStore

_CHALLENGE_NAMESPACE = UUID("0f3a1b7c-9a58-4f1e-b0d1-2f4d5c6e7a80")
CHALLENGE_LIFETIME = timedelta(minutes=5)
CONFIRMATION_TEXTS: Mapping[AuthorizationMode, str] = {
    AuthorizationMode.EXACT_ORDER: "ORDER {amount} USD",
    AuthorizationMode.COMPLETE_STRATEGY: "STRATEGY {amount} USD",
    AuthorizationMode.AUTOMATION_SESSION: "SESSION 15 MIN {amount} USD",
}
ExecutorFactory = Callable[
    [
        Mapping[UUID, VerifiedExecutionCapability],
        Mapping[UUID, SignerCapabilityProof],
        Callable[[], ExecutionEvidence],
    ],
    PilotExecutor,
]


@dataclass(frozen=True, slots=True)
class PilotEnvironment:
    """Everything the services read that they cannot derive themselves."""

    account_fingerprint: Sha256
    wallet_fingerprint: Sha256
    venue_binding: VenueBinding | None
    manifest: VenueManifest | None
    manifest_state: str
    protocol_state: str
    qualifications: tuple[QualificationReport, ...]
    eligibility_expires_at: datetime | None
    credentials_present: bool
    reconciliation: PilotReconciliationState
    account_state: Callable[[], PilotAccountState]
    account_binding: AccountSignatureBinding | None = None
    credential_provisioner: CredentialProvisioner | None = None
    # Given the grants it just verified, this builds the executor that may spend them. The
    # services never construct transport themselves.
    executor_factory: ExecutorFactory | None = None
    activation_inputs: ActivationInputs | None = None


@dataclass
class _LaunchState:
    """One launch's in-memory ceremony state. A restart invalidates all of it."""

    policy: PilotPolicyProfile | None = None
    challenges: dict[UUID, AuthorizationChallenge] = field(default_factory=dict)
    confirmations: dict[UUID, str] = field(default_factory=dict)
    grants: dict[UUID, IssuedGrantPair] = field(default_factory=dict)
    session_sequence: int = 0
    last_result: ExecutionResult | None = None
    kill_engaged: bool = True
    kill_reason: str | None = None


class LivePilotServices:
    """The operator-facing behaviour of every pilot route."""

    def __init__(
        self,
        *,
        store: PredictionMarketStore,
        environment: PilotEnvironment,
        passkeys: PasskeyService,
        issuer: PilotCapabilityIssuer,
        verifier: PilotCapabilityVerifier,
        presence: PresenceMonitor,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._environment = environment
        self._passkeys = passkeys
        self._issuer = issuer
        self._verifier = verifier
        self._presence = presence
        self._clock = clock
        self._state = _LaunchState()

    # -- reads --------------------------------------------------------------------------

    def readiness(self) -> dict[str, Any]:
        now = self._clock()
        environment = self._environment
        view = build_readiness_view(
            kill_engaged=self._state.kill_engaged,
            presence_state=self._presence.state,
            manifest_state=environment.manifest_state,
            protocol_state=environment.protocol_state,
            protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
            qualifications=environment.qualifications,
            eligibility_expires_at=environment.eligibility_expires_at,
            credentials_present=environment.credentials_present,
            reconciliation_complete=environment.reconciliation.reconciliation_complete,
            information_cutoff=environment.reconciliation.observed_at,
            as_of=now,
            evidence_hashes=self._evidence_hashes(),
        )
        return {
            **view.model_dump(mode="json"),
            "live_authority": bool(self._state.grants) and not self._state.kill_engaged,
            "secret_store_available": environment.credentials_present,
        }

    def policy(self) -> dict[str, Any]:
        requested = None if self._state.policy is None else self._state.policy.requested_limits
        return build_limits_view(requested).model_dump(mode="json")

    def opportunities(self) -> dict[str, Any]:
        now = self._clock()
        if self._state.kill_engaged:
            return {"opportunities": [], "reason": "PILOT_KILL_ENGAGED"}
        ranked = self._ranked_opportunities(now)
        views = build_opportunity_views(ranked, as_of=now)
        return {
            "opportunities": [view.model_dump(mode="json") for view in views],
            "reason": None,
        }

    def live_session(self) -> dict[str, Any]:
        now = self._clock()
        account = self._environment.account_state()
        result = self._state.last_result
        loss = PilotLossState(
            status=LossStatus.KNOWN
            if self._environment.reconciliation.reconciliation_complete
            else LossStatus.UNKNOWN,
            session_start_equity=account.collateral_usd
            if self._environment.reconciliation.reconciliation_complete
            else None,
            realized_loss=Decimal("0")
            if self._environment.reconciliation.reconciliation_complete
            else None,
            unrealized_loss=Decimal("0")
            if self._environment.reconciliation.reconciliation_complete
            else None,
            evidence_hashes=(self._environment.reconciliation.reconciliation_hash,),
            evaluated_at=now,
        )
        view = build_session_view(
            active=result is not None and result.state == "COMPLETED",
            mode=None if not self._state.grants else self._latest_mode(),
            authority_expires_at=self._authority_expiry(),
            presence_state=self._presence.state,
            last_heartbeat_at=now,
            strategies_started=0 if result is None else 1,
            deployed_capital_usd=Decimal("0") if result is None else result.deployed_capital_usd,
            loss_state=loss,
            utc_day_loss_usd=Decimal("0"),
            reconciliation_complete=self._environment.reconciliation.reconciliation_complete,
        )
        payload = view.model_dump(mode="json")
        payload["legs"] = [] if result is None else _leg_views(result)
        return {"session": payload, "reason": None}

    def audit(self) -> dict[str, Any]:
        events = self._store.verified_pilot_capability_events(self._environment.account_fingerprint)
        return {
            "events": [
                {
                    "occurred_at": event.occurred_at.isoformat(),
                    "kind": "CAPABILITY",
                    "outcome": event.event_type.value,
                    "digest": event.capability_digest,
                }
                for event in events
            ]
        }

    # -- ceremonies ---------------------------------------------------------------------

    def update_policy(self, payload: Mapping[str, object]) -> dict[str, Any]:
        limits = self._requested_limits(payload)
        profile = PilotPolicyProfile(
            schema_version=1,
            policy_id=uuid4(),
            account_fingerprint=self._environment.account_fingerprint,
            wallet_fingerprint=self._environment.wallet_fingerprint,
            requested_limits=limits,
            ceiling_hash=PILOT_CEILING_HASH,
            enabled_proof_families=self._qualified_families(),
            eligibility_attestation_id=uuid5(
                _CHALLENGE_NAMESPACE, self._environment.account_fingerprint
            ),
            eligibility_attestation_hash=self._environment.reconciliation.reconciliation_hash,
            created_at=self._clock(),
        )
        self._store.append_pilot_policy_profile(profile)
        self._state.policy = profile
        return {"policy_id": str(profile.policy_id), "accepted": True}

    def register_options(self, payload: Mapping[str, object]) -> dict[str, Any]:
        del payload
        try:
            return dict(
                self._passkeys.registration_options(
                    account_fingerprint=self._environment.account_fingerprint,
                    wallet_unlocked=self._environment.credentials_present,
                )
            )
        except PasskeyError as error:
            raise PilotRequestError(HTTPStatus.CONFLICT, error.code) from error

    def register_verify(self, payload: Mapping[str, object]) -> dict[str, Any]:
        credential = payload.get("credential")
        if not isinstance(credential, Mapping):
            raise PilotRequestError(HTTPStatus.BAD_REQUEST, "CREDENTIAL_INVALID")
        try:
            registered = self._passkeys.complete_registration(
                credential=dict(credential),
                account_fingerprint=self._environment.account_fingerprint,
                wallet_fingerprint=self._environment.wallet_fingerprint,
                registered_at=self._clock(),
            )
        except PasskeyError as error:
            raise PilotRequestError(HTTPStatus.CONFLICT, error.code) from error
        return {"credential_id_hash": registered.credential_id_hash}

    def auth_options(self, payload: Mapping[str, object]) -> dict[str, Any]:
        challenge = self._build_challenge(payload)
        self._store.append_pilot_authorization_challenge(challenge)
        self._store.claim_pilot_nonce(
            PilotNonceClaim(
                schema_version=1,
                scope=NonceScope.CHALLENGE,
                nonce=challenge.nonce,
                account_fingerprint=challenge.account_fingerprint,
                payload_hash=action_challenge_digest(challenge),
                claimed_at=self._clock(),
            )
        )
        self._state.challenges[challenge.challenge_id] = challenge
        try:
            options = dict(self._passkeys.authentication_options(challenge))
        except PasskeyError as error:
            raise PilotRequestError(HTTPStatus.CONFLICT, error.code) from error
        return {
            **options,
            "challenge_id": str(challenge.challenge_id),
            "confirmation_text": challenge.confirmation_text,
        }

    def authorize(self, payload: Mapping[str, object]) -> dict[str, Any]:
        challenge = self._challenge_for(payload)
        confirmation = payload.get("confirmation_text")
        if confirmation != challenge.confirmation_text:
            raise PilotRequestError(HTTPStatus.BAD_REQUEST, "CONFIRMATION_TEXT_MISMATCH")
        credential = payload.get("assertion")
        if not isinstance(credential, Mapping):
            raise PilotRequestError(HTTPStatus.BAD_REQUEST, "ASSERTION_INVALID")
        assertion = self._verified_assertion(challenge, dict(credential), payload)
        grants = self._issue(challenge, assertion)
        return self._run(challenge, grants)

    def presence(self, payload: Mapping[str, object]) -> dict[str, Any]:
        now = self._clock()
        kind = payload.get("kind", "HEARTBEAT")
        decision = (
            self._presence.record_page_closed(now)
            if kind == "PAGE_CLOSED"
            else self._presence.record_browser_heartbeat(now)
        )
        native = self._presence.record_native_state(now)
        verdict = native if native.kill_reason is not None else decision
        if verdict.state is PresenceState.TERMINAL:
            self._persist_presence(PresenceEventType.TERMINAL, verdict.kill_reason, now)
            self._engage_kill(verdict.kill_reason or "PRESENCE_LOST", now)
        return {
            "presence_state": verdict.state.value,
            "kill_reason": verdict.kill_reason,
            "kill_engaged": self._state.kill_engaged,
        }

    def stop(self, payload: Mapping[str, object]) -> dict[str, Any]:
        del payload
        now = self._clock()
        self._engage_kill("OPERATOR_STOP", now)
        return {"kill_engaged": True, "reason": "OPERATOR_STOP"}

    def clear_kill(self, payload: Mapping[str, object]) -> dict[str, Any]:
        challenge = self._challenge_for(payload)
        credential = payload.get("assertion")
        if not isinstance(credential, Mapping):
            raise PilotRequestError(HTTPStatus.BAD_REQUEST, "ASSERTION_INVALID")
        assertion = self._verified_assertion(challenge, dict(credential), payload)
        kill_event_id = _uuid(payload.get("kill_event_id"), "KILL_EVENT_ID_INVALID")
        request = KillClearanceRequest(
            clearance_event_id=uuid4(),
            kill_event_id=kill_event_id,
            account_fingerprint=self._environment.account_fingerprint,
            state=self._environment.reconciliation,
            discrepancy_evidence_hashes=self._evidence_hashes(),
            confirmation_phrase=str(payload.get("confirmation_phrase", "")),
            assertion=assertion,
        )
        try:
            event = clear_pilot_kill(request, now=self._clock())
        except KillClearanceError as error:
            raise PilotRequestError(HTTPStatus.CONFLICT, error.code) from error
        self._store.append_pilot_kill_clearance_event(event)
        self._state.kill_engaged = False
        self._state.kill_reason = None
        return {"cleared": True, "clearance_event_id": str(event.clearance_event_id)}

    def activate(self, payload: Mapping[str, object]) -> dict[str, Any]:
        challenge = self._challenge_for(payload)
        credential = payload.get("assertion")
        if not isinstance(credential, Mapping):
            raise PilotRequestError(HTTPStatus.BAD_REQUEST, "ASSERTION_INVALID")
        assertion = self._verified_assertion(challenge, dict(credential), payload)
        inputs = self._environment.activation_inputs
        if inputs is None:
            raise PilotRequestError(HTTPStatus.CONFLICT, "ACTIVATION_EVIDENCE_UNAVAILABLE")
        try:
            manifest, ceremony = promote_pilot_manifest(inputs, assertion, now=self._clock())
        except ActivationError as error:
            raise PilotRequestError(HTTPStatus.CONFLICT, error.code) from error
        self._store.append_venue_manifest(manifest)
        self._store.append_pilot_activation_ceremony(ceremony)
        return {
            "manifest_record_hash": ceremony.manifest_record_hash,
            "implementation_state": manifest.implementation_state.value,
            "live_authority": False,
        }

    def provision_credentials(self, payload: Mapping[str, object]) -> dict[str, Any]:
        del payload
        provisioner = self._environment.credential_provisioner
        binding = self._environment.account_binding
        if provisioner is None or binding is None:
            raise PilotRequestError(HTTPStatus.CONFLICT, "CREDENTIAL_CEREMONY_UNAVAILABLE")
        now = self._clock()
        grant = CredentialProvisioningGrant(
            grant_id=uuid4(),
            grant_kind="CREDENTIAL_PROVISIONING",
            operation="CREATE",
            wallet_fingerprint=self._environment.wallet_fingerprint,
            account_fingerprint=self._environment.account_fingerprint,
            protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
            grant_digest=canonical_execution_hash(
                {"ceremony": "credential", "at": now.isoformat()}
            ),
            issued_at=now,
            expires_at=now + timedelta(seconds=60),
        )
        try:
            fingerprint = provisioner.provision(grant, binding, now=now)
        except CredentialProvisioningError as error:
            raise PilotRequestError(HTTPStatus.CONFLICT, error.code) from error
        return {
            "credential_fingerprint": fingerprint.credential_fingerprint,
            "result": fingerprint.result,
        }

    def open_challenge(self, challenge_id: UUID) -> AuthorizationChallenge:
        """The challenge behind one issued challenge id, for a caller that already has the id."""
        challenge = self._state.challenges.get(challenge_id)
        if challenge is None:
            raise PilotRequestError(HTTPStatus.NOT_FOUND, "CHALLENGE_UNKNOWN")
        return challenge

    # -- internals ----------------------------------------------------------------------

    def _requested_limits(self, payload: Mapping[str, object]) -> PilotLimits:
        raw = payload.get("requested_limits")
        if not isinstance(raw, Mapping):
            raise PilotRequestError(HTTPStatus.BAD_REQUEST, "REQUESTED_LIMITS_INVALID")
        fields = dict(COMPILED_PILOT_CEILINGS.model_dump(mode="python"))
        for name, value in raw.items():
            if name not in fields:
                raise PilotRequestError(HTTPStatus.BAD_REQUEST, "REQUESTED_LIMITS_INVALID")
            fields[name] = _decimal(value) if name != "concurrent_strategies" else value
        try:
            requested = RequestedPilotLimits.model_validate(fields, strict=True)
            return effective_limits(requested)
        except (ValidationError, TypeError) as error:
            raise PilotRequestError(HTTPStatus.BAD_REQUEST, "REQUESTED_LIMITS_INVALID") from error
        except PilotPolicyError as error:
            raise PilotRequestError(HTTPStatus.BAD_REQUEST, error.code) from error

    def _qualified_families(self) -> tuple[PilotProofFamily, ...]:
        families = {
            report.proof_family for report in self._environment.qualifications if report.qualified
        }
        if not families:
            raise PilotRequestError(HTTPStatus.CONFLICT, "QUALIFICATION_INCOMPLETE")
        return tuple(sorted(families, key=lambda family: family.value))

    def _ranked_opportunities(self, now: datetime) -> tuple[PilotOpportunity, ...]:
        limits = (
            COMPILED_PILOT_CEILINGS
            if self._state.policy is None
            else self._state.policy.requested_limits
        )
        return eligible_opportunities(
            self._store,
            self._environment.account_state(),
            now,
            limits=limits,
            enabled_families=frozenset(self._qualified_families()),
        )

    def _build_challenge(self, payload: Mapping[str, object]) -> AuthorizationChallenge:
        now = self._clock()
        mode = _mode(payload.get("mode"))
        if mode is AuthorizationMode.AUTOMATION_SESSION:
            raise PilotRequestError(HTTPStatus.CONFLICT, "AUTOMATION_NOT_ACTIVATED")
        opportunity = self._selected_opportunity(payload, now)
        amount = (
            opportunity.deployed_capital_usd
            if opportunity is not None
            else COMPILED_PILOT_CEILINGS.session_deployed_capital
        )
        confirmation = CONFIRMATION_TEXTS[mode].format(amount=f"{amount:.2f}")
        limits = (
            COMPILED_PILOT_CEILINGS
            if self._state.policy is None
            else self._state.policy.requested_limits
        )
        challenge_id = uuid4()
        challenge = AuthorizationChallenge(
            schema_version=1,
            challenge_id=challenge_id,
            account_fingerprint=self._environment.account_fingerprint,
            wallet_fingerprint=self._environment.wallet_fingerprint,
            browser_session_hash=str(payload.get("browser_session_hash", "")),
            credential_id_hash=self._credential_id_hash(),
            mode=mode,
            grant_kind=GrantKind.PRIMARY,
            target_id=opportunity.proof_id if opportunity is not None else challenge_id,
            policy_id=self._state.policy.policy_id if self._state.policy else challenge_id,
            evidence_hashes=self._evidence_hashes(),
            requested_limits_hash=canonical_execution_hash(limits),
            ceiling_hash=PILOT_CEILING_HASH,
            allowed_operations=(
                ExecutionOperation.SIGN_ORDER,
                ExecutionOperation.SUBMIT_ORDER,
            ),
            recovery_operations=(ExecutionOperation.CANCEL_ORDER,),
            confirmation_text=confirmation,
            confirmation_text_hash=canonical_execution_hash({"confirmation": confirmation}),
            nonce=f"challenge-{challenge_id}",
            state=ChallengeState.ISSUED,
            not_before=now,
            expires_at=now + CHALLENGE_LIFETIME,
        )
        self._state.confirmations[challenge_id] = confirmation
        return challenge

    def _selected_opportunity(
        self, payload: Mapping[str, object], now: datetime
    ) -> PilotOpportunity | None:
        identifier = payload.get("opportunity_id")
        if identifier is None:
            return None
        for opportunity in self._ranked_opportunities(now):
            if str(opportunity.proof_id) == str(identifier):
                return opportunity
        raise PilotRequestError(HTTPStatus.NOT_FOUND, "OPPORTUNITY_NOT_ELIGIBLE")

    def _credential_id_hash(self) -> Sha256:
        credentials = getattr(self._passkeys, "credentials", ())
        if not credentials:
            raise PilotRequestError(HTTPStatus.CONFLICT, "CREDENTIAL_UNKNOWN")
        return credentials[0].credential_id_hash

    def _challenge_for(self, payload: Mapping[str, object]) -> AuthorizationChallenge:
        challenge_id = _uuid(payload.get("challenge_id"), "CHALLENGE_ID_INVALID")
        challenge = self._state.challenges.get(challenge_id)
        if challenge is None:
            raise PilotRequestError(HTTPStatus.NOT_FOUND, "CHALLENGE_UNKNOWN")
        return challenge

    def _verified_assertion(
        self,
        challenge: AuthorizationChallenge,
        credential: Mapping[str, object],
        payload: Mapping[str, object],
    ) -> VerifiedOperatorAssertion:
        try:
            return self._passkeys.verify(
                credential=dict(credential),
                challenge=challenge,
                browser_session_hash=str(payload.get("browser_session_hash", "")),
                origin=getattr(self._passkeys, "origin", ""),
                rp_id="localhost",
                verified_at=self._clock(),
            )
        except PasskeyError as error:
            raise PilotRequestError(HTTPStatus.FORBIDDEN, error.code) from error

    def _issue(
        self, challenge: AuthorizationChallenge, assertion: VerifiedOperatorAssertion
    ) -> IssuedGrantPair:
        if self._environment.venue_binding is None:
            raise PilotRequestError(HTTPStatus.CONFLICT, "MANIFEST_NOT_ELIGIBLE")
        now = self._clock()
        limits = (
            COMPILED_PILOT_CEILINGS
            if self._state.policy is None
            else self._state.policy.requested_limits
        )
        lifetime = {
            AuthorizationMode.EXACT_ORDER: timedelta(seconds=60),
            AuthorizationMode.COMPLETE_STRATEGY: timedelta(minutes=5),
            AuthorizationMode.AUTOMATION_SESSION: limits.session_duration,
        }[challenge.mode]
        capability_id, recovery_id = uuid4(), uuid4()
        request = CapabilityRequest(
            schema_version=1,
            capability_id=capability_id,
            recovery_capability_id=recovery_id,
            challenge_id=challenge.challenge_id,
            mode=challenge.mode,
            venue_binding=self._environment.venue_binding,
            account_fingerprint=challenge.account_fingerprint,
            wallet_fingerprint=challenge.wallet_fingerprint,
            browser_session_hash=challenge.browser_session_hash,
            policy_id=challenge.policy_id,
            target_id=challenge.target_id,
            session_id=None,
            effective_limits=limits,
            requested_limits_hash=challenge.requested_limits_hash,
            ceiling_hash=PILOT_CEILING_HASH,
            plan_hash=self._environment.venue_binding.protocol_fixture_hash,
            strategy_hash=canonical_execution_hash({"strategy": str(challenge.target_id)}),
            proof_family_hash=canonical_execution_hash(
                {"families": [family.value for family in self._qualified_families()]}
            ),
            recovery_policy_hash=canonical_execution_hash({"recovery": "frozen-unwind-v1"}),
            evidence_hashes=challenge.evidence_hashes,
            allowed_operations=challenge.allowed_operations,
            recovery_operations=challenge.recovery_operations,
            primary_nonce=f"primary-{capability_id}",
            recovery_nonce=f"recovery-{recovery_id}",
            not_before=now,
            expires_at=now + lifetime,
            recovery_expires_at=now + lifetime + timedelta(seconds=120),
            presence_deadline=now + lifetime,
        )
        try:
            grants = self._issuer.issue(request, assertion, challenge)
        except CapabilityIssueError as error:
            raise PilotRequestError(HTTPStatus.CONFLICT, error.code) from error
        claim = PilotNonceClaim(
            schema_version=1,
            scope=NonceScope.CAPABILITY,
            nonce=request.primary_nonce,
            account_fingerprint=challenge.account_fingerprint,
            payload_hash=grants.primary.grant.digest,
            claimed_at=now,
        )
        event = PilotCapabilityEvent(
            schema_version=1,
            event_id=uuid4(),
            capability_id=capability_id,
            challenge_id=challenge.challenge_id,
            account_fingerprint=challenge.account_fingerprint,
            mode=challenge.mode,
            grant_kind=GrantKind.PRIMARY,
            event_type=CapabilityEventType.ISSUED,
            capability_digest=grants.primary.grant.digest,
            nonce=request.primary_nonce,
            reason=None,
            expires_at=request.expires_at,
            occurred_at=now,
        )
        # The nonce claim and its capability event commit together: an issued capability always
        # has a durable claim behind it.
        self._store.claim_pilot_nonce(claim, capability_event=event)
        self._state.grants[capability_id] = grants
        return grants

    def _run(self, challenge: AuthorizationChallenge, grants: IssuedGrantPair) -> dict[str, Any]:
        if challenge.mode is AuthorizationMode.AUTOMATION_SESSION:
            raise PilotRequestError(HTTPStatus.CONFLICT, "AUTOMATION_NOT_ACTIVATED")
        factory = self._environment.executor_factory
        if factory is None:
            return {
                "capability_id": str(grants.primary.grant.capability_id),
                "executed": False,
                "reason": "EXECUTION_UNAVAILABLE",
            }
        now = self._clock()
        opportunity = next(
            (
                item
                for item in self._ranked_opportunities(now)
                if item.proof_id == challenge.target_id
            ),
            None,
        )
        if opportunity is None:
            raise PilotRequestError(HTTPStatus.CONFLICT, "OPPORTUNITY_NOT_ELIGIBLE")
        limits = (
            COMPILED_PILOT_CEILINGS
            if self._state.policy is None
            else self._state.policy.requested_limits
        )
        try:
            plan = compile_frozen_pilot_plan(
                opportunity,
                limits,
                self._environment.account_state(),
                deadline=now + timedelta(seconds=30),
            )
        except PilotSelectionError as error:
            raise PilotRequestError(HTTPStatus.CONFLICT, error.code) from error
        verified_grants, signer_proofs, evidence = self._executor_inputs(grants, now)
        executor = factory(verified_grants, signer_proofs, evidence)
        result = (
            executor.execute_exact_order(plan, grants)
            if challenge.mode is AuthorizationMode.EXACT_ORDER
            else executor.execute_complete_strategy(plan, grants)
        )
        self._state.last_result = result
        self._persist_session(challenge, result, now)
        if result.stop_reason is not None:
            self._engage_kill(result.stop_reason, now)
        return {
            "capability_id": str(grants.primary.grant.capability_id),
            "executed": True,
            "state": result.state,
            "stop_reason": result.stop_reason,
            "plan_hash": result.plan_hash,
        }

    def _executor_inputs(
        self,
        grants: IssuedGrantPair,
        now: datetime,
    ) -> tuple[
        Mapping[UUID, VerifiedExecutionCapability],
        Mapping[UUID, SignerCapabilityProof],
        Callable[[], ExecutionEvidence],
    ]:
        """Build one executor's authority from this action's two ephemeral grants only."""

        verified_grants: dict[UUID, VerifiedExecutionCapability] = {}
        signer_proofs: dict[UUID, SignerCapabilityProof] = {}
        for signed in (grants.primary, grants.recovery):
            verified = self._verifier.verify(capability=signed, now=now)
            if not isinstance(verified, VerifiedExecutionCapability):
                raise PilotRequestError(
                    HTTPStatus.FORBIDDEN,
                    str(verified.reason or "CAPABILITY_INVALID"),
                )
            capability_id = signed.grant.capability_id
            verified_grants[capability_id] = verified
            signer_proofs[capability_id] = SignerCapabilityProof(
                grant=signed.grant,
                signature=b64encode(signed.signature),
            )
        return verified_grants, signer_proofs, self._execution_evidence

    def _execution_evidence(self) -> ExecutionEvidence:
        """Read the current fail-closed parent evidence for one mutation boundary check."""

        now = self._clock()
        environment = self._environment
        reconciliation = environment.reconciliation
        reconciliation_age = now - reconciliation.observed_at
        reconciliation_fresh = (
            -CHALLENGE_LIFETIME <= reconciliation_age <= CHALLENGE_LIFETIME
        )

        account: PilotAccountState | None
        try:
            account = environment.account_state()
        except Exception:
            account = None
        account_fresh = (
            account is not None
            and -MAXIMUM_EQUITY_EVIDENCE_AGE
            <= now - account.observed_at
            <= MAXIMUM_EQUITY_EVIDENCE_AGE
        )
        account_matches = (
            account is not None and account.account_fingerprint == environment.account_fingerprint
        )

        operator_present = False
        try:
            presence = self._presence.evaluate(now)
            if presence.state is PresenceState.PRESENT:
                presence = self._presence.record_native_state(now)
            operator_present = presence.state is PresenceState.PRESENT
        except Exception:
            pass

        manifest = environment.manifest
        activation_inputs = environment.activation_inputs
        geoblock_allowed = (
            activation_inputs.geoblock_allowed
            if activation_inputs is not None
            else (
                manifest is not None
                and manifest.jurisdiction_review_status == "ELIGIBILITY_REVIEWED"
            )
        )
        reconciliation_expires_at = reconciliation.observed_at + CHALLENGE_LIFETIME
        account_expires_at = (
            reconciliation_expires_at
            if account is None
            else account.observed_at + MAXIMUM_EQUITY_EVIDENCE_AGE
        )
        return ExecutionEvidence(
            manifest=manifest,
            geoblock_allowed=geoblock_allowed,
            geoblock_evidence_hash=reconciliation.reconciliation_hash,
            geoblock_expires_at=environment.eligibility_expires_at,
            account_scope_evidence_hash=reconciliation.reconciliation_hash,
            account_scope_expires_at=min(reconciliation_expires_at, account_expires_at),
            kill_engaged=(
                self._state.kill_engaged
                or not reconciliation.reconciliation_complete
                or not reconciliation_fresh
                or not account_fresh
                or not account_matches
                or (account is not None and account.kill_engaged)
            ),
            operator_present=operator_present,
        )

    def _persist_session(
        self, challenge: AuthorizationChallenge, result: ExecutionResult, now: datetime
    ) -> None:
        terminal = result.state != "COMPLETED"
        session = PilotExecutionSession(
            schema_version=1,
            event_id=uuid4(),
            session_id=challenge.challenge_id,
            sequence_number=self._state.session_sequence,
            account_fingerprint=challenge.account_fingerprint,
            wallet_fingerprint=challenge.wallet_fingerprint,
            mode=challenge.mode,
            capability_id=challenge.challenge_id,
            policy_id=challenge.policy_id,
            effective_limits=COMPILED_PILOT_CEILINGS
            if self._state.policy is None
            else self._state.policy.requested_limits,
            state=PilotSessionState.STOPPED if terminal else PilotSessionState.ACTIVE,
            loss_state=PilotLossState(
                status=LossStatus.UNKNOWN,
                session_start_equity=None,
                realized_loss=None,
                unrealized_loss=None,
                evidence_hashes=(self._environment.reconciliation.reconciliation_hash,),
                evaluated_at=now,
            ),
            presence_state=self._presence.state,
            strategies_started=1,
            deployed_capital=result.deployed_capital_usd,
            result=PilotSessionResult.FAILED if terminal else None,
            started_at=now,
            expires_at=now + timedelta(minutes=5),
            occurred_at=now,
            ended_at=now if terminal else None,
        )
        self._store.append_pilot_execution_session(session)
        self._state.session_sequence += 1

    def _persist_presence(
        self, event_type: PresenceEventType, detail: str | None, now: datetime
    ) -> None:
        self._store.append_pilot_presence_event(
            PilotPresenceEvent(
                schema_version=1,
                event_id=uuid4(),
                session_id=None,
                account_fingerprint=self._environment.account_fingerprint,
                event_type=event_type,
                monotonic_gap_ms=None,
                detail_code=detail,
                occurred_at=now,
            )
        )

    def _engage_kill(self, reason: str, now: datetime) -> None:
        del now
        for grants in self._state.grants.values():
            self._verifier.revoke(grants.primary.grant.capability_id)
        self._state.kill_engaged = True
        self._state.kill_reason = reason

    def _authority_expiry(self) -> datetime | None:
        expiries = [grants.primary.grant.expires_at for grants in self._state.grants.values()]
        return max(expiries) if expiries else None

    def _latest_mode(self) -> str | None:
        modes = [grants.primary.grant.mode.value for grants in self._state.grants.values()]
        return modes[-1] if modes else None

    def _evidence_hashes(self) -> tuple[Sha256, ...]:
        hashes = {self._environment.reconciliation.reconciliation_hash}
        for report in self._environment.qualifications:
            hashes.update(report.evidence_hashes)
        return tuple(sorted(hashes))


def _leg_views(result: ExecutionResult) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for kind, outcomes in (("normal", result.submitted_legs), ("recovery", result.recovery_legs)):
        for outcome in outcomes:
            views.append(
                {
                    "kind": kind,
                    "leg_index": outcome.leg_index,
                    "state": outcome.state,
                    "side": "—",
                    "size": str(outcome.filled_size),
                    "limit_price": "—",
                    "order_type": "FAK/FOK",
                }
            )
    return views


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PilotRequestError(HTTPStatus.BAD_REQUEST, "REQUESTED_LIMITS_INVALID") from error


def _mode(value: object) -> AuthorizationMode:
    for mode in AuthorizationMode:
        if mode.value == value:
            return mode
    raise PilotRequestError(HTTPStatus.BAD_REQUEST, "MODE_INVALID")


def _uuid(value: object, code: str) -> UUID:
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise PilotRequestError(HTTPStatus.BAD_REQUEST, code) from error


def qualified_family_values(reports: Sequence[QualificationReport]) -> tuple[str, ...]:
    return tuple(sorted(report.proof_family.value for report in reports if report.qualified))


__all__ = [
    "CHALLENGE_LIFETIME",
    "CONFIRMATION_TEXTS",
    "LivePilotServices",
    "PilotEnvironment",
    "qualified_family_values",
]
