from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from polytrading.carry.study import _prepare_blocks
from polytrading.carry.study_models import (
    AvailabilityClass,
    CoverageSummary,
    IncompleteBlock,
    PairedFundingBlock,
)
from polytrading.domain.models import Asset, Venue
from tests.carry.study_helpers import at, complete_block, funding_row

START = at("2026-01-01T00:00:00Z")
END = at("2026-01-01T08:00:00Z")
KNOWN_AS_OF = END + timedelta(minutes=5)


def prepare(rows: tuple = (), **overrides: object):
    selected = rows or complete_block(START)
    values = {
        "asset": Asset.BTC,
        "start": START,
        "end": END,
        "known_as_of": KNOWN_AS_OF,
        "bybit_rows": tuple(row for row in selected if row.venue is Venue.BYBIT),
        "hyperliquid_rows": tuple(row for row in selected if row.venue is Venue.HYPERLIQUID),
    }
    values.update(overrides)
    return _prepare_blocks(**values)


def test_complete_native_intervals_form_one_signed_eight_hour_block() -> None:
    prepared = prepare()

    assert prepared.paired_blocks == (
        PairedFundingBlock(
            schema_version=1,
            block_start=START,
            block_end=END,
            bybit_rate=Decimal("0.00008"),
            hyperliquid_rate=Decimal("0.00016"),
            spread=Decimal("0.00008"),
        ),
    )
    assert prepared.coverage == CoverageSummary(
        schema_version=1,
        requested_blocks=1,
        bybit_complete_blocks=1,
        hyperliquid_complete_blocks=1,
        paired_complete_blocks=1,
        coverage_ratio=Decimal(1),
        first_paired_at=END,
        last_paired_at=END,
        incomplete_blocks=(),
    )
    assert prepared.availability is AvailabilityClass.POINT_IN_TIME


def test_start_boundary_is_excluded_and_end_boundary_is_included() -> None:
    rows = complete_block(START)
    start_boundary = funding_row(
        Venue.HYPERLIQUID,
        START,
        observed_at=START + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="outside requested study window"):
        prepare((start_boundary, *rows))

    assert prepare(rows).paired_blocks[0].block_end == END


@pytest.mark.parametrize(
    ("start", "end", "known_as_of", "message"),
    [
        (END, START, KNOWN_AS_OF, "study start must precede end"),
        (
            START + timedelta(hours=1),
            END,
            KNOWN_AS_OF,
            "study boundaries must align",
        ),
        (START, END, END - timedelta(microseconds=1), "known_as_of must not precede end"),
    ],
)
def test_study_window_validation(
    start: datetime, end: datetime, known_as_of: datetime, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        prepare(start=start, end=end, known_as_of=known_as_of)


def test_identical_revisions_are_economically_deduplicated_with_full_provenance() -> None:
    rows = complete_block(START)
    original = rows[0]
    revision = original.model_copy(
        update={
            "observed_at": original.observed_at + timedelta(minutes=1),
            "source_hash": "b" * 64,
        }
    )

    prepared = prepare((*rows, revision))

    assert prepared.paired_blocks[0].bybit_rate == original.rate
    assert prepared.source_hashes == ("a" * 64, "b" * 64)


def test_conflicting_revisions_fail_closed() -> None:
    rows = complete_block(START)
    conflict = rows[0].model_copy(
        update={
            "rate": rows[0].rate + Decimal("0.00001"),
            "observed_at": rows[0].observed_at + timedelta(minutes=1),
            "source_hash": "b" * 64,
        }
    )

    with pytest.raises(ValueError, match="conflicting funding revisions"):
        prepare((*rows, conflict))


def test_missing_native_interval_is_reported_and_never_filled() -> None:
    rows = complete_block(START)
    prepared = prepare(tuple(row for row in rows if row.effective_at != START + timedelta(hours=4)))

    assert prepared.paired_blocks == ()
    assert prepared.availability is AvailabilityClass.INSUFFICIENT_DATA
    assert prepared.coverage.incomplete_blocks == (
        IncompleteBlock(
            schema_version=1,
            block_end=END,
            reason_codes=("HYPERLIQUID_INTERVAL_UNDERFILLED",),
        ),
    )


def test_native_intervals_cannot_overfill_a_common_block() -> None:
    rows = complete_block(START)
    extra = funding_row(
        Venue.HYPERLIQUID,
        END - timedelta(minutes=30),
        observed_at=END - timedelta(minutes=29),
        rate=Decimal("0.00002"),
        interval_hours=Decimal(1),
        source_hash="b" * 64,
    )

    with pytest.raises(ValueError, match="native funding intervals exceed eight-hour block"):
        prepare((*rows, extra))


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            funding_row(Venue.BYBIT, END, symbol="ETHUSDT"),
            "funding symbol does not match study asset and venue",
        ),
        (
            funding_row(Venue.BYBIT, END, asset=Asset.ETH),
            "funding asset does not match study asset",
        ),
        (
            funding_row(
                Venue.BYBIT,
                END,
                observed_at=END - timedelta(microseconds=1),
            ),
            "funding observation precedes settlement",
        ),
    ],
)
def test_record_identity_and_time_are_validated(row: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        prepare((row, *complete_block(START)))


def test_availability_threshold_is_exactly_five_minutes() -> None:
    assert prepare(complete_block(START, observation_lag=timedelta(minutes=5))).availability is (
        AvailabilityClass.POINT_IN_TIME
    )
    assert (
        prepare(
            complete_block(START, observation_lag=timedelta(minutes=5, microseconds=1)),
            known_as_of=END + timedelta(minutes=6),
        ).availability
        is AvailabilityClass.HISTORICAL_RECONSTRUCTION
    )


def test_report_models_reject_invalid_arithmetic_and_counts() -> None:
    with pytest.raises(ValidationError, match="spread must equal"):
        PairedFundingBlock(
            schema_version=1,
            block_start=START,
            block_end=END,
            bybit_rate=Decimal("1"),
            hyperliquid_rate=Decimal("2"),
            spread=Decimal("2"),
        )
    with pytest.raises(ValidationError):
        CoverageSummary(
            schema_version=1,
            requested_blocks=1,
            bybit_complete_blocks=1,
            hyperliquid_complete_blocks=1,
            paired_complete_blocks=2,
            coverage_ratio=Decimal("2"),
            first_paired_at=END,
            last_paired_at=END,
            incomplete_blocks=(),
        )
