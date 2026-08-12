from decimal import Decimal
from typing import Any, cast

import pytest

from polytrading.carry.compatibility import CompatibilityReason, compare_contracts
from polytrading.domain.models import Asset, InstrumentSpec, Venue
from tests.domain.factories import instrument_spec


def hyperliquid_btc() -> InstrumentSpec:
    return instrument_spec(
        instrument_id="hyperliquid:BTC",
        venue=Venue.HYPERLIQUID,
        symbol="BTC",
        index_family=None,
        oracle_family=None,
        mark_method=None,
        liquidation_method=None,
        collateral_asset="USDC",
        pnl_asset="USDC",
        funding_formula_id=None,
        funding_cap=None,
        funding_interval_hours=Decimal("1"),
        funding_payment_offset_minutes=None,
    )


def bybit_btc() -> InstrumentSpec:
    return instrument_spec(
        index_family=None,
        oracle_family=None,
        mark_method=None,
        liquidation_method=None,
        funding_formula_id=None,
        funding_payment_offset_minutes=None,
    )


def test_identical_linear_perpetual_contracts_are_compatible() -> None:
    result = compare_contracts(instrument_spec(), instrument_spec())

    assert result.compatible is True
    assert result.reasons == ()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("asset", Asset.ETH, CompatibilityReason.ASSET_MISMATCH),
        ("kind", "other_perpetual", CompatibilityReason.KIND_MISMATCH),
        ("contract_multiplier", Decimal("2"), CompatibilityReason.MULTIPLIER_MISMATCH),
        ("is_inverse", True, CompatibilityReason.INVERSE_UNSUPPORTED),
        ("index_family", "ETH", CompatibilityReason.INDEX_MISMATCH),
        ("oracle_family", "ETH", CompatibilityReason.ORACLE_MISMATCH),
        ("mark_method", "alternate_mark", CompatibilityReason.MARK_METHOD_MISMATCH),
        (
            "liquidation_method",
            "alternate_rulebook",
            CompatibilityReason.LIQUIDATION_METHOD_MISMATCH,
        ),
        ("collateral_asset", "USDC", CompatibilityReason.COLLATERAL_MISMATCH),
        ("pnl_asset", "USDC", CompatibilityReason.PNL_ASSET_MISMATCH),
        (
            "funding_formula_id",
            "alternate_formula",
            CompatibilityReason.FUNDING_FORMULA_MISMATCH,
        ),
        ("funding_cap", Decimal("0.1"), CompatibilityReason.FUNDING_CAP_MISMATCH),
        (
            "funding_interval_hours",
            Decimal("1"),
            CompatibilityReason.FUNDING_INTERVAL_MISMATCH,
        ),
        (
            "funding_payment_offset_minutes",
            60,
            CompatibilityReason.FUNDING_PAYMENT_TIME_MISMATCH,
        ),
        ("is_prelaunch", True, CompatibilityReason.PRELAUNCH_UNSUPPORTED),
    ],
)
def test_contract_mismatch_has_its_stable_reason(
    field: str, value: Any, reason: CompatibilityReason
) -> None:
    # Catches a compatibility check that silently accepts a differing or unsupported contract.
    left = instrument_spec()
    right = cast(InstrumentSpec, instrument_spec().model_copy(update={field: value}))

    result = compare_contracts(left, right)

    assert result.compatible is False
    assert result.reasons == (reason,)


@pytest.mark.parametrize(
    "field",
    [
        "index_family",
        "oracle_family",
        "mark_method",
        "liquidation_method",
        "collateral_asset",
        "pnl_asset",
        "funding_formula_id",
        "funding_cap",
        "funding_interval_hours",
        "funding_payment_offset_minutes",
    ],
)
def test_missing_required_metadata_fails_closed(field: str) -> None:
    # Catches treating unknown contract metadata as equal or using it as mismatch evidence.
    left = cast(InstrumentSpec, instrument_spec().model_copy(update={field: None}))

    result = compare_contracts(left, instrument_spec())

    assert result.compatible is False
    assert result.reasons == (f"missing_metadata:{field}",)


def test_contract_reasons_have_deterministic_enum_then_missing_order() -> None:
    left = cast(
        InstrumentSpec,
        instrument_spec().model_copy(
            update={
                "asset": Asset.ETH,
                "contract_multiplier": Decimal("2"),
                "is_inverse": True,
                "collateral_asset": "USDC",
                "funding_payment_offset_minutes": None,
                "is_prelaunch": True,
            }
        ),
    )

    result = compare_contracts(left, instrument_spec())

    assert result.reasons == (
        CompatibilityReason.ASSET_MISMATCH,
        CompatibilityReason.MULTIPLIER_MISMATCH,
        CompatibilityReason.INVERSE_UNSUPPORTED,
        CompatibilityReason.COLLATERAL_MISMATCH,
        CompatibilityReason.PRELAUNCH_UNSUPPORTED,
        "missing_metadata:funding_payment_offset_minutes",
    )


def test_hyperliquid_usdc_and_bybit_usdt_are_ineligible() -> None:
    result = compare_contracts(hyperliquid_btc(), bybit_btc())

    assert result.compatible is False
    assert CompatibilityReason.COLLATERAL_MISMATCH in result.reasons
    assert CompatibilityReason.PNL_ASSET_MISMATCH in result.reasons
