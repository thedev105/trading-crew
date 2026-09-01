from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.models import (
    ExecutionOperation,
    ImmediateOrderType,
    canonical_execution_hash,
)
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.pilot.activation import PilotReconciliationState
from polytrading.predictions.pilot.capabilities import (
    CapabilityRequest,
    PilotCapabilityIssuer,
    VenueBinding,
)
from polytrading.predictions.pilot.execution_port import ExecutionEvidence, GeoblockEvidence
from polytrading.predictions.pilot.models import (
    PILOT_CEILING_HASH,
    PILOT_CEILINGS,
    AuthorizationChallenge,
    AuthorizationMode,
    PresenceState,
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
from polytrading.predictions.pilot.server import PilotRequestError
from polytrading.predictions.pilot.services import LivePilotServices, PilotEnvironment
from polytrading.predictions.pilot.sessions import (
    RECOVERY_GRACE,
    ExecutionResult,
    LegOutcome,
    PilotExecutionError,
    PilotExecutor,
)
from polytrading.predictions.pilot.verifier import PilotCapabilityVerifier
from polytrading.predictions.polymarket_execution.ipc import SignerCapabilityProof
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.manifest_helpers import venue_manifest
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
        self.revoke_primary()
        self.killed.append(reason)
        self.events.append(f"kill:{reason}")


def executor(port: FakeExecutionPort, **overrides: Any) -> PilotExecutor:
    return PilotExecutor(port, clock=lambda: NOW, **overrides)


def test_live_service_refuses_automation_before_challenge_persistence(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "automation-refusal.duckdb")
    passkeys = FakePasskeyService(port=PORT)
    passkeys.registration_options(account_fingerprint=ACCOUNT_FINGERPRINT, wallet_unlocked=True)
    passkeys.complete_registration(
        credential={"id": CREDENTIAL_ID, "public_key": "cHVibGlj", "sign_count": 0},
        account_fingerprint=ACCOUNT_FINGERPRINT,
        wallet_fingerprint=WALLET_FINGERPRINT,
        registered_at=NOW,
    )
    services = LivePilotServices(
        store=store,
        environment=SimpleNamespace(
            account_fingerprint=ACCOUNT_FINGERPRINT,
            wallet_fingerprint=WALLET_FINGERPRINT,
            reconciliation=SimpleNamespace(reconciliation_hash="8" * 64),
            qualifications=(),
        ),  # type: ignore[arg-type]
        passkeys=passkeys,
        issuer=object(),  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
        presence=object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(PilotRequestError, match="AUTOMATION_NOT_ACTIVATED"):
            services.auth_options(
                {
                    "mode": "AUTOMATION_SESSION",
                    "browser_session_hash": BROWSER_SESSION_HASH,
                }
            )

        assert store.verified_pilot_authorization_challenges(ACCOUNT_FINGERPRINT) == ()
    finally:
        store.close()


class _PresentMonitor:
    state = PresenceState.PRESENT

    def evaluate(self, now: datetime) -> SimpleNamespace:
        return SimpleNamespace(state=self.state, observed_at=now)

    def record_native_state(self, now: datetime) -> SimpleNamespace:
        return SimpleNamespace(state=self.state, observed_at=now)


def test_executor_inputs_contain_only_freshly_issued_proofs_and_live_evidence() -> None:
    pair = grants()
    account_state = PilotAccountState.model_validate(
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
    reconciliation = PilotReconciliationState(
        account_fingerprint=ACCOUNT_FINGERPRINT,
        active_submissions=0,
        unknown_outcomes=0,
        reconciliation_complete=True,
        unexplained_difference_usd=Decimal("0"),
        reconciliation_hash="8" * 64,
        observed_at=NOW,
    )
    manifest = venue_manifest(
        venue=PredictionVenue.POLYMARKET,
        implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
        jurisdiction_review_status="ELIGIBILITY_REVIEWED",
        source_hashes=(EVIDENCE_HASH,),
        reviewed_at=NOW - timedelta(days=1),
    )
    provider_state: dict[str, Any] = {
        "manifest": manifest,
        "reconciliation": reconciliation,
    }
    provider_calls = {"manifest": 0, "reconciliation": 0}
    available = True

    def current_account() -> PilotAccountState:
        if not available:
            raise PilotRequestError(HTTPStatus.CONFLICT, "ACCOUNT_UNAVAILABLE")
        return account_state

    def current_manifest() -> Any:
        provider_calls["manifest"] += 1
        current = provider_state["manifest"]
        if isinstance(current, Exception):
            raise current
        return current

    def current_reconciliation() -> Any:
        provider_calls["reconciliation"] += 1
        current = provider_state["reconciliation"]
        if isinstance(current, Exception):
            raise current
        return current

    environment = PilotEnvironment(
        account_fingerprint=ACCOUNT_FINGERPRINT,
        wallet_fingerprint=WALLET_FINGERPRINT,
        venue_binding=VenueBinding.model_validate(
            venue_binding_fields(
                manifest_record_hash=canonical_execution_hash(manifest),
                manifest_source_hashes=manifest.source_hashes,
            ),
            strict=True,
        ),
        manifest=manifest,
        manifest_state="LIVE_ELIGIBLE",
        protocol_state="CURRENT",
        qualifications=(),
        eligibility_expires_at=NOW + timedelta(minutes=1),
        credentials_present=True,
        reconciliation=reconciliation,
        account_state=current_account,
        manifest_provider=current_manifest,
        reconciliation_provider=current_reconciliation,
        geoblock_provider=lambda: GeoblockEvidence(
            allowed=True,
            evidence_hash="9" * 64,
            expires_at=NOW + timedelta(minutes=1),
        ),
    )
    services = LivePilotServices(
        store=object(),  # type: ignore[arg-type]
        environment=environment,
        passkeys=object(),  # type: ignore[arg-type]
        issuer=object(),  # type: ignore[arg-type]
        verifier=PilotCapabilityVerifier(pair.primary.public_verification_key),
        presence=_PresentMonitor(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    services._state.kill_engaged = False

    verified, proofs, evidence = services._executor_inputs(pair, NOW)

    expected_ids = {
        pair.primary.grant.capability_id,
        pair.recovery.grant.capability_id,
    }
    assert set(verified) == expected_ids
    assert set(proofs) == expected_ids
    assert all(isinstance(proof, SignerCapabilityProof) for proof in proofs.values())
    assert proofs[pair.primary.grant.capability_id].grant == pair.primary.grant
    fresh = evidence()
    assert isinstance(fresh, ExecutionEvidence)
    assert fresh.account == account_state
    assert fresh.reconciliation_hash == "8" * 64
    assert fresh.reconciliation_observed_at == NOW
    assert fresh.geoblock_allowed is True
    assert fresh.geoblock_evidence_hash == "9" * 64
    assert fresh.geoblock_expires_at == NOW + timedelta(minutes=1)
    assert fresh.kill_engaged is False
    assert provider_calls == {"manifest": 1, "reconciliation": 1}

    provider_state["reconciliation"] = reconciliation.model_copy(
        update={"observed_at": NOW - timedelta(minutes=5, microseconds=1)}
    )
    assert evidence().kill_engaged is True

    provider_state["reconciliation"] = RuntimeError("reconciliation provider failed")
    assert evidence().kill_engaged is True

    provider_state["reconciliation"] = None
    assert evidence().kill_engaged is True

    provider_state["reconciliation"] = reconciliation
    provider_state["manifest"] = None
    assert evidence().kill_engaged is True

    provider_state["manifest"] = RuntimeError("manifest provider failed")
    assert evidence().kill_engaged is True

    provider_state["manifest"] = manifest
    fallback_services = LivePilotServices(
        store=object(),  # type: ignore[arg-type]
        environment=replace(
            environment,
            manifest_provider=None,
            reconciliation_provider=None,
        ),
        passkeys=object(),  # type: ignore[arg-type]
        issuer=object(),  # type: ignore[arg-type]
        verifier=PilotCapabilityVerifier(pair.primary.public_verification_key),
        presence=_PresentMonitor(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    fallback_services._state.kill_engaged = False
    assert fallback_services._execution_evidence().kill_engaged is False

    unavailable_geoblock_services = LivePilotServices(
        store=object(),  # type: ignore[arg-type]
        environment=replace(
            environment,
            geoblock_provider=lambda: (_ for _ in ()).throw(
                RuntimeError("secret geoblock detail")
            ),
        ),
        passkeys=object(),  # type: ignore[arg-type]
        issuer=object(),  # type: ignore[arg-type]
        verifier=PilotCapabilityVerifier(pair.primary.public_verification_key),
        presence=_PresentMonitor(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    unavailable_geoblock_services._state.kill_engaged = False
    missing_geoblock = unavailable_geoblock_services._execution_evidence()
    assert missing_geoblock.geoblock_allowed is None
    assert missing_geoblock.geoblock_evidence_hash is None
    assert missing_geoblock.geoblock_expires_at is None
    assert missing_geoblock.kill_engaged is True

    available = False
    unavailable_account = evidence()
    assert unavailable_account.account is None
    assert unavailable_account.kill_engaged is True
    assert environment.manifest is manifest
    assert environment.reconciliation is reconciliation


def test_live_service_refuses_automation_before_executor_construction() -> None:
    pair = grants(AuthorizationMode.AUTOMATION_SESSION)
    challenge = AuthorizationChallenge.model_validate(
        challenge_fields(
            credential_id_hash=CREDENTIAL_ID_HASH,
            mode=AuthorizationMode.AUTOMATION_SESSION,
        ),
        strict=True,
    )
    factory_calls = 0

    def factory(*args: object) -> PilotExecutor:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("automation reached executor construction")

    services = LivePilotServices(
        store=object(),  # type: ignore[arg-type]
        environment=SimpleNamespace(executor_factory=factory),  # type: ignore[arg-type]
        passkeys=object(),  # type: ignore[arg-type]
        issuer=object(),  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
        presence=object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    with pytest.raises(PilotRequestError, match="AUTOMATION_NOT_ACTIVATED"):
        services._run(challenge, pair, plan=plan(), now=NOW)

    assert factory_calls == 0


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
