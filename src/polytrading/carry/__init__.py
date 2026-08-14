"""Deterministic, research-only perpetual funding diagnostics."""

from polytrading.carry.audit import AuditStatus, CarryAuditor, CarryAuditReport
from polytrading.carry.compatibility import CompatibilityReason, compare_contracts
from polytrading.carry.economics import CandidateEconomicsEvaluator
from polytrading.carry.economics_models import CandidateEconomicsReport, EconomicsDecision
from polytrading.carry.models import CompatibilityResult, FundingSpreadDiagnostic
from polytrading.carry.normalize import compare_latest_funding, funding_spread

__all__ = [
    "AuditStatus",
    "CandidateEconomicsEvaluator",
    "CandidateEconomicsReport",
    "CarryAuditReport",
    "CarryAuditor",
    "CompatibilityReason",
    "CompatibilityResult",
    "EconomicsDecision",
    "FundingSpreadDiagnostic",
    "compare_contracts",
    "compare_latest_funding",
    "funding_spread",
]
