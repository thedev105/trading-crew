"""The bridge from one approved pilot plan to the signer that actually mutates the venue.

The executor decides *whether* a leg may go; this port is what carries it. Every intent is
persisted before it is signed or submitted, every submission is verified against the pilot grant
at the authority boundary first, and every result comes back from an authoritative read rather
than from the submitting call's own optimism.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid5

from polytrading.predictions.domain import PredictionVenue, Sha256
from polytrading.predictions.execution.authority import (
    AuthorityDecision,
    VerifiedExecutionCapability,
    verify_mutation_authority,
)
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ExecutionOperation,
    ImmediateOrderType,
    deterministic_intent_id,
)
from polytrading.predictions.manifest import VenueManifest
from polytrading.predictions.pilot.selector import (
    FrozenPilotPlan,
    PilotAccountState,
    PilotLeg,
)
from polytrading.predictions.pilot.sessions import LegOutcome
from polytrading.predictions.pilot.verifier import (
    PilotCapabilityVerifier,
    build_authority_context,
)
from polytrading.predictions.polymarket_execution.ipc import SanitizedOperationResult
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
)
from polytrading.predictions.storage.store import PredictionMarketStore

_INTENT_PLAN_NAMESPACE = UUID("2b0f0a54-1f1d-4f8f-9a3f-6f7f8b3a51c2")


class PilotExecutionPortError(ValueError):
    """A refusal raised before anything reaches the venue."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class VenueSubmissionPort(Protocol):
    """The signer-side surface. Implementations construct transport, this module never does."""

    def submit(self, intent: ExecutionIntent, capability_id: UUID) -> LegOutcome: ...

    def cancel(self, intent: ExecutionIntent, capability_id: UUID) -> LegOutcome: ...

    def account_state(self) -> PilotAccountState: ...

    def positions(self) -> Mapping[str, Decimal]: ...

    def orders(self) -> SanitizedOperationResult: ...

    def trades(self) -> SanitizedOperationResult: ...

    def engage_kill(self, capability_ids: Iterable[UUID]) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """The fresh evidence the boundary must corroborate for every single leg."""

    manifest: VenueManifest | None
    account: PilotAccountState | None
    geoblock_allowed: bool | None
    geoblock_evidence_hash: Sha256 | None
    geoblock_expires_at: datetime | None
    account_scope_evidence_hash: Sha256 | None
    account_scope_expires_at: datetime | None
    kill_engaged: bool
    operator_present: bool


class CoordinatorExecutionPort:
    """Implements the executor's port over the store, the authority layer, and one signer."""

    def __init__(
        self,
        *,
        store: PredictionMarketStore,
        signer: VenueSubmissionPort,
        verifier: PilotCapabilityVerifier,
        grants: Mapping[UUID, VerifiedExecutionCapability],
        evidence: Callable[[], ExecutionEvidence],
        clock: Callable[[], datetime],
        on_kill: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._signer = signer
        self._verifier = verifier
        self._grants = dict(grants)
        self._evidence = evidence
        self._clock = clock
        self._on_kill = on_kill
        self._submitted: set[tuple[UUID, int, str]] = set()
        self._deployed = Decimal("0")
        self._intents: dict[tuple[UUID, int, str], ExecutionIntent] = {}

    # -- executor port ------------------------------------------------------------------

    def persist_intent(self, plan: FrozenPilotPlan, leg: PilotLeg) -> Sha256:
        """Write the intent before anything is signed, so an UNKNOWN outcome still has evidence."""

        intent = self._intent_for(plan, leg)
        self._store.append_execution_intent(intent)
        self._intents[(plan.proposal_id, leg.leg_index, leg.side)] = intent
        return intent.intent_fingerprint

    def submit(self, plan: FrozenPilotPlan, leg: PilotLeg, capability_id: UUID) -> LegOutcome:
        intent = self._intents.get((plan.proposal_id, leg.leg_index, leg.side))
        if intent is None:
            raise PilotExecutionPortError(
                "INTENT_NOT_PERSISTED", "a leg must be persisted before it is submitted"
            )
        capability = self._grants.get(capability_id)
        if capability is None:
            raise PilotExecutionPortError("GRANT_UNKNOWN", "no verified grant for this action")
        operation = (
            ExecutionOperation.CANCEL_ORDER
            if leg.side != plan.legs[0].side and capability.grant_kind == "RECOVERY"
            else ExecutionOperation.SUBMIT_ORDER
        )
        decision = self._authorize(plan, leg, capability, operation)
        if not decision.allowed:
            raise PilotExecutionPortError("AUTHORITY_REFUSED", str(decision.reason))
        # One grant may carry the frozen legs of its own plan, but never the same leg twice:
        # a resubmission is a replay, whatever the venue answered the first time.
        submission = (capability_id, leg.leg_index, leg.side)
        if submission in self._submitted:
            raise PilotExecutionPortError("GRANT_REPLAYED", "this leg was already submitted")
        self._submitted.add(submission)
        outcome = (
            self._signer.cancel(intent, capability_id)
            if operation is ExecutionOperation.CANCEL_ORDER
            else self._signer.submit(intent, capability_id)
        )
        self._deployed += outcome.notional_usd
        return outcome

    def authoritative_state(self, plan: FrozenPilotPlan) -> PilotAccountState:
        del plan
        return self._signer.account_state()

    def positions(self, plan: FrozenPilotPlan) -> Mapping[str, Decimal]:
        del plan
        return self._signer.positions()

    def revoke_primary(self) -> None:
        for capability_id, capability in self._grants.items():
            if capability.grant_kind == "PRIMARY":
                self._verifier.revoke(capability_id)

    def engage_kill(self, reason: str) -> None:
        capability_ids = tuple(sorted(self._grants, key=str))
        self.revoke_primary()
        try:
            if self._on_kill is not None:
                self._on_kill(reason)
        finally:
            # Parent revocation and kill state are authoritative even when sidecar IPC fails.
            with suppress(Exception):
                self._signer.engage_kill(capability_ids)

    # -- internals ----------------------------------------------------------------------

    def _authorize(
        self,
        plan: FrozenPilotPlan,
        leg: PilotLeg,
        capability: VerifiedExecutionCapability,
        operation: ExecutionOperation,
    ) -> AuthorityDecision:
        try:
            evidence = self._evidence()
        except Exception:
            self.engage_kill("EVIDENCE_STALE")
            return AuthorityDecision(False, "EXECUTION_KILL_ENGAGED", ())
        account = evidence.account
        if (
            evidence.kill_engaged
            or account is None
            or account.account_fingerprint != plan.account_fingerprint
            or account.kill_engaged
        ):
            self.engage_kill("EVIDENCE_STALE")
            return AuthorityDecision(False, "EXECUTION_KILL_ENGAGED", ())
        context = build_authority_context(
            capability=capability,
            manifest=evidence.manifest,
            now=self._clock(),
            account_fingerprint=plan.account_fingerprint,
            action_id=capability.parent_action_id,
            requested_notional=leg.notional,
            capital_after=self._deployed + leg.notional,
            position_after=self._deployed + leg.notional,
            loss_after=Decimal("0"),
            used_activation_nonces=frozenset(),
            revoked_capability_ids=self._verifier.revoked_capability_ids,
            geoblock_allowed=evidence.geoblock_allowed,
            geoblock_evidence_hash=evidence.geoblock_evidence_hash,
            geoblock_expires_at=evidence.geoblock_expires_at,
            account_scope_evidence_hash=evidence.account_scope_evidence_hash,
            account_scope_expires_at=evidence.account_scope_expires_at,
            kill_engaged=False,
            operator_present=evidence.operator_present,
            evidence_hashes=plan.evidence_hashes,
        )
        return verify_mutation_authority(context, operation)

    def _intent_for(self, plan: FrozenPilotPlan, leg: PilotLeg) -> ExecutionIntent:
        if leg.order_type not in (ImmediateOrderType.FAK, ImmediateOrderType.FOK):
            raise PilotExecutionPortError("ORDER_TYPE_UNSUPPORTED", "only FAK and FOK exist")
        created_at = self._clock()
        fields: dict[str, object] = {
            "schema_version": 1,
            "intent_id": UUID(int=0),
            "plan_id": uuid5(_INTENT_PLAN_NAMESPACE, str(plan.proposal_id)),
            "leg_sequence": leg.leg_index,
            "venue": PredictionVenue.POLYMARKET,
            "token_id": leg.outcome_token_id,
            "side": leg.side,
            "limit_price": leg.limit_price,
            "tick_size": Decimal("0.01"),
            "exchange_kind": "standard",
            "base_size": leg.size,
            "maximum_spend": leg.notional,
            "order_type": leg.order_type,
            "fee_rate_bps_cap": 0,
            "rounding_mode": "ROUND_DOWN",
            "account_fingerprint": plan.account_fingerprint,
            "capability_fingerprint": plan.plan_hash,
            "created_at": created_at,
            "deadline": plan.deadline,
            "protocol_version": POLYMARKET_PILOT_PROTOCOL_VERSION,
            "intent_fingerprint": "0" * 64,
        }
        draft = ExecutionIntent.model_construct(**fields)
        from polytrading.predictions.execution.models import _intent_fingerprint

        fields["intent_fingerprint"] = _intent_fingerprint(draft)
        fields["intent_id"] = deterministic_intent_id(ExecutionIntent.model_construct(**fields))
        return ExecutionIntent.model_validate(fields)


__all__ = [
    "CoordinatorExecutionPort",
    "ExecutionEvidence",
    "PilotExecutionPortError",
    "VenueSubmissionPort",
]
