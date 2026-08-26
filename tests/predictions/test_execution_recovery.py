from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from polytrading.predictions.execution.coordinator import (
    CoordinatorCode,
    ExecutionCoordinator,
    RecoveryReport,
)
from polytrading.predictions.execution.kill_switch import KillState
from polytrading.predictions.execution.models import (
    LiveExecutionPlan,
    LiveReconciliation,
    VenueOrderState,
    VenueTradeEvent,
    VenueTradeState,
)
from polytrading.predictions.polymarket_execution.heartbeat import HeartbeatState
from polytrading.predictions.polymarket_execution.rest import RestResult
from polytrading.predictions.polymarket_execution.routes import (
    BalanceAllowancePayload,
    CancellationPayload,
    MakerOrderReadPayload,
    OrderReadPayload,
    OrdersReadPayload,
    RestCode,
    RouteKey,
    TradeReadPayload,
    TradesReadPayload,
    expected_route_result_flags,
)
from polytrading.predictions.polymarket_execution.user_stream import UserStreamHealth
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.test_execution_authority import HASHES
from tests.predictions.test_execution_coordinator import (
    ACCOUNT_FINGERPRINT,
    NOW,
    FakeAuthority,
    FakePreflight,
    FakeSigner,
    execution_intent,
    execution_plan,
    lifecycle_event,
    preflight_evidence,
    submit_result,
)


@pytest.fixture
def store(tmp_path: Path) -> PredictionMarketStore:
    value = PredictionMarketStore(tmp_path / "recovery.duckdb")
    try:
        yield value
    finally:
        value.close()


def read_result(route: RouteKey, payload: object) -> RestResult:
    recovery_required, kill_required = expected_route_result_flags(
        route=route,
        code=RestCode.READ_OK,
        payload=payload,  # type: ignore[arg-type]
    )
    return RestResult(
        route=route,
        code=RestCode.READ_OK,
        observed_at=NOW + timedelta(milliseconds=250),
        raw_body_hash=HASHES[10],
        request_body_hash=None,
        attempts=1,
        recovery_required=recovery_required,
        kill_required=kill_required,
        payload=payload,
    )


def failed_read_result(route: RouteKey, code: RestCode) -> RestResult:
    recovery_required, kill_required = expected_route_result_flags(
        route=route,
        code=code,
        payload=None,
    )
    return RestResult(
        route=route,
        code=code,
        observed_at=NOW + timedelta(milliseconds=250),
        raw_body_hash=HASHES[10],
        request_body_hash=None,
        attempts=1,
        recovery_required=recovery_required,
        kill_required=kill_required,
        payload=None,
    )


def order_read(
    *,
    order_id: str,
    asset_id: str,
    price: str,
    original_size: str = "10",
    size_matched: str = "10",
    status: str = "MATCHED",
) -> OrderReadPayload:
    return OrderReadPayload(
        kind="ORDER_READ",
        id=order_id,
        market="condition-1",
        asset_id=asset_id,
        maker_address="0x" + "11" * 20,
        side="BUY",
        price=price,
        original_size=original_size,
        size_matched=size_matched,
        outcome="YES",
        order_type="FAK",
        status=status,
        associate_trades=(),
        created_at="1787673600",
        expiration="1787677200",
    )


def confirmed_maker_trade(
    *,
    order_id: str,
    asset_id: str,
    price: str,
    matched_amount: str = "10",
    trade_id: str = "trade-1",
) -> TradeReadPayload:
    return TradeReadPayload(
        kind="TRADE_READ",
        id=trade_id,
        market="condition-1",
        asset_id=asset_id,
        maker_address="0x" + "11" * 20,
        taker_order_id="taker-order-1",
        side="BUY",
        trader_side="MAKER",
        price=price,
        size=matched_amount,
        outcome="YES",
        status="CONFIRMED",
        fee_rate_bps="100",
        bucket_index=0,
        transaction_hash="0x" + "22" * 32,
        maker_orders=(
            MakerOrderReadPayload(
                order_id=order_id,
                maker_address="0x" + "11" * 20,
                matched_amount=matched_amount,
                price=price,
                fee_rate_bps="100",
                asset_id=asset_id,
                outcome="YES",
                outcome_index=0,
                side="BUY",
            ),
        ),
        match_time="1787673600",
        last_update="1787673601",
    )


class RecordingAccountReader:
    account_fingerprint = ACCOUNT_FINGERPRINT

    def __init__(
        self,
        *,
        orders: OrdersReadPayload,
        trades: TradesReadPayload | None = None,
        balance: BalanceAllowancePayload | None = None,
        order: RestResult | None = None,
    ) -> None:
        self.orders = read_result(RouteKey.READ_OPEN_ORDERS, orders)
        self.trades = read_result(
            RouteKey.READ_TRADES,
            TradesReadPayload(kind="TRADES_READ", items=()) if trades is None else trades,
        )
        self.balance = read_result(
            RouteKey.READ_BALANCE_ALLOWANCE,
            BalanceAllowancePayload(
                kind="BALANCE_ALLOWANCE",
                balance="10000000",
                allowances=(),
            )
            if balance is None
            else balance,
        )
        self.order = order
        self.calls: list[RouteKey] = []

    def read_open_orders(self) -> RestResult:
        self.calls.append(RouteKey.READ_OPEN_ORDERS)
        return self.orders

    def read_trades(self) -> RestResult:
        self.calls.append(RouteKey.READ_TRADES)
        return self.trades

    def read_balance_allowance(self) -> RestResult:
        self.calls.append(RouteKey.READ_BALANCE_ALLOWANCE)
        return self.balance

    def read_order(self, venue_order_id: str) -> RestResult:
        del venue_order_id
        self.calls.append(RouteKey.READ_ORDER)
        if self.order is None:
            raise AssertionError("order read was not configured")
        return self.order


class CancellationSigner(FakeSigner):
    def __init__(self, result: RestResult) -> None:
        super().__init__()
        self.result = result

    def cancel(self, intent, envelope, venue_order_id, evidence):  # type: ignore[no-untyped-def]
        del intent, envelope, venue_order_id, evidence
        self.cancel_calls += 1
        return self.result


def cancellation_result(order_id: str = "venue-order-1") -> RestResult:
    payload = CancellationPayload(
        kind="CANCELLATION",
        order_id=order_id,
        confirmation_required=True,
    )
    recovery_required, kill_required = expected_route_result_flags(
        route=RouteKey.CANCEL_ORDER,
        code=RestCode.CANCEL_ACKNOWLEDGED,
        payload=payload,
    )
    return RestResult(
        route=RouteKey.CANCEL_ORDER,
        code=RestCode.CANCEL_ACKNOWLEDGED,
        observed_at=NOW + timedelta(milliseconds=250),
        raw_body_hash=HASHES[11],
        request_body_hash=HASHES[12],
        attempts=1,
        recovery_required=recovery_required,
        kill_required=kill_required,
        payload=payload,
    )


def cancellation_failure(code: RestCode) -> RestResult:
    recovery_required, kill_required = expected_route_result_flags(
        route=RouteKey.CANCEL_ORDER,
        code=code,
        payload=None,
    )
    return RestResult(
        route=RouteKey.CANCEL_ORDER,
        code=code,
        observed_at=NOW + timedelta(milliseconds=250),
        raw_body_hash=HASHES[11],
        request_body_hash=HASHES[12],
        attempts=1,
        recovery_required=recovery_required,
        kill_required=kill_required,
        payload=None,
    )


def recovering_coordinator(
    store: PredictionMarketStore,
    reader: RecordingAccountReader,
    signer: FakeSigner,
    plan: LiveExecutionPlan,
) -> ExecutionCoordinator:
    return ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=signer,
        account_reader=reader,
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW + timedelta(milliseconds=500),
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )


@pytest.mark.parametrize("response_was_accepted", [False, True])
def test_response_lost_before_or_after_acceptance_has_identical_conservative_recovery(
    store: PredictionMarketStore,
    response_was_accepted: bool,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    first_signer = FakeSigner(submit_result(RestCode.ORDER_OUTCOME_UNKNOWN))
    first = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=first_signer,
        account_reader=RecordingAccountReader(
            orders=OrdersReadPayload(kind="ORDERS_READ", items=())
        ),
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )
    assert first.submit_intent(intent).state is VenueOrderState.UNKNOWN
    similar_order = order_read(
        order_id="unbound-venue-order",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
    )
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(
            kind="ORDERS_READ",
            items=(similar_order,) if response_was_accepted else (),
        )
    )
    recovery_signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))

    report = recovering_coordinator(
        store,
        reader,
        recovery_signer,
        plan,
    ).recover_account(ACCOUNT_FINGERPRINT)

    assert isinstance(report, RecoveryReport)
    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.reads == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )
    assert report.blocked_intent_ids == (intent.intent_id,)
    assert report.recovered_intent_ids == ()
    assert report.submit_attempts == 0
    assert reader.calls == list(report.reads)
    assert recovery_signer.sign_calls == recovery_signer.submit_calls == 0
    latest = store.latest_order_state(intent.intent_id)
    assert latest is not None
    assert latest.normalized_state is VenueOrderState.UNKNOWN
    assert tuple(
        item.trigger
        for item in store.verified_kill_switch_events(
            ACCOUNT_FINGERPRINT,
            NOW + timedelta(seconds=4),
        )
    ) == (RestCode.ORDER_OUTCOME_UNKNOWN.value,)


def test_known_venue_id_and_exact_order_read_recovers_a_full_fill(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    first = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=FakeSigner(submit_result(RestCode.ORDER_ACK_DELAYED)),
        account_reader=RecordingAccountReader(
            orders=OrdersReadPayload(kind="ORDERS_READ", items=())
        ),
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )
    assert first.submit_intent(intent).state is VenueOrderState.ACK_DELAYED
    exact_order = order_read(
        order_id="venue-order-1",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
    )
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(kind="ORDERS_READ", items=(exact_order,))
    )
    recovery_signer = FakeSigner()

    report = recovering_coordinator(
        store,
        reader,
        recovery_signer,
        plan,
    ).recover_account(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    assert report.recovered_intent_ids == (intent.intent_id,)
    assert report.blocked_intent_ids == ()
    assert store.latest_order_state(intent.intent_id).normalized_state is VenueOrderState.FILLED  # type: ignore[union-attr]
    assert recovery_signer.sign_calls == recovery_signer.submit_calls == 0


def test_unique_maker_trade_correlation_persists_trade_and_recovers_fill(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    first = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=FakeSigner(submit_result(RestCode.ORDER_ACK_DELAYED)),
        account_reader=RecordingAccountReader(
            orders=OrdersReadPayload(kind="ORDERS_READ", items=())
        ),
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )
    first.submit_intent(intent)
    trade = confirmed_maker_trade(
        order_id="venue-order-1",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
    )
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(kind="ORDERS_READ", items=()),
        trades=TradesReadPayload(kind="TRADES_READ", items=(trade,)),
    )

    report = recovering_coordinator(
        store,
        reader,
        FakeSigner(),
        plan,
    ).recover_account(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    persisted = store.verified_venue_trade_events_for_intent(
        intent.intent_id,
        NOW + timedelta(seconds=4),
    )
    assert len(persisted) == 1
    assert persisted[0].venue_trade_id == "trade-1"
    assert persisted[0].venue_order_id == "venue-order-1"
    assert persisted[0].intent_id == intent.intent_id
    assert store.latest_order_state(intent.intent_id).normalized_state is VenueOrderState.FILLED  # type: ignore[union-attr]


def test_multiple_distinct_maker_trades_for_one_known_order_recover_exact_total(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    first = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=FakeSigner(submit_result(RestCode.ORDER_ACK_DELAYED)),
        account_reader=RecordingAccountReader(
            orders=OrdersReadPayload(kind="ORDERS_READ", items=())
        ),
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )
    first.submit_intent(intent)
    trades = TradesReadPayload(
        kind="TRADES_READ",
        items=(
            confirmed_maker_trade(
                order_id="venue-order-1",
                asset_id=intent.token_id,
                price=str(intent.limit_price),
                matched_amount="4",
                trade_id="trade-1",
            ),
            confirmed_maker_trade(
                order_id="venue-order-1",
                asset_id=intent.token_id,
                price=str(intent.limit_price),
                matched_amount="6",
                trade_id="trade-2",
            ),
        ),
    )
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(kind="ORDERS_READ", items=()),
        trades=trades,
    )

    report = recovering_coordinator(
        store,
        reader,
        FakeSigner(),
        plan,
    ).recover_account(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    persisted = store.verified_venue_trade_events_for_intent(
        intent.intent_id,
        NOW + timedelta(seconds=4),
    )
    assert tuple(item.venue_trade_id for item in persisted) == ("trade-1", "trade-2")
    assert store.latest_order_state(intent.intent_id).normalized_state is VenueOrderState.FILLED  # type: ignore[union-attr]


def test_ambiguous_duplicate_maker_correlation_stays_blocked_without_guessing(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    first = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=FakeSigner(submit_result(RestCode.ORDER_ACK_DELAYED)),
        account_reader=RecordingAccountReader(
            orders=OrdersReadPayload(kind="ORDERS_READ", items=())
        ),
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )
    first.submit_intent(intent)
    first_trade = confirmed_maker_trade(
        order_id="venue-order-1",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
        trade_id="trade-1",
    )
    second_trade = confirmed_maker_trade(
        order_id="venue-order-1",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
        trade_id="trade-1",
    )
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(kind="ORDERS_READ", items=()),
        trades=TradesReadPayload(
            kind="TRADES_READ",
            items=(first_trade, second_trade),
        ),
    )

    report = recovering_coordinator(
        store,
        reader,
        FakeSigner(),
        plan,
    ).recover_account(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)
    assert (
        store.verified_venue_trade_events_for_intent(
            intent.intent_id,
            NOW + timedelta(seconds=4),
        )
        == ()
    )
    assert (
        store.latest_order_state(intent.intent_id).normalized_state is VenueOrderState.ACK_DELAYED
    )  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("confirming_status", "expected_code", "expected_state"),
    [
        ("CANCELED", CoordinatorCode.RECOVERY_COMPLETE, VenueOrderState.CANCELLED),
        ("LIVE", CoordinatorCode.RECOVERY_BLOCKED, VenueOrderState.CANCEL_PENDING),
    ],
)
def test_cancellation_retries_only_bound_order_and_requires_confirming_order_read(
    store: PredictionMarketStore,
    confirming_status: str,
    expected_code: CoordinatorCode,
    expected_state: VenueOrderState,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    first = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=FakeSigner(submit_result(RestCode.ORDER_ACK_LIVE_UNEXPECTED)),
        account_reader=RecordingAccountReader(
            orders=OrdersReadPayload(kind="ORDERS_READ", items=())
        ),
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )
    assert first.submit_intent(intent).state is VenueOrderState.ACK_LIVE_UNEXPECTED
    assert (
        first.apply_order_event(
            intent,
            lifecycle_event(
                intent,
                VenueOrderState.CANCEL_PENDING,
                received_at=NOW + timedelta(milliseconds=100),
            ),
        ).state
        is VenueOrderState.CANCEL_PENDING
    )
    open_order = order_read(
        order_id="venue-order-1",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
        size_matched="0",
        status="LIVE",
    )
    confirming_order = order_read(
        order_id="venue-order-1",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
        size_matched="0",
        status=confirming_status,
    )
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(kind="ORDERS_READ", items=(open_order,)),
        order=read_result(RouteKey.READ_ORDER, confirming_order),
    )
    signer = CancellationSigner(cancellation_result())

    report = recovering_coordinator(store, reader, signer, plan).recover_account(
        ACCOUNT_FINGERPRINT
    )

    assert report.code is expected_code
    assert report.reads == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
        RouteKey.READ_ORDER,
    )
    assert reader.calls == list(report.reads)
    assert signer.cancel_calls == 1
    assert signer.sign_calls == signer.submit_calls == 0
    latest = store.latest_order_state(intent.intent_id)
    assert latest is not None
    assert latest.normalized_state is expected_state


def test_ambiguous_cancel_outcome_preserves_exact_reason_without_confirming_read(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    first = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED)),
        account_reader=RecordingAccountReader(
            orders=OrdersReadPayload(kind="ORDERS_READ", items=())
        ),
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )
    first.submit_intent(intent)
    first.apply_order_event(
        intent,
        lifecycle_event(
            intent,
            VenueOrderState.CANCEL_PENDING,
            received_at=NOW + timedelta(milliseconds=100),
        ),
    )
    open_order = order_read(
        order_id="venue-order-1",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
        size_matched="0",
        status="LIVE",
    )
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(kind="ORDERS_READ", items=(open_order,))
    )
    signer = CancellationSigner(cancellation_failure(RestCode.CANCEL_OUTCOME_UNKNOWN))

    report = recovering_coordinator(store, reader, signer, plan).recover_account(
        ACCOUNT_FINGERPRINT
    )

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.kill_reason == RestCode.CANCEL_OUTCOME_UNKNOWN.value
    assert report.reads == (
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    )
    assert signer.cancel_calls == 1
    assert (
        store.latest_order_state(intent.intent_id).normalized_state
        is VenueOrderState.CANCEL_PENDING
    )  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "health",
    [
        UserStreamHealth.connected(NOW, monotonic_at=0.0).on_disconnect(
            NOW + timedelta(milliseconds=10),
            monotonic_at=0.1,
        ),
        UserStreamHealth.connected(NOW, monotonic_at=0.0).on_protocol_error(
            NOW + timedelta(milliseconds=10),
            monotonic_at=0.1,
        ),
    ],
    ids=["disconnect", "gap"],
)
def test_disconnect_or_gap_requires_exact_reads_and_preserves_stream_kill(
    store: PredictionMarketStore,
    health: UserStreamHealth,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    first = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED)),
        account_reader=RecordingAccountReader(
            orders=OrdersReadPayload(kind="ORDERS_READ", items=())
        ),
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )
    first.submit_intent(intent)
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))

    report = recovering_coordinator(
        store,
        reader,
        FakeSigner(),
        plan,
    ).recover_account(ACCOUNT_FINGERPRINT, stream_health=health)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.kill_reason == health.kill_reason
    assert reader.calls == [
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
    ]


def test_missed_heartbeat_requires_exact_reads_and_preserves_uncertainty_kill(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    first = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED)),
        account_reader=RecordingAccountReader(
            orders=OrdersReadPayload(kind="ORDERS_READ", items=())
        ),
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )
    first.submit_intent(intent)
    heartbeat = HeartbeatState.uncertain(
        observed_at=NOW + timedelta(milliseconds=10),
        previous_heartbeat_id="heartbeat-1",
        evidence_hashes=(HASHES[9],),
    )
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))

    report = recovering_coordinator(
        store,
        reader,
        FakeSigner(),
        plan,
    ).recover_account(ACCOUNT_FINGERPRINT, heartbeat_state=heartbeat)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.kill_reason == "HEARTBEAT_CANCELLATION_UNCERTAIN"
    assert reader.calls == list(heartbeat.required_reads)


def test_startup_scans_expired_submitting_intent_and_blocks_without_replay_state(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)

    class ProcessCrash(BaseException):
        pass

    class CrashingSigner(FakeSigner):
        def submit(self, candidate, envelope, evidence):  # type: ignore[no-untyped-def]
            del candidate, envelope, evidence
            self.submit_calls += 1
            raise ProcessCrash

    with pytest.raises(ProcessCrash):
        ExecutionCoordinator(
            store=store,
            preflight=FakePreflight(preflight_evidence(plan)),
            signer=CrashingSigner(),
            account_reader=RecordingAccountReader(
                orders=OrdersReadPayload(kind="ORDERS_READ", items=())
            ),
            authority=FakeAuthority(),
            account_fingerprint=ACCOUNT_FINGERPRINT,
            clock=lambda: NOW,
            test_only_kill_state=KillState(engaged=False, latest_event=None),
        ).submit_intent(intent)
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))
    restart_signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    restart = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=restart_signer,
        account_reader=reader,
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW + timedelta(seconds=6),
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )

    report = restart.recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)
    assert restart.new_intents_blocked
    assert restart_signer.sign_calls == restart_signer.submit_calls == 0
    assert reader.calls == list(report.reads)


@pytest.mark.parametrize(
    "trade_state",
    [
        VenueTradeState.MATCHED_NOT_BROADCASTED,
        VenueTradeState.MATCHED,
        VenueTradeState.MINED,
        VenueTradeState.RETRYING,
        VenueTradeState.FAILED,
    ],
)
def test_startup_blocks_settlement_retry_or_failure_boundaries(
    store: PredictionMarketStore,
    trade_state: VenueTradeState,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    first = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED)),
        account_reader=RecordingAccountReader(
            orders=OrdersReadPayload(kind="ORDERS_READ", items=())
        ),
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )
    first.submit_intent(intent)
    store.append_venue_trade_event(
        VenueTradeEvent(
            schema_version=1,
            trade_event_id=uuid4(),
            venue="polymarket",
            raw_event_hash=HASHES[11],
            source_channel="user_stream",
            venue_trade_id="trade-settlement-1",
            venue_order_id="venue-order-1",
            intent_id=intent.intent_id,
            original_venue_state=trade_state.value,
            normalized_state=trade_state,
            terminal=trade_state is VenueTradeState.FAILED,
            venue_timestamp=NOW + timedelta(milliseconds=100),
            received_at=NOW + timedelta(milliseconds=100),
            sequence_number=None,
            protocol_version=intent.protocol_version,
        )
    )
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))
    restart = recovering_coordinator(store, reader, FakeSigner(), plan)

    report = restart.recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)
    assert report.kill_reason == f"SETTLEMENT_{trade_state.value}"


def test_startup_blocks_account_with_incomplete_reconciliation_even_without_intents(
    store: PredictionMarketStore,
) -> None:
    store.append_live_reconciliation(
        LiveReconciliation(
            schema_version=1,
            reconciliation_id=uuid4(),
            account_fingerprint=ACCOUNT_FINGERPRINT,
            observed_at=NOW + timedelta(milliseconds=100),
            complete=False,
            differences=("balance_mismatch",),
            evidence_hashes=(HASHES[9],),
            next_action="manual_review",
        )
    )
    plan = execution_plan()
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))
    restart = recovering_coordinator(store, reader, FakeSigner(), plan)

    report = restart.recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == ()
    assert report.kill_reason == "RECONCILIATION_INCOMPLETE"
    assert restart.new_intents_blocked


def test_startup_store_corruption_engages_account_kill_without_trusting_bad_identity(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    store.append_live_execution_plan(plan)
    store._connection.execute(
        "UPDATE live_execution_plans SET record_hash = ? WHERE plan_id = ?",
        ["0" * 64, plan.plan_id],
    )
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))
    restart = recovering_coordinator(store, reader, FakeSigner(), plan)

    report = restart.recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == ()
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value
    assert restart.new_intents_blocked


def test_rate_limited_authoritative_read_blocks_and_makes_no_submit_retry(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)

    class ProcessCrash(BaseException):
        pass

    class CrashingSigner(FakeSigner):
        def submit(self, candidate, envelope, evidence):  # type: ignore[no-untyped-def]
            del candidate, envelope, evidence
            self.submit_calls += 1
            raise ProcessCrash

    with pytest.raises(ProcessCrash):
        ExecutionCoordinator(
            store=store,
            preflight=FakePreflight(preflight_evidence(plan)),
            signer=CrashingSigner(),
            account_reader=RecordingAccountReader(
                orders=OrdersReadPayload(kind="ORDERS_READ", items=())
            ),
            authority=FakeAuthority(),
            account_fingerprint=ACCOUNT_FINGERPRINT,
            clock=lambda: NOW,
            test_only_kill_state=KillState(engaged=False, latest_event=None),
        ).submit_intent(intent)
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))
    reader.orders = failed_read_result(RouteKey.READ_OPEN_ORDERS, RestCode.RATE_LIMITED)
    signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))

    report = recovering_coordinator(store, reader, signer, plan).recover_account(
        ACCOUNT_FINGERPRINT
    )

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.kill_reason == RestCode.RATE_LIMITED.value
    assert reader.calls == list(report.reads)
    assert signer.sign_calls == signer.submit_calls == 0


def test_recovery_rejects_wrong_account_before_any_port_read(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))
    reader.account_fingerprint = "9" * 64
    executor = recovering_coordinator(store, reader, FakeSigner(), plan)

    with pytest.raises(ValueError, match="RECOVERY_ACCOUNT_MISMATCH"):
        executor.recover_account(ACCOUNT_FINGERPRINT)

    assert reader.calls == []
