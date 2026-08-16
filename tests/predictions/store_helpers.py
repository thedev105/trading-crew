from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID

from polytrading.predictions.domain import PredictionRawEnvelope, PredictionVenue

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
SOURCE_HASH = "a" * 64
RAW_PAYLOAD_JSON = "{}"
RAW_PAYLOAD_HASH = sha256(RAW_PAYLOAD_JSON.encode()).hexdigest()


def raw_envelope(**overrides: Any) -> PredictionRawEnvelope:
    values: dict[str, Any] = {
        "schema_version": 1,
        "event_id": UUID("00000000-0000-0000-0000-0000000d0e01"),
        "venue": PredictionVenue.POLYMARKET,
        "endpoint": "/markets",
        "venue_timestamp": None,
        "observed_at": NOW,
        "received_monotonic_ns": 1,
        "request_latency_ms": Decimal("10"),
        "source_version": "gamma-v1",
        "payload_json": RAW_PAYLOAD_JSON,
        "source_hash": RAW_PAYLOAD_HASH,
    }
    values.update(overrides)
    return PredictionRawEnvelope(**values)
