from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from polytrading.predictions.economics_models import ScanReport, deterministic_scan_report_id

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000003001")
PROOF_ID = UUID("00000000-0000-0000-0000-000000006001")


def scan_report(**overrides: Any) -> ScanReport:
    values: dict[str, Any] = {
        "candidate_id": CANDIDATE_ID,
        "proof_id": None,
        "decision": "INSUFFICIENT_EVIDENCE",
        "reason": "no proof compiled",
        "economics": None,
        "policy_id": "research-v1",
        "policy_version": "1",
        "as_of": NOW,
        "observed_at": NOW,
    }
    values.update(overrides)
    if "report_id" not in values:
        values["report_id"] = deterministic_scan_report_id(
            candidate_id=values["candidate_id"],
            proof_id=values["proof_id"],
            decision=values["decision"],
            reason=values["reason"],
            economics=values["economics"],
            policy_id=values["policy_id"],
            policy_version=values["policy_version"],
            as_of=values["as_of"],
        )
    return ScanReport(**values)
