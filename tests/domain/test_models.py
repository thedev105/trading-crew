from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from polytrading.domain.models import (
    Asset,
    BookLevel,
    FeeSchedule,
    FundingObservation,
    Level2BookSnapshot,
    MarketSnapshot,
    RawEnvelope,
    Venue,
)
from tests.domain.factories import SOURCE_HASH, instrument_spec

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


def valid_funding(**overrides: object) -> FundingObservation:
    values: dict[str, object] = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "rate": Decimal("0.0001"),
        "interval_hours": Decimal("8"),
        "effective_at": NOW,
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return FundingObservation(**values)


def valid_market(**overrides: object) -> MarketSnapshot:
    values: dict[str, object] = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "bid": Decimal("100"),
        "ask": Decimal("101"),
        "mark": Decimal("100.5"),
        "index": Decimal("100.25"),
        "open_interest": Decimal("1000"),
        "effective_at": NOW,
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return MarketSnapshot(**values)


def valid_book_level(**overrides: object) -> BookLevel:
    values: dict[str, object] = {
        "price": Decimal("100"),
        "quantity": Decimal("1"),
        "order_count": None,
    }
    values.update(overrides)
    return BookLevel(**values)


def valid_fee(**overrides: object) -> FeeSchedule:
    values: dict[str, object] = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "tier_name": "VIP0",
        "maker_rate": Decimal("0.0002"),
        "taker_rate": Decimal("0.00055"),
        "effective_from": NOW,
        "observed_at": NOW,
        "source_url": "https://example.test/fees",
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return FeeSchedule(**values)


def test_funding_normalizes_to_hourly_rate() -> None:
    item = FundingObservation(
        schema_version=1,
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        asset=Asset.BTC,
        rate=Decimal("0.0001"),
        interval_hours=Decimal("8"),
        effective_at=NOW,
        observed_at=NOW,
        source_hash=SOURCE_HASH,
    )

    assert item.hourly_rate == Decimal("0.0000125")


def test_funding_rejects_rate_below_duckdb_decimal_scale() -> None:
    with pytest.raises(ValidationError) as exception:
        FundingObservation(
            schema_version=1,
            venue=Venue.BYBIT,
            symbol="BTCUSDT",
            asset=Asset.BTC,
            rate=Decimal("1E-19"),
            interval_hours=Decimal("8"),
            effective_at=NOW,
            observed_at=NOW,
            source_hash=SOURCE_HASH,
        )

    assert exception.value.errors()[0]["type"] == "decimal_max_places"


def test_instrument_rejects_integer_beyond_duckdb_decimal_width() -> None:
    with pytest.raises(ValidationError) as exception:
        instrument_spec(contract_multiplier=Decimal("100000000000000000000"))

    assert exception.value.errors()[0]["type"] == "decimal_whole_digits"


@pytest.mark.parametrize("rate", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_persisted_decimal38_fields_reject_non_finite_values(rate: Decimal) -> None:
    with pytest.raises(ValidationError) as exception:
        valid_funding(rate=rate)

    assert exception.value.errors()[0]["type"] == "finite_number"


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (instrument_spec, "contract_multiplier"),
        (instrument_spec, "funding_cap"),
        (instrument_spec, "funding_interval_hours"),
        (instrument_spec, "min_notional"),
        (instrument_spec, "quantity_step"),
        (instrument_spec, "price_tick"),
        (valid_funding, "rate"),
        (valid_funding, "interval_hours"),
        (valid_market, "bid"),
        (valid_market, "ask"),
        (valid_market, "mark"),
        (valid_market, "index"),
        (valid_market, "open_interest"),
        (valid_book_level, "price"),
        (valid_book_level, "quantity"),
        (valid_fee, "maker_rate"),
        (valid_fee, "taker_rate"),
    ],
)
def test_persisted_decimal38_fields_reject_unrepresentable_scale(
    factory: Callable[..., object], field: str
) -> None:
    with pytest.raises(ValidationError) as exception:
        factory(**{field: Decimal("1E-19")})

    assert exception.value.errors()[0]["type"] == "decimal_max_places"


@pytest.mark.parametrize(
    "request_latency_ms",
    [
        Decimal("-0.000001"),
        Decimal("0.0000001"),
        Decimal("1000000000000"),
        Decimal("Infinity"),
    ],
)
def test_raw_latency_rejects_values_outside_duckdb_decimal_contract(
    request_latency_ms: Decimal,
) -> None:
    with pytest.raises(ValidationError):
        RawEnvelope(
            schema_version=1,
            event_id=uuid4(),
            venue=Venue.HYPERLIQUID,
            endpoint="/info",
            venue_timestamp=NOW,
            observed_at=NOW,
            received_monotonic_ns=1,
            request_latency_ms=request_latency_ms,
            source_version="v1",
            payload_json="{}",
            source_hash=SOURCE_HASH,
        )


def test_naive_timestamp_fails_closed() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        instrument_spec(observed_at=datetime(2026, 8, 12, 12))


def test_unknown_fields_are_rejected() -> None:
    payload = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "rate": Decimal("0.0001"),
        "interval_hours": Decimal("8"),
        "effective_at": NOW,
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }

    with pytest.raises(ValidationError) as exception:
        FundingObservation.model_validate({**payload, "unexpected": "field"})

    error = exception.value.errors()[0]
    assert error["type"] == "extra_forbidden"
    assert error["loc"] == ("unexpected",)


@given(
    rate=st.decimals(
        min_value=Decimal("-1000"),
        max_value=Decimal("1000"),
        places=6,
    ),
    interval_hours=st.sampled_from(
        [Decimal("0.01"), Decimal("0.1"), Decimal("1"), Decimal("10"), Decimal("100")]
    ),
)
def test_funding_hourly_rate_preserves_rate_for_positive_intervals(
    rate: Decimal, interval_hours: Decimal
) -> None:
    item = FundingObservation(
        schema_version=1,
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        asset=Asset.BTC,
        rate=rate,
        interval_hours=interval_hours,
        effective_at=NOW,
        observed_at=NOW,
        source_hash=SOURCE_HASH,
    )

    assert item.hourly_rate * item.interval_hours == item.rate


@given(st.datetimes(timezones=st.none()))
def test_naive_observed_at_always_fails(datetime_value: datetime) -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        instrument_spec(observed_at=datetime_value)


def test_timestamps_normalize_to_utc() -> None:
    eastern = timezone(-timedelta(hours=4))
    item = FundingObservation(
        schema_version=1,
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        asset=Asset.BTC,
        rate=Decimal("0.0001"),
        interval_hours=Decimal("8"),
        effective_at=datetime(2026, 8, 12, 8, tzinfo=eastern),
        observed_at=datetime(2026, 8, 12, 8, tzinfo=eastern),
        source_hash=SOURCE_HASH,
    )

    assert item.effective_at == NOW
    assert item.observed_at == NOW


@pytest.mark.parametrize("field", ["contract_multiplier", "funding_interval_hours"])
def test_instrument_spec_rejects_non_positive_decimal_constraints(field: str) -> None:
    with pytest.raises(ValidationError):
        instrument_spec(**{field: Decimal("0")})


@pytest.mark.parametrize("field", ["bid", "ask", "mark", "index"])
def test_market_snapshot_rejects_non_positive_price_constraints(field: str) -> None:
    values = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "bid": Decimal("100"),
        "ask": Decimal("101"),
        "mark": Decimal("100.5"),
        "index": Decimal("100.25"),
        "open_interest": Decimal("1000"),
        "effective_at": NOW,
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values[field] = Decimal("0")

    with pytest.raises(ValidationError):
        MarketSnapshot(**values)


def test_market_snapshot_rejects_crossed_quote() -> None:
    with pytest.raises(ValidationError, match="ask must be greater than or equal to bid"):
        MarketSnapshot(
            schema_version=1,
            venue=Venue.BYBIT,
            symbol="BTCUSDT",
            asset=Asset.BTC,
            bid=Decimal("101"),
            ask=Decimal("100"),
            mark=Decimal("100.5"),
            index=Decimal("100.25"),
            open_interest=None,
            effective_at=NOW,
            observed_at=NOW,
            source_hash=SOURCE_HASH,
        )


def test_level2_book_requires_both_sides_in_price_order_without_crossing() -> None:
    book = Level2BookSnapshot(
        schema_version=1,
        cycle_id=uuid4(),
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        asset=Asset.BTC,
        bids=(
            BookLevel(price=Decimal("100"), quantity=Decimal("1"), order_count=1),
            BookLevel(price=Decimal("99"), quantity=Decimal("2"), order_count=None),
        ),
        asks=(
            BookLevel(price=Decimal("101"), quantity=Decimal("1"), order_count=1),
            BookLevel(price=Decimal("102"), quantity=Decimal("2"), order_count=None),
        ),
        depth_limit=20,
        sequence="42",
        effective_at=NOW,
        observed_at=NOW,
        source_hash=SOURCE_HASH,
    )

    assert book.bids[0].price == Decimal("100")


@pytest.mark.parametrize(
    ("bids", "asks"),
    [
        ((), (BookLevel(price=Decimal("101"), quantity=Decimal("1"), order_count=None),)),
        ((BookLevel(price=Decimal("100"), quantity=Decimal("1"), order_count=None),), ()),
        (
            (
                BookLevel(price=Decimal("99"), quantity=Decimal("1"), order_count=None),
                BookLevel(price=Decimal("100"), quantity=Decimal("1"), order_count=None),
            ),
            (BookLevel(price=Decimal("101"), quantity=Decimal("1"), order_count=None),),
        ),
        (
            (BookLevel(price=Decimal("100"), quantity=Decimal("1"), order_count=None),),
            (
                BookLevel(price=Decimal("102"), quantity=Decimal("1"), order_count=None),
                BookLevel(price=Decimal("101"), quantity=Decimal("1"), order_count=None),
            ),
        ),
        (
            (BookLevel(price=Decimal("101"), quantity=Decimal("1"), order_count=None),),
            (BookLevel(price=Decimal("101"), quantity=Decimal("1"), order_count=None),),
        ),
    ],
)
def test_level2_book_rejects_empty_unsorted_or_crossed_sides(
    bids: tuple[BookLevel, ...], asks: tuple[BookLevel, ...]
) -> None:
    with pytest.raises(ValidationError):
        Level2BookSnapshot(
            schema_version=1,
            cycle_id=uuid4(),
            venue=Venue.BYBIT,
            symbol="BTCUSDT",
            asset=Asset.BTC,
            bids=bids,
            asks=asks,
            depth_limit=20,
            sequence=None,
            effective_at=NOW,
            observed_at=NOW,
            source_hash=SOURCE_HASH,
        )


@pytest.mark.parametrize("field", ["price", "quantity"])
def test_book_level_rejects_non_positive_values(field: str) -> None:
    values = {"price": Decimal("100"), "quantity": Decimal("1"), "order_count": None}
    values[field] = Decimal("0")

    with pytest.raises(ValidationError):
        BookLevel(**values)


@pytest.mark.parametrize("source_hash", ["A" * 64, "a" * 63, "g" * 64])
def test_records_reject_non_sha256_lowercase_source_hash(source_hash: str) -> None:
    with pytest.raises(ValidationError, match="64 lowercase hexadecimal"):
        instrument_spec(source_hash=source_hash)


def test_decimal_fields_serialize_as_json_strings() -> None:
    item = FundingObservation(
        schema_version=1,
        venue=Venue.BYBIT,
        symbol="BTCUSDT",
        asset=Asset.BTC,
        rate=Decimal("0.0001"),
        interval_hours=Decimal("8"),
        effective_at=NOW,
        observed_at=NOW,
        source_hash=SOURCE_HASH,
    )

    assert item.model_dump(mode="json")["rate"] == "0.0001"
    assert item.model_dump(mode="json")["hourly_rate"] == "0.0000125"


def test_records_are_frozen() -> None:
    item = instrument_spec()

    with pytest.raises(ValidationError, match="frozen"):
        item.symbol = "ETHUSDT"


def test_raw_envelope_normalizes_venue_timestamp() -> None:
    item = RawEnvelope(
        schema_version=1,
        event_id=uuid4(),
        venue=Venue.HYPERLIQUID,
        endpoint="/info",
        venue_timestamp=NOW,
        observed_at=NOW,
        received_monotonic_ns=1,
        request_latency_ms=Decimal("2.5"),
        source_version="v1",
        payload_json="{}",
        source_hash=SOURCE_HASH,
    )

    assert item.venue_timestamp == NOW


def test_fee_schedule_normalizes_effective_from_timestamp() -> None:
    item = FeeSchedule(
        schema_version=1,
        venue=Venue.BYBIT,
        tier_name="VIP0",
        maker_rate=Decimal("0.0002"),
        taker_rate=Decimal("0.00055"),
        effective_from=NOW,
        observed_at=NOW,
        source_url="https://example.test/fees",
        source_hash=SOURCE_HASH,
    )

    assert item.effective_from == NOW
