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
