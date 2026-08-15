from datetime import UTC, datetime
from typing import Any

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.manifest import AdapterImplementationState, VenueManifest

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)
SOURCE_HASH = "a" * 64


def venue_manifest(**overrides: Any) -> VenueManifest:
    values: dict[str, Any] = {
        "schema_version": 1,
        "venue": PredictionVenue.POLYMARKET,
        "underlying_exchange": None,
        "is_independent_liquidity": True,
        "official_sources": ("https://docs.polymarket.com/api-reference/introduction",),
        "public_capability": True,
        "authenticated_demo_capability": False,
        "authenticated_live_capability": False,
        "data_retention_status": "permitted",
        "automated_use_status": "permitted",
        "commercial_use_status": "unknown",
        "redistribution_status": "restricted",
        "model_training_status": "restricted",
        "implementation_state": AdapterImplementationState.READ_ONLY,
        "jurisdiction_review_status": "UNREVIEWED",
        "review_identity": "unit-test-fixture",
        "reviewed_at": NOW,
        "source_hashes": (SOURCE_HASH,),
        "invalidation_conditions": ("terms_of_service_change",),
    }
    values.update(overrides)
    return VenueManifest(**values)
