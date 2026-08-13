from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from polytrading.domain.models import Asset, Venue
from polytrading.venues.funding_cycle_models import (
    FUNDING_CYCLE_PROTOCOL_VERSION,
    FUNDING_CYCLE_WARNINGS,
    FundingCaptureOutcome,
    FundingCollectionCycle,
    FundingCycleItem,
    FundingCycleStatus,
    InstrumentCaptureOutcome,
    validate_cycle_timing,
)

CYCLE_END = datetime(2026, 8, 13, 17, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def item(**overrides: object) -> FundingCycleItem:
    values: dict[str, object] = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "asset": Asset.BTC,
        "symbol": "BTCUSDT",
        "instrument_outcome": InstrumentCaptureOutcome.CAPTURED,
        "funding_outcome": FundingCaptureOutcome.CAPTURED,
        "instrument_observed_at": CYCLE_END + timedelta(minutes=1),
        "funding_effective_at": CYCLE_END,
        "funding_observed_at": CYCLE_END + timedelta(minutes=2),
        "instrument_source_hashes": (HASH_A,),
        "funding_source_hashes": (HASH_B,),
        "reason_codes": (),
    }
    values.update(overrides)
    return FundingCycleItem(**values)


def complete_items() -> tuple[FundingCycleItem, ...]:
    return (
        item(
            funding_outcome=FundingCaptureOutcome.NO_SETTLEMENT,
            funding_effective_at=None,
            funding_observed_at=CYCLE_END + timedelta(minutes=1),
        ),
        item(
            venue=Venue.HYPERLIQUID,
            symbol="BTC",
            instrument_source_hashes=(HASH_C,),
            funding_source_hashes=(HASH_D,),
        ),
    )


def cycle(**overrides: object) -> FundingCollectionCycle:
    items = complete_items()
    values: dict[str, object] = {
        "schema_version": 1,
        "protocol_version": FUNDING_CYCLE_PROTOCOL_VERSION,
        "cycle_id": UUID("00000000-0000-0000-0000-000000000901"),
        "cycle_end": CYCLE_END,
        "assets": (Asset.BTC,),
        "venues": (Venue.BYBIT, Venue.HYPERLIQUID),
        "request_started_at": CYCLE_END + timedelta(seconds=30),
        "request_completed_at": CYCLE_END + timedelta(minutes=2),
        "items": items,
        "status": FundingCycleStatus.COMPLETE,
        "source_hashes": (HASH_A, HASH_B, HASH_C, HASH_D),
        "warnings": FUNDING_CYCLE_WARNINGS,
    }
    values.update(overrides)
    return FundingCollectionCycle(**values)


def test_cycle_timing_accepts_the_inclusive_point_in_time_cutoff() -> None:
    cycle_end, now, is_late = validate_cycle_timing(CYCLE_END, CYCLE_END + timedelta(minutes=5))

    assert cycle_end == CYCLE_END
    assert now == CYCLE_END + timedelta(minutes=5)
    assert is_late is False


def test_cycle_timing_normalizes_aware_values_and_marks_first_late_microsecond() -> None:
    eastern = timezone(timedelta(hours=-4))

    cycle_end, now, is_late = validate_cycle_timing(
        CYCLE_END.astimezone(eastern),
        (CYCLE_END + timedelta(minutes=5, microseconds=1)).astimezone(eastern),
    )

    assert cycle_end == CYCLE_END
    assert now == CYCLE_END + timedelta(minutes=5, microseconds=1)
    assert is_late is True


@pytest.mark.parametrize(
    ("cycle_end", "now", "message"),
    [
        (
            CYCLE_END.replace(minute=1),
            CYCLE_END + timedelta(minutes=2),
            "cycle end must align to a whole UTC hour",
        ),
        (
            CYCLE_END.replace(tzinfo=None),
            CYCLE_END,
            "timestamp must be timezone-aware",
        ),
        (
            CYCLE_END,
            CYCLE_END - timedelta(microseconds=1),
            "collection clock precedes cycle end",
        ),
    ],
)
def test_cycle_timing_rejects_invalid_boundaries(
    cycle_end: datetime, now: datetime, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_cycle_timing(cycle_end, now)


@pytest.mark.parametrize(
    "overrides",
    [
        {"instrument_observed_at": None},
        {
            "instrument_outcome": InstrumentCaptureOutcome.FAILED,
            "reason_codes": ("INSTRUMENT_FAILED:bybit:TimeoutError",),
        },
        {"funding_effective_at": None},
        {"funding_observed_at": None},
        {
            "funding_outcome": FundingCaptureOutcome.NO_SETTLEMENT,
            "funding_effective_at": None,
            "funding_observed_at": None,
        },
    ],
)
def test_item_outcomes_require_consistent_component_timestamps(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="outcome timestamps are inconsistent"):
        item(**overrides)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"symbol": "BTC"}, "symbol does not match venue and asset"),
        (
            {"instrument_source_hashes": (HASH_B, HASH_A)},
            "source hashes must be sorted and unique",
        ),
        (
            {"reason_codes": ("Z", "A")},
            "reason codes must be sorted and unique",
        ),
        (
            {
                "instrument_outcome": InstrumentCaptureOutcome.FAILED,
                "instrument_observed_at": None,
                "instrument_source_hashes": (),
            },
            "reason codes do not match component outcomes",
        ),
        (
            {
                "venue": Venue.HYPERLIQUID,
                "symbol": "BTC",
                "funding_outcome": FundingCaptureOutcome.NO_SETTLEMENT,
                "funding_effective_at": None,
                "funding_observed_at": CYCLE_END + timedelta(minutes=1),
            },
            "funding outcome is invalid for venue",
        ),
    ],
)
def test_item_rejects_noncanonical_or_semantically_invalid_evidence(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        item(**overrides)


def test_cycle_requires_canonical_cartesian_item_coverage_and_hash_conservation() -> None:
    items = complete_items()

    with pytest.raises(ValidationError, match="items must be ordered by venue and asset"):
        cycle(items=tuple(reversed(items)))
    with pytest.raises(ValidationError, match="items must cover every requested venue and asset"):
        cycle(items=items[:1])
    with pytest.raises(ValidationError, match="cycle source hashes must equal item source hashes"):
        cycle(source_hashes=(HASH_A,))


def test_cycle_rejects_wrong_boundary_status_and_request_order() -> None:
    with pytest.raises(ValidationError, match="funding effective time must equal cycle end"):
        cycle(
            items=(
                complete_items()[0],
                complete_items()[1].model_copy(
                    update={"funding_effective_at": CYCLE_END - timedelta(hours=1)}
                ),
            )
        )
    with pytest.raises(ValidationError, match="cycle status does not match item evidence"):
        cycle(status=FundingCycleStatus.DEGRADED)
    with pytest.raises(ValidationError, match="request completion must not precede request start"):
        cycle(request_completed_at=CYCLE_END)


def test_cycle_rejects_component_observations_before_the_named_boundary() -> None:
    early_bybit = complete_items()[0].model_copy(
        update={"instrument_observed_at": CYCLE_END - timedelta(microseconds=1)}
    )

    with pytest.raises(ValidationError, match="item observation must not precede cycle end"):
        cycle(items=(early_bybit, complete_items()[1]))


def test_cycle_status_is_degraded_for_bootstrap_and_late_after_the_cutoff() -> None:
    bootstrap = item(
        instrument_source_hashes=(HASH_A,),
        funding_outcome=FundingCaptureOutcome.BOOTSTRAP_REQUIRED,
        funding_effective_at=None,
        funding_observed_at=None,
        funding_source_hashes=(),
        reason_codes=("BYBIT_INSTRUMENT_BOOTSTRAP_REQUIRED",),
    )
    hyperliquid = complete_items()[1]

    degraded = cycle(
        items=(bootstrap, hyperliquid),
        status=FundingCycleStatus.DEGRADED,
        source_hashes=(HASH_A, HASH_C, HASH_D),
    )
    late_hyperliquid = hyperliquid.model_copy(
        update={"instrument_observed_at": CYCLE_END + timedelta(minutes=5, microseconds=1)}
    )
    late = cycle(
        request_completed_at=CYCLE_END + timedelta(minutes=6),
        items=(complete_items()[0], late_hyperliquid),
        status=FundingCycleStatus.LATE,
    )

    assert degraded.status is FundingCycleStatus.DEGRADED
    assert late.status is FundingCycleStatus.LATE


def test_late_cycle_requires_every_component_to_be_explicitly_missed() -> None:
    late_items = tuple(
        item(
            venue=venue,
            symbol="BTCUSDT" if venue is Venue.BYBIT else "BTC",
            instrument_outcome=InstrumentCaptureOutcome.LATE_NOT_COLLECTED,
            funding_outcome=FundingCaptureOutcome.LATE_NOT_COLLECTED,
            instrument_observed_at=None,
            funding_effective_at=None,
            funding_observed_at=None,
            instrument_source_hashes=(),
            funding_source_hashes=(),
            reason_codes=("COLLECTION_WINDOW_MISSED",),
        )
        for venue in (Venue.BYBIT, Venue.HYPERLIQUID)
    )

    result = cycle(
        request_started_at=CYCLE_END + timedelta(minutes=6),
        request_completed_at=CYCLE_END + timedelta(minutes=6),
        items=late_items,
        status=FundingCycleStatus.LATE,
        source_hashes=(),
    )

    assert result.status is FundingCycleStatus.LATE


@given(
    selected=st.sets(st.sampled_from(tuple(Asset)), min_size=1, max_size=3),
    late_pair_index=st.integers(min_value=0, max_value=5),
    late_component=st.sampled_from(("instrument", "funding")),
)
@settings(max_examples=50)
def test_cycle_properties_conserve_hashes_and_flip_at_first_late_microsecond(
    selected: set[Asset], late_pair_index: int, late_component: str
) -> None:
    assets = tuple(sorted(selected, key=lambda asset: asset.value))
    cutoff = CYCLE_END + timedelta(minutes=5)
    items = tuple(
        item(
            venue=venue,
            asset=asset,
            symbol=f"{asset.value}USDT" if venue is Venue.BYBIT else asset.value,
            funding_outcome=(
                FundingCaptureOutcome.NO_SETTLEMENT
                if venue is Venue.BYBIT
                else FundingCaptureOutcome.CAPTURED
            ),
            instrument_observed_at=cutoff,
            funding_effective_at=None if venue is Venue.BYBIT else CYCLE_END,
            funding_observed_at=cutoff,
            instrument_source_hashes=(
                sha256(f"{venue.value}:{asset.value}:instrument".encode()).hexdigest(),
            ),
            funding_source_hashes=(
                sha256(f"{venue.value}:{asset.value}:funding".encode()).hexdigest(),
            ),
        )
        for venue in (Venue.BYBIT, Venue.HYPERLIQUID)
        for asset in assets
    )
    hashes = tuple(
        sorted(
            source_hash
            for current in items
            for source_hash in (
                *current.instrument_source_hashes,
                *current.funding_source_hashes,
            )
        )
    )
    complete = cycle(
        assets=assets,
        request_completed_at=cutoff + timedelta(microseconds=1),
        items=items,
        source_hashes=hashes,
    )

    assert tuple((current.venue, current.asset) for current in complete.items) == tuple(
        (venue, asset) for venue in (Venue.BYBIT, Venue.HYPERLIQUID) for asset in assets
    )
    assert complete.source_hashes == hashes
    assert complete.status is FundingCycleStatus.COMPLETE

    index = late_pair_index % len(items)
    field = "instrument_observed_at" if late_component == "instrument" else "funding_observed_at"
    late_items = list(items)
    late_items[index] = late_items[index].model_copy(
        update={field: cutoff + timedelta(microseconds=1)}
    )
    late = cycle(
        assets=assets,
        request_completed_at=cutoff + timedelta(microseconds=1),
        items=tuple(late_items),
        status=FundingCycleStatus.LATE,
        source_hashes=hashes,
    )

    assert late.status is FundingCycleStatus.LATE
