from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from polytrading.predictions.execution.models import ExecutionOperation
from polytrading.predictions.pilot.reconciliation import reconcile_startup
from polytrading.predictions.pilot.selector import PilotAccountState
from polytrading.predictions.polymarket_execution.ipc import SanitizedOperationResult
from polytrading.predictions.polymarket_execution.routes import (
    OrderReadPayload,
    OrdersReadPayload,
    RestCode,
    RouteKey,
    TradeReadPayload,
    TradesReadPayload,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
ACCOUNT = "a" * 64
WALLET = "b" * 64


def account_state(**overrides: Any) -> PilotAccountState:
    fields: dict[str, Any] = {
        "account_fingerprint": ACCOUNT,
        "wallet_fingerprint": WALLET,
        "collateral_usd": Decimal("200"),
        "allowance_usd": Decimal("200"),
        "kill_engaged": True,
        "observed_at": NOW,
    }
    fields.update(overrides)
    return PilotAccountState.model_validate(fields, strict=True)


def orders_result(
    *items: OrderReadPayload,
    raw_body_hash: str = "1" * 64,
) -> SanitizedOperationResult:
    return SanitizedOperationResult(
        operation=ExecutionOperation.READ_ORDERS,
        result_code=RestCode.READ_OK,
        evidence_hashes=(raw_body_hash,),
        route=RouteKey.READ_OPEN_ORDERS,
        observed_at=NOW,
        raw_body_hash=raw_body_hash,
        attempts=1,
        recovery_required=False,
        kill_required=False,
        public_payload=OrdersReadPayload(kind="ORDERS_READ", items=items),
    )


def trades_result(*items: TradeReadPayload) -> SanitizedOperationResult:
    return SanitizedOperationResult(
        operation=ExecutionOperation.READ_TRADES,
        result_code=RestCode.READ_OK,
        evidence_hashes=("2" * 64,),
        route=RouteKey.READ_TRADES,
        observed_at=NOW,
        raw_body_hash="2" * 64,
        attempts=1,
        recovery_required=False,
        kill_required=False,
        public_payload=TradesReadPayload(kind="TRADES_READ", items=items),
    )


def open_order(order_id: str = "order-1") -> OrderReadPayload:
    return OrderReadPayload(
        kind="ORDER_READ",
        id=order_id,
        market="market-1",
        asset_id="217426",
        maker_address="0x" + "11" * 20,
        side="BUY",
        price="0.40",
        original_size="10",
        size_matched="0",
        outcome="YES",
        order_type="GTC",
        status="LIVE",
        associate_trades=(),
        created_at="1788177600",
        expiration="0",
    )


def trade(status: str = "CONFIRMED", trade_id: str = "trade-1") -> TradeReadPayload:
    return TradeReadPayload(
        kind="TRADE_READ",
        id=trade_id,
        market="market-1",
        asset_id="217426",
        maker_address="0x" + "11" * 20,
        taker_order_id="order-1",
        side="BUY",
        trader_side="TAKER",
        price="0.40",
        size="10",
        outcome="YES",
        status=status,
        fee_rate_bps="0",
        bucket_index=0,
        transaction_hash="0x" + "22" * 32,
        maker_orders=(),
        match_time="1788177500",
        last_update="1788177550",
    )


class FakeVenuePort:
    def __init__(
        self,
        *,
        account: PilotAccountState | None = None,
        positions: Mapping[str, Decimal] | None = None,
        orders: SanitizedOperationResult | None = None,
        trades: SanitizedOperationResult | None = None,
        fail: str | None = None,
    ) -> None:
        self.account = account or account_state()
        self.position_values = {} if positions is None else positions
        self.order_result = orders or orders_result()
        self.trade_result = trades or trades_result()
        self.fail = fail
        self.reads: list[str] = []

    def _read(self, name: str) -> None:
        self.reads.append(name)
        if self.fail == name:
            raise RuntimeError("secret transport detail")

    def account_state(self) -> PilotAccountState:
        self._read("account_state")
        return self.account

    def positions(self) -> Mapping[str, Decimal]:
        self._read("positions")
        return self.position_values

    def orders(self) -> SanitizedOperationResult:
        self._read("orders")
        return self.order_result

    def trades(self) -> SanitizedOperationResult:
        self._read("trades")
        return self.trade_result


def test_startup_reconciliation_marks_a_clean_authoritative_snapshot_complete() -> None:
    port = FakeVenuePort(positions={"217426": Decimal("10")})

    state = reconcile_startup(port, account_fingerprint=ACCOUNT, now=lambda: NOW)

    assert state.reconciliation_complete is True
    assert state.active_submissions == state.unknown_outcomes == 0
    assert port.reads == ["account_state", "positions", "orders", "trades"]


@pytest.mark.parametrize("failed_read", ["account_state", "positions", "orders", "trades"])
def test_startup_reconciliation_propagates_transport_read_failures(
    failed_read: str,
) -> None:
    port = FakeVenuePort(fail=failed_read)

    with pytest.raises(RuntimeError, match="secret transport detail"):
        reconcile_startup(port, account_fingerprint=ACCOUNT, now=lambda: NOW)


def test_sanitized_venue_read_failure_remains_an_unknown_outcome() -> None:
    failed = SanitizedOperationResult(
        operation=ExecutionOperation.READ_ORDERS,
        result_code=RestCode.READ_FAILED,
        evidence_hashes=("3" * 64,),
        route=RouteKey.READ_OPEN_ORDERS,
        observed_at=NOW,
        raw_body_hash="3" * 64,
        attempts=1,
        recovery_required=True,
        kill_required=True,
        public_payload=None,
    )

    state = reconcile_startup(
        FakeVenuePort(orders=failed),
        account_fingerprint=ACCOUNT,
        now=lambda: NOW,
    )

    assert state.reconciliation_complete is False
    assert state.unknown_outcomes == 1


def test_an_open_order_is_an_active_unknown_submission() -> None:
    state = reconcile_startup(
        FakeVenuePort(orders=orders_result(open_order())),
        account_fingerprint=ACCOUNT,
        now=lambda: NOW,
    )

    assert state.reconciliation_complete is False
    assert state.active_submissions == 1
    assert state.unknown_outcomes == 1


def test_a_nonterminal_trade_is_unknown_but_a_confirmed_trade_is_closed() -> None:
    nonterminal = reconcile_startup(
        FakeVenuePort(trades=trades_result(trade("MATCHED"))),
        account_fingerprint=ACCOUNT,
        now=lambda: NOW,
    )
    terminal = reconcile_startup(
        FakeVenuePort(trades=trades_result(trade("CONFIRMED"))),
        account_fingerprint=ACCOUNT,
        now=lambda: NOW,
    )

    assert nonterminal.reconciliation_complete is False
    assert nonterminal.unknown_outcomes == 1
    assert terminal.reconciliation_complete is True
    assert terminal.unknown_outcomes == 0


@pytest.mark.parametrize(
    "port",
    [
        FakeVenuePort(account=account_state(account_fingerprint="c" * 64)),
        FakeVenuePort(positions={"217426": Decimal("-1")}),
    ],
)
def test_an_account_or_position_contradiction_fails_closed(port: FakeVenuePort) -> None:
    state = reconcile_startup(port, account_fingerprint=ACCOUNT, now=lambda: NOW)

    assert state.reconciliation_complete is False
    assert state.unknown_outcomes == 1


def test_the_reconciliation_hash_excludes_balances_sizes_and_raw_evidence_hashes() -> None:
    first = reconcile_startup(
        FakeVenuePort(
            account=account_state(collateral_usd=Decimal("100")),
            positions={"217426": Decimal("1")},
            orders=orders_result(),
        ),
        account_fingerprint=ACCOUNT,
        now=lambda: NOW,
    )
    second = reconcile_startup(
        FakeVenuePort(
            account=account_state(collateral_usd=Decimal("200")),
            positions={"217426": Decimal("9")},
            orders=orders_result(raw_body_hash="3" * 64),
        ),
        account_fingerprint=ACCOUNT,
        now=lambda: NOW,
    )

    assert first.reconciliation_hash == second.reconciliation_hash


def test_the_reconciliation_hash_sorts_public_ids_and_result_codes() -> None:
    forward = reconcile_startup(
        FakeVenuePort(
            positions={"111": Decimal("1"), "222": Decimal("2")},
            trades=trades_result(trade("CONFIRMED", "trade-1"), trade("FAILED", "trade-2")),
        ),
        account_fingerprint=ACCOUNT,
        now=lambda: NOW,
    )
    reversed_snapshot = reconcile_startup(
        FakeVenuePort(
            positions={"222": Decimal("2"), "111": Decimal("1")},
            trades=trades_result(trade("FAILED", "trade-2"), trade("CONFIRMED", "trade-1")),
        ),
        account_fingerprint=ACCOUNT,
        now=lambda: NOW,
    )

    assert forward.reconciliation_hash == reversed_snapshot.reconciliation_hash
