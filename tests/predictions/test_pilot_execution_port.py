from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ImmediateOrderType,
    canonical_execution_hash,
)
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.pilot.execution_port import (
    CoordinatorExecutionPort,
    ExecutionEvidence,
    PilotExecutionPortError,
)
from polytrading.predictions.pilot.models import PILOT_CEILINGS
from polytrading.predictions.pilot.selector import (
    FrozenPilotPlan,
    FrozenRecoveryBranch,
    PilotAccountState,
    PilotLeg,
)
from polytrading.predictions.pilot.sessions import LegOutcome, PilotExecutor
from polytrading.predictions.pilot.verifier import (
    PilotCapabilityVerifier,
    verified_capability_from_grant,
)
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.manifest_helpers import venue_manifest
from tests.predictions.pilot_helpers import ACCOUNT_FINGERPRINT, EVIDENCE_HASH, WALLET_FINGERPRINT
from tests.predictions.test_pilot_verifier import NOW, issued

ELIGIBLE_MANIFEST = venue_manifest(
    venue=PredictionVenue.POLYMARKET,
    implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
    jurisdiction_review_status="ELIGIBILITY_REVIEWED",
    source_hashes=(EVIDENCE_HASH,),
    reviewed_at=NOW - timedelta(days=1),
)


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


def plan(**overrides: Any) -> FrozenPilotPlan:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "proof_id": UUID("00000000-0000-0000-0000-000000006001"),
        "proposal_id": UUID("70000000-0000-0000-0000-000000000001"),
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "legs": (leg(0), leg(1)),
        "recovery_branches": (
            FrozenRecoveryBranch.model_validate(
                {
                    "trigger": "LEG_INCOMPLETE",
                    "leg": leg(0, side="sell", order_type=ImmediateOrderType.FOK),
                    "worst_case_exposure_before_usd": Decimal("4"),
                    "worst_case_exposure_after_usd": Decimal("0.50"),
                },
                strict=True,
            ),
        ),
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


def account_state(**overrides: Any) -> PilotAccountState:
    fields: dict[str, Any] = {
        "account_fingerprint": ACCOUNT_FINGERPRINT,
        "wallet_fingerprint": WALLET_FINGERPRINT,
        "collateral_usd": Decimal("200"),
        "allowance_usd": Decimal("200"),
        "kill_engaged": False,
        "observed_at": NOW + timedelta(seconds=1),
    }
    fields.update(overrides)
    return PilotAccountState.model_validate(fields, strict=True)


class FakeSigner:
    """Stands in for the signer sidecar: records intents, never builds transport."""

    def __init__(
        self,
        *,
        outcomes: list[str] | None = None,
        account: PilotAccountState | None = None,
        account_fails: bool = False,
        kill_fails: bool = False,
    ) -> None:
        self.submitted: list[tuple[ExecutionIntent, UUID]] = []
        self.cancelled: list[tuple[ExecutionIntent, UUID]] = []
        self.kill_calls: list[tuple[UUID, ...]] = []
        self.outcomes = outcomes or []
        self._account = account
        self._account_fails = account_fails
        self._kill_fails = kill_fails
        self.account_reads = 0

    def _outcome(self, intent: ExecutionIntent) -> LegOutcome:
        state = self.outcomes.pop(0) if self.outcomes else "FILLED"
        return LegOutcome.model_validate(
            {
                "leg_index": intent.leg_sequence,
                "state": state,
                "filled_size": intent.base_size if state == "FILLED" else Decimal("0"),
                "notional_usd": intent.maximum_spend if state == "FILLED" else Decimal("0"),
                "venue_order_id": None if state == "UNKNOWN" else "order-1",
                "observed_at": NOW + timedelta(seconds=1),
            },
            strict=True,
        )

    def submit(self, intent: ExecutionIntent, capability_id: UUID) -> LegOutcome:
        self.submitted.append((intent, capability_id))
        return self._outcome(intent)

    def cancel(self, intent: ExecutionIntent, capability_id: UUID) -> LegOutcome:
        self.cancelled.append((intent, capability_id))
        return self._outcome(intent)

    def account_state(self) -> PilotAccountState:
        self.account_reads += 1
        if self._account_fails:
            raise RuntimeError("sanitized fake account read failure")
        return self._account or account_state()

    def positions(self) -> Mapping[str, Decimal]:
        return {"token-0": Decimal("10")}

    def engage_kill(self, capability_ids: tuple[UUID, ...]) -> None:
        self.kill_calls.append(capability_ids)
        if self._kill_fails:
            raise RuntimeError("sanitized fake IPC failure")


def evidence(**overrides: Any) -> ExecutionEvidence:
    fields: dict[str, Any] = {
        "manifest": ELIGIBLE_MANIFEST,
        "account": account_state(),
        "geoblock_allowed": True,
        "geoblock_evidence_hash": "a" * 64,
        "geoblock_expires_at": NOW + timedelta(minutes=1),
        "account_scope_evidence_hash": "b" * 64,
        "account_scope_expires_at": NOW + timedelta(minutes=1),
        "kill_engaged": False,
        "operator_present": True,
        "reconciliation_hash": "8" * 64,
        "reconciliation_observed_at": NOW,
    }
    fields.update(overrides)
    return ExecutionEvidence(**fields)


def wired(store: PredictionMarketStore, **overrides: Any):
    issuer, grants = issued()
    verifier = PilotCapabilityVerifier(issuer.public_verification_key)
    verified = {
        grants.primary.grant.capability_id: verified_capability_from_grant(
            grants.primary.grant, verified_at=NOW
        ),
        grants.recovery.grant.capability_id: verified_capability_from_grant(
            grants.recovery.grant, verified_at=NOW
        ),
    }
    killed: list[str] = []
    port = CoordinatorExecutionPort(
        store=store,
        signer=overrides.pop("signer", FakeSigner()),
        verifier=verifier,
        grants=verified,
        evidence=overrides.pop("evidence", evidence),
        clock=lambda: NOW + timedelta(seconds=1),
        on_kill=killed.append,
    )
    return port, grants, killed, verifier


@pytest.fixture
def store(tmp_path: Path) -> PredictionMarketStore:
    opened = PredictionMarketStore(tmp_path / "port.duckdb")
    try:
        yield opened
    finally:
        opened.close()


def test_a_leg_is_persisted_before_it_can_be_submitted(store: PredictionMarketStore) -> None:
    port, grants, _killed, _verifier = wired(store)
    frozen = plan()

    with pytest.raises(PilotExecutionPortError) as raised:
        port.submit(frozen, frozen.legs[0], grants.primary.grant.capability_id)
    assert raised.value.code == "INTENT_NOT_PERSISTED"

    fingerprint = port.persist_intent(frozen, frozen.legs[0])
    assert len(fingerprint) == 64
    stored = store.verified_execution_intent_history_as_of(NOW + timedelta(minutes=1))
    assert [intent.intent_fingerprint for intent in stored] == [fingerprint]


def test_a_persisted_leg_submits_once_under_its_own_grant(store: PredictionMarketStore) -> None:
    signer = FakeSigner()
    port, grants, _killed, _verifier = wired(store, signer=signer)
    frozen = plan()
    port.persist_intent(frozen, frozen.legs[0])

    outcome = port.submit(frozen, frozen.legs[0], grants.primary.grant.capability_id)

    assert outcome.state == "FILLED"
    assert len(signer.submitted) == 1
    assert signer.submitted[0][1] == grants.primary.grant.capability_id
    assert signer.submitted[0][0].protocol_version == "polymarket-clob-2026-08-29-v2"
    assert signer.submitted[0][0].order_type is ImmediateOrderType.FAK


def test_an_unknown_grant_never_reaches_the_signer(store: PredictionMarketStore) -> None:
    signer = FakeSigner()
    port, _grants, _killed, _verifier = wired(store, signer=signer)
    frozen = plan()
    port.persist_intent(frozen, frozen.legs[0])

    with pytest.raises(PilotExecutionPortError) as raised:
        port.submit(frozen, frozen.legs[0], UUID("00000000-0000-0000-0000-0000000000aa"))

    assert raised.value.code == "GRANT_UNKNOWN"
    assert signer.submitted == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"kill_engaged": True},
        {"operator_present": False},
        {"geoblock_allowed": False},
        {"manifest": None},
    ],
)
def test_the_authority_boundary_stops_a_leg_before_the_venue(
    overrides: dict[str, Any], store: PredictionMarketStore
) -> None:
    signer = FakeSigner()
    port, grants, _killed, _verifier = wired(
        store, signer=signer, evidence=lambda: evidence(**overrides)
    )
    frozen = plan()
    port.persist_intent(frozen, frozen.legs[0])

    with pytest.raises(PilotExecutionPortError) as raised:
        port.submit(frozen, frozen.legs[0], grants.primary.grant.capability_id)

    assert raised.value.code == "AUTHORITY_REFUSED"
    assert signer.submitted == []


def test_unavailable_account_evidence_kills_once_and_returns_a_stable_refusal(
    store: PredictionMarketStore,
) -> None:
    signer = FakeSigner(account_fails=True)

    def current_evidence() -> ExecutionEvidence:
        try:
            account = signer.account_state()
        except RuntimeError:
            account = None
        return evidence(account=account, kill_engaged=account is None)

    port, grants, killed, verifier = wired(store, signer=signer, evidence=current_evidence)
    executor = PilotExecutor(port, clock=lambda: NOW)
    primary_id = grants.primary.grant.capability_id
    recovery_id = grants.recovery.grant.capability_id

    with pytest.raises(PilotExecutionPortError) as raised:
        executor.execute_complete_strategy(plan(), grants)

    assert raised.value.code == "AUTHORITY_REFUSED"
    assert signer.account_reads == 1
    assert signer.submitted == []
    assert killed == ["EVIDENCE_STALE"]
    assert verifier.revoked_capability_ids == frozenset({primary_id})
    assert signer.kill_calls == [tuple(sorted((primary_id, recovery_id), key=str))]


def test_revoking_primary_authority_blocks_the_next_leg(store: PredictionMarketStore) -> None:
    signer = FakeSigner()
    port, grants, _killed, _verifier = wired(store, signer=signer)
    frozen = plan()
    port.persist_intent(frozen, frozen.legs[0])
    port.revoke_primary()

    with pytest.raises(PilotExecutionPortError) as raised:
        port.submit(frozen, frozen.legs[0], grants.primary.grant.capability_id)

    assert raised.value.code == "AUTHORITY_REFUSED"
    assert "REVOKED" in str(raised.value)
    assert signer.submitted == []


def test_coordinator_kill_propagates_to_the_signer_before_returning(
    store: PredictionMarketStore,
) -> None:
    signer = FakeSigner()
    port, grants, killed, verifier = wired(store, signer=signer)
    primary_id = grants.primary.grant.capability_id
    recovery_id = grants.recovery.grant.capability_id

    port.engage_kill("UNKNOWN_OUTCOME")

    assert verifier.revoked_capability_ids == frozenset({primary_id})
    assert killed == ["UNKNOWN_OUTCOME"]
    assert signer.kill_calls == [tuple(sorted((primary_id, recovery_id), key=str))]


def test_coordinator_kill_remains_engaged_when_signer_ipc_fails(
    store: PredictionMarketStore,
) -> None:
    signer = FakeSigner(kill_fails=True)
    port, grants, killed, verifier = wired(store, signer=signer)
    primary_id = grants.primary.grant.capability_id

    port.engage_kill("UNKNOWN_OUTCOME")

    assert verifier.revoked_capability_ids == frozenset({primary_id})
    assert killed == ["UNKNOWN_OUTCOME"]
    assert len(signer.kill_calls) == 1


def test_recovery_runs_as_a_cancellation_under_the_recovery_grant(
    store: PredictionMarketStore,
) -> None:
    signer = FakeSigner()
    port, grants, _killed, _verifier = wired(store, signer=signer)
    frozen = plan()
    branch = frozen.recovery_branches[0].leg
    port.persist_intent(frozen, branch)

    port.submit(frozen, branch, grants.recovery.grant.capability_id)

    assert signer.submitted == []
    assert len(signer.cancelled) == 1
    assert signer.cancelled[0][1] == grants.recovery.grant.capability_id


def test_the_executor_drives_the_real_port_end_to_end(store: PredictionMarketStore) -> None:
    signer = FakeSigner()
    port, grants, killed, _verifier = wired(store, signer=signer)
    executor = PilotExecutor(port, clock=lambda: NOW)

    result = executor.execute_complete_strategy(plan(), grants)

    assert result.state == "COMPLETED"
    assert [outcome.state for outcome in result.submitted_legs] == ["FILLED", "FILLED"]
    assert killed == []
    persisted = store.verified_execution_intent_history_as_of(NOW + timedelta(minutes=1))
    assert len(persisted) == 2


def test_an_unknown_outcome_recovers_through_the_port(store: PredictionMarketStore) -> None:
    signer = FakeSigner(outcomes=["UNKNOWN", "FILLED"])
    port, grants, killed, _verifier = wired(store, signer=signer)
    executor = PilotExecutor(port, clock=lambda: NOW)

    result = executor.execute_complete_strategy(plan(), grants)

    assert result.stop_reason == "UNKNOWN_OUTCOME"
    assert len(signer.submitted) == 1
    assert len(signer.cancelled) == 1
    assert killed == ["UNKNOWN_OUTCOME"]


def test_every_persisted_intent_is_immediate_and_bound_to_its_plan(
    store: PredictionMarketStore,
) -> None:
    port, _grants, _killed, _verifier = wired(store)
    frozen = plan()
    port.persist_intent(frozen, frozen.legs[0])

    intent = store.verified_execution_intent_history_as_of(NOW + timedelta(minutes=1))[0]

    assert intent.order_type in (ImmediateOrderType.FAK, ImmediateOrderType.FOK)
    assert intent.capability_fingerprint == frozen.plan_hash
    assert intent.deadline == frozen.deadline
    assert intent.venue is PredictionVenue.POLYMARKET
    assert canonical_execution_hash(intent)


def test_the_same_leg_cannot_be_submitted_twice_under_one_grant(
    store: PredictionMarketStore,
) -> None:
    signer = FakeSigner()
    port, grants, _killed, _verifier = wired(store, signer=signer)
    frozen = plan()
    port.persist_intent(frozen, frozen.legs[0])
    port.submit(frozen, frozen.legs[0], grants.primary.grant.capability_id)

    with pytest.raises(PilotExecutionPortError) as raised:
        port.submit(frozen, frozen.legs[0], grants.primary.grant.capability_id)

    assert raised.value.code == "GRANT_REPLAYED"
    assert len(signer.submitted) == 1
