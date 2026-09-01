"""Fail-closed startup closure over the signer's fixed authoritative read surface."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal

from polytrading.predictions.domain import Sha256, normalize_utc_timestamp
from polytrading.predictions.execution.models import ExecutionOperation, canonical_execution_hash
from polytrading.predictions.pilot.activation import PilotReconciliationState
from polytrading.predictions.pilot.execution_port import VenueSubmissionPort
from polytrading.predictions.pilot.selector import PilotAccountState
from polytrading.predictions.polymarket_execution.ipc import SanitizedOperationResult
from polytrading.predictions.polymarket_execution.routes import (
    OrdersReadPayload,
    RestCode,
    TradesReadPayload,
)

_TERMINAL_TRADE_STATES = frozenset({"CONFIRMED", "FAILED"})


def reconcile_startup(
    port: VenueSubmissionPort,
    *,
    account_fingerprint: Sha256,
    now: Callable[[], datetime],
) -> PilotReconciliationState:
    """Take all four authoritative reads and classify every ambiguity as unknown."""
    observed_at = normalize_utc_timestamp(now())
    public_ids: list[str] = [account_fingerprint]
    result_codes: list[str] = []
    observation_times: list[str] = [observed_at.isoformat()]
    unknown_outcomes = 0

    unknown_outcomes += _read_account(
        port,
        account_fingerprint=account_fingerprint,
        public_ids=public_ids,
        result_codes=result_codes,
        observation_times=observation_times,
    )

    unknown_outcomes += _read_positions(
        port,
        public_ids=public_ids,
        result_codes=result_codes,
    )

    active_submissions, order_unknown = _read_orders(
        port,
        public_ids=public_ids,
        result_codes=result_codes,
        observation_times=observation_times,
    )
    unknown_outcomes += order_unknown

    unknown_outcomes += _read_trades(
        port,
        public_ids=public_ids,
        result_codes=result_codes,
        observation_times=observation_times,
    )

    reconciliation_hash = canonical_execution_hash(
        {
            "public_ids": sorted(public_ids),
            "result_codes": sorted(result_codes),
            "observation_times": sorted(observation_times),
        }
    )
    return PilotReconciliationState(
        account_fingerprint=account_fingerprint,
        active_submissions=active_submissions,
        unknown_outcomes=unknown_outcomes,
        reconciliation_complete=unknown_outcomes == 0 and active_submissions == 0,
        unexplained_difference_usd=Decimal("0"),
        reconciliation_hash=reconciliation_hash,
        observed_at=observed_at,
    )


def _read_account(
    port: VenueSubmissionPort,
    *,
    account_fingerprint: Sha256,
    public_ids: list[str],
    result_codes: list[str],
    observation_times: list[str],
) -> int:
    account = port.account_state()
    if type(account) is not PilotAccountState:
        result_codes.append("ACCOUNT_READ_MISSING")
        return 1
    public_ids.extend((account.account_fingerprint, account.wallet_fingerprint))
    observation_times.append(account.observed_at.isoformat())
    if account.account_fingerprint != account_fingerprint:
        result_codes.append("ACCOUNT_READ_CONTRADICTED")
        return 1
    result_codes.append("ACCOUNT_READ_OK")
    return 0


def _read_positions(
    port: VenueSubmissionPort,
    *,
    public_ids: list[str],
    result_codes: list[str],
) -> int:
    positions = port.positions()
    if not isinstance(positions, Mapping):
        result_codes.append("POSITIONS_READ_MISSING")
        return 1
    invalid = False
    for token_id, value in positions.items():
        if type(token_id) is not str or not token_id:
            invalid = True
            continue
        public_ids.append(token_id)
        if type(value) is not Decimal or not value.is_finite() or value < 0:
            invalid = True
    result_codes.append("POSITIONS_READ_CONTRADICTED" if invalid else "POSITIONS_READ_OK")
    return int(invalid)


def _read_orders(
    port: VenueSubmissionPort,
    *,
    public_ids: list[str],
    result_codes: list[str],
    observation_times: list[str],
) -> tuple[int, int]:
    result = port.orders()
    payload = _validated_result(
        result,
        operation=ExecutionOperation.READ_ORDERS,
        payload_type=OrdersReadPayload,
        result_codes=result_codes,
        observation_times=observation_times,
    )
    if payload is None:
        return 0, 1
    assert isinstance(payload, OrdersReadPayload)
    order_ids = [item.id for item in payload.items]
    public_ids.extend(order_ids)
    active = len(order_ids)
    if active:
        result_codes.extend("OPEN_ORDER_PRESENT" for _item in payload.items)
    return active, active


def _read_trades(
    port: VenueSubmissionPort,
    *,
    public_ids: list[str],
    result_codes: list[str],
    observation_times: list[str],
) -> int:
    result = port.trades()
    payload = _validated_result(
        result,
        operation=ExecutionOperation.READ_TRADES,
        payload_type=TradesReadPayload,
        result_codes=result_codes,
        observation_times=observation_times,
    )
    if payload is None:
        return 1
    assert isinstance(payload, TradesReadPayload)
    trade_ids = [item.id for item in payload.items]
    public_ids.extend(trade_ids)
    result_codes.extend(item.status for item in payload.items)
    nonterminal = sum(item.status not in _TERMINAL_TRADE_STATES for item in payload.items)
    duplicate = len(trade_ids) != len(set(trade_ids))
    if duplicate:
        result_codes.append("DUPLICATE_TRADE_ID")
    return nonterminal + int(duplicate and nonterminal == 0)


def _validated_result(
    result: SanitizedOperationResult,
    *,
    operation: ExecutionOperation,
    payload_type: type[OrdersReadPayload] | type[TradesReadPayload],
    result_codes: list[str],
    observation_times: list[str],
) -> OrdersReadPayload | TradesReadPayload | None:
    if type(result) is not SanitizedOperationResult:
        result_codes.append(f"{operation.value}_MISSING")
        return None
    code = (
        result.result_code.value if isinstance(result.result_code, RestCode) else result.result_code
    )
    result_codes.append(code)
    if result.observed_at is not None:
        observation_times.append(result.observed_at.isoformat())
    if (
        result.operation is not operation
        or result.result_code is not RestCode.READ_OK
        or result.observed_at is None
        or type(result.public_payload) is not payload_type
    ):
        result_codes.append(f"{operation.value}_INCOMPLETE")
        return None
    return result.public_payload


__all__ = ["reconcile_startup"]
