from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from polytrading.predictions.execution.models import ExecutionOperation, ImmediateOrderType
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
from polytrading.predictions.pilot.selector import (
    FrozenPilotPlan,
    FrozenRecoveryBranch,
    PilotAccountState,
    PilotLeg,
)
from polytrading.predictions.pilot.sessions import (
    RECOVERY_GRACE,
    ExecutionResult,
    LegOutcome,
    PilotExecutionError,
    PilotExecutor,
)
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

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
PORT = 8788
CREDENTIAL_ID = "pilot-credential"
CREDENTIAL_ID_HASH = __import__("hashlib").sha256(CREDENTIAL_ID.encode()).hexdigest()
RECOVERY_CAPABILITY_ID = UUID("00000000-0000-0000-0000-00000000e001")


def leg(index: int, **overrides: Any) -> PilotLeg:
    fields: dict[str, Any] = {
        "leg_index": index,
        "outcome_token_id": f"token-{index}",
        "side": "buy",
        "limit_price": Decimal("0.40"),
        "size": Decimal("10"),
        "order_type": ImmediateOrderType.FAK,
    }
    fields.update(overrides)
    return PilotLeg.model_validate(fields, strict=True)


def recovery_branch(index: int) -> FrozenRecoveryBranch:
    return FrozenRecoveryBranch.model_validate(
        {
            "trigger": "LEG_INCOMPLETE",
            "leg": leg(index, side="sell", order_type=ImmediateOrderType.FOK),
            "worst_case_exposure_before_usd": Decimal("4"),
            "worst_case_exposure_after_usd": Decimal("0.50"),
        },
        strict=True,
    )


def plan(**overrides: Any) -> FrozenPilotPlan:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "proof_id": UUID("00000000-0000-0000-0000-000000006001"),
        "proposal_id": UUID("70000000-0000-0000-0000-000000000001"),
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "legs": (leg(0), leg(1)),
        "recovery_branches": (recovery_branch(0),),
        "gross_notional_usd": Decimal("12"),
        "deployed_capital_usd": Decimal("8"),
        "worst_case_incomplete_loss_usd": Decimal("0.50"),
        "effective_limits": PILOT_CEILINGS,
        "evidence_hashes": (EVIDENCE_HASH,),
        "deadline": NOW + timedelta(seconds=30),
        "information_cutoff": NOW,
    }
    fields.update(overrides)
    return FrozenPilotPlan.model_validate(fields, strict=True)


def grants(mode: AuthorizationMode = AuthorizationMode.COMPLETE_STRATEGY):
    target = AuthorizationChallenge.model_validate(
        challenge_fields(credential_id_hash=CREDENTIAL_ID_HASH, mode=mode), strict=True
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
        verified_at=target.not_before,
    )
    lifetime = (
        timedelta(minutes=5) if mode is not AuthorizationMode.EXACT_ORDER else timedelta(seconds=60)
    )
    request = CapabilityRequest.model_validate(
        {
            "schema_version": 1,
            "capability_id": CAPABILITY_ID,
            "recovery_capability_id": RECOVERY_CAPABILITY_ID,
            "challenge_id": CHALLENGE_ID,
            "mode": mode,
            "venue_binding": VenueBinding.model_validate(venue_binding_fields(), strict=True),
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
            "expires_at": NOW + lifetime,
            "recovery_expires_at": NOW + lifetime + timedelta(seconds=120),
            "presence_deadline": NOW + lifetime,
        },
        strict=True,
    )
    return PilotCapabilityIssuer(key_id="launch-1").issue(request, assertion, target)


class FakeExecutionPort:
    """Records the order of persistence, submission, and authoritative reads."""

    def __init__(
        self,
        *,
        outcomes: list[str] | None = None,
        positions: Mapping[str, Decimal] | None = None,
        account: PilotAccountState | None = None,
    ) -> None:
        self.events: list[str] = []
        self.outcomes = outcomes or []
        self._positions = positions if positions is not None else {"token-0": Decimal("10")}
        self._account = account
        self.killed: list[str] = []
        self.revoked = 0
        self.submissions: list[tuple[int, UUID]] = []

    def persist_intent(self, plan_: FrozenPilotPlan, leg_: PilotLeg) -> str:
        self.events.append(f"persist:{leg_.leg_index}:{leg_.side}")
        return "a" * 64

    def submit(self, plan_: FrozenPilotPlan, leg_: PilotLeg, capability_id: UUID) -> LegOutcome:
        self.events.append(f"submit:{leg_.leg_index}:{leg_.side}")
        self.submissions.append((leg_.leg_index, capability_id))
        state = self.outcomes.pop(0) if self.outcomes else "FILLED"
        filled = leg_.size if state in ("FILLED", "PARTIALLY_FILLED") else Decimal("0")
        return LegOutcome.model_validate(
            {
                "leg_index": leg_.leg_index,
                "state": state,
                "filled_size": filled if state != "PARTIALLY_FILLED" else filled / 2,
                "notional_usd": leg_.notional if state == "FILLED" else Decimal("0"),
                "venue_order_id": None if state == "UNKNOWN" else f"order-{leg_.leg_index}",
                "observed_at": NOW,
            },
            strict=True,
        )

    def authoritative_state(self, plan_: FrozenPilotPlan) -> PilotAccountState:
        self.events.append("read")
        if self._account is not None:
            return self._account
        return PilotAccountState.model_validate(
            {
                "account_fingerprint": ACCOUNT_FINGERPRINT,
                "wallet_fingerprint": WALLET_FINGERPRINT,
                "collateral_usd": Decimal("200"),
                "allowance_usd": Decimal("200"),
                "kill_engaged": False,
                "observed_at": NOW,
            },
            strict=True,
        )

    def positions(self, plan_: FrozenPilotPlan) -> Mapping[str, Decimal]:
        return self._positions

    def revoke_primary(self) -> None:
        self.revoked += 1
        self.events.append("revoke")

    def engage_kill(self, reason: str) -> None:
        self.killed.append(reason)
        self.events.append(f"kill:{reason}")


def executor(port: FakeExecutionPort, **overrides: Any) -> PilotExecutor:
    return PilotExecutor(port, clock=lambda: NOW, **overrides)


def test_a_complete_strategy_persists_before_every_submission() -> None:
    port = FakeExecutionPort()
    result = executor(port).execute_complete_strategy(plan(), grants())

    assert result.state == "COMPLETED"
    assert result.stop_reason is None
    assert port.events == [
        "persist:0:buy",
        "submit:0:buy",
        "read",
        "persist:1:buy",
        "submit:1:buy",
        "read",
    ]
    assert isinstance(result, ExecutionResult)


def test_an_unknown_outcome_is_never_retried_and_engages_recovery() -> None:
    port = FakeExecutionPort(outcomes=["UNKNOWN"])
    result = executor(port).execute_complete_strategy(plan(), grants())

    assert result.stop_reason == "UNKNOWN_OUTCOME"
    assert [event for event in port.events if event.startswith("submit")] == [
        "submit:0:buy",
        "submit:0:sell",
    ]
    assert port.killed == ["UNKNOWN_OUTCOME"]
    assert port.revoked == 1


def test_a_first_leg_rejection_halts_without_recovery() -> None:
    port = FakeExecutionPort(outcomes=["REJECTED"])
    result = executor(port).execute_complete_strategy(plan(), grants())

    assert result.state == "STOPPED"
    assert result.stop_reason == "LEG_REJECTED"
    assert result.recovery_legs == ()
    assert port.killed == ["LEG_REJECTED"]


def test_a_partial_fill_recovers_the_filled_leg() -> None:
    port = FakeExecutionPort(outcomes=["PARTIALLY_FILLED"])
    result = executor(port).execute_complete_strategy(plan(), grants())

    assert result.state == "RECOVERED"
    assert [outcome.leg_index for outcome in result.recovery_legs] == [0]
    assert port.submissions[-1][1] != port.submissions[0][1]


def test_recovery_runs_under_the_recovery_capability_only() -> None:
    port = FakeExecutionPort(outcomes=["UNKNOWN"])
    pair = grants()
    executor(port).execute_complete_strategy(plan(), pair)

    normal, recovery = port.submissions
    assert normal[1] == pair.primary.grant.capability_id
    assert recovery[1] == pair.recovery.grant.capability_id


def test_a_stale_authoritative_read_stops_normal_execution() -> None:
    stale = PilotAccountState.model_validate(
        {
            "account_fingerprint": ACCOUNT_FINGERPRINT,
            "wallet_fingerprint": WALLET_FINGERPRINT,
            "collateral_usd": Decimal("200"),
            "allowance_usd": Decimal("200"),
            "kill_engaged": False,
            "observed_at": NOW - timedelta(seconds=1),
        },
        strict=True,
    )
    port = FakeExecutionPort(account=stale)
    result = executor(port).execute_complete_strategy(plan(), grants())

    assert result.stop_reason == "EVIDENCE_STALE"


def test_an_account_mismatch_halts_without_recovery() -> None:
    mismatched = PilotAccountState.model_validate(
        {
            "account_fingerprint": "9" * 64,
            "wallet_fingerprint": WALLET_FINGERPRINT,
            "collateral_usd": Decimal("200"),
            "allowance_usd": Decimal("200"),
            "kill_engaged": False,
            "observed_at": NOW,
        },
        strict=True,
    )
    port = FakeExecutionPort(account=mismatched)
    result = executor(port).execute_complete_strategy(plan(), grants())

    assert result.state == "STOPPED"
    assert result.stop_reason == "ACCOUNT_MISMATCH"
    assert result.recovery_legs == ()


def test_a_kill_between_legs_stops_and_recovers() -> None:
    killed = PilotAccountState.model_validate(
        {
            "account_fingerprint": ACCOUNT_FINGERPRINT,
            "wallet_fingerprint": WALLET_FINGERPRINT,
            "collateral_usd": Decimal("200"),
            "allowance_usd": Decimal("200"),
            "kill_engaged": True,
            "observed_at": NOW,
        },
        strict=True,
    )
    port = FakeExecutionPort(account=killed)
    result = executor(port).execute_complete_strategy(plan(), grants())

    assert result.stop_reason == "PRESENCE_LOST"
    assert result.state == "RECOVERED"


def test_lost_presence_stops_normal_continuation() -> None:
    port = FakeExecutionPort()
    result = executor(port, presence=False).execute_complete_strategy(plan(), grants())

    assert result.stop_reason == "PRESENCE_LOST"


def test_an_exact_order_must_reduce_a_known_position() -> None:
    reducing = plan(
        legs=(leg(0, side="sell", size=Decimal("5"), order_type=ImmediateOrderType.FOK),),
        recovery_branches=(),
    )
    port = FakeExecutionPort()
    result = executor(port).execute_exact_order(reducing, grants(AuthorizationMode.EXACT_ORDER))

    assert result.state == "COMPLETED"
    assert [outcome.leg_index for outcome in result.submitted_legs] == [0]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"legs": (leg(0, side="buy"),)}, "LIMIT_BREACH"),
        ({"legs": (leg(0, side="sell", size=Decimal("50")),)}, "LIMIT_BREACH"),
        ({"legs": (leg(0, side="sell"), leg(1, side="sell"))}, "GRANT_INVALID"),
    ],
)
def test_an_exact_order_refuses_anything_it_is_not(overrides: dict[str, Any], code: str) -> None:
    port = FakeExecutionPort()
    with pytest.raises(PilotExecutionError) as raised:
        executor(port).execute_exact_order(
            plan(recovery_branches=(), **overrides), grants(AuthorizationMode.EXACT_ORDER)
        )
    assert raised.value.code == code
    assert port.events == []


def test_an_exact_order_with_an_unknown_position_is_refused() -> None:
    port = FakeExecutionPort(positions={})
    with pytest.raises(PilotExecutionError) as raised:
        executor(port).execute_exact_order(
            plan(legs=(leg(0, side="sell"),), recovery_branches=()),
            grants(AuthorizationMode.EXACT_ORDER),
        )
    assert raised.value.code == "UNKNOWN_OUTCOME"


def test_a_strategy_grant_cannot_run_an_exact_order() -> None:
    port = FakeExecutionPort()
    with pytest.raises(PilotExecutionError) as raised:
        executor(port).execute_exact_order(
            plan(legs=(leg(0, side="sell"),), recovery_branches=()), grants()
        )
    assert raised.value.code == "GRANT_INVALID"


def test_a_single_use_capability_is_refused_on_its_second_run() -> None:
    port = FakeExecutionPort()
    runner = executor(port)
    pair = grants()
    runner.execute_complete_strategy(plan(), pair)

    with pytest.raises(PilotExecutionError) as raised:
        runner.execute_complete_strategy(plan(), pair)
    assert raised.value.code == "GRANT_INVALID"


def test_a_leg_above_the_order_ceiling_never_reaches_the_venue() -> None:
    port = FakeExecutionPort()
    with pytest.raises(PilotExecutionError) as raised:
        executor(port).execute_complete_strategy(
            plan(legs=(leg(0, size=Decimal("30")),), recovery_branches=()), grants()
        )
    assert raised.value.code == "LIMIT_BREACH"
    assert port.events == []


def test_stopping_revokes_primary_and_leaves_a_bounded_recovery_window() -> None:
    port = FakeExecutionPort()
    result = executor(port).stop("OPERATOR_STOP", now=NOW)

    assert result.primary_revoked is True
    assert result.kill_engaged is True
    assert result.recovery_deadline == NOW + RECOVERY_GRACE
    assert port.killed == ["OPERATOR_STOP"]
    assert port.revoked == 1
