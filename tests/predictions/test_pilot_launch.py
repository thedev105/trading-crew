from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polytrading.predictions.execution.models import ExecutionOperation
from polytrading.predictions.pilot.launch import compose_pilot_environment
from polytrading.predictions.pilot.selector import PilotAccountState
from polytrading.predictions.pilot.server import PilotRequestError
from polytrading.predictions.polymarket_execution.ipc import SanitizedOperationResult
from polytrading.predictions.polymarket_execution.routes import (
    OrdersReadPayload,
    RestCode,
    RouteKey,
    TradesReadPayload,
)
from polytrading.predictions.storage.store import PredictionMarketStore

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


class CleanVenuePort:
    def account_state(self) -> PilotAccountState:
        return PilotAccountState(
            account_fingerprint="a" * 64,
            wallet_fingerprint="a" * 64,
            collateral_usd=Decimal("200"),
            allowance_usd=Decimal("200"),
            kill_engaged=True,
            observed_at=NOW,
        )

    def positions(self) -> dict[str, Decimal]:
        return {}

    def orders(self) -> SanitizedOperationResult:
        return _empty_result(
            ExecutionOperation.READ_ORDERS,
            RouteKey.READ_OPEN_ORDERS,
            OrdersReadPayload(kind="ORDERS_READ", items=()),
        )

    def trades(self) -> SanitizedOperationResult:
        return _empty_result(
            ExecutionOperation.READ_TRADES,
            RouteKey.READ_TRADES,
            TradesReadPayload(kind="TRADES_READ", items=()),
        )


def _empty_result(
    operation: ExecutionOperation,
    route: RouteKey,
    payload: OrdersReadPayload | TradesReadPayload,
) -> SanitizedOperationResult:
    return SanitizedOperationResult(
        operation=operation,
        result_code=RestCode.READ_OK,
        evidence_hashes=("1" * 64,),
        route=route,
        observed_at=NOW,
        raw_body_hash="1" * 64,
        attempts=1,
        recovery_required=False,
        kill_required=False,
        public_payload=payload,
    )


def test_compose_uses_the_authoritative_venue_snapshot_for_reconciliation(tmp_path) -> None:
    store = PredictionMarketStore(tmp_path / "pilot.duckdb")
    venue_port = CleanVenuePort()
    try:
        environment = compose_pilot_environment(
            store,
            account_fingerprint="a" * 64,
            wallet_fingerprint="a" * 64,
            credentials_present=False,
            now=lambda: NOW,
            venue_port=venue_port,
        )
        assert environment.manifest is None
        assert environment.manifest_state == "MISSING"
        assert environment.venue_binding is None
        assert environment.credentials_present is False
        assert environment.executor_factory is None
        assert environment.reconciliation.reconciliation_complete is True
        assert environment.account_state() == venue_port.account_state()
    finally:
        store.close()


def test_compose_without_a_venue_port_preserves_the_unavailable_fallback(tmp_path) -> None:
    store = PredictionMarketStore(tmp_path / "pilot.duckdb")
    try:
        environment = compose_pilot_environment(
            store,
            account_fingerprint="a" * 64,
            wallet_fingerprint="a" * 64,
            credentials_present=False,
            now=lambda: NOW,
        )

        assert environment.reconciliation.reconciliation_complete is False
        assert environment.reconciliation.unknown_outcomes == 0
        with pytest.raises(PilotRequestError, match="EXECUTION_UNAVAILABLE"):
            environment.account_state()
    finally:
        store.close()
