from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from polytrading.predictions.attestations import RuleAttestation
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.propositions import PropositionSpan

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
HASH = "a" * 64
RULE_VERSION_ID = UUID("00000000-0000-0000-0000-000000005001")
ATTESTATION_ID = UUID("00000000-0000-0000-0000-000000005101")


def supporting_span(**overrides: Any) -> PropositionSpan:
    values: dict[str, Any] = {
        "start_char": 0,
        "end_char": 10,
        "exact_text": "resolves YES",
        "rule_source_hash": HASH,
    }
    values.update(overrides)
    return PropositionSpan(**values)


def rule_attestation(**overrides: Any) -> RuleAttestation:
    values: dict[str, Any] = {
        "schema_version": 1,
        "attestation_id": ATTESTATION_ID,
        "venue": PredictionVenue.POLYMARKET,
        "market_id": "0xcondition",
        "rule_version_id": RULE_VERSION_ID,
        "rule_source_hash": HASH,
        "payout_unit": "usdc_1_per_share",
        "winner_payout_per_share": Decimal("1"),
        "loser_payout_per_share": Decimal("0"),
        "outcome_set_exhaustive": True,
        "void_or_invalid_possible": False,
        "void_behavior": "unknown",
        "tie_possible": False,
        "tie_behavior": None,
        "resolution_source_attested": "https://example.test/rules",
        "deadline_utc": None,
        "threshold_text": None,
        "threshold_inclusive": None,
        "supporting_spans": (supporting_span(),),
        "review_identity": "reviewer@example.test",
        "reviewed_at": NOW,
    }
    values.update(overrides)
    return RuleAttestation(**values)
