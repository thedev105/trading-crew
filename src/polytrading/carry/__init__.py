"""Deterministic, research-only perpetual funding diagnostics."""

from polytrading.carry.compatibility import CompatibilityReason, compare_contracts
from polytrading.carry.models import CompatibilityResult, FundingSpreadDiagnostic
from polytrading.carry.normalize import compare_latest_funding, funding_spread

__all__ = [
    "CompatibilityReason",
    "CompatibilityResult",
    "FundingSpreadDiagnostic",
    "compare_contracts",
    "compare_latest_funding",
    "funding_spread",
]
