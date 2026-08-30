"""Compiled pilot ceilings, conservative equity marking, and deterministic loss accounting.

The ceilings here are the single compiled copy of the approved envelope. A requested limit may
only lower one: an attempted increase is rejected with a stable code rather than clamped, so the
control server, coordinator, and signer each fail closed on the same input instead of quietly
trading under a number the operator never approved.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator

from polytrading.predictions.domain import Sha256
from polytrading.predictions.pilot.models import (
    PILOT_CEILINGS,
    LossStatus,
    PilotLimits,
    PilotLossState,
    PilotRecord,
    UtcTimestamp,
)

COMPILED_PILOT_CEILINGS = PILOT_CEILINGS

# Account evidence older than this cannot support a conservative mark (spec section 3.1).
MAXIMUM_EQUITY_EVIDENCE_AGE = timedelta(seconds=60)

PilotPolicyCode = Literal[
    "WALLET_TRADING_EQUITY_CEILING",
    "ORDER_NOTIONAL_CEILING",
    "STRATEGY_GROSS_NOTIONAL_CEILING",
    "SESSION_DURATION_CEILING",
    "SESSION_DEPLOYED_CAPITAL_CEILING",
    "CONCURRENT_STRATEGIES_CEILING",
    "SESSION_LOSS_CEILING",
    "UTC_DAY_LOSS_CEILING",
    "POSITION_MARK_UNKNOWN",
    "EQUITY_EVIDENCE_STALE",
    "RECOVERY_DEPLOYS_CAPITAL",
]

_CEILING_CODES: tuple[tuple[str, PilotPolicyCode], ...] = (
    ("wallet_trading_equity", "WALLET_TRADING_EQUITY_CEILING"),
    ("order_notional", "ORDER_NOTIONAL_CEILING"),
    ("strategy_gross_notional", "STRATEGY_GROSS_NOTIONAL_CEILING"),
    ("session_duration", "SESSION_DURATION_CEILING"),
    ("session_deployed_capital", "SESSION_DEPLOYED_CAPITAL_CEILING"),
    ("concurrent_strategies", "CONCURRENT_STRATEGIES_CEILING"),
    ("session_loss", "SESSION_LOSS_CEILING"),
    ("utc_day_loss", "UTC_DAY_LOSS_CEILING"),
)

PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


class PilotPolicyError(ValueError):
    """A requested pilot bound, mark, or budget that the compiled policy refuses."""

    def __init__(self, code: PilotPolicyCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class RequestedPilotLimits(PilotRecord):
    """Limits as the browser may request them: well-formed, but not yet bounded by ceilings."""

    wallet_trading_equity: PositiveDecimal
    order_notional: PositiveDecimal
    strategy_gross_notional: PositiveDecimal
    session_duration: Annotated[timedelta, Field(gt=timedelta(0))]
    session_deployed_capital: PositiveDecimal
    concurrent_strategies: Annotated[int, Field(ge=1)]
    session_loss: PositiveDecimal
    utc_day_loss: PositiveDecimal


class PositionMark(PilotRecord):
    """One open position with the executable unwind bid that conservatively marks it."""

    outcome_token_id: str
    quantity: PositiveDecimal
    unwind_bid: PositiveDecimal | None
    unwind_depth: NonNegativeDecimal | None
    observed_at: UtcTimestamp


class EquitySnapshot(PilotRecord):
    account_fingerprint: Sha256
    reconciled_collateral_usd: NonNegativeDecimal
    positions: tuple[PositionMark, ...]
    evidence_hashes: tuple[Sha256, ...]
    observed_at: UtcTimestamp

    @field_validator("evidence_hashes")
    @classmethod
    def _validate_evidence(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if not value:
            raise ValueError("evidence_hashes must cite the reconciled account evidence")
        if value != tuple(sorted(set(value))):
            raise ValueError("evidence_hashes must be sorted and unique")
        return value


class LossWindow(PilotRecord):
    """Everything one loss evaluation may use, at one coherent information cutoff."""

    session_start_equity_usd: NonNegativeDecimal
    utc_day_start_equity_usd: NonNegativeDecimal
    current_equity: EquitySnapshot
    realized_loss_usd: NonNegativeDecimal
    confirmed_external_flows_usd: FiniteDecimal
    evaluated_at: UtcTimestamp


def effective_limits(requested: RequestedPilotLimits) -> PilotLimits:
    """Bind a requested policy to the compiled ceilings, rejecting any attempted increase."""

    for field, code in _CEILING_CODES:
        wanted = getattr(requested, field)
        ceiling = getattr(COMPILED_PILOT_CEILINGS, field)
        if wanted > ceiling:
            raise PilotPolicyError(code, f"requested {field} {wanted} exceeds ceiling {ceiling}")
    return PilotLimits.model_validate(
        {field: getattr(requested, field) for field in PilotLimits.model_fields}, strict=True
    )


def mark_trading_equity(snapshot: EquitySnapshot) -> Decimal:
    """Mark reconciled collateral plus every position at its executable unwind bid.

    A position without a fresh bid, or with less executable depth than the position itself, has
    no conservative mark: equity is unavailable rather than optimistic.
    """

    equity = snapshot.reconciled_collateral_usd
    for position in snapshot.positions:
        if position.unwind_bid is None or position.unwind_depth is None:
            raise PilotPolicyError(
                "POSITION_MARK_UNKNOWN",
                f"position {position.outcome_token_id} has no executable unwind mark",
            )
        if position.unwind_depth < position.quantity:
            raise PilotPolicyError(
                "POSITION_MARK_UNKNOWN",
                f"position {position.outcome_token_id} exceeds executable unwind depth",
            )
        equity += position.quantity * position.unwind_bid
    return equity


def loss_state(window: LossWindow) -> PilotLossState:
    """Recompute session loss from reconciled start equity and conservative current marks."""

    evidence = window.current_equity.evidence_hashes
    try:
        _require_fresh_equity(window)
        current_equity = mark_trading_equity(window.current_equity)
    except PilotPolicyError:
        return PilotLossState(
            status=LossStatus.UNKNOWN,
            session_start_equity=None,
            realized_loss=None,
            unrealized_loss=None,
            evidence_hashes=evidence,
            evaluated_at=window.evaluated_at,
        )
    adjusted_equity = current_equity - window.confirmed_external_flows_usd
    total_loss = max(Decimal("0"), window.session_start_equity_usd - adjusted_equity)
    unrealized_loss = max(Decimal("0"), total_loss - window.realized_loss_usd)
    return PilotLossState(
        status=LossStatus.KNOWN,
        session_start_equity=window.session_start_equity_usd,
        realized_loss=window.realized_loss_usd,
        unrealized_loss=unrealized_loss,
        evidence_hashes=evidence,
        evaluated_at=window.evaluated_at,
    )


def _require_fresh_equity(window: LossWindow) -> None:
    observations = (
        window.current_equity.observed_at,
        *(position.observed_at for position in window.current_equity.positions),
    )
    for observed_at in observations:
        if (
            not -MAXIMUM_EQUITY_EVIDENCE_AGE
            <= window.evaluated_at - observed_at
            <= (MAXIMUM_EQUITY_EVIDENCE_AGE)
        ):
            raise PilotPolicyError(
                "EQUITY_EVIDENCE_STALE",
                f"account evidence observed at {observed_at.isoformat()} is not current",
            )


def require_order_within_budget(
    limits: PilotLimits,
    *,
    committed_gross_notional: Decimal,
    order_notional: Decimal,
    deployed_capital: Decimal,
    recovery: bool,
    additional_deployed_capital: Decimal = Decimal("0"),
) -> None:
    """Charge one normal or recovery order against the shared strategy and session budgets.

    Recovery legs consume the same order and strategy allowances as normal legs and may never
    add deployed capital (spec section 3.1).
    """

    if order_notional > limits.order_notional:
        raise PilotPolicyError(
            "ORDER_NOTIONAL_CEILING",
            f"order notional {order_notional} exceeds {limits.order_notional}",
        )
    if committed_gross_notional + order_notional > limits.strategy_gross_notional:
        raise PilotPolicyError(
            "STRATEGY_GROSS_NOTIONAL_CEILING",
            "strategy gross notional would exceed "
            f"{limits.strategy_gross_notional} including recovery",
        )
    if recovery and additional_deployed_capital > 0:
        raise PilotPolicyError(
            "RECOVERY_DEPLOYS_CAPITAL",
            "recovery may reduce exposure but never deploy additional capital",
        )
    if deployed_capital + additional_deployed_capital > limits.session_deployed_capital:
        raise PilotPolicyError(
            "SESSION_DEPLOYED_CAPITAL_CEILING",
            f"deployed capital would exceed {limits.session_deployed_capital}",
        )
