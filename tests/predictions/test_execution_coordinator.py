from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock, Thread
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from polytrading.predictions.execution.authority import AuthorityContext
from polytrading.predictions.execution.coordinator import (
    CoordinatorAuthorityPort,
    CoordinatorCode,
    ExecutionCoordinator,
    PostFillDecision,
    PreflightEvidence,
    PreflightRefusal,
    PreflightRefusalCode,
    RecoveryReport,
    SubmissionResult,
)
from polytrading.predictions.execution.kill_switch import KillState
from polytrading.predictions.execution.models import (
    ActivationEvidence,
    ExecutionIntent,
    ExecutionOperation,
    ImmediateOrderType,
    KillSwitchEvent,
    LiveExecutionPlan,
    SignedOrderEnvelope,
    VenueOrderEvent,
    VenueOrderState,
)
from polytrading.predictions.polymarket_execution.order import sign_order
from polytrading.predictions.polymarket_execution.protocol import load_protocol_snapshot
from polytrading.predictions.polymarket_execution.rest import RestResult
from polytrading.predictions.polymarket_execution.routes import (
    OrderAckPayload,
    RestCode,
    RouteKey,
    expected_route_result_flags,
)
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.execution_helpers import execution_intent_fields, live_execution_plan_fields
from tests.predictions.test_execution_authority import (
    HASHES,
    MANIFEST_HASH,
    authority_context,
    verified_capability,
)

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)
PRIVATE_KEY = (1).to_bytes(32, "big")
ACCOUNT_FINGERPRINT = "d6f781065c489e6513f45bc3dab82156055056d393c42f49a4defec22b5ee73f"
CAPABILITY_FINGERPRINT = HASHES[8]


@pytest.fixture
def store(tmp_path: Path) -> PredictionMarketStore:
    value = PredictionMarketStore(tmp_path / "predictions.duckdb")
    try:
        yield value
    finally:
        value.close()


def execution_plan(**overrides: object) -> LiveExecutionPlan:
    return LiveExecutionPlan(
        **live_execution_plan_fields(
            account_fingerprint=ACCOUNT_FINGERPRINT,
            manifest_hash=MANIFEST_HASH,
            risk_policy_hash=HASHES[4],
            protocol_hash=HASHES[7],
            capability_fingerprint=CAPABILITY_FINGERPRINT,
            **overrides,
        )
    )


def execution_intent(
    plan: LiveExecutionPlan | None = None,
    **overrides: object,
) -> ExecutionIntent:
    plan = execution_plan() if plan is None else plan
    fields: dict[str, object] = {
        "plan_id": plan.plan_id,
        "token_id": plan.token_ids[0],
        "order_type": plan.leg_order_types[0],
        "limit_price": plan.limit_prices[0],
        "fee_rate_bps_cap": plan.fee_rate_bps_caps[0],
        "account_fingerprint": plan.account_fingerprint,
        "capability_fingerprint": plan.capability_fingerprint,
    }
    fields.update(overrides)
    return ExecutionIntent(**execution_intent_fields(**fields))


def activation_evidence() -> ActivationEvidence:
    return ActivationEvidence(
        schema_version=1,
        activation_evidence_id=UUID("711f9db9-c505-4beb-a2a7-3428225397fa"),
        capability_digest=CAPABILITY_FINGERPRINT,
        manifest_digest=MANIFEST_HASH,
        verifier_result=True,
        verified_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=4),
        rejection_codes=(),
    )


def preflight_evidence(
    plan: LiveExecutionPlan | None = None,
    **overrides: object,
) -> PreflightEvidence:
    plan = execution_plan() if plan is None else plan
    deadline = NOW + timedelta(seconds=4)
    fields: dict[str, object] = {
        "schema_version": 1,
        "plan": plan,
        "activation_evidence": activation_evidence(),
        "proof_artifact_hash": plan.proof_artifact_hash,
        "economics_report_hash": plan.economics_report_hash,
        "book_snapshot_ids": plan.book_snapshot_ids,
        "fee_evidence_ids": plan.fee_evidence_ids,
        "account_evidence_hash": HASHES[9],
        "balance_evidence_hash": HASHES[10],
        "allowance_evidence_hash": HASHES[11],
        "geoblock_evidence_hash": HASHES[12],
        "manifest_hash": plan.manifest_hash,
        "risk_policy_hash": plan.risk_policy_hash,
        "protocol_hash": plan.protocol_hash,
        "protocol_version": "polymarket-clob-2026-08-25-v1",
        "capability_fingerprint": plan.capability_fingerprint,
        "signer_account_fingerprint": plan.account_fingerprint,
        "signer_healthy": True,
        "fee_deadline": deadline,
        "balance_deadline": deadline,
        "allowance_deadline": deadline,
        "manifest_deadline": deadline,
        "activation_deadline": deadline,
        "risk_deadline": deadline,
        "protocol_deadline": deadline,
        "capability_deadline": deadline,
        "evidence_hashes": tuple(sorted(HASHES[9:13])),
    }
    fields.update(overrides)
    return PreflightEvidence.model_validate(fields)


class FakePreflight:
    def __init__(self, outcome: PreflightEvidence | PreflightRefusal) -> None:
        self.outcome = outcome
        self.validate_calls = 0
        self.revalidate_calls = 0
        self.decision = PostFillDecision.CONTINUE_FROZEN_PLAN

    def validate(
        self,
        intent: ExecutionIntent,
        now: datetime,
    ) -> PreflightEvidence | PreflightRefusal:
        del intent, now
        self.validate_calls += 1
        return self.outcome

    def revalidate_after_fill(
        self,
        plan: LiveExecutionPlan,
        intent: ExecutionIntent,
        event: VenueOrderEvent,
        now: datetime,
    ) -> PostFillDecision:
        del plan, intent, event, now
        self.revalidate_calls += 1
        return self.decision


class FakeSigner:
    def __init__(self, submit_result: RestResult | None = None) -> None:
        self.sign_calls = 0
        self.submit_calls = 0
        self.cancel_calls = 0
        self.submit_result = submit_result

    def sign(
        self,
        intent: ExecutionIntent,
        evidence: PreflightEvidence,
    ) -> SignedOrderEnvelope:
        del evidence
        self.sign_calls += 1
        return sign_order(intent, PRIVATE_KEY, load_protocol_snapshot())

    def submit(
        self,
        intent: ExecutionIntent,
        envelope: SignedOrderEnvelope,
        evidence: PreflightEvidence,
    ) -> RestResult:
        del intent, envelope, evidence
        self.submit_calls += 1
        if self.submit_result is None:
            raise AssertionError("submit result was not configured")
        return self.submit_result

    def cancel(
        self,
        intent: ExecutionIntent,
        envelope: SignedOrderEnvelope,
        venue_order_id: str,
        evidence: PreflightEvidence,
    ) -> RestResult:
        del intent, envelope, venue_order_id, evidence
        self.cancel_calls += 1
        raise AssertionError("cancel result was not configured")


class FakeAccountReader:
    account_fingerprint = ACCOUNT_FINGERPRINT

    def read_open_orders(self) -> RestResult:
        raise AssertionError("account read was not configured")

    def read_trades(self) -> RestResult:
        raise AssertionError("account read was not configured")

    def read_balance_allowance(self) -> RestResult:
        raise AssertionError("account read was not configured")

    def read_order(self, venue_order_id: str) -> RestResult:
        del venue_order_id
        raise AssertionError("order read was not configured")


class FakeAuthority(CoordinatorAuthorityPort):
    def __init__(self) -> None:
        self.calls: list[ExecutionOperation] = []

    def snapshot(
        self,
        intent: ExecutionIntent,
        evidence: PreflightEvidence,
        operation: ExecutionOperation,
        now: datetime,
    ) -> AuthorityContext:
        self.calls.append(operation)
        return authority_context(
            now=now,
            account_fingerprint=intent.account_fingerprint,
            manifest_record_hash=evidence.manifest_hash,
            protocol_fixture_hash=evidence.protocol_hash,
            verified_capability=verified_capability(
                account_fingerprint=intent.account_fingerprint,
                manifest_record_hash=evidence.manifest_hash,
                protocol_fixture_hash=evidence.protocol_hash,
                capability_digest=intent.capability_fingerprint,
            ),
            account_scope_account_fingerprint=intent.account_fingerprint,
            kill_engaged=False,
        )


def coordinator(
    store: PredictionMarketStore,
    preflight: FakePreflight,
    signer: FakeSigner | None = None,
    *,
    authority: CoordinatorAuthorityPort | None = None,
    clock: object | None = None,
) -> ExecutionCoordinator:
    return ExecutionCoordinator(
        store=store,
        preflight=preflight,
        signer=FakeSigner() if signer is None else signer,
        account_reader=FakeAccountReader(),
        authority=FakeAuthority() if authority is None else authority,
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=(lambda: NOW) if clock is None else clock,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )


def engage_durable_kill(
    store: PredictionMarketStore,
    intent: ExecutionIntent,
    *,
    occurred_at: datetime = NOW,
) -> None:
    store.append_kill_switch_event(
        KillSwitchEvent(
            schema_version=1,
            kill_event_id=uuid4(),
            trigger=CoordinatorCode.RECOVERY_BLOCKED.value,
            scope=ACCOUNT_FINGERPRINT,
            source_intent_id=intent.intent_id,
            source_order_id=None,
            prior_state=False,
            occurred_at=occurred_at,
            clearance_evidence_hashes=(),
        )
    )


def submit_result(code: RestCode, *, order_id: str = "venue-order-1") -> RestResult:
    payload = (
        OrderAckPayload(
            kind="ORDER_ACK",
            order_id=order_id,
            status={
                RestCode.ORDER_ACK_MATCHED: "matched",
                RestCode.ORDER_ACK_DELAYED: "delayed",
                RestCode.ORDER_ACK_LIVE_UNEXPECTED: "live",
                RestCode.ORDER_ACK_UNMATCHED: "unmatched",
            }[code],
            making_amount="5100000",
            taking_amount="10000000",
            transaction_hashes=(),
            trade_ids=(),
        )
        if code
        in {
            RestCode.ORDER_ACK_MATCHED,
            RestCode.ORDER_ACK_DELAYED,
            RestCode.ORDER_ACK_LIVE_UNEXPECTED,
            RestCode.ORDER_ACK_UNMATCHED,
        }
        else None
    )
    recovery_required, kill_required = expected_route_result_flags(
        route=RouteKey.SUBMIT_ORDER,
        code=code,
        payload=payload,
    )
    build_failed = code is RestCode.AUTH_REQUEST_BUILD_FAILED
    return RestResult(
        route=RouteKey.SUBMIT_ORDER,
        code=code,
        observed_at=NOW,
        raw_body_hash=None if build_failed else HASHES[10],
        request_body_hash=None if build_failed else HASHES[11],
        attempts=0 if build_failed else 1,
        recovery_required=recovery_required,
        kill_required=kill_required,
        payload=payload,
    )


def lifecycle_event(
    intent: ExecutionIntent,
    state: VenueOrderState,
    *,
    venue_order_id: str = "venue-order-1",
    received_at: datetime = NOW + timedelta(seconds=1),
    sequence_number: int | None = None,
) -> VenueOrderEvent:
    return VenueOrderEvent(
        schema_version=1,
        event_id=uuid4(),
        venue="polymarket",
        raw_event_hash=HASHES[9],
        source_channel="user_stream",
        venue_order_id=venue_order_id,
        intent_id=None,
        original_venue_state=state.value,
        normalized_state=state,
        terminal=state
        in {
            VenueOrderState.FILLED,
            VenueOrderState.CANCELLED,
            VenueOrderState.REJECTED,
            VenueOrderState.RECONCILED,
        },
        venue_timestamp=received_at,
        received_at=received_at,
        sequence_number=sequence_number,
        protocol_version=intent.protocol_version,
    )


def test_simultaneous_submission_has_one_permanent_claim_and_one_mutation(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    executor = coordinator(store, FakePreflight(preflight_evidence(plan)), signer)
    start = Barrier(3)
    results: list[SubmissionResult] = []
    failures: list[BaseException] = []
    result_lock = Lock()

    def submit() -> None:
        start.wait()
        try:
            result = executor.submit_intent(intent)
            with result_lock:
                results.append(result)
        except BaseException as error:
            with result_lock:
                failures.append(error)

    threads = (Thread(target=submit), Thread(target=submit))
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(result.code for result in results) == [
        CoordinatorCode.DUPLICATE_INTENT,
        CoordinatorCode.SUBMITTED,
    ]
    assert signer.sign_calls == signer.submit_calls == 1
    assert tuple(
        event.normalized_state
        for event in store.verified_venue_order_events_for_intent(
            intent.intent_id,
            NOW + timedelta(seconds=1),
        )
    ) == (VenueOrderState.SUBMITTING, VenueOrderState.ACK_MATCHED)


def test_separate_coordinators_on_one_store_share_the_permanent_claim(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    executors = (
        coordinator(store, FakePreflight(preflight_evidence(plan)), signer),
        coordinator(store, FakePreflight(preflight_evidence(plan)), signer),
    )
    start = Barrier(3)
    results: list[SubmissionResult] = []
    failures: list[BaseException] = []
    result_lock = Lock()

    def submit(executor: ExecutionCoordinator) -> None:
        start.wait()
        try:
            result = executor.submit_intent(intent)
            with result_lock:
                results.append(result)
        except BaseException as error:
            with result_lock:
                failures.append(error)

    threads = tuple(Thread(target=submit, args=(executor,)) for executor in executors)
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(result.code for result in results) == [
        CoordinatorCode.DUPLICATE_INTENT,
        CoordinatorCode.SUBMITTED,
    ]
    assert signer.sign_calls == signer.submit_calls == 1
    assert tuple(
        event.normalized_state
        for event in store.verified_venue_order_events_for_intent(
            intent.intent_id,
            NOW + timedelta(seconds=1),
        )
    ) == (VenueOrderState.SUBMITTING, VenueOrderState.ACK_MATCHED)


def test_separate_coordinators_and_stores_on_one_path_share_durable_claim(
    tmp_path: Path,
) -> None:
    path = tmp_path / "two-store-claims.duckdb"
    stores = (PredictionMarketStore(path), PredictionMarketStore(path))
    plan = execution_plan()
    intent = execution_intent(plan)
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    executors = tuple(
        coordinator(store, FakePreflight(preflight_evidence(plan)), signer) for store in stores
    )
    start = Barrier(3)
    results: list[SubmissionResult] = []
    failures: list[BaseException] = []
    result_lock = Lock()

    def submit(executor: ExecutionCoordinator) -> None:
        start.wait()
        try:
            result = executor.submit_intent(intent)
            with result_lock:
                results.append(result)
        except BaseException as error:
            with result_lock:
                failures.append(error)

    threads = tuple(Thread(target=submit, args=(executor,)) for executor in executors)
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert failures == []
    assert sorted(result.code for result in results) == [
        CoordinatorCode.DUPLICATE_INTENT,
        CoordinatorCode.SUBMITTED,
    ]
    assert signer.sign_calls == signer.submit_calls == 1
    assert all(not store._in_transaction for store in stores)
    for store in stores:
        store.close()


@pytest.mark.parametrize("callback", ["preflight", "authority", "signer"])
def test_recursive_submission_fails_immediately_without_callback_lock_or_transaction(
    store: PredictionMarketStore,
    callback: str,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    evidence = preflight_evidence(plan)
    recursive: list[SubmissionResult] = []
    holder: dict[str, ExecutionCoordinator] = {}

    class RecursivePreflight(FakePreflight):
        def validate(self, candidate: ExecutionIntent, now: datetime) -> PreflightEvidence:
            if callback == "preflight" and not recursive:
                recursive.append(holder["executor"].submit_intent(candidate))
            return super().validate(candidate, now)  # type: ignore[return-value]

    class RecursiveAuthority(FakeAuthority):
        def snapshot(
            self,
            candidate: ExecutionIntent,
            candidate_evidence: PreflightEvidence,
            operation: ExecutionOperation,
            now: datetime,
        ) -> AuthorityContext:
            executor = holder["executor"]
            assert not store._execution_claim_lock.locked()
            assert not store._in_transaction
            if callback == "authority" and not recursive:
                recursive.append(executor.submit_intent(candidate))
            return super().snapshot(candidate, candidate_evidence, operation, now)

    class RecursiveSigner(FakeSigner):
        def sign(
            self,
            candidate: ExecutionIntent,
            candidate_evidence: PreflightEvidence,
        ) -> SignedOrderEnvelope:
            executor = holder["executor"]
            assert not store._execution_claim_lock.locked()
            assert not store._in_transaction
            if callback == "signer" and not recursive:
                recursive.append(executor.submit_intent(candidate))
            return super().sign(candidate, candidate_evidence)

    signer = RecursiveSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    executor = coordinator(
        store,
        RecursivePreflight(evidence),
        signer,
        authority=RecursiveAuthority(),
    )
    holder["executor"] = executor

    assert executor.submit_intent(intent).code is CoordinatorCode.SUBMITTED
    assert [result.code for result in recursive] == [CoordinatorCode.DUPLICATE_INTENT]
    assert signer.sign_calls == signer.submit_calls == 1


def test_durable_kill_between_prepare_and_sign_prevents_all_signer_io(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    executor = coordinator(store, FakePreflight(preflight_evidence(plan)), signer)
    assert executor.prepare(intent).code is CoordinatorCode.PREPARED
    engage_durable_kill(store, intent)

    result = executor.submit_intent(intent)

    assert result.code is CoordinatorCode.EXECUTION_KILL_ENGAGED
    assert signer.sign_calls == signer.submit_calls == 0
    assert store.verified_signed_order_envelope(intent.intent_id) is None
    assert store.latest_order_state(intent.intent_id) is None


@pytest.mark.parametrize(
    "operation", [ExecutionOperation.SIGN_ORDER, ExecutionOperation.SUBMIT_ORDER]
)
def test_kill_installed_by_fresh_authority_callback_stops_the_next_mutation(
    store: PredictionMarketStore,
    operation: ExecutionOperation,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))

    class KillingAuthority(FakeAuthority):
        def snapshot(
            self,
            candidate: ExecutionIntent,
            evidence: PreflightEvidence,
            candidate_operation: ExecutionOperation,
            now: datetime,
        ) -> AuthorityContext:
            context = super().snapshot(candidate, evidence, candidate_operation, now)
            matching_call = candidate_operation is operation and self.calls.count(
                candidate_operation
            ) == (2 if operation is ExecutionOperation.SIGN_ORDER else 1)
            if matching_call:
                engage_durable_kill(store, candidate, occurred_at=now)
            return context

    executor = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        signer,
        authority=KillingAuthority(),
    )

    result = executor.submit_intent(intent)

    assert result.code is CoordinatorCode.EXECUTION_KILL_ENGAGED
    assert signer.submit_calls == 0
    assert signer.sign_calls == (0 if operation is ExecutionOperation.SIGN_ORDER else 1)
    assert (store.verified_signed_order_envelope(intent.intent_id) is not None) is (
        operation is ExecutionOperation.SUBMIT_ORDER
    )
    latest = store.latest_order_state(intent.intent_id)
    assert (latest is not None and latest.normalized_state is VenueOrderState.SUBMITTING) is (
        operation is ExecutionOperation.SUBMIT_ORDER
    )


@pytest.mark.parametrize(
    "boundary",
    ["expired_intent", "future_intent", "future_plan", "future_cutoff", "future_activation"],
)
def test_prepare_rejects_every_temporal_boundary_with_zero_writes(
    store: PredictionMarketStore,
    boundary: str,
) -> None:
    plan_overrides: dict[str, object] = {}
    intent_overrides: dict[str, object] = {}
    evidence_overrides: dict[str, object] = {}
    if boundary == "expired_intent":
        intent_overrides.update(
            created_at=NOW - timedelta(seconds=2),
            deadline=NOW - timedelta(seconds=1),
        )
    elif boundary == "future_intent":
        intent_overrides.update(
            created_at=NOW + timedelta(seconds=1),
            deadline=NOW + timedelta(seconds=4),
        )
    elif boundary == "future_plan":
        plan_overrides["observed_at"] = NOW + timedelta(seconds=1)
    elif boundary == "future_cutoff":
        plan_overrides["information_cutoff"] = NOW + timedelta(seconds=1)
    else:
        activation = activation_evidence().model_copy(
            update={"verified_at": NOW + timedelta(seconds=1)}
        )
        evidence_overrides["activation_evidence"] = activation
    plan = execution_plan(**plan_overrides)
    intent = execution_intent(plan, **intent_overrides)
    signer = FakeSigner()

    result = coordinator(
        store,
        FakePreflight(preflight_evidence(plan, **evidence_overrides)),
        signer,
    ).prepare(intent)

    assert result.code is CoordinatorCode.PREFLIGHT_EVIDENCE_STALE
    assert store.verified_live_execution_plan(plan.plan_id) is None
    assert store.verified_execution_intent(intent.intent_id) is None
    assert signer.sign_calls == signer.submit_calls == 0


@pytest.mark.parametrize(
    "lineage",
    [
        tuple(sorted(HASHES[9:12])),
        tuple(sorted((HASHES[9], HASHES[10], HASHES[11], HASHES[13]))),
    ],
)
def test_prepare_rejects_omitted_or_swapped_evidence_lineage(
    store: PredictionMarketStore,
    lineage: tuple[str, ...],
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)

    result = coordinator(
        store,
        FakePreflight(preflight_evidence(plan, evidence_hashes=lineage)),
    ).prepare(intent)

    assert result.code is CoordinatorCode.PREFLIGHT_IDENTITY_MISMATCH
    assert store.verified_execution_intent(intent.intent_id) is None


def test_callback_owned_evidence_alias_cannot_change_later_mutation_truth(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    evidence = preflight_evidence(plan)
    seen_evidence_ids: list[int] = []

    class MutatingSigner(FakeSigner):
        def sign(
            self,
            candidate: ExecutionIntent,
            candidate_evidence: PreflightEvidence,
        ) -> SignedOrderEnvelope:
            seen_evidence_ids.append(id(candidate_evidence))
            object.__setattr__(candidate_evidence, "signer_healthy", False)
            return super().sign(candidate, candidate_evidence)

        def submit(
            self,
            candidate: ExecutionIntent,
            envelope: SignedOrderEnvelope,
            candidate_evidence: PreflightEvidence,
        ) -> RestResult:
            seen_evidence_ids.append(id(candidate_evidence))
            assert candidate_evidence.signer_healthy is True
            return super().submit(candidate, envelope, candidate_evidence)

    signer = MutatingSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    executor = coordinator(store, FakePreflight(evidence), signer)
    assert executor.prepare(intent).code is CoordinatorCode.PREPARED
    object.__setattr__(evidence, "protocol_version", "hostile-alias")

    result = executor.submit_intent(intent)

    assert result.code is CoordinatorCode.SUBMITTED
    assert signer.sign_calls == signer.submit_calls == 1
    assert len(set(seen_evidence_ids)) == 2


def test_callback_owned_rest_result_alias_is_not_retained_for_classification(
    store: PredictionMarketStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    callback_result = submit_result(RestCode.ORDER_ACK_MATCHED)
    classified_results: list[RestResult] = []
    original = ExecutionCoordinator._classify_submit_result

    def observe_classification(
        executor: ExecutionCoordinator,
        candidate: ExecutionIntent,
        result: RestResult,
        now: datetime,
    ) -> SubmissionResult:
        classified_results.append(result)
        return original(executor, candidate, result, now)

    monkeypatch.setattr(
        ExecutionCoordinator,
        "_classify_submit_result",
        observe_classification,
    )
    result = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        FakeSigner(callback_result),
    ).submit_intent(intent)

    assert result.code is CoordinatorCode.SUBMITTED
    assert classified_results == [callback_result]
    assert classified_results[0] is not callback_result


def test_preflight_refusal_writes_nothing_and_never_calls_signer(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    refusal = PreflightRefusal(
        code=PreflightRefusalCode.BOOK_STALE,
        observed_at=NOW,
        evidence_hashes=(),
    )
    preflight = FakePreflight(refusal)
    signer = FakeSigner()

    result = coordinator(store, preflight, signer).prepare(intent)

    assert result.code is CoordinatorCode.PREFLIGHT_REFUSED
    assert store.verified_live_execution_plan(plan.plan_id) is None
    assert store.verified_execution_intent(intent.intent_id) is None
    assert store.verified_kill_switch_events(ACCOUNT_FINGERPRINT, NOW) == ()
    assert signer.sign_calls == signer.submit_calls == 0


def test_stale_individual_preflight_deadline_refuses_with_zero_writes(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    preflight = FakePreflight(preflight_evidence(plan, fee_deadline=NOW))

    result = coordinator(store, preflight).prepare(intent)

    assert result.code is CoordinatorCode.PREFLIGHT_EVIDENCE_STALE
    assert store.verified_live_execution_plan(plan.plan_id) is None
    assert store.verified_execution_intent(intent.intent_id) is None


def test_prepare_commits_the_exact_plan_and_intent_without_signing(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    preflight = FakePreflight(preflight_evidence(plan))
    signer = FakeSigner()

    result = coordinator(store, preflight, signer).prepare(intent)

    assert result.code is CoordinatorCode.PREPARED
    assert result.plan_id == plan.plan_id
    assert result.intent_id == intent.intent_id
    assert store.verified_live_execution_plan(plan.plan_id) == plan
    assert store.verified_execution_intent(intent.intent_id) == intent
    assert signer.sign_calls == signer.submit_calls == 0


def test_plan_and_intent_are_committed_before_envelope_construction(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)

    class BoundarySigner(FakeSigner):
        def sign(
            self,
            candidate: ExecutionIntent,
            evidence: PreflightEvidence,
        ) -> SignedOrderEnvelope:
            assert store.verified_live_execution_plan(plan.plan_id) == plan
            assert store.verified_execution_intent(intent.intent_id) == intent
            assert store.verified_signed_order_envelope(intent.intent_id) is None
            assert store.latest_order_state(intent.intent_id) is None
            return super().sign(candidate, evidence)

    signer = BoundarySigner(submit_result(RestCode.ORDER_OUTCOME_UNKNOWN))

    result = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        signer,
    ).submit_intent(intent)

    assert isinstance(result, SubmissionResult)
    assert signer.sign_calls == 1


def test_envelope_and_submitting_event_are_committed_before_transport_io(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)

    class BoundarySigner(FakeSigner):
        def submit(
            self,
            candidate: ExecutionIntent,
            envelope: SignedOrderEnvelope,
            evidence: PreflightEvidence,
        ) -> RestResult:
            assert store.verified_signed_order_envelope(intent.intent_id) == envelope
            latest = store.latest_order_state(intent.intent_id)
            assert latest is not None
            assert latest.normalized_state is VenueOrderState.SUBMITTING
            return super().submit(candidate, envelope, evidence)

    signer = BoundarySigner(submit_result(RestCode.ORDER_OUTCOME_UNKNOWN))

    result = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        signer,
    ).submit_intent(intent)

    assert result.state is VenueOrderState.UNKNOWN
    assert signer.submit_calls == 1
    events = store.verified_venue_order_events_for_intent(
        intent.intent_id,
        NOW + timedelta(seconds=1),
    )
    assert tuple(event.normalized_state for event in events) == (
        VenueOrderState.SUBMITTING,
        VenueOrderState.UNKNOWN,
    )
    kills = store.verified_kill_switch_events(ACCOUNT_FINGERPRINT, NOW)
    assert tuple(event.trigger for event in kills) == ("ORDER_OUTCOME_UNKNOWN",)


@pytest.mark.parametrize(
    ("code", "expected_state", "expected_kill"),
    [
        (RestCode.ORDER_ACK_MATCHED, VenueOrderState.ACK_MATCHED, False),
        (RestCode.ORDER_ACK_DELAYED, VenueOrderState.ACK_DELAYED, True),
        (
            RestCode.ORDER_ACK_LIVE_UNEXPECTED,
            VenueOrderState.ACK_LIVE_UNEXPECTED,
            True,
        ),
        (RestCode.ORDER_ACK_UNMATCHED, VenueOrderState.UNKNOWN, True),
        (RestCode.ORDER_OUTCOME_UNKNOWN, VenueOrderState.UNKNOWN, True),
        (RestCode.AUTH_REJECTED, VenueOrderState.UNKNOWN, True),
        (RestCode.AUTH_REQUEST_BUILD_FAILED, VenueOrderState.REJECTED, True),
    ],
)
def test_submit_maps_only_the_closed_task8_acknowledgements(
    store: PredictionMarketStore,
    code: RestCode,
    expected_state: VenueOrderState,
    expected_kill: bool,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    signer = FakeSigner(submit_result(code))

    result = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        signer,
    ).submit_intent(intent)

    assert result.state is expected_state
    assert signer.submit_calls == 1
    assert bool(store.verified_kill_switch_events(ACCOUNT_FINGERPRINT, NOW)) is expected_kill


@pytest.mark.parametrize(
    ("decision", "expected_kill"),
    [
        (PostFillDecision.CONTINUE_FROZEN_PLAN, False),
        (PostFillDecision.FROZEN_UNWIND, False),
        (PostFillDecision.HALT_EXPOSED, True),
    ],
)
def test_first_fill_revalidation_accepts_only_the_three_closed_decisions(
    store: PredictionMarketStore,
    decision: PostFillDecision,
    expected_kill: bool,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    preflight = FakePreflight(preflight_evidence(plan))
    preflight.decision = decision
    executor = coordinator(
        store,
        preflight,
        FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED)),
    )
    assert executor.submit_intent(intent).state is VenueOrderState.ACK_MATCHED

    result = executor.apply_order_event(
        intent,
        lifecycle_event(intent, VenueOrderState.PARTIALLY_FILLED),
    )

    assert result.state is VenueOrderState.PARTIALLY_FILLED
    assert result.post_fill_decision is decision
    assert preflight.revalidate_calls == 1
    assert (
        bool(
            store.verified_kill_switch_events(
                ACCOUNT_FINGERPRINT,
                NOW + timedelta(seconds=2),
            )
        )
        is expected_kill
    )


def test_second_fill_transition_does_not_repeat_first_fill_revalidation(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    preflight = FakePreflight(preflight_evidence(plan))
    executor = coordinator(
        store,
        preflight,
        FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED)),
    )
    executor.submit_intent(intent)
    executor.apply_order_event(
        intent,
        lifecycle_event(intent, VenueOrderState.PARTIALLY_FILLED),
    )

    result = executor.apply_order_event(
        intent,
        lifecycle_event(
            intent,
            VenueOrderState.FILLED,
            received_at=NOW + timedelta(seconds=2),
        ),
    )

    assert result.state is VenueOrderState.FILLED
    assert result.post_fill_decision is None
    assert preflight.revalidate_calls == 1


@pytest.mark.parametrize(
    "state",
    [VenueOrderState.FILLED, VenueOrderState.REJECTED, VenueOrderState.CANCELLED],
)
def test_authoritative_terminal_order_facts_append_without_rewriting_history(
    store: PredictionMarketStore,
    state: VenueOrderState,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    executor = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED)),
    )
    executor.submit_intent(intent)

    result = executor.apply_order_event(intent, lifecycle_event(intent, state))

    assert result.state is state
    history = store.verified_venue_order_events_for_intent(
        intent.intent_id,
        NOW + timedelta(seconds=3),
    )
    assert tuple(item.normalized_state for item in history) == (
        VenueOrderState.SUBMITTING,
        VenueOrderState.ACK_MATCHED,
        state,
    )


def test_authoritative_fok_rejection_is_terminal_and_never_resubmitted(
    store: PredictionMarketStore,
) -> None:
    original = execution_plan()
    plan = original.model_copy(
        update={
            "leg_order_types": (
                ImmediateOrderType.FOK,
                original.leg_order_types[1],
            )
        }
    )
    intent = execution_intent(plan)
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    executor = coordinator(store, FakePreflight(preflight_evidence(plan)), signer)
    executor.submit_intent(intent)

    result = executor.apply_order_event(
        intent,
        lifecycle_event(intent, VenueOrderState.REJECTED),
    )

    assert result.state is VenueOrderState.REJECTED
    assert result.event is not None and result.event.terminal
    assert signer.sign_calls == signer.submit_calls == 1


def test_illegal_or_mismatched_order_transition_becomes_unknown_and_preserves_first_kill(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    executor = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED)),
    )
    executor.submit_intent(intent)

    result = executor.apply_order_event(
        intent,
        lifecycle_event(
            intent,
            VenueOrderState.SUBMITTING,
            venue_order_id="foreign-order",
        ),
    )

    assert result.state is VenueOrderState.UNKNOWN
    assert result.kill_reason == "ORDER_EVENT_CONTRADICTION"
    assert executor.new_intents_blocked


def test_reordered_authoritative_sequence_becomes_unknown_and_kills(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    executor = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED)),
    )
    executor.submit_intent(intent)
    executor.apply_order_event(
        intent,
        lifecycle_event(
            intent,
            VenueOrderState.PARTIALLY_FILLED,
            sequence_number=2,
        ),
    )

    result = executor.apply_order_event(
        intent,
        lifecycle_event(
            intent,
            VenueOrderState.FILLED,
            received_at=NOW + timedelta(seconds=2),
            sequence_number=1,
        ),
    )

    assert result.code is CoordinatorCode.ORDER_EVENT_CONTRADICTION
    assert result.state is VenueOrderState.UNKNOWN
    assert result.kill_reason == CoordinatorCode.ORDER_EVENT_CONTRADICTION.value


def test_duplicate_submit_performs_no_second_sign_or_submit_and_engages_kill(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    executor = coordinator(store, FakePreflight(preflight_evidence(plan)), signer)
    assert executor.submit_intent(intent).state is VenueOrderState.ACK_MATCHED

    duplicate = executor.submit_intent(intent)

    assert duplicate.code is CoordinatorCode.DUPLICATE_INTENT
    assert duplicate.kill_reason == CoordinatorCode.DUPLICATE_INTENT.value
    assert signer.sign_calls == signer.submit_calls == 1


def test_persisted_duplicate_intent_is_never_signed_after_restart(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    with store.transaction() as transaction:
        transaction.append_live_execution_plan(plan)
        transaction.append_execution_intent(intent)
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))

    result = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        signer,
    ).submit_intent(intent)

    assert result.code is CoordinatorCode.DUPLICATE_INTENT
    assert signer.sign_calls == signer.submit_calls == 0
    assert store.latest_order_state(intent.intent_id) is None


def test_stored_envelope_collision_performs_no_sign_or_submit(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    preflight = FakePreflight(preflight_evidence(plan))
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    executor = coordinator(store, preflight, signer)
    assert executor.prepare(intent).code is CoordinatorCode.PREPARED
    store.append_signed_order_envelope(sign_order(intent, PRIVATE_KEY, load_protocol_snapshot()))

    result = executor.submit_intent(intent)

    assert result.code is CoordinatorCode.ENVELOPE_COLLISION
    assert signer.sign_calls == signer.submit_calls == 0
    assert store.latest_order_state(intent.intent_id) is None


def test_corrupted_persisted_intent_collision_fails_closed_without_signing(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    with store.transaction() as transaction:
        transaction.append_live_execution_plan(plan)
        transaction.append_execution_intent(intent)
    store._connection.execute(
        "UPDATE execution_intents SET record_hash = ? WHERE intent_id = ?",
        ["0" * 64, intent.intent_id],
    )
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))

    result = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        signer,
    ).submit_intent(intent)

    assert result.code is CoordinatorCode.INTENT_COLLISION
    assert result.kill_reason == CoordinatorCode.INTENT_COLLISION.value
    assert signer.sign_calls == signer.submit_calls == 0


def test_signer_crash_persists_unknown_without_envelope_or_submit_attempt(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)

    class CrashingSigner(FakeSigner):
        def sign(
            self,
            candidate: ExecutionIntent,
            evidence: PreflightEvidence,
        ) -> SignedOrderEnvelope:
            del candidate, evidence
            self.sign_calls += 1
            raise RuntimeError("sensitive signer detail")

    signer = CrashingSigner()

    result = coordinator(
        store,
        FakePreflight(preflight_evidence(plan)),
        signer,
    ).submit_intent(intent)

    assert result.state is VenueOrderState.UNKNOWN
    assert result.kill_reason == CoordinatorCode.SIGNER_FAILED.value
    assert "sensitive" not in repr(result)
    assert store.verified_signed_order_envelope(intent.intent_id) is None
    assert signer.sign_calls == 1
    assert signer.submit_calls == 0


def test_coordinator_crash_after_submitting_boundary_leaves_durable_recovery_fact(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)

    class ProcessCrash(BaseException):
        pass

    class CrashingSigner(FakeSigner):
        def submit(
            self,
            candidate: ExecutionIntent,
            envelope: SignedOrderEnvelope,
            evidence: PreflightEvidence,
        ) -> RestResult:
            del candidate, envelope, evidence
            self.submit_calls += 1
            raise ProcessCrash

    signer = CrashingSigner()
    executor = coordinator(store, FakePreflight(preflight_evidence(plan)), signer)

    with pytest.raises(ProcessCrash):
        executor.submit_intent(intent)

    assert store.verified_signed_order_envelope(intent.intent_id) is not None
    latest = store.latest_order_state(intent.intent_id)
    assert latest is not None
    assert latest.normalized_state is VenueOrderState.SUBMITTING
    assert signer.sign_calls == signer.submit_calls == 1


def test_coordinator_denies_mutation_subclass_copy_pickle_and_reinitialization(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    executor = coordinator(store, FakePreflight(preflight_evidence(plan)))

    with pytest.raises(AttributeError, match="COORDINATOR_IMMUTABLE"):
        executor._recovering = True  # type: ignore[misc]
    with pytest.raises(TypeError, match="COORDINATOR_NOT_SUBCLASSABLE"):

        class UnsafeCoordinator(ExecutionCoordinator):
            pass

    for bypass in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError, match="COORDINATOR_STATE_COPY_DENIED"):
            bypass(executor)
    with pytest.raises(ValueError, match="COORDINATOR_ALREADY_INITIALIZED"):
        executor.__init__(
            store=store,
            preflight=FakePreflight(preflight_evidence(plan)),
            signer=FakeSigner(),
            account_reader=FakeAccountReader(),
            authority=FakeAuthority(),
            account_fingerprint=ACCOUNT_FINGERPRINT,
            clock=lambda: NOW,
            test_only_kill_state=KillState(engaged=False, latest_event=None),
        )


def test_coordinator_records_revalidate_copies_and_reject_unclosed_public_results(
    store: PredictionMarketStore,
) -> None:
    del store
    evidence = preflight_evidence()
    with pytest.raises(ValidationError):
        evidence.model_copy(update={"protocol_version": ""})
    with pytest.raises(ValidationError):
        SubmissionResult(
            code=CoordinatorCode.RECOVERY_BLOCKED,
            intent_id=execution_intent().intent_id,
            event=None,
            kill_reason="hostile-public-canary",
        )
    with pytest.raises(ValidationError):
        RecoveryReport(
            code=CoordinatorCode.RECOVERY_COMPLETE,
            account_fingerprint=ACCOUNT_FINGERPRINT,
            reads=(RouteKey.SUBMIT_ORDER,),
            recovered_intent_ids=(),
            blocked_intent_ids=(),
            submit_attempts=0,
            kill_reason=None,
        )
