"""Bounded execution of one frozen pilot plan, leg by leg, under a primary grant.

Every leg is persisted before it is submitted, submitted exactly once, and followed by a fresh
authoritative read; only then does the executor decide to continue, recover, or halt. An unknown
outcome is never retried: it stops normal execution and leaves only the frozen risk-reducing
recovery branch, which itself may never deploy new capital.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator

from polytrading.predictions.domain import Sha256
from polytrading.predictions.execution.models import ImmediateOrderType
from polytrading.predictions.pilot.capabilities import IssuedGrantPair
from polytrading.predictions.pilot.models import (
    AuthorizationMode,
    GrantKind,
    PilotRecord,
    UtcTimestamp,
)
from polytrading.predictions.pilot.policy import (
    PilotPolicyError,
    require_order_within_budget,
)
from polytrading.predictions.pilot.selector import (
    FrozenPilotPlan,
    PilotAccountState,
    PilotLeg,
)

# Spec section 4.5: recovery authority survives a kill for at most this long.
RECOVERY_GRACE = timedelta(seconds=120)

StopReason = Literal[
    "OPERATOR_STOP",
    "PRESENCE_LOST",
    "UNKNOWN_OUTCOME",
    "LEG_REJECTED",
    "LIMIT_BREACH",
    "EVIDENCE_STALE",
    "ACCOUNT_MISMATCH",
    "GRANT_INVALID",
]
LegState = Literal["FILLED", "PARTIALLY_FILLED", "REJECTED", "UNKNOWN"]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class PilotExecutionError(ValueError):
    """An execution the pilot refuses before anything reaches the venue."""

    def __init__(self, code: StopReason | str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class LegOutcome(PilotRecord):
    """One authoritative venue result for exactly one submitted leg."""

    leg_index: Annotated[int, Field(ge=0)]
    state: LegState
    filled_size: NonNegativeDecimal
    notional_usd: NonNegativeDecimal
    venue_order_id: str | None
    observed_at: UtcTimestamp


class ContinuationDecision(PilotRecord):
    action: Literal["CONTINUE", "RECOVER", "HALT"]
    reason: StopReason | None

    @model_validator(mode="after")
    def _reason_matches_action(self) -> ContinuationDecision:
        if (self.action == "CONTINUE") == (self.reason is not None):
            raise ValueError("only a non-continuing decision names a stop reason")
        return self


class ExecutionResult(PilotRecord):
    state: Literal["COMPLETED", "RECOVERED", "STOPPED"]
    submitted_legs: tuple[LegOutcome, ...]
    recovery_legs: tuple[LegOutcome, ...]
    deployed_capital_usd: NonNegativeDecimal
    gross_notional_usd: NonNegativeDecimal
    stop_reason: StopReason | None
    plan_hash: Sha256

    @model_validator(mode="after")
    def _reason_matches_state(self) -> ExecutionResult:
        if (self.state == "COMPLETED") == (self.stop_reason is not None):
            raise ValueError("only an interrupted execution names a stop reason")
        return self


class StopResult(PilotRecord):
    reason: StopReason
    primary_revoked: Literal[True]
    kill_engaged: Literal[True]
    recovery_deadline: UtcTimestamp


class ExecutionPort(Protocol):
    """The coordinator/signer boundary. Every method is authoritative, never optimistic."""

    def persist_intent(self, plan: FrozenPilotPlan, leg: PilotLeg) -> Sha256: ...

    def submit(self, plan: FrozenPilotPlan, leg: PilotLeg, capability_id: UUID) -> LegOutcome: ...

    def authoritative_state(self, plan: FrozenPilotPlan) -> PilotAccountState: ...

    def positions(self, plan: FrozenPilotPlan) -> Mapping[str, Decimal]: ...

    def engage_kill(self, reason: StopReason) -> None: ...


class PilotExecutor:
    """Runs one frozen plan under one grant pair, and nothing else."""

    def __init__(
        self,
        port: ExecutionPort,
        *,
        clock: object,
        presence: object | None = None,
    ) -> None:
        self._port = port
        self._clock = clock
        self._presence = presence
        self._used_capabilities: set[UUID] = set()

    def execute_exact_order(
        self, plan: FrozenPilotPlan, grants: IssuedGrantPair
    ) -> ExecutionResult:
        """Authorize exactly one precomputed order that reduces an existing known exposure."""

        self._require_grant(grants, AuthorizationMode.EXACT_ORDER)
        if len(plan.legs) != 1:
            raise PilotExecutionError("GRANT_INVALID", "an exact order authorizes exactly one leg")
        self._require_risk_reducing_exact_order(plan)
        return self._run_frozen_plan(plan, grants, maximum_legs=1)

    def execute_complete_strategy(
        self, plan: FrozenPilotPlan, grants: IssuedGrantPair
    ) -> ExecutionResult:
        self._require_grant(grants, AuthorizationMode.COMPLETE_STRATEGY)
        return self._run_frozen_plan(plan, grants, maximum_legs=len(plan.legs))

    def stop(self, reason: StopReason, *, now: datetime) -> StopResult:
        """Destroy primary authority and engage kill; recovery lives on for 120 seconds."""

        self._port.engage_kill(reason)
        return StopResult(
            reason=reason,
            primary_revoked=True,
            kill_engaged=True,
            recovery_deadline=now + RECOVERY_GRACE,
        )

    def _require_grant(self, grants: IssuedGrantPair, mode: AuthorizationMode) -> None:
        primary = grants.primary.grant
        recovery = grants.recovery.grant
        if primary.grant_kind is not GrantKind.PRIMARY:
            raise PilotExecutionError("GRANT_INVALID", "the primary grant is not a primary grant")
        if recovery.grant_kind is not GrantKind.RECOVERY:
            raise PilotExecutionError("GRANT_INVALID", "the recovery grant is not a recovery grant")
        if primary.mode is not mode:
            raise PilotExecutionError(
                "GRANT_INVALID", f"{primary.mode.value} cannot run a {mode.value}"
            )
        if primary.single_use and primary.capability_id in self._used_capabilities:
            raise PilotExecutionError("GRANT_INVALID", "this capability was already used")
        self._used_capabilities.add(primary.capability_id)

    def _require_risk_reducing_exact_order(self, plan: FrozenPilotPlan) -> None:
        leg = plan.legs[0]
        if leg.order_type not in (ImmediateOrderType.FAK, ImmediateOrderType.FOK):
            raise PilotExecutionError("GRANT_INVALID", "only FAK and FOK orders exist here")
        positions = self._port.positions(plan)
        held = positions.get(leg.outcome_token_id)
        if held is None:
            raise PilotExecutionError("UNKNOWN_OUTCOME", "the current position is unknown")
        if leg.side != "sell" or leg.size > held:
            raise PilotExecutionError(
                "LIMIT_BREACH", "an exact order may only reduce a known position"
            )

    def _run_frozen_plan(
        self, plan: FrozenPilotPlan, grants: IssuedGrantPair, *, maximum_legs: int
    ) -> ExecutionResult:
        submitted: list[LegOutcome] = []
        recovery: list[LegOutcome] = []
        committed = Decimal("0")
        deployed = Decimal("0")
        stop_reason: StopReason | None = None

        for leg in plan.legs[:maximum_legs]:
            try:
                require_order_within_budget(
                    plan.effective_limits,
                    committed_gross_notional=committed,
                    order_notional=leg.notional,
                    deployed_capital=deployed,
                    recovery=False,
                    additional_deployed_capital=leg.notional,
                )
            except PilotPolicyError as error:
                stop_reason = "LIMIT_BREACH"
                raise PilotExecutionError("LIMIT_BREACH", error.code) from error

            # Persist before signing or submitting: an unknown outcome must still have evidence.
            self._port.persist_intent(plan, leg)
            outcome = self._port.submit(plan, leg, grants.primary.grant.capability_id)
            submitted.append(outcome)
            committed += leg.notional
            deployed += outcome.notional_usd

            decision = self._continuation(plan, outcome, deployed)
            if decision.action == "CONTINUE":
                continue
            stop_reason = decision.reason
            if decision.action == "RECOVER":
                recovery.extend(self._recover(plan, grants, submitted))
            break

        state: Literal["COMPLETED", "RECOVERED", "STOPPED"]
        if stop_reason is None:
            state = "COMPLETED"
        elif recovery:
            state = "RECOVERED"
        else:
            state = "STOPPED"
        if stop_reason is not None:
            self._port.engage_kill(stop_reason)
        return ExecutionResult(
            state=state,
            submitted_legs=tuple(submitted),
            recovery_legs=tuple(recovery),
            deployed_capital_usd=deployed,
            gross_notional_usd=committed
            + sum((outcome.notional_usd for outcome in recovery), Decimal("0")),
            stop_reason=stop_reason,
            plan_hash=plan.plan_hash,
        )

    def _continuation(
        self, plan: FrozenPilotPlan, outcome: LegOutcome, deployed: Decimal
    ) -> ContinuationDecision:
        """Re-derive the decision from authoritative state after every single leg."""

        if outcome.state == "UNKNOWN":
            # Never retried: the venue result is ambiguous, so normal execution ends here.
            return ContinuationDecision(action="RECOVER", reason="UNKNOWN_OUTCOME")
        if outcome.state == "REJECTED":
            action = "RECOVER" if outcome.leg_index > 0 else "HALT"
            return ContinuationDecision(action=action, reason="LEG_REJECTED")
        if outcome.state == "PARTIALLY_FILLED":
            return ContinuationDecision(action="RECOVER", reason="LEG_REJECTED")
        account = self._port.authoritative_state(plan)
        if account.kill_engaged:
            return ContinuationDecision(action="RECOVER", reason="PRESENCE_LOST")
        if account.account_fingerprint != plan.account_fingerprint:
            return ContinuationDecision(action="HALT", reason="ACCOUNT_MISMATCH")
        if account.observed_at < outcome.observed_at:
            return ContinuationDecision(action="RECOVER", reason="EVIDENCE_STALE")
        if deployed > plan.effective_limits.session_deployed_capital:
            return ContinuationDecision(action="RECOVER", reason="LIMIT_BREACH")
        if self._presence is not None and not bool(self._presence):
            return ContinuationDecision(action="RECOVER", reason="PRESENCE_LOST")
        return ContinuationDecision(action="CONTINUE", reason=None)

    def _recover(
        self,
        plan: FrozenPilotPlan,
        grants: IssuedGrantPair,
        submitted: list[LegOutcome],
    ) -> list[LegOutcome]:
        """Run only the frozen branches for legs that actually left exposure behind."""

        # An UNKNOWN leg is treated as filled: recovery exists precisely for exposure that may
        # have been created without a confirmed result.
        filled = {
            outcome.leg_index
            for outcome in submitted
            if outcome.state in ("FILLED", "PARTIALLY_FILLED", "UNKNOWN")
        }
        outcomes: list[LegOutcome] = []
        for branch in plan.recovery_branches:
            if branch.leg.leg_index not in filled:
                continue
            require_order_within_budget(
                plan.effective_limits,
                committed_gross_notional=sum(
                    (outcome.notional_usd for outcome in submitted), Decimal("0")
                ),
                order_notional=branch.leg.notional,
                deployed_capital=plan.deployed_capital_usd,
                recovery=True,
            )
            self._port.persist_intent(plan, branch.leg)
            outcomes.append(
                self._port.submit(plan, branch.leg, grants.recovery.grant.capability_id)
            )
        return outcomes


class SessionDecision(PilotRecord):
    """One tick's verdict, always derived from persisted and authoritative state."""

    action: Literal["START_STRATEGY", "WAIT", "STOP"]
    reason: StopReason | Literal["SESSION_EXPIRED", "FINAL_MINUTE", "STRATEGY_ACTIVE"] | None
    strategies_started: Annotated[int, Field(ge=0)]
    deployed_capital_usd: NonNegativeDecimal
    observed_at: UtcTimestamp

    @model_validator(mode="after")
    def _reason_matches_action(self) -> SessionDecision:
        if (self.action == "START_STRATEGY") == (self.reason is not None):
            raise ValueError("only a non-starting decision names a reason")
        return self


class AutomationSessionRunner:
    """Runs one 15-minute automation session, one strategy at a time, prompting only once."""

    # No new strategy may start in the session's final minute (spec section 7.2).
    FINAL_MINUTE = timedelta(seconds=60)

    def __init__(
        self,
        *,
        started_at: datetime,
        limits: object,
        state_reader: object,
    ) -> None:
        self._started_at = started_at
        self._limits = limits
        self._state_reader = state_reader
        self._expires_at = started_at + limits.session_duration  # type: ignore[attr-defined]
        self._stopped = False

    @property
    def expires_at(self) -> datetime:
        return self._expires_at

    def tick(self, now: datetime) -> SessionDecision:
        """Read authoritative state and decide; the browser never supplies a counter."""

        state = self._state_reader(now)  # type: ignore[operator]
        if self._stopped or state.kill_engaged:
            return self._decision("STOP", "PRESENCE_LOST", state, now)
        if not state.presence_present:
            return self._decision("STOP", "PRESENCE_LOST", state, now)
        if state.loss_unknown:
            return self._decision("STOP", "UNKNOWN_OUTCOME", state, now)
        if state.session_loss_usd > self._limits.session_loss:  # type: ignore[attr-defined]
            return self._decision("STOP", "LIMIT_BREACH", state, now)
        if state.utc_day_loss_usd > self._limits.utc_day_loss:  # type: ignore[attr-defined]
            return self._decision("STOP", "LIMIT_BREACH", state, now)
        if now >= self._expires_at:
            return self._decision("STOP", "SESSION_EXPIRED", state, now)
        if state.active_strategies >= self._limits.concurrent_strategies:  # type: ignore[attr-defined]
            return self._decision("WAIT", "STRATEGY_ACTIVE", state, now)
        if now > self._expires_at - self.FINAL_MINUTE:
            return self._decision("WAIT", "FINAL_MINUTE", state, now)
        deployable = (
            self._limits.session_deployed_capital - state.deployed_capital_usd  # type: ignore[attr-defined]
        )
        if deployable <= 0:
            return self._decision("WAIT", "LIMIT_BREACH", state, now)
        return self._decision("START_STRATEGY", None, state, now)

    def stop(self, reason: StopReason, now: datetime) -> SessionDecision:
        self._stopped = True
        state = self._state_reader(now)  # type: ignore[operator]
        return self._decision("STOP", reason, state, now)

    def _decision(
        self,
        action: Literal["START_STRATEGY", "WAIT", "STOP"],
        reason: object,
        state: object,
        now: datetime,
    ) -> SessionDecision:
        return SessionDecision(
            action=action,
            reason=reason,  # type: ignore[arg-type]
            strategies_started=state.strategies_started,  # type: ignore[attr-defined]
            deployed_capital_usd=state.deployed_capital_usd,  # type: ignore[attr-defined]
            observed_at=now,
        )


__all__ = [
    "RECOVERY_GRACE",
    "AutomationSessionRunner",
    "ContinuationDecision",
    "ExecutionPort",
    "ExecutionResult",
    "LegOutcome",
    "PilotExecutionError",
    "PilotExecutor",
    "SessionDecision",
    "StopReason",
    "StopResult",
]
