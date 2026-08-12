from polytrading.carry.models import CompatibilityReason, CompatibilityResult
from polytrading.domain.models import InstrumentKind, InstrumentSpec

_METADATA_COMPARISONS = (
    ("index_family", CompatibilityReason.INDEX_MISMATCH),
    ("oracle_family", CompatibilityReason.ORACLE_MISMATCH),
    ("mark_method", CompatibilityReason.MARK_METHOD_MISMATCH),
    ("liquidation_method", CompatibilityReason.LIQUIDATION_METHOD_MISMATCH),
    ("collateral_asset", CompatibilityReason.COLLATERAL_MISMATCH),
    ("pnl_asset", CompatibilityReason.PNL_ASSET_MISMATCH),
    ("funding_formula_id", CompatibilityReason.FUNDING_FORMULA_MISMATCH),
    ("funding_cap", CompatibilityReason.FUNDING_CAP_MISMATCH),
    ("funding_interval_hours", CompatibilityReason.FUNDING_INTERVAL_MISMATCH),
    (
        "funding_payment_offset_minutes",
        CompatibilityReason.FUNDING_PAYMENT_TIME_MISMATCH,
    ),
)


def compare_contracts(left: InstrumentSpec, right: InstrumentSpec) -> CompatibilityResult:
    """Return every proven incompatibility, or missing evidence, in stable order."""
    reasons: list[CompatibilityReason] = []
    missing_metadata: list[str] = []

    if left.asset != right.asset:
        reasons.append(CompatibilityReason.ASSET_MISMATCH)
    if (
        left.kind is not InstrumentKind.LINEAR_PERPETUAL
        or right.kind is not InstrumentKind.LINEAR_PERPETUAL
    ):
        reasons.append(CompatibilityReason.KIND_MISMATCH)
    if left.contract_multiplier != right.contract_multiplier:
        reasons.append(CompatibilityReason.MULTIPLIER_MISMATCH)
    if left.is_inverse or right.is_inverse:
        reasons.append(CompatibilityReason.INVERSE_UNSUPPORTED)

    for field, mismatch_reason in _METADATA_COMPARISONS:
        left_value = getattr(left, field)
        right_value = getattr(right, field)
        if left_value is None or right_value is None:
            missing_metadata.append(f"missing_metadata:{field}")
        elif left_value != right_value:
            reasons.append(mismatch_reason)

    if left.is_prelaunch or right.is_prelaunch:
        reasons.append(CompatibilityReason.PRELAUNCH_UNSUPPORTED)

    all_reasons = tuple(reasons + missing_metadata)
    return CompatibilityResult(compatible=not all_reasons, reasons=all_reasons)
