from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ImmediateOrderType,
    _intent_fingerprint,
    deterministic_intent_id,
)


def execution_intent_fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "schema_version": 1,
        "intent_id": UUID("27d29661-47ff-5f4e-8136-92c9f4f9a782"),
        "plan_id": UUID("0d7c250b-0a21-55f3-a897-8bc98c59f904"),
        "leg_sequence": 0,
        "venue": PredictionVenue.POLYMARKET,
        "token_id": "217426",
        "side": "buy",
        "limit_price": Decimal("0.51"),
        "base_size": Decimal("10"),
        "maximum_spend": Decimal("5.10"),
        "order_type": ImmediateOrderType.FAK,
        "fee_rate_bps_cap": 100,
        "rounding_mode": "ROUND_DOWN",
        "account_fingerprint": "a" * 64,
        "capability_fingerprint": "b" * 64,
        "created_at": datetime(2026, 8, 25, 16, tzinfo=UTC),
        "deadline": datetime(2026, 8, 25, 16, 0, 5, tzinfo=UTC),
        "protocol_version": "polymarket-clob-2026-08-25-v1",
        "intent_fingerprint": "c" * 64,
    }
    fields.update(overrides)
    if not isinstance(fields["order_type"], ImmediateOrderType):
        return fields
    projection = ExecutionIntent.model_construct(**fields)
    fields["intent_fingerprint"] = _intent_fingerprint(projection)
    projection = ExecutionIntent.model_construct(**fields)
    fields["intent_id"] = deterministic_intent_id(projection)
    return fields


def live_execution_plan_fields(**overrides: object) -> dict[str, object]:
    observed_at = datetime(2026, 8, 25, 16, tzinfo=UTC)
    fields: dict[str, object] = {
        "schema_version": 1,
        "plan_id": UUID("7485040f-95e8-43f5-8773-0bfbd9f227fa"),
        "proposal_id": UUID("4d4da28d-db55-4d18-b2c8-a5fd85d45dd0"),
        "candidate_id": UUID("8bb7872a-e36b-4318-b82d-2d5e081ba03a"),
        "proof_artifact_hash": "a" * 64,
        "economics_report_hash": "b" * 64,
        "venue": PredictionVenue.POLYMARKET,
        "account_fingerprint": "c" * 64,
        "book_snapshot_ids": (UUID("4f4c9c8a-5712-4e4b-a80c-818d6a922487"),),
        "fee_evidence_ids": (UUID("c9e2e6ca-5744-41b1-9f6c-6142103c0b05"),),
        "information_cutoff": datetime(2026, 8, 25, 15, 59, tzinfo=UTC),
        "token_ids": ("217426", "217427"),
        "leg_order_types": (ImmediateOrderType.FAK, ImmediateOrderType.FOK),
        "maximum_size": Decimal("10"),
        "maximum_spend": Decimal("5.10"),
        "limit_prices": (Decimal("0.51"), Decimal("0.52")),
        "fee_rate_bps_caps": (100, 100),
        "assigned_capital": Decimal("5.10"),
        "incomplete_exposure_reserve": Decimal("0"),
        "risk_policy_hash": "d" * 64,
        "manifest_hash": "e" * 64,
        "eligibility_hash": "f" * 64,
        "protocol_hash": "1" * 64,
        "capability_fingerprint": "2" * 64,
        "book_deadline": datetime(2026, 8, 25, 16, 0, 5, tzinfo=UTC),
        "proof_deadline": datetime(2026, 8, 25, 16, 0, 5, tzinfo=UTC),
        "economics_deadline": datetime(2026, 8, 25, 16, 0, 5, tzinfo=UTC),
        "account_deadline": datetime(2026, 8, 25, 16, 0, 5, tzinfo=UTC),
        "geoblock_deadline": datetime(2026, 8, 25, 16, 0, 5, tzinfo=UTC),
        "kill_conditions": ("stale_book",),
        "unwind_conditions": ("unexpected_fill",),
        "plan_fingerprint": "3" * 64,
        "observed_at": observed_at,
    }
    fields.update(overrides)
    return fields
