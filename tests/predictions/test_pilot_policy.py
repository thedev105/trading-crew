from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from polytrading.predictions.pilot.models import (
    PILOT_CEILINGS,
    LossStatus,
    PilotLimits,
)
from polytrading.predictions.pilot.policy import (
    COMPILED_PILOT_CEILINGS,
    EquitySnapshot,
    LossWindow,
    PilotPolicyError,
    PositionMark,
    RequestedPilotLimits,
    effective_limits,
    loss_state,
    mark_trading_equity,
    require_order_within_budget,
)
from tests.predictions.pilot_helpers import EVIDENCE_HASH, NOW, limits_fields


def requested(**overrides: object) -> RequestedPilotLimits:
    return RequestedPilotLimits.model_validate(limits_fields(**overrides), strict=True)


def position_mark(**overrides: object) -> PositionMark:
    fields: dict[str, object] = {
        "outcome_token_id": "token-1",
        "quantity": Decimal("10"),
        "unwind_bid": Decimal("0.40"),
        "unwind_depth": Decimal("10"),
        "observed_at": NOW,
    }
    fields.update(overrides)
    return PositionMark.model_validate(fields, strict=True)


def equity_snapshot(**overrides: object) -> EquitySnapshot:
    fields: dict[str, object] = {
        "account_fingerprint": "a" * 64,
        "reconciled_collateral_usd": Decimal("100"),
        "positions": (position_mark(),),
        "evidence_hashes": (EVIDENCE_HASH,),
        "observed_at": NOW,
    }
    fields.update(overrides)
    return EquitySnapshot.model_validate(fields, strict=True)


def loss_window(
    *, position_mark_value: Decimal | None = Decimal("0.40"), **overrides: object
) -> LossWindow:
    positions = (
        (position_mark(unwind_bid=position_mark_value, unwind_depth=Decimal("10")),)
        if position_mark_value is not None
        else (position_mark(unwind_bid=None, unwind_depth=None),)
    )
    fields: dict[str, object] = {
        "session_start_equity_usd": Decimal("110"),
        "utc_day_start_equity_usd": Decimal("110"),
        "current_equity": equity_snapshot(positions=positions),
        "realized_loss_usd": Decimal("0"),
        "confirmed_external_flows_usd": Decimal("0"),
        "evaluated_at": NOW,
    }
    fields.update(overrides)
    return LossWindow.model_validate(fields, strict=True)


def test_compiled_ceilings_are_the_immutable_pilot_envelope() -> None:
    assert COMPILED_PILOT_CEILINGS == PILOT_CEILINGS


def test_effective_limits_rejects_increase_instead_of_clamping() -> None:
    with pytest.raises(PilotPolicyError, match="ORDER_NOTIONAL_CEILING"):
        effective_limits(requested(order_notional=Decimal("10.01")))


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("wallet_trading_equity", Decimal("250.01"), "WALLET_TRADING_EQUITY_CEILING"),
        ("strategy_gross_notional", Decimal("25.01"), "STRATEGY_GROSS_NOTIONAL_CEILING"),
        ("session_duration", timedelta(minutes=16), "SESSION_DURATION_CEILING"),
        ("session_deployed_capital", Decimal("50.01"), "SESSION_DEPLOYED_CAPITAL_CEILING"),
        ("concurrent_strategies", 2, "CONCURRENT_STRATEGIES_CEILING"),
        ("session_loss", Decimal("5.01"), "SESSION_LOSS_CEILING"),
        ("utc_day_loss", Decimal("10.01"), "UTC_DAY_LOSS_CEILING"),
    ],
)
def test_every_ceiling_is_independently_enforced(field: str, value: object, code: str) -> None:
    with pytest.raises(PilotPolicyError) as raised:
        effective_limits(requested(**{field: value}))
    assert raised.value.code == code


def test_effective_limits_keep_lower_requested_values() -> None:
    limits = effective_limits(
        requested(order_notional=Decimal("4"), session_duration=timedelta(minutes=5))
    )
    assert limits == PilotLimits.model_validate(
        limits_fields(order_notional=Decimal("4"), session_duration=timedelta(minutes=5)),
        strict=True,
    )


def test_requested_limits_reject_non_positive_or_non_finite_values() -> None:
    with pytest.raises(ValidationError, match="order_notional"):
        RequestedPilotLimits.model_validate(limits_fields(order_notional=Decimal("0")), strict=True)


def test_trading_equity_marks_positions_at_executable_unwind_bids() -> None:
    assert mark_trading_equity(equity_snapshot()) == Decimal("104.00")


def test_trading_equity_marks_only_the_depth_that_is_actually_executable() -> None:
    snapshot = equity_snapshot(
        positions=(position_mark(quantity=Decimal("10"), unwind_depth=Decimal("4")),)
    )
    with pytest.raises(PilotPolicyError, match="POSITION_MARK_UNKNOWN"):
        mark_trading_equity(snapshot)


def test_unknown_position_mark_makes_the_equity_mark_unavailable() -> None:
    snapshot = equity_snapshot(positions=(position_mark(unwind_bid=None, unwind_depth=None),))
    with pytest.raises(PilotPolicyError, match="POSITION_MARK_UNKNOWN"):
        mark_trading_equity(snapshot)


def test_unknown_position_mark_makes_loss_unknown() -> None:
    assert loss_state(loss_window(position_mark_value=None)).status is LossStatus.UNKNOWN


def test_known_loss_is_measured_from_reconciled_start_equity() -> None:
    state = loss_state(loss_window())
    assert state.status is LossStatus.KNOWN
    assert state.session_start_equity == Decimal("110")
    assert state.unrealized_loss == Decimal("6.00")
    assert state.realized_loss == Decimal("0")


def test_confirmed_external_flows_do_not_count_as_loss() -> None:
    state = loss_state(
        loss_window(
            confirmed_external_flows_usd=Decimal("-6.00"),
        )
    )
    assert state.status is LossStatus.KNOWN
    assert state.unrealized_loss == Decimal("0")


def test_profitable_windows_report_zero_loss() -> None:
    state = loss_state(loss_window(session_start_equity_usd=Decimal("100")))
    assert state.realized_loss == Decimal("0")
    assert state.unrealized_loss == Decimal("0")


def test_stale_evidence_makes_loss_unknown() -> None:
    state = loss_state(
        loss_window(
            current_equity=equity_snapshot(observed_at=NOW - timedelta(minutes=10)),
            evaluated_at=NOW,
        )
    )
    assert state.status is LossStatus.UNKNOWN


def test_order_budget_consumes_the_shared_strategy_allowance() -> None:
    limits = effective_limits(requested())
    require_order_within_budget(
        limits,
        committed_gross_notional=Decimal("15"),
        order_notional=Decimal("10"),
        deployed_capital=Decimal("20"),
        recovery=False,
    )
    with pytest.raises(PilotPolicyError) as raised:
        require_order_within_budget(
            limits,
            committed_gross_notional=Decimal("16"),
            order_notional=Decimal("10"),
            deployed_capital=Decimal("20"),
            recovery=False,
        )
    assert raised.value.code == "STRATEGY_GROSS_NOTIONAL_CEILING"


def test_recovery_orders_share_the_budget_and_add_no_capital() -> None:
    limits = effective_limits(requested())
    require_order_within_budget(
        limits,
        committed_gross_notional=Decimal("15"),
        order_notional=Decimal("5"),
        deployed_capital=Decimal("50"),
        recovery=True,
    )
    with pytest.raises(PilotPolicyError) as raised:
        require_order_within_budget(
            limits,
            committed_gross_notional=Decimal("15"),
            order_notional=Decimal("5"),
            deployed_capital=Decimal("50"),
            recovery=True,
            additional_deployed_capital=Decimal("0.01"),
        )
    assert raised.value.code == "RECOVERY_DEPLOYS_CAPITAL"


def test_order_notional_ceiling_applies_to_every_single_order() -> None:
    limits = effective_limits(requested())
    with pytest.raises(PilotPolicyError) as raised:
        require_order_within_budget(
            limits,
            committed_gross_notional=Decimal("0"),
            order_notional=Decimal("10.01"),
            deployed_capital=Decimal("0"),
            recovery=False,
        )
    assert raised.value.code == "ORDER_NOTIONAL_CEILING"
