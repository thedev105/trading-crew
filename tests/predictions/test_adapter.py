from dataclasses import replace

import pytest

from polytrading.predictions.adapter import (
    PredictionAdapterBatch,
    PredictionAdapterBatchIntegrityError,
    validate_prediction_adapter_batch,
)
from tests.predictions.domain_helpers import market_record, raw_envelope


def test_validate_prediction_adapter_batch_rejects_hash_mismatch() -> None:
    raw = raw_envelope(payload_json="{}")
    tampered = raw.model_copy(update={"source_hash": "0" * 64})
    with pytest.raises(PredictionAdapterBatchIntegrityError) as error:
        validate_prediction_adapter_batch(PredictionAdapterBatch(raw=(tampered,), normalized=()))
    assert error.value.code == "raw_source_hash_mismatch"


def test_validate_prediction_adapter_batch_rejects_orphaned_normalized_lineage() -> None:
    raw = raw_envelope()
    orphan_market = market_record(venue=raw.venue, raw_hash="f" * 64)
    with pytest.raises(PredictionAdapterBatchIntegrityError) as error:
        validate_prediction_adapter_batch(
            PredictionAdapterBatch(raw=(raw,), normalized=(orphan_market,))
        )
    assert error.value.code == "normalized_lineage_mismatch"


def test_validate_prediction_adapter_batch_accepts_matching_lineage() -> None:
    raw = raw_envelope()
    market = market_record(venue=raw.venue, raw_hash=raw.source_hash)
    validate_prediction_adapter_batch(PredictionAdapterBatch(raw=(raw,), normalized=(market,)))


def test_validate_prediction_adapter_batch_rejects_cross_venue_lineage() -> None:
    from polytrading.predictions.domain import PredictionVenue

    raw = raw_envelope(venue=PredictionVenue.POLYMARKET)
    market = market_record(
        venue=PredictionVenue.KALSHI, negative_risk=None, raw_hash=raw.source_hash
    )
    with pytest.raises(PredictionAdapterBatchIntegrityError) as error:
        validate_prediction_adapter_batch(PredictionAdapterBatch(raw=(raw,), normalized=(market,)))
    assert error.value.code == "normalized_lineage_mismatch"


def test_prediction_adapter_warning_requires_typed_fields() -> None:
    from polytrading.predictions.adapter import PredictionAdapterWarning
    from polytrading.predictions.domain import PredictionVenue

    warning = PredictionAdapterWarning(
        code="TEST_WARNING",
        venue=PredictionVenue.POLYMARKET,
        endpoint="/markets",
        market_id="0xcondition",
        message="test",
    )
    assert warning.venue is PredictionVenue.POLYMARKET

    with pytest.raises(TypeError):
        replace(warning, venue="polymarket")
