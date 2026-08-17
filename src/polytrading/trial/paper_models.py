from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import field_validator, model_validator

from polytrading.carry.economics_models import FundingDirection
from polytrading.domain.models import Asset, Decimal38x18, StrictRecord, normalize_utc_timestamp

PAPER_RESEARCH_WARNING = (
    "Research only — simulated paper position, not a live fill or trading authorization."
)


class PaperCloseReason(StrEnum):
    REGIME_REVERSED = "REGIME_REVERSED"
    MAX_HORIZON_REACHED = "MAX_HORIZON_REACHED"
    OPERATOR_CLOSED = "OPERATOR_CLOSED"


class PaperPosition(StrictRecord):
    schema_version: Literal[1]
    position_id: UUID
    source_evaluation_id: UUID
    asset: Asset
    direction: FundingDirection
    opened_at: datetime
    base_quantity: Decimal38x18
    lighter_entry_notional_usd: Decimal38x18
    dydx_entry_notional_usd: Decimal38x18
    lighter_entry_price: Decimal38x18
    dydx_entry_price: Decimal38x18
    opening_book_cycle_id: UUID
    warning: Literal[
        "Research only — simulated paper position, not a live fill or trading authorization."
    ]

    @field_validator("opened_at")
    @classmethod
    def require_utc_opened_at(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_positive_economics(self) -> PaperPosition:
        for value, label in (
            (self.base_quantity, "base quantity"),
            (self.lighter_entry_notional_usd, "Lighter entry notional"),
            (self.dydx_entry_notional_usd, "dYdX entry notional"),
            (self.lighter_entry_price, "Lighter entry price"),
            (self.dydx_entry_price, "dYdX entry price"),
        ):
            if value <= 0:
                raise ValueError(f"{label} must be positive")
        return self


class PaperPositionClosure(StrictRecord):
    schema_version: Literal[1]
    position_id: UUID
    closed_at: datetime
    close_reason: PaperCloseReason
    lighter_exit_notional_usd: Decimal38x18
    dydx_exit_notional_usd: Decimal38x18
    lighter_exit_price: Decimal38x18
    dydx_exit_price: Decimal38x18
    closing_book_cycle_id: UUID
    realized_funding_usd: Decimal38x18
    realized_pnl_usd: Decimal38x18

    @field_validator("closed_at")
    @classmethod
    def require_utc_closed_at(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_positive_exit_economics(self) -> PaperPositionClosure:
        for value, label in (
            (self.lighter_exit_notional_usd, "Lighter exit notional"),
            (self.dydx_exit_notional_usd, "dYdX exit notional"),
            (self.lighter_exit_price, "Lighter exit price"),
            (self.dydx_exit_price, "dYdX exit price"),
        ):
            if value <= 0:
                raise ValueError(f"{label} must be positive")
        return self
