from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from polytrading.predictions.domain import (
    PredictionBookSnapshot,
    PredictionRecord,
    PredictionSource,
    PredictionVenue,
)
from tests.predictions.domain_helpers import (
    NOW,
    RAW_PAYLOAD_HASH,
    fee_rate,
    level,
    market_record,
    prediction_book_snapshot,
    raw_envelope,
    rule_version,
    trade_record,
)


def test_prediction_record_forbids_extra_and_mutation_and_requires_aware_utc() -> None:
    class _Probe(PredictionRecord):
        observed_at: datetime

    with pytest.raises(ValidationError):
        _Probe(observed_at=NOW, unexpected=1)

    probe = _Probe(observed_at=NOW)
    with pytest.raises(ValidationError):
        probe.observed_at = NOW

    with pytest.raises(ValidationError):
        _Probe(observed_at=datetime(2026, 8, 15, 12, 0, 0))


def test_prediction_record_normalizes_non_utc_aware_timestamps() -> None:
    class _Probe(PredictionRecord):
        observed_at: datetime

    eastern = timezone(-timedelta(hours=4))
    probe = _Probe(observed_at=datetime(2026, 8, 15, 8, tzinfo=eastern))
    assert probe.observed_at == NOW
    assert probe.observed_at.utcoffset() == timedelta(0)


def test_prediction_venue_and_source_share_exact_two_values() -> None:
    assert {member.value for member in PredictionVenue} == {"polymarket", "kalshi"}
    assert {member.value for member in PredictionSource} == {"polymarket", "kalshi"}


def test_raw_envelope_requires_sha256_source_hash() -> None:
    with pytest.raises(ValidationError):
        raw_envelope(source_hash="not-a-hash")
    assert raw_envelope().source_hash == RAW_PAYLOAD_HASH


def test_market_record_negative_risk_is_none_for_kalshi() -> None:
    market = market_record(venue=PredictionVenue.KALSHI, negative_risk=None)
    assert market.negative_risk is None

    with pytest.raises(ValidationError, match="negative_risk"):
        market_record(venue=PredictionVenue.KALSHI, negative_risk=False)


def test_market_record_requires_matching_token_and_outcome_counts() -> None:
    with pytest.raises(ValidationError, match="align"):
        market_record(outcomes=("Yes", "No"), outcome_token_ids=("111",))


def test_market_record_rejects_empty_outcomes() -> None:
    with pytest.raises(ValidationError):
        market_record(outcomes=())


def test_rule_version_links_back_to_its_market() -> None:
    market = market_record()
    version = rule_version(market_id=market.market_id, rule_version_id=market.rule_version_id)
    assert version.market_id == market.market_id
    assert version.rule_version_id == market.rule_version_id


def test_prediction_book_snapshot_rejects_crossed_or_misordered_book() -> None:
    with pytest.raises(ValidationError, match="descending"):
        prediction_book_snapshot(bids=(level("0.40", "10"), level("0.45", "10")))
    with pytest.raises(ValidationError, match="ascending"):
        prediction_book_snapshot(asks=(level("0.70", "10"), level("0.65", "10")))
    with pytest.raises(ValidationError, match="cross"):
        prediction_book_snapshot(bids=(level("0.60", "10"),), asks=(level("0.55", "10"),))


def test_prediction_book_snapshot_requires_both_sides() -> None:
    with pytest.raises(ValidationError):
        prediction_book_snapshot(bids=())
    with pytest.raises(ValidationError):
        prediction_book_snapshot(asks=())


def test_trade_and_book_prices_are_bounded_probabilities() -> None:
    with pytest.raises(ValidationError):
        trade_record(price=Decimal("1.01"))
    with pytest.raises(ValidationError):
        trade_record(price=Decimal("0"))
    with pytest.raises(ValidationError):
        trade_record(price=Decimal("1"))


def test_fee_rate_allows_a_venue_wide_rate_with_no_market_id() -> None:
    rate = fee_rate(market_id=None)
    assert rate.market_id is None


@given(offset_hours=st.integers(min_value=-23, max_value=23))
def test_market_record_round_trips_through_json_for_any_timezone_offset(
    offset_hours: int,
) -> None:
    tz = timezone(timedelta(hours=offset_hours))
    market = market_record(retrieved_at=NOW.astimezone(tz))
    restored = type(market).model_validate_json(market.model_dump_json())
    assert restored == market


def test_prediction_book_snapshot_round_trips_through_json() -> None:
    snapshot = prediction_book_snapshot()
    restored = PredictionBookSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored == snapshot
