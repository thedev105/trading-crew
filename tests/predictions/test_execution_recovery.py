from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from polytrading.predictions.execution.coordinator import (
    CoordinatorCode,
    ExecutionCoordinator,
    PostFillDecision,
    RecoveryReport,
)
from polytrading.predictions.execution.kill_switch import KillState
from polytrading.predictions.execution.ledger import (
    AuthoritativeTradeEconomics,
    postings_for_confirmed_trades,
)
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    LiveExecutionPlan,
    LiveLedgerPosting,
    LiveReconciliation,
    VenueOrderEvent,
    VenueOrderState,
    VenueTradeEvent,
    VenueTradeState,
)
from polytrading.predictions.execution.reconciliation import (
    AssetAmountObservation,
    RecentTradeObservation,
    SettlementObservation,
    VenueAccountSnapshot,
    reconcile_live_account,
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
    engage_durable_kill,
    execution_intent,
    execution_plan,
    lifecycle_event,
    preflight_evidence,
    submit_result,
)

SIGNER_ADDRESS = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
TAIL_ORDER_HASH = f"{401:064x}"
TAIL_TRADE_HASH = f"{402:064x}"


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
        observed_at=NOW + timedelta(milliseconds=500),
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
        observed_at=NOW + timedelta(milliseconds=500),
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
    maker_address: str = SIGNER_ADDRESS,
    order_type: str = "FAK",
) -> OrderReadPayload:
    return OrderReadPayload(
        kind="ORDER_READ",
        id=order_id,
        market="condition-1",
        asset_id=asset_id,
        maker_address=maker_address,
        side="BUY",
        price=price,
        original_size=original_size,
        size_matched=size_matched,
        outcome="YES",
        order_type=order_type,
        status=status,
        associate_trades=(),
        created_at="1787673600",
        expiration="0",
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
        maker_address=SIGNER_ADDRESS,
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
                maker_address=SIGNER_ADDRESS,
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


def confirmed_taker_trade(
    *,
    order_id: str,
    asset_id: str,
    price: str,
    matched_amount: str = "10",
    trade_id: str = "trade-taker-1",
) -> TradeReadPayload:
    return TradeReadPayload(
        kind="TRADE_READ",
        id=trade_id,
        market="condition-1",
        asset_id=asset_id,
        maker_address="0x" + "33" * 20,
        taker_order_id=order_id,
        side="BUY",
        trader_side="TAKER",
        price=price,
        size=matched_amount,
        outcome="YES",
        status="CONFIRMED",
        fee_rate_bps="100",
        bucket_index=0,
        transaction_hash="0x" + "44" * 32,
        maker_orders=(),
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
        observed_at=NOW + timedelta(milliseconds=500),
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
        observed_at=NOW + timedelta(milliseconds=500),
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


def test_recovery_snapshots_each_read_before_later_callback_alias_mutation(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()

    class AliasingReader(RecordingAccountReader):
        def read_trades(self) -> RestResult:
            object.__setattr__(self.orders, "route", RouteKey.READ_TRADES)
            return super().read_trades()

    reader = AliasingReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))

    report = recovering_coordinator(store, reader, FakeSigner(), plan).recover_account(
        ACCOUNT_FINGERPRINT
    )

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    assert reader.calls == list(report.reads)


def test_recovery_accepts_observations_inside_each_natural_read_interval(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))
    reader.orders = reader.orders.model_copy(
        update={"observed_at": NOW + timedelta(milliseconds=200)}
    )
    reader.trades = reader.trades.model_copy(
        update={"observed_at": NOW + timedelta(milliseconds=400)}
    )
    reader.balance = reader.balance.model_copy(
        update={"observed_at": NOW + timedelta(milliseconds=600)}
    )
    samples = iter(NOW + timedelta(milliseconds=value) for value in (100, 300, 300, 500, 500, 700))
    last = NOW + timedelta(milliseconds=700)

    def advancing_clock() -> datetime:
        return next(samples, last)

    executor = ExecutionCoordinator(
        store=store,
        preflight=FakePreflight(preflight_evidence(plan)),
        signer=FakeSigner(),
        account_reader=reader,
        authority=FakeAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=advancing_clock,
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )

    report = executor.recover_account(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE


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


@pytest.mark.parametrize(
    "order_update",
    [
        {"status": "LIVE"},
        {"status": "CANCELED"},
        {"status": "UNKNOWN"},
        {"maker_address": "0x" + "44" * 20},
    ],
)
def test_known_order_read_rejects_status_or_maker_contradictions_without_persistence(
    store: PredictionMarketStore,
    order_update: dict[str, str],
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
    exact = order_read(
        order_id="venue-order-1",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
    ).model_copy(update=order_update)
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=(exact,)))

    report = recovering_coordinator(store, reader, FakeSigner(), plan).recover_account(
        ACCOUNT_FINGERPRINT
    )

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert tuple(
        event.normalized_state
        for event in store.verified_venue_order_events_for_intent(
            intent.intent_id,
            NOW + timedelta(seconds=1),
        )
    ) == (VenueOrderState.SUBMITTING, VenueOrderState.ACK_DELAYED)


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


@pytest.mark.parametrize(
    ("matched_amount", "expected_state"),
    [("4", VenueOrderState.PARTIALLY_FILLED), ("10", VenueOrderState.FILLED)],
)
def test_exact_known_taker_order_recovers_partial_or_full_fill(
    store: PredictionMarketStore,
    matched_amount: str,
    expected_state: VenueOrderState,
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
    trade = confirmed_taker_trade(
        order_id="venue-order-1",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
        matched_amount=matched_amount,
    )
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(kind="ORDERS_READ", items=()),
        trades=TradesReadPayload(kind="TRADES_READ", items=(trade,)),
    )

    report = recovering_coordinator(store, reader, FakeSigner(), plan).recover_account(
        ACCOUNT_FINGERPRINT
    )

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    assert report.recovered_intent_ids == (intent.intent_id,)
    persisted = store.verified_venue_trade_events_for_intent(
        intent.intent_id,
        NOW + timedelta(seconds=4),
    )
    assert tuple(event.venue_trade_id for event in persisted) == ("trade-taker-1",)
    assert store.latest_order_state(intent.intent_id).normalized_state is expected_state  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "conflicting_update",
    [
        {"size": "5"},
        {"taker_order_id": "venue-order-1"},
    ],
)
def test_conflicting_top_level_and_maker_trade_fields_persist_nothing(
    store: PredictionMarketStore,
    conflicting_update: dict[str, str],
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
    valid = confirmed_maker_trade(
        order_id="venue-order-1",
        asset_id=intent.token_id,
        price=str(intent.limit_price),
        matched_amount="4",
    )
    conflicting = valid.model_copy(update=conflicting_update)
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(kind="ORDERS_READ", items=()),
        trades=TradesReadPayload(kind="TRADES_READ", items=(conflicting,)),
    )

    report = recovering_coordinator(store, reader, FakeSigner(), plan).recover_account(
        ACCOUNT_FINGERPRINT
    )

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
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
    ("confirming_update", "expected_code", "expected_state"),
    [
        ({}, CoordinatorCode.RECOVERY_COMPLETE, VenueOrderState.CANCELLED),
        ({"status": "LIVE"}, CoordinatorCode.RECOVERY_BLOCKED, VenueOrderState.CANCEL_PENDING),
        (
            {"maker_address": "0x" + "44" * 20},
            CoordinatorCode.RECOVERY_BLOCKED,
            VenueOrderState.CANCEL_PENDING,
        ),
        (
            {"original_size": "11"},
            CoordinatorCode.RECOVERY_BLOCKED,
            VenueOrderState.CANCEL_PENDING,
        ),
        (
            {"order_type": "FOK"},
            CoordinatorCode.RECOVERY_BLOCKED,
            VenueOrderState.CANCEL_PENDING,
        ),
    ],
)
def test_cancellation_retries_only_bound_order_and_requires_confirming_order_read(
    store: PredictionMarketStore,
    confirming_update: dict[str, str],
    expected_code: CoordinatorCode,
    expected_state: VenueOrderState,
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
    assert first.submit_intent(intent).state is VenueOrderState.ACK_MATCHED
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
        status="CANCELED",
    ).model_copy(update=confirming_update)
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


@pytest.mark.parametrize(
    ("boundary", "expected_cancel_calls"),
    [("evidence", 0), ("authority", 0), ("cancel_result", 1)],
)
def test_durable_kill_wins_at_every_cancel_callback_boundary(
    store: PredictionMarketStore,
    boundary: str,
    expected_cancel_calls: int,
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
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(
            kind="ORDERS_READ",
            items=(
                order_read(
                    order_id="venue-order-1",
                    asset_id=intent.token_id,
                    price=str(intent.limit_price),
                    size_matched="0",
                    status="LIVE",
                ),
            ),
        ),
        order=read_result(
            RouteKey.READ_ORDER,
            order_read(
                order_id="venue-order-1",
                asset_id=intent.token_id,
                price=str(intent.limit_price),
                size_matched="0",
                status="CANCELED",
            ),
        ),
    )

    class KillingPreflight(FakePreflight):
        def validate(self, candidate, now):  # type: ignore[no-untyped-def]
            result = super().validate(candidate, now)
            if boundary == "evidence":
                engage_durable_kill(store, candidate, occurred_at=now)
            return result

    class KillingAuthority(FakeAuthority):
        def snapshot(self, candidate, evidence, operation, now):  # type: ignore[no-untyped-def]
            result = super().snapshot(candidate, evidence, operation, now)
            if boundary == "authority":
                engage_durable_kill(store, candidate, occurred_at=now)
            return result

    class KillingSigner(CancellationSigner):
        def cancel(self, candidate, envelope, order_id, evidence):  # type: ignore[no-untyped-def]
            result = super().cancel(candidate, envelope, order_id, evidence)
            if boundary == "cancel_result":
                engage_durable_kill(store, intent, occurred_at=NOW + timedelta(milliseconds=500))
            return result

    signer = KillingSigner(cancellation_result())
    executor = ExecutionCoordinator(
        store=store,
        preflight=KillingPreflight(preflight_evidence(plan)),
        signer=signer,
        account_reader=reader,
        authority=KillingAuthority(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW + timedelta(milliseconds=500),
        test_only_kill_state=KillState(engaged=False, latest_event=None),
    )

    report = executor.recover_account(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert signer.cancel_calls == expected_cancel_calls
    assert RouteKey.READ_ORDER not in reader.calls
    assert (
        store.latest_order_state(intent.intent_id).normalized_state
        is VenueOrderState.CANCEL_PENDING
    )  # type: ignore[union-attr]


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


def test_startup_blocks_intent_only_crash_during_sign_without_replay(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)

    class ProcessCrash(BaseException):
        pass

    class CrashingSigner(FakeSigner):
        def sign(self, candidate, evidence):  # type: ignore[no-untyped-def]
            del candidate, evidence
            self.sign_calls += 1
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
    assert store.verified_execution_intent(intent.intent_id) == intent
    assert store.verified_signed_order_envelope(intent.intent_id) is None
    assert store.latest_order_state(intent.intent_id) is None
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))
    restart_signer = FakeSigner(submit_result(RestCode.ORDER_ACK_MATCHED))
    restart = recovering_coordinator(store, reader, restart_signer, plan)

    report = restart.recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value
    assert restart.new_intents_blocked
    assert restart_signer.sign_calls == restart_signer.submit_calls == 0


@pytest.mark.parametrize(
    "decision",
    [
        PostFillDecision.CONTINUE_FROZEN_PLAN,
        PostFillDecision.FROZEN_UNWIND,
        PostFillDecision.HALT_EXPOSED,
        None,
    ],
    ids=["continue", "unwind", "halt", "process-crash"],
)
def test_startup_conservatively_blocks_every_persisted_first_fill(
    store: PredictionMarketStore,
    decision: PostFillDecision | None,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    preflight = FakePreflight(preflight_evidence(plan))
    if decision is not None:
        preflight.decision = decision
    else:

        class ProcessCrash(BaseException):
            pass

        def crash(*args: object) -> PostFillDecision:
            del args
            preflight.revalidate_calls += 1
            raise ProcessCrash

        preflight.revalidate_after_fill = crash  # type: ignore[method-assign]
    first = ExecutionCoordinator(
        store=store,
        preflight=preflight,
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
    if decision is None:
        with pytest.raises(ProcessCrash):
            first.apply_order_event(
                intent,
                lifecycle_event(
                    intent,
                    VenueOrderState.PARTIALLY_FILLED,
                    received_at=NOW + timedelta(milliseconds=100),
                ),
            )
    else:
        first.apply_order_event(
            intent,
            lifecycle_event(
                intent,
                VenueOrderState.PARTIALLY_FILLED,
                received_at=NOW + timedelta(milliseconds=100),
            ),
        )
    assert preflight.revalidate_calls == 1
    reader = RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=()))
    restart_signer = FakeSigner()

    report = recovering_coordinator(
        store,
        reader,
        restart_signer,
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)
    assert restart_signer.sign_calls == restart_signer.submit_calls == 0
    assert preflight.revalidate_calls == 1


def test_startup_blocks_any_prior_fill_even_when_latest_order_state_is_terminal(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    intent = execution_intent(plan)
    executor = ExecutionCoordinator(
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
    executor.submit_intent(intent)
    executor.apply_order_event(
        intent,
        lifecycle_event(
            intent,
            VenueOrderState.PARTIALLY_FILLED,
            received_at=NOW + timedelta(milliseconds=100),
        ),
    )
    executor.apply_order_event(
        intent,
        lifecycle_event(
            intent,
            VenueOrderState.CANCEL_PENDING,
            received_at=NOW + timedelta(milliseconds=200),
        ),
    )
    executor.apply_order_event(
        intent,
        lifecycle_event(
            intent,
            VenueOrderState.CANCELLED,
            received_at=NOW + timedelta(milliseconds=300),
        ),
    )

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)


def test_cancel_ack_is_durable_before_confirmation_and_never_reissued_after_restart(
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

    class ProcessCrash(BaseException):
        pass

    class CrashingReader(RecordingAccountReader):
        def read_order(self, venue_order_id: str) -> RestResult:
            del venue_order_id
            self.calls.append(RouteKey.READ_ORDER)
            raise ProcessCrash

    crash_reader = CrashingReader(
        orders=OrdersReadPayload(
            kind="ORDERS_READ",
            items=(
                order_read(
                    order_id="venue-order-1",
                    asset_id=intent.token_id,
                    price=str(intent.limit_price),
                    size_matched="0",
                    status="LIVE",
                ),
            ),
        )
    )
    first_cancel = CancellationSigner(cancellation_result())
    with pytest.raises(ProcessCrash):
        recovering_coordinator(store, crash_reader, first_cancel, plan).recover_account(
            ACCOUNT_FINGERPRINT
        )
    assert first_cancel.cancel_calls == 1
    latest = store.latest_order_state(intent.intent_id)
    assert latest is not None
    assert latest.normalized_state is VenueOrderState.CANCEL_PENDING
    assert latest.original_venue_state == RestCode.CANCEL_ACKNOWLEDGED.value
    assert latest.source_channel == "recovery_cancel_ack"
    assert latest.lineage_hashes == tuple(sorted((HASHES[11], HASHES[12])))

    confirming = read_result(
        RouteKey.READ_ORDER,
        order_read(
            order_id="venue-order-1",
            asset_id=intent.token_id,
            price=str(intent.limit_price),
            size_matched="0",
            status="CANCELED",
        ),
    )
    restart_reader = RecordingAccountReader(
        orders=OrdersReadPayload(kind="ORDERS_READ", items=()),
        order=confirming,
    )
    restart_signer = CancellationSigner(cancellation_result())

    report = recovering_coordinator(
        store,
        restart_reader,
        restart_signer,
        plan,
    ).recover_account(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    assert report.recovered_intent_ids == (intent.intent_id,)
    assert restart_signer.cancel_calls == 0
    assert restart_reader.calls.count(RouteKey.READ_ORDER) == 1
    assert store.latest_order_state(intent.intent_id).normalized_state is VenueOrderState.CANCELLED  # type: ignore[union-attr]


def test_forged_cancel_acknowledgement_identity_blocks_without_cancel_or_read(
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
    store.append_venue_order_event(
        lifecycle_event(
            intent,
            VenueOrderState.CANCEL_PENDING,
            received_at=NOW + timedelta(milliseconds=200),
        ).model_copy(
            update={
                "intent_id": intent.intent_id,
                "original_venue_state": RestCode.CANCEL_ACKNOWLEDGED.value,
                "source_channel": "recovery_cancel_ack",
                "raw_event_hash": HASHES[11],
                "lineage_hashes": tuple(sorted((HASHES[11], HASHES[12]))),
            }
        )
    )
    reader = RecordingAccountReader(
        orders=OrdersReadPayload(kind="ORDERS_READ", items=()),
        order=read_result(
            RouteKey.READ_ORDER,
            order_read(
                order_id="venue-order-1",
                asset_id=intent.token_id,
                price=str(intent.limit_price),
                size_matched="0",
                status="CANCELED",
            ),
        ),
    )
    signer = CancellationSigner(cancellation_result())

    report = recovering_coordinator(store, reader, signer, plan).recover_account(
        ACCOUNT_FINGERPRINT
    )

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert signer.cancel_calls == 0
    assert RouteKey.READ_ORDER not in reader.calls
    assert (
        store.latest_order_state(intent.intent_id).normalized_state
        is VenueOrderState.CANCEL_PENDING
    )  # type: ignore[union-attr]


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


@pytest.mark.parametrize(
    ("history_shape", "expected_reason"),
    [
        ("ambiguous_nonterminal_tie", CoordinatorCode.RECOVERY_BLOCKED.value),
        ("post_terminal", CoordinatorCode.RECOVERY_BLOCKED.value),
    ],
)
def test_startup_kills_ambiguous_or_contradictory_trade_history(
    store: PredictionMarketStore,
    history_shape: str,
    expected_reason: str,
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
    first_state = (
        VenueTradeState.MATCHED
        if history_shape == "ambiguous_nonterminal_tie"
        else VenueTradeState.CONFIRMED
    )
    second_state = (
        VenueTradeState.MATCHED if history_shape == "post_terminal" else (VenueTradeState.RETRYING)
    )
    for index, (state, received_ms) in enumerate(
        (
            (first_state, 100),
            (second_state, 200 if history_shape == "post_terminal" else 100),
        )
    ):
        store.append_venue_trade_event(
            VenueTradeEvent(
                schema_version=1,
                trade_event_id=UUID(f"42b33848-ff46-4c45-b9ab-0c74510687f{index}"),
                venue="polymarket",
                raw_event_hash=f"{111 + index:064x}",
                source_channel="recovery_read",
                venue_trade_id="trade-history-1",
                venue_order_id="venue-order-1",
                intent_id=intent.intent_id,
                original_venue_state=state.value,
                normalized_state=state,
                terminal=state in {VenueTradeState.CONFIRMED, VenueTradeState.FAILED},
                venue_timestamp=NOW + timedelta(milliseconds=received_ms),
                received_at=NOW + timedelta(milliseconds=received_ms),
                sequence_number=None,
                protocol_version=intent.protocol_version,
            )
        )

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)
    assert report.kill_reason == expected_reason


def test_startup_keeps_sequence_monotonicity_independent_across_trade_ids(
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
    for index in range(2):
        store.append_venue_trade_event(
            VenueTradeEvent(
                schema_version=1,
                trade_event_id=UUID(f"42b33848-ff46-4c45-b9ab-0c74510687f{index}"),
                venue="polymarket",
                raw_event_hash=f"{111 + index:064x}",
                source_channel="recovery_read",
                venue_trade_id=f"trade-independent-{index}",
                venue_order_id="venue-order-1",
                intent_id=intent.intent_id,
                original_venue_state=VenueTradeState.MATCHED.value,
                normalized_state=VenueTradeState.MATCHED,
                terminal=False,
                venue_timestamp=NOW + timedelta(milliseconds=100 + index),
                received_at=NOW + timedelta(milliseconds=100 + index),
                sequence_number=1,
                protocol_version=intent.protocol_version,
            )
        )

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)
    assert report.kill_reason == "SETTLEMENT_MATCHED"


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


def test_startup_inspects_complete_trade_history_not_only_last_confirmation(
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
    for trade_id, state, offset in (
        ("trade-unresolved", VenueTradeState.MATCHED, 100),
        ("trade-later-confirmed", VenueTradeState.CONFIRMED, 200),
    ):
        store.append_venue_trade_event(
            VenueTradeEvent(
                schema_version=1,
                trade_event_id=uuid4(),
                venue="polymarket",
                raw_event_hash=HASHES[11] if offset == 100 else HASHES[12],
                source_channel="user_stream",
                venue_trade_id=trade_id,
                venue_order_id="venue-order-1",
                intent_id=intent.intent_id,
                original_venue_state=state.value,
                normalized_state=state,
                terminal=state is VenueTradeState.CONFIRMED,
                venue_timestamp=NOW + timedelta(milliseconds=offset),
                received_at=NOW + timedelta(milliseconds=offset),
                sequence_number=None,
                protocol_version=intent.protocol_version,
            )
        )
    restart = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    )

    report = restart.recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)
    assert report.kill_reason == "SETTLEMENT_MATCHED"


@pytest.mark.parametrize("coverage", ["none", "newer", "uncovered", "missing"])
def test_startup_blocks_unreconciled_or_out_of_cutoff_live_ledger_posting(
    store: PredictionMarketStore,
    coverage: str,
) -> None:
    posting_id = uuid4()
    posting_time = NOW + timedelta(milliseconds=200 if coverage == "newer" else 100)
    if coverage != "missing":
        store.append_live_ledger_posting(
            LiveLedgerPosting(
                schema_version=1,
                posting_id=posting_id,
                account_fingerprint=ACCOUNT_FINGERPRINT,
                intent_id=None,
                venue_order_id=None,
                venue_trade_id=None,
                settlement_hash=None,
                fee_hash=None,
                balance_evidence_hashes=(HASHES[9],),
                debit_account="cash",
                credit_account="position",
                asset_id="217426",
                debit_amount=Decimal("1"),
                credit_amount=Decimal("0"),
                occurred_at=posting_time,
            )
        )
    if coverage != "none":
        store.append_live_reconciliation(
            LiveReconciliation(
                schema_version=1,
                reconciliation_id=uuid4(),
                account_fingerprint=ACCOUNT_FINGERPRINT,
                observed_at=NOW + timedelta(milliseconds=150),
                complete=True,
                differences=(),
                evidence_hashes=(HASHES[10],),
                next_action=None,
                expected_posting_ids=(posting_id,) if coverage in {"newer", "missing"} else (),
            )
        )
    plan = execution_plan()
    restart = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    )

    report = restart.recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == ()
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value
    assert restart.new_intents_blocked


def _seed_reconciliation_history(
    store: PredictionMarketStore,
    *,
    posting_time: datetime = NOW + timedelta(milliseconds=150),
    related_order_id: str = "venue-order-1",
    trade_time: datetime = NOW + timedelta(milliseconds=100),
) -> tuple[LiveExecutionPlan, ExecutionIntent, LiveLedgerPosting]:
    plan = execution_plan()
    intent = execution_intent(plan)
    ExecutionCoordinator(
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
    ).submit_intent(intent)
    for trade_id, raw_hash, received_at in (
        ("trade-1", HASHES[11], trade_time),
        ("settlement-evidence", HASHES[12], trade_time),
    ):
        store.append_venue_trade_event(
            VenueTradeEvent(
                schema_version=1,
                trade_event_id=uuid4(),
                venue="polymarket",
                raw_event_hash=raw_hash,
                source_channel="recovery_read",
                venue_trade_id=trade_id,
                venue_order_id=related_order_id,
                intent_id=intent.intent_id,
                original_venue_state=VenueTradeState.CONFIRMED.value,
                normalized_state=VenueTradeState.CONFIRMED,
                terminal=True,
                venue_timestamp=received_at,
                received_at=received_at,
                sequence_number=None,
                protocol_version=intent.protocol_version,
            )
        )
    posting = LiveLedgerPosting(
        schema_version=1,
        posting_id=uuid4(),
        account_fingerprint=ACCOUNT_FINGERPRINT,
        intent_id=intent.intent_id,
        venue_order_id=related_order_id,
        venue_trade_id="trade-1",
        settlement_hash=HASHES[12],
        fee_hash=HASHES[13],
        balance_evidence_hashes=(HASHES[9],),
        debit_account="cash",
        credit_account="position",
        asset_id=intent.token_id,
        debit_amount=Decimal("1"),
        credit_amount=Decimal("0"),
        occurred_at=posting_time,
    )
    store.append_live_ledger_posting(posting)
    return plan, intent, posting


def _append_complete_reconciliation(
    store: PredictionMarketStore,
    *,
    observed_at: datetime,
    posting_ids: tuple[UUID, ...],
    order_hashes: tuple[str, ...] = (HASHES[10],),
    trade_hashes: tuple[str, ...] = (HASHES[11], HASHES[12]),
    fee_hashes: tuple[str, ...] = (HASHES[13],),
    balance_hashes: tuple[str, ...] = (HASHES[9],),
) -> None:
    store.append_live_reconciliation(
        LiveReconciliation(
            schema_version=1,
            reconciliation_id=uuid4(),
            account_fingerprint=ACCOUNT_FINGERPRINT,
            observed_at=observed_at,
            complete=True,
            differences=(),
            evidence_hashes=tuple(sorted(fee_hashes)),
            next_action=None,
            venue_order_hashes=tuple(sorted(order_hashes)),
            venue_trade_hashes=tuple(sorted(trade_hashes)),
            balance_hashes=tuple(sorted(balance_hashes)),
            expected_posting_ids=posting_ids,
        )
    )


@pytest.mark.parametrize(
    "omitted_family",
    ["order", "trade", "settlement", "fee", "balance"],
)
@pytest.mark.parametrize("malformed_position", ["first", "middle"])
@pytest.mark.parametrize("malformation", ["omitted", "cross_bound"])
def test_every_complete_reconciliation_closes_its_own_posting_evidence(
    store: PredictionMarketStore,
    omitted_family: str,
    malformed_position: str,
    malformation: str,
) -> None:
    plan, intent, posting = _seed_reconciliation_history(store)
    first_fields = {
        "order_hashes": (HASHES[10],),
        "trade_hashes": (HASHES[11], HASHES[12]),
        "fee_hashes": (HASHES[13],),
        "balance_hashes": (HASHES[9],),
    }
    missing_value: tuple[str, ...] = ()
    if malformation == "cross_bound":
        if omitted_family == "order":
            order_history = store.verified_venue_order_events_for_intent(
                intent.intent_id,
                NOW + timedelta(milliseconds=500),
            )
            missing_value = (
                next(
                    event.raw_event_hash
                    for event in order_history
                    if event.venue_order_id != posting.venue_order_id
                ),
            )
        elif omitted_family == "trade":
            missing_value = (HASHES[12],)
        elif omitted_family == "settlement":
            missing_value = (HASHES[11],)
        elif omitted_family == "fee":
            missing_value = (HASHES[9],)
        else:
            missing_value = (HASHES[13],)
    if omitted_family == "order":
        first_fields["order_hashes"] = missing_value
    elif omitted_family in {"trade", "settlement"}:
        first_fields["trade_hashes"] = missing_value
    elif omitted_family == "fee":
        first_fields["fee_hashes"] = missing_value
    else:
        first_fields["balance_hashes"] = missing_value
    if malformed_position == "middle":
        _append_complete_reconciliation(
            store,
            observed_at=NOW + timedelta(milliseconds=175),
            posting_ids=(posting.posting_id,),
        )
    _append_complete_reconciliation(
        store,
        observed_at=NOW + timedelta(milliseconds=200),
        posting_ids=(posting.posting_id,),
        **first_fields,  # type: ignore[arg-type]
    )
    _append_complete_reconciliation(
        store,
        observed_at=NOW + timedelta(milliseconds=300),
        posting_ids=(posting.posting_id,),
    )

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value


@pytest.mark.parametrize("future_reference", ["order", "trade"])
def test_complete_reconciliation_rejects_future_cross_bound_reference(
    store: PredictionMarketStore,
    future_reference: str,
) -> None:
    if future_reference == "trade":
        plan, intent, posting = _seed_reconciliation_history(
            store,
            posting_time=NOW + timedelta(milliseconds=100),
            trade_time=NOW + timedelta(milliseconds=250),
        )
        first_order_hashes = (HASHES[10],)
    else:
        plan, intent, posting = _seed_reconciliation_history(
            store,
            posting_time=NOW + timedelta(milliseconds=100),
            related_order_id="future-order",
            trade_time=NOW + timedelta(milliseconds=100),
        )
        future_order = lifecycle_event(
            intent,
            VenueOrderState.ACK_MATCHED,
            venue_order_id="future-order",
            received_at=NOW + timedelta(milliseconds=250),
        ).model_copy(update={"intent_id": intent.intent_id, "raw_event_hash": HASHES[14]})
        store.append_venue_order_event(future_order)
        first_order_hashes = ()
    _append_complete_reconciliation(
        store,
        observed_at=NOW + timedelta(milliseconds=200),
        posting_ids=(posting.posting_id,),
        order_hashes=first_order_hashes,
        trade_hashes=(),
    )
    _append_complete_reconciliation(
        store,
        observed_at=NOW + timedelta(milliseconds=300),
        posting_ids=(posting.posting_id,),
        order_hashes=(HASHES[14],) if future_reference == "order" else (HASHES[10],),
    )

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)


@pytest.mark.parametrize("checkpoint_shape", ["cumulative", "equal_time"])
def test_valid_complete_reconciliations_close_each_bounded_snapshot(
    store: PredictionMarketStore,
    checkpoint_shape: str,
) -> None:
    plan, _, _, _, first = _persist_honest_task11_checkpoint(store)
    second = first.model_copy(
        update={
            "reconciliation_id": uuid4(),
            "observed_at": NOW
            + timedelta(milliseconds=300 if checkpoint_shape == "cumulative" else 200),
        }
    )
    store.append_live_reconciliation(second)

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    assert report.blocked_intent_ids == ()


def _authoritative_economics_for_recovery(
    source_intent: ExecutionIntent,
    *,
    venue_order_id: str = "venue-order-1",
    venue_trade_id: str = "trade-1",
    trade_event_hash: str = HASHES[11],
    occurred_at: datetime = NOW + timedelta(milliseconds=100),
    information_cutoff: datetime = NOW + timedelta(milliseconds=150),
) -> AuthoritativeTradeEconomics:
    return AuthoritativeTradeEconomics(
        schema_version=1,
        account_fingerprint=source_intent.account_fingerprint,
        intent_id=source_intent.intent_id,
        venue_order_id=venue_order_id,
        venue_trade_id=venue_trade_id,
        trade_event_hash=trade_event_hash,
        cash_asset_id="USDC",
        position_asset_id=source_intent.token_id,
        side=source_intent.side,
        price=Decimal("0.51"),
        size=Decimal("10"),
        fee=Decimal("0.01"),
        cash_quantum=Decimal("0.000001"),
        position_quantum=Decimal("0.01"),
        trade_state=VenueTradeState.CONFIRMED,
        settlement_state=VenueTradeState.CONFIRMED,
        fee_hash=HASHES[13],
        settlement_hash=HASHES[12],
        source_hash=HASHES[14],
        balance_evidence_hashes=(f"{101:064x}",),
        occurred_at=occurred_at,
        information_cutoff=information_cutoff,
        protocol_version=source_intent.protocol_version,
        realized_pnl=None,
        cost_basis_evidence_hash=None,
    )


def _checkpoint_asset(
    asset_id: str,
    amount: str,
    quantum: str,
    hash_index: int,
) -> AssetAmountObservation:
    return AssetAmountObservation(
        schema_version=1,
        asset_id=asset_id,
        amount=Decimal(amount),
        quantum=Decimal(quantum),
        evidence_hash=f"{hash_index:064x}",
    )


def _persist_honest_task11_checkpoint(
    store: PredictionMarketStore,
    *,
    history_shape: str = "single",
) -> tuple[
    LiveExecutionPlan,
    ExecutionIntent,
    AuthoritativeTradeEconomics,
    tuple[LiveLedgerPosting, ...],
    LiveReconciliation,
]:
    plan = execution_plan()
    source_intent = execution_intent(plan)
    order_event = lifecycle_event(
        source_intent,
        VenueOrderState.RECONCILED,
        received_at=NOW + timedelta(milliseconds=50),
    ).model_copy(update={"intent_id": source_intent.intent_id})

    def history_event(
        *,
        state: VenueTradeState,
        event_id: str,
        raw_event_hash: str,
        received_ms: int,
        sequence_number: int | None,
    ) -> VenueTradeEvent:
        return VenueTradeEvent(
            schema_version=1,
            trade_event_id=UUID(event_id),
            venue="polymarket",
            raw_event_hash=raw_event_hash,
            source_channel="recovery_read",
            venue_trade_id="trade-1",
            venue_order_id="venue-order-1",
            intent_id=source_intent.intent_id,
            original_venue_state=state.value,
            normalized_state=state,
            terminal=state in {VenueTradeState.CONFIRMED, VenueTradeState.FAILED},
            venue_timestamp=NOW + timedelta(milliseconds=received_ms),
            received_at=NOW + timedelta(milliseconds=received_ms),
            sequence_number=sequence_number,
            protocol_version=source_intent.protocol_version,
        )

    terminal_sequence = (
        2 if history_shape == "terminal_tie" else (4 if history_shape == "progress" else 1)
    )
    confirmed = history_event(
        state=VenueTradeState.CONFIRMED,
        event_id="42b33848-ff46-4c45-b9ab-0c74510687f3",
        raw_event_hash=HASHES[11],
        received_ms=100,
        sequence_number=terminal_sequence,
    )
    if history_shape == "cutoff_equal":
        confirmed = confirmed.model_copy(update={"received_at": NOW + timedelta(milliseconds=200)})
    if history_shape in {"single", "cutoff_equal"}:
        trade_events = (confirmed,)
    elif history_shape == "progress":
        trade_events = (
            history_event(
                state=VenueTradeState.MATCHED,
                event_id="42b33848-ff46-4c45-b9ab-0c74510687f0",
                raw_event_hash=f"{111:064x}",
                received_ms=60,
                sequence_number=1,
            ),
            history_event(
                state=VenueTradeState.RETRYING,
                event_id="42b33848-ff46-4c45-b9ab-0c74510687f1",
                raw_event_hash=f"{112:064x}",
                received_ms=70,
                sequence_number=2,
            ),
            history_event(
                state=VenueTradeState.MINED,
                event_id="42b33848-ff46-4c45-b9ab-0c74510687f2",
                raw_event_hash=f"{113:064x}",
                received_ms=80,
                sequence_number=3,
            ),
            confirmed,
        )
    elif history_shape == "terminal_tie":
        trade_events = (
            history_event(
                state=VenueTradeState.MATCHED,
                event_id="42b33848-ff46-4c45-b9ab-0c74510687f0",
                raw_event_hash=f"{111:064x}",
                received_ms=100,
                sequence_number=1,
            ),
            confirmed,
        )
    elif history_shape == "nonterminal_tie":
        trade_events = (
            history_event(
                state=VenueTradeState.MATCHED,
                event_id="42b33848-ff46-4c45-b9ab-0c74510687f0",
                raw_event_hash=f"{111:064x}",
                received_ms=75,
                sequence_number=None,
            ),
            history_event(
                state=VenueTradeState.RETRYING,
                event_id="42b33848-ff46-4c45-b9ab-0c74510687f1",
                raw_event_hash=f"{112:064x}",
                received_ms=75,
                sequence_number=None,
            ),
            confirmed,
        )
    else:
        raise AssertionError(f"unknown history shape: {history_shape}")
    exact = _authoritative_economics_for_recovery(
        source_intent,
        information_cutoff=(
            NOW + timedelta(milliseconds=200)
            if history_shape == "cutoff_equal"
            else NOW + timedelta(milliseconds=150)
        ),
    )
    postings = postings_for_confirmed_trades((source_intent,), trade_events, (exact,))
    snapshot = VenueAccountSnapshot(
        schema_version=1,
        account_fingerprint=ACCOUNT_FINGERPRINT,
        cutoff_at=NOW,
        observed_at=NOW + timedelta(milliseconds=200),
        opening_cash_balances=(_checkpoint_asset("USDC", "100", "0.000001", 201),),
        current_cash_balances=(_checkpoint_asset("USDC", "94.89", "0.000001", 101),),
        opening_token_positions=(_checkpoint_asset(source_intent.token_id, "0", "0.01", 202),),
        current_token_positions=(_checkpoint_asset(source_intent.token_id, "10", "0.01", 101),),
        opening_allowances=(),
        current_allowances=(),
        opening_cumulative_fees=(_checkpoint_asset("USDC", "0", "0.000001", 203),),
        current_cumulative_fees=(_checkpoint_asset("USDC", "0.01", "0.000001", 204),),
        open_orders=(),
        recent_trades=(
            RecentTradeObservation(
                schema_version=1,
                venue_trade_id=exact.venue_trade_id,
                venue_order_id=exact.venue_order_id,
                intent_id=source_intent.intent_id,
                cash_asset_id=exact.cash_asset_id,
                position_asset_id=exact.position_asset_id,
                side=exact.side,
                state=VenueTradeState.CONFIRMED,
                trade_event_hash=exact.trade_event_hash,
                settlement_hash=exact.settlement_hash,
                fee_hash=exact.fee_hash,
                source_hash=exact.source_hash,
                balance_evidence_hashes=exact.balance_evidence_hashes,
                economics_fingerprint=exact.economics_fingerprint,
                realized_pnl=None,
                cost_basis_evidence_hash=None,
                occurred_at=exact.occurred_at,
            ),
        ),
        settlements=(
            SettlementObservation(
                schema_version=1,
                venue_trade_id=exact.venue_trade_id,
                venue_order_id=exact.venue_order_id,
                intent_id=source_intent.intent_id,
                position_asset_id=exact.position_asset_id,
                state=VenueTradeState.CONFIRMED,
                settlement_hash=exact.settlement_hash,
                evidence_hash=exact.settlement_hash,
                occurred_at=exact.occurred_at,
            ),
        ),
        opening_cash_source_hash=f"{205:064x}",
        current_cash_source_hash=f"{206:064x}",
        opening_position_source_hash=f"{207:064x}",
        current_position_source_hash=f"{208:064x}",
        opening_allowance_source_hash=f"{209:064x}",
        current_allowance_source_hash=f"{210:064x}",
        opening_fee_source_hash=f"{211:064x}",
        current_fee_source_hash=f"{212:064x}",
        open_orders_source_hash=f"{213:064x}",
        recent_trades_source_hash=f"{214:064x}",
        settlements_source_hash=f"{215:064x}",
    )
    reconciliation = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        trade_events,
        (exact,),
    )
    assert reconciliation.complete
    store.append_live_execution_plan(plan)
    store.append_execution_intent(source_intent)
    store.append_venue_order_event(order_event)
    for trade_event in trade_events:
        store.append_venue_trade_event(trade_event)
    store.append_authoritative_trade_economics(exact)
    for posting in postings:
        store.append_live_ledger_posting(posting)
    store.append_live_reconciliation(reconciliation)
    return plan, source_intent, exact, postings, reconciliation


def _stored_record(record: object) -> tuple[str, str]:
    payload = json.dumps(
        record.model_dump(mode="json"),  # type: ignore[union-attr]
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return payload, sha256(payload.encode()).hexdigest()


def test_reopened_startup_accepts_honest_task11_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "honest-checkpoint.duckdb"
    initial = PredictionMarketStore(path)
    plan, *_ = _persist_honest_task11_checkpoint(initial)
    initial.close()
    reopened = PredictionMarketStore(path)

    report = recovering_coordinator(
        reopened,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    assert report.blocked_intent_ids == ()
    reopened.close()


@pytest.mark.parametrize(
    "history_shape",
    ["progress", "terminal_tie", "nonterminal_tie"],
)
def test_reopened_startup_accepts_honest_multi_event_trade_history_checkpoint(
    tmp_path: Path,
    history_shape: str,
) -> None:
    path = tmp_path / f"honest-{history_shape}-checkpoint.duckdb"
    initial = PredictionMarketStore(path)
    plan, _, _, _, reconciliation = _persist_honest_task11_checkpoint(
        initial,
        history_shape=history_shape,
    )
    expected_hashes = {
        "progress": tuple(sorted((f"{111:064x}", f"{112:064x}", f"{113:064x}", HASHES[11]))),
        "terminal_tie": tuple(sorted((f"{111:064x}", HASHES[11]))),
        "nonterminal_tie": tuple(sorted((f"{111:064x}", f"{112:064x}", HASHES[11]))),
    }[history_shape]
    assert reconciliation.venue_trade_hashes == expected_hashes
    initial.close()
    reopened = PredictionMarketStore(path)

    report = recovering_coordinator(
        reopened,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    assert report.blocked_intent_ids == ()
    reopened.close()


@pytest.mark.parametrize("corruption", ["missing", "extra", "cross_category"])
def test_startup_requires_exact_multi_event_checkpoint_trade_hash_family(
    store: PredictionMarketStore,
    corruption: str,
) -> None:
    plan, intent, exact, _, reconciliation = _persist_honest_task11_checkpoint(
        store,
        history_shape="progress",
    )
    hashes = set(reconciliation.venue_trade_hashes)
    if corruption == "missing":
        hashes.remove(f"{111:064x}")
    elif corruption == "extra":
        hashes.add(f"{199:064x}")
    else:
        hashes.add(exact.fee_hash)
    corrupt = reconciliation.model_copy(update={"venue_trade_hashes": tuple(sorted(hashes))})
    payload, record_hash = _stored_record(corrupt)
    store._connection.execute(
        "UPDATE live_reconciliations SET record_json = ?, record_hash = ? "
        "WHERE reconciliation_id = ?",
        [payload, record_hash, reconciliation.reconciliation_id],
    )

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)


def _task11_tail_records(
    plan: LiveExecutionPlan,
    *,
    occurred_at: datetime = NOW + timedelta(milliseconds=250),
    information_cutoff: datetime = NOW + timedelta(milliseconds=300),
) -> tuple[ExecutionIntent, VenueOrderEvent, VenueTradeEvent, AuthoritativeTradeEconomics]:
    source_intent = execution_intent(
        plan,
        leg_sequence=1,
        token_id=plan.token_ids[1],
        order_type=plan.leg_order_types[1],
        limit_price=plan.limit_prices[1],
        base_size=Decimal("10"),
        maximum_spend=Decimal("5.10"),
    )
    order_event = lifecycle_event(
        source_intent,
        VenueOrderState.RECONCILED,
        venue_order_id="venue-order-2",
        received_at=occurred_at - timedelta(milliseconds=25),
    ).model_copy(
        update={
            "intent_id": source_intent.intent_id,
            "raw_event_hash": TAIL_ORDER_HASH,
        }
    )
    trade_event = VenueTradeEvent(
        schema_version=1,
        trade_event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f4"),
        venue="polymarket",
        raw_event_hash=TAIL_TRADE_HASH,
        source_channel="recovery_read",
        venue_trade_id="trade-2",
        venue_order_id="venue-order-2",
        intent_id=source_intent.intent_id,
        original_venue_state=VenueTradeState.CONFIRMED.value,
        normalized_state=VenueTradeState.CONFIRMED,
        terminal=True,
        venue_timestamp=occurred_at,
        received_at=occurred_at,
        sequence_number=1,
        protocol_version=source_intent.protocol_version,
    )
    exact = _authoritative_economics_for_recovery(
        source_intent,
        venue_order_id="venue-order-2",
        venue_trade_id="trade-2",
        trade_event_hash=TAIL_TRADE_HASH,
        occurred_at=occurred_at,
        information_cutoff=information_cutoff,
    )
    return source_intent, order_event, trade_event, exact


@pytest.mark.parametrize(
    "tail_shape",
    [
        "economics_only",
        "trade_without_postings",
        "postings_without_complete_reconciliation",
        "equal_time_unclosed",
        "later_incomplete",
    ],
)
def test_latest_complete_checkpoint_must_close_every_economics_tail(
    store: PredictionMarketStore,
    tail_shape: str,
) -> None:
    plan, first_intent, first_exact, _first_postings, first_reconciliation = (
        _persist_honest_task11_checkpoint(store)
    )
    at_boundary = tail_shape == "equal_time_unclosed"
    occurred_at = NOW + timedelta(milliseconds=200 if at_boundary else 250)
    information_cutoff = NOW + timedelta(milliseconds=200 if at_boundary else 300)
    source_intent, order_event, trade_event, exact = _task11_tail_records(
        plan,
        occurred_at=occurred_at,
        information_cutoff=information_cutoff,
    )
    store.append_execution_intent(source_intent)
    store.append_venue_order_event(order_event)
    if tail_shape != "economics_only":
        store.append_venue_trade_event(trade_event)
    store.append_authoritative_trade_economics(exact)
    if tail_shape == "postings_without_complete_reconciliation":
        cumulative = postings_for_confirmed_trades(
            (first_intent, source_intent),
            (
                *store.verified_venue_trade_events_for_intent(
                    first_intent.intent_id, information_cutoff
                ),
                trade_event,
            ),
            (first_exact, exact),
        )
        for posting in cumulative:
            store.append_live_ledger_posting(posting)
    if tail_shape == "later_incomplete":
        store.append_live_reconciliation(
            first_reconciliation.model_copy(
                update={
                    "reconciliation_id": uuid4(),
                    "observed_at": NOW + timedelta(milliseconds=400),
                    "complete": False,
                    "differences": ("tail_unclosed",),
                    "next_action": "HALT_AND_RECONCILE",
                }
            )
        )

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == tuple(
        sorted((first_intent.intent_id, source_intent.intent_id))
    )
    assert report.kill_reason in {
        CoordinatorCode.RECOVERY_BLOCKED.value,
        "RECONCILIATION_INCOMPLETE",
    }


def test_standalone_economics_requires_a_complete_checkpoint(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    _source_intent, _, _, exact = _task11_tail_records(plan)
    store.append_authoritative_trade_economics(exact)

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == ()
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value


def _run_task11_recovery_path(
    store: PredictionMarketStore,
    plan: LiveExecutionPlan,
    recovery_path: str,
) -> RecoveryReport:
    executor = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    )
    if recovery_path == "startup":
        return executor.recover_on_startup(ACCOUNT_FINGERPRINT)
    return executor.recover_account(ACCOUNT_FINGERPRINT)


@pytest.mark.parametrize("recovery_path", ["startup", "account"])
@pytest.mark.parametrize("checkpoint", ["none", "earlier"])
@pytest.mark.parametrize("conflict", ["trade_identity", "raw_hash"])
@pytest.mark.parametrize("reverse_intents", [False, True])
def test_account_wide_recovery_classification_rejects_cross_intent_conflicts(
    store: PredictionMarketStore,
    monkeypatch: pytest.MonkeyPatch,
    recovery_path: str,
    checkpoint: str,
    conflict: str,
    reverse_intents: bool,
) -> None:
    plan, first_intent, _, _, _ = _persist_honest_task11_checkpoint(store)
    if checkpoint == "none":
        store._connection.execute("DELETE FROM live_reconciliations")
        store._connection.execute("DELETE FROM live_ledger_postings")
        store._connection.execute("DELETE FROM authoritative_trade_economics")
    second_intent, second_order, second_trade, _ = _task11_tail_records(plan)
    if conflict == "trade_identity":
        second_trade = second_trade.model_copy(update={"venue_trade_id": "trade-1"})
    else:
        second_trade = second_trade.model_copy(update={"raw_event_hash": HASHES[11]})
    store.append_execution_intent(second_intent)
    store.append_venue_order_event(second_order)
    store.append_venue_trade_event(second_trade)

    executor = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    )
    if reverse_intents:
        original = type(executor)._account_intents
        monkeypatch.setattr(
            type(executor),
            "_account_intents",
            lambda self, account, now: tuple(reversed(original(self, account, now))),
        )
    report = (
        executor.recover_on_startup(ACCOUNT_FINGERPRINT)
        if recovery_path == "startup"
        else executor.recover_account(ACCOUNT_FINGERPRINT)
    )

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == tuple(
        sorted((first_intent.intent_id, second_intent.intent_id))
    )
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value


def test_account_wide_recovery_classification_blocks_unexpected_store_faults(
    store: PredictionMarketStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, source_intent, *_ = _persist_honest_task11_checkpoint(store)

    def fail_history(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("hostile store fault")

    monkeypatch.setattr(store, "verified_venue_trade_events_for_intent", fail_history)

    report = _run_task11_recovery_path(store, plan, "account")

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (source_intent.intent_id,)
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value


@pytest.mark.parametrize("recovery_path", ["startup", "account"])
@pytest.mark.parametrize("tail_shape", ["single", "terminal_prefix", "equal_cutoff"])
def test_confirmed_raw_checkpoint_closure_blocks_trade_only_tails(
    store: PredictionMarketStore,
    recovery_path: str,
    tail_shape: str,
) -> None:
    plan, first_intent, *_ = _persist_honest_task11_checkpoint(store)
    second_intent, second_order, second_trade, _ = _task11_tail_records(plan)
    tail_events = (second_trade,)
    if tail_shape == "equal_cutoff":
        second_trade = second_trade.model_copy(
            update={"received_at": NOW + timedelta(milliseconds=200)}
        )
        tail_events = (second_trade,)
    elif tail_shape == "terminal_prefix":
        prefix = second_trade.model_copy(
            update={
                "trade_event_id": UUID("42b33848-ff46-4c45-b9ab-0c74510687f5"),
                "raw_event_hash": f"{403:064x}",
                "original_venue_state": VenueTradeState.MATCHED.value,
                "normalized_state": VenueTradeState.MATCHED,
                "terminal": False,
                "venue_timestamp": NOW + timedelta(milliseconds=225),
                "received_at": NOW + timedelta(milliseconds=225),
                "sequence_number": 1,
            }
        )
        second_trade = second_trade.model_copy(update={"sequence_number": 2})
        tail_events = (prefix, second_trade)
    store.append_execution_intent(second_intent)
    store.append_venue_order_event(second_order)
    for event in tail_events:
        store.append_venue_trade_event(event)

    report = _run_task11_recovery_path(store, plan, recovery_path)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == tuple(
        sorted((first_intent.intent_id, second_intent.intent_id))
    )
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value


@pytest.mark.parametrize("recovery_path", ["startup", "account"])
def test_confirmed_raw_checkpoint_closure_requires_a_checkpoint(
    store: PredictionMarketStore,
    recovery_path: str,
) -> None:
    plan, source_intent, *_ = _persist_honest_task11_checkpoint(store)
    store._connection.execute("DELETE FROM live_reconciliations")
    store._connection.execute("DELETE FROM live_ledger_postings")
    store._connection.execute("DELETE FROM authoritative_trade_economics")

    report = _run_task11_recovery_path(store, plan, recovery_path)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (source_intent.intent_id,)
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value


@pytest.mark.parametrize("recovery_path", ["startup", "account"])
def test_confirmed_raw_checkpoint_closure_rejects_a_later_incomplete_checkpoint(
    store: PredictionMarketStore,
    recovery_path: str,
) -> None:
    plan, first_intent, _, _, first_reconciliation = _persist_honest_task11_checkpoint(store)
    second_intent, second_order, second_trade, _ = _task11_tail_records(plan)
    store.append_execution_intent(second_intent)
    store.append_venue_order_event(second_order)
    store.append_venue_trade_event(second_trade)
    store.append_live_reconciliation(
        first_reconciliation.model_copy(
            update={
                "reconciliation_id": uuid4(),
                "observed_at": NOW + timedelta(milliseconds=400),
                "complete": False,
                "differences": ("tail_unclosed",),
                "next_action": "HALT_AND_RECONCILE",
            }
        )
    )

    report = _run_task11_recovery_path(store, plan, recovery_path)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == tuple(
        sorted((first_intent.intent_id, second_intent.intent_id))
    )
    assert report.kill_reason in {
        CoordinatorCode.RECOVERY_BLOCKED.value,
        "RECONCILIATION_INCOMPLETE",
    }


@pytest.mark.parametrize("recovery_path", ["startup", "account"])
def test_confirmed_raw_checkpoint_closure_rejects_a_forged_hash_only_checkpoint(
    store: PredictionMarketStore,
    recovery_path: str,
) -> None:
    plan, source_intent, *_ = _persist_honest_task11_checkpoint(store)
    store._connection.execute("DELETE FROM live_reconciliations")
    store._connection.execute("DELETE FROM live_ledger_postings")
    store._connection.execute("DELETE FROM authoritative_trade_economics")
    _append_complete_reconciliation(
        store,
        observed_at=NOW + timedelta(milliseconds=200),
        posting_ids=(),
        order_hashes=(),
        trade_hashes=(HASHES[11],),
        fee_hashes=(),
        balance_hashes=(),
    )

    report = _run_task11_recovery_path(store, plan, recovery_path)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (source_intent.intent_id,)
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value


@pytest.mark.parametrize("recovery_path", ["startup", "account"])
@pytest.mark.parametrize("history_shape", ["single", "progress", "cutoff_equal"])
def test_confirmed_raw_checkpoint_closure_accepts_honest_complete_histories(
    store: PredictionMarketStore,
    recovery_path: str,
    history_shape: str,
) -> None:
    plan, *_ = _persist_honest_task11_checkpoint(store, history_shape=history_shape)

    report = _run_task11_recovery_path(store, plan, recovery_path)

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    assert report.blocked_intent_ids == ()


def test_fully_cumulative_later_complete_checkpoint_closes_the_economics_tail(
    store: PredictionMarketStore,
) -> None:
    plan, first_intent, first_exact, _, _ = _persist_honest_task11_checkpoint(store)
    second_intent, second_order, second_trade, second_exact = _task11_tail_records(plan)
    first_trade = store.verified_venue_trade_events_for_intent(
        first_intent.intent_id, second_exact.information_cutoff
    )[0]
    intents = (first_intent, second_intent)
    trades = (first_trade, second_trade)
    economics = (first_exact, second_exact)
    postings = postings_for_confirmed_trades(intents, trades, economics)
    snapshot = VenueAccountSnapshot(
        schema_version=1,
        account_fingerprint=ACCOUNT_FINGERPRINT,
        cutoff_at=NOW,
        observed_at=NOW + timedelta(milliseconds=400),
        opening_cash_balances=(_checkpoint_asset("USDC", "100", "0.000001", 301),),
        current_cash_balances=(_checkpoint_asset("USDC", "89.78", "0.000001", 101),),
        opening_token_positions=(
            _checkpoint_asset(first_intent.token_id, "0", "0.01", 302),
            _checkpoint_asset(second_intent.token_id, "0", "0.01", 303),
        ),
        current_token_positions=(
            _checkpoint_asset(first_intent.token_id, "10", "0.01", 101),
            _checkpoint_asset(second_intent.token_id, "10", "0.01", 101),
        ),
        opening_allowances=(),
        current_allowances=(),
        opening_cumulative_fees=(_checkpoint_asset("USDC", "0", "0.000001", 304),),
        current_cumulative_fees=(_checkpoint_asset("USDC", "0.02", "0.000001", 305),),
        open_orders=(),
        recent_trades=tuple(
            RecentTradeObservation(
                schema_version=1,
                venue_trade_id=exact.venue_trade_id,
                venue_order_id=exact.venue_order_id,
                intent_id=source_intent.intent_id,
                cash_asset_id=exact.cash_asset_id,
                position_asset_id=exact.position_asset_id,
                side=exact.side,
                state=VenueTradeState.CONFIRMED,
                trade_event_hash=exact.trade_event_hash,
                settlement_hash=exact.settlement_hash,
                fee_hash=exact.fee_hash,
                source_hash=exact.source_hash,
                balance_evidence_hashes=exact.balance_evidence_hashes,
                economics_fingerprint=exact.economics_fingerprint,
                realized_pnl=None,
                cost_basis_evidence_hash=None,
                occurred_at=exact.occurred_at,
            )
            for source_intent, exact in zip(intents, economics, strict=True)
        ),
        settlements=tuple(
            SettlementObservation(
                schema_version=1,
                venue_trade_id=exact.venue_trade_id,
                venue_order_id=exact.venue_order_id,
                intent_id=source_intent.intent_id,
                position_asset_id=exact.position_asset_id,
                state=VenueTradeState.CONFIRMED,
                settlement_hash=exact.settlement_hash,
                evidence_hash=exact.settlement_hash,
                occurred_at=exact.occurred_at,
            )
            for source_intent, exact in zip(intents, economics, strict=True)
        ),
        opening_cash_source_hash=f"{306:064x}",
        current_cash_source_hash=f"{307:064x}",
        opening_position_source_hash=f"{308:064x}",
        current_position_source_hash=f"{309:064x}",
        opening_allowance_source_hash=f"{310:064x}",
        current_allowance_source_hash=f"{311:064x}",
        opening_fee_source_hash=f"{312:064x}",
        current_fee_source_hash=f"{313:064x}",
        open_orders_source_hash=f"{314:064x}",
        recent_trades_source_hash=f"{315:064x}",
        settlements_source_hash=f"{316:064x}",
    )
    reconciliation = reconcile_live_account(postings, snapshot, intents, trades, economics)
    assert reconciliation.complete
    store.append_execution_intent(second_intent)
    store.append_venue_order_event(second_order)
    store.append_venue_trade_event(second_trade)
    store.append_authoritative_trade_economics(second_exact)
    for posting in postings:
        store.append_live_ledger_posting(posting)
    store.append_live_reconciliation(reconciliation)

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_COMPLETE
    assert report.blocked_intent_ids == ()

    account_report = _run_task11_recovery_path(store, plan, "account")
    assert account_report.code is CoordinatorCode.RECOVERY_COMPLETE
    assert account_report.blocked_intent_ids == ()


@pytest.mark.parametrize(
    "corruption",
    ["missing_economics", "cash_asset", "wrong_hash_family", "orphan", "incomplete"],
)
def test_startup_blocks_task11_checkpoint_without_canonical_economics_closure(
    store: PredictionMarketStore,
    corruption: str,
) -> None:
    plan, intent, exact, postings, reconciliation = _persist_honest_task11_checkpoint(store)
    if corruption == "missing_economics":
        store._connection.execute("DELETE FROM authoritative_trade_economics")
    elif corruption == "cash_asset":
        cash_posting = next(
            posting
            for posting in postings
            if posting.asset_id == exact.cash_asset_id
            and "venue_cash"
            in {posting.debit_account.partition(":")[0], posting.credit_account.partition(":")[0]}
        )
        corrupt = cash_posting.model_copy(update={"asset_id": "CORRUPT-CASH"})
        payload, record_hash = _stored_record(corrupt)
        store._connection.execute(
            "UPDATE live_ledger_postings SET record_json = ?, record_hash = ? WHERE posting_id = ?",
            [payload, record_hash, cash_posting.posting_id],
        )
    elif corruption == "wrong_hash_family":
        wrong_family = reconciliation.model_copy(
            update={
                "evidence_hashes": tuple(
                    value for value in reconciliation.evidence_hashes if value != exact.fee_hash
                ),
                "venue_trade_hashes": tuple(
                    sorted({*reconciliation.venue_trade_hashes, exact.fee_hash})
                ),
            }
        )
        payload, record_hash = _stored_record(wrong_family)
        store._connection.execute(
            "UPDATE live_reconciliations SET record_json = ?, record_hash = ? "
            "WHERE reconciliation_id = ?",
            [payload, record_hash, reconciliation.reconciliation_id],
        )
    elif corruption == "orphan":
        store.append_authoritative_trade_economics(
            _authoritative_economics_for_recovery(
                intent,
                venue_trade_id="orphan-economics",
                trade_event_hash=HASHES[10],
            )
        )
    else:
        omitted_ids = {
            posting.posting_id
            for posting in postings
            if "fees_paid"
            in {posting.debit_account.partition(":")[0], posting.credit_account.partition(":")[0]}
        }
        for posting_id in omitted_ids:
            store._connection.execute(
                "DELETE FROM live_ledger_postings WHERE posting_id = ?", [posting_id]
            )
        incomplete = reconciliation.model_copy(
            update={
                "expected_posting_ids": tuple(
                    value
                    for value in reconciliation.expected_posting_ids
                    if value not in omitted_ids
                )
            }
        )
        payload, record_hash = _stored_record(incomplete)
        store._connection.execute(
            "UPDATE live_reconciliations SET record_json = ?, record_hash = ? "
            "WHERE reconciliation_id = ?",
            [payload, record_hash, reconciliation.reconciliation_id],
        )

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == (intent.intent_id,)


def test_startup_treats_plan_linked_wrong_account_intent_as_corruption(
    store: PredictionMarketStore,
) -> None:
    plan = execution_plan()
    mismatched = execution_intent(plan, account_fingerprint="9" * 64)
    with store.transaction() as transaction:
        transaction.append_live_execution_plan(plan)
        transaction.append_execution_intent(mismatched)
    restart = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        plan,
    )

    report = restart.recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == ()
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value
    assert restart.new_intents_blocked


def test_startup_treats_account_intent_without_plan_as_corruption(
    store: PredictionMarketStore,
) -> None:
    orphan = execution_intent(execution_plan())
    store.append_execution_intent(orphan)
    restart = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        execution_plan(),
    )

    report = restart.recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.blocked_intent_ids == ()
    assert restart.new_intents_blocked


def test_startup_blocks_covered_posting_with_orphan_relationships(
    store: PredictionMarketStore,
) -> None:
    posting_id = uuid4()
    store.append_live_ledger_posting(
        LiveLedgerPosting(
            schema_version=1,
            posting_id=posting_id,
            account_fingerprint=ACCOUNT_FINGERPRINT,
            intent_id=uuid4(),
            venue_order_id="orphan-order",
            venue_trade_id="orphan-trade",
            settlement_hash=HASHES[11],
            fee_hash=HASHES[12],
            balance_evidence_hashes=(HASHES[9],),
            debit_account="cash",
            credit_account="position",
            asset_id="wrong-asset",
            debit_amount=Decimal("1"),
            credit_amount=Decimal("0"),
            occurred_at=NOW + timedelta(milliseconds=100),
        )
    )
    store.append_live_reconciliation(
        LiveReconciliation(
            schema_version=1,
            reconciliation_id=uuid4(),
            account_fingerprint=ACCOUNT_FINGERPRINT,
            observed_at=NOW + timedelta(milliseconds=200),
            complete=True,
            differences=(),
            evidence_hashes=(HASHES[12],),
            next_action=None,
            venue_order_hashes=(HASHES[10],),
            venue_trade_hashes=(HASHES[11],),
            balance_hashes=(HASHES[9],),
            expected_posting_ids=(posting_id,),
        )
    )

    report = recovering_coordinator(
        store,
        RecordingAccountReader(orders=OrdersReadPayload(kind="ORDERS_READ", items=())),
        FakeSigner(),
        execution_plan(),
    ).recover_on_startup(ACCOUNT_FINGERPRINT)

    assert report.code is CoordinatorCode.RECOVERY_BLOCKED
    assert report.kill_reason == CoordinatorCode.RECOVERY_BLOCKED.value


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
