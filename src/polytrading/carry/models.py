from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import field_validator

from polytrading.domain.models import Asset, StrictRecord, Venue, normalize_utc_timestamp


class CompatibilityReason(StrEnum):
    ASSET_MISMATCH = "asset_mismatch"
    KIND_MISMATCH = "kind_mismatch"
    MULTIPLIER_MISMATCH = "multiplier_mismatch"
    INVERSE_UNSUPPORTED = "inverse_unsupported"
    INDEX_MISMATCH = "index_mismatch"
    ORACLE_MISMATCH = "oracle_mismatch"
    MARK_METHOD_MISMATCH = "mark_method_mismatch"
    LIQUIDATION_METHOD_MISMATCH = "liquidation_method_mismatch"
    COLLATERAL_MISMATCH = "collateral_mismatch"
    PNL_ASSET_MISMATCH = "pnl_asset_mismatch"
    FUNDING_FORMULA_MISMATCH = "funding_formula_mismatch"
    FUNDING_CAP_MISMATCH = "funding_cap_mismatch"
    FUNDING_INTERVAL_MISMATCH = "funding_interval_mismatch"
    FUNDING_PAYMENT_TIME_MISMATCH = "funding_payment_time_mismatch"
    PRELAUNCH_UNSUPPORTED = "prelaunch_unsupported"


class CompatibilityResult(StrictRecord):
    compatible: bool
    reasons: tuple[CompatibilityReason | str, ...]


class FundingSpreadDiagnostic(StrictRecord):
    schema_version: Literal[1]
    asset: Asset
    long_venue: Venue
    long_symbol: str
    short_venue: Venue
    short_symbol: str
    long_hourly_rate: Decimal
    short_hourly_rate: Decimal
    hourly_spread: Decimal
    diagnostic_annualized_spread: Decimal
    as_of: datetime
    compatibility: CompatibilityResult
    forecast_status: Literal["not_evaluated"] = "not_evaluated"

    @field_validator("as_of")
    @classmethod
    def require_utc_as_of(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)
