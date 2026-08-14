from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from polytrading.domain.models import Asset, Venue
from polytrading.trial.funding_models import (
    LighterDydxFundingCycle,
    LighterDydxFundingItem,
    TrialFundingCycleStatus,
    TrialFundingOutcome,
    TrialInstrumentOutcome,
    resolve_current_trial_cycle_end,
    validate_trial_cycle_timing,
)
from tests.trial.funding_helpers import CYCLE_END, trial_funding_cycle, trial_funding_item


def test_current_trial_boundary_uses_one_aware_utc_floor() -> None:
    eastern = timezone(-timedelta(hours=4))
    now = datetime(2026, 8, 14, 3, 59, 59, 999999, tzinfo=eastern)

    assert resolve_current_trial_cycle_end(now) == datetime(2026, 8, 14, 7, tzinfo=UTC)


@pytest.mark.parametrize(
    ("offset", "late"),
    [
        (timedelta(0), False),
        (timedelta(minutes=5), False),
        (timedelta(minutes=5, microseconds=1), True),
    ],
)
def test_trial_cycle_timing_has_an_inclusive_five_minute_window(
    offset: timedelta, late: bool
) -> None:
    cycle_end = datetime(2026, 8, 14, 7, tzinfo=UTC)
    _, _, actual = validate_trial_cycle_timing(cycle_end, cycle_end + offset)
    assert actual is late


@pytest.mark.parametrize(
    ("cycle_end", "now", "message"),
    [
        (
            datetime(2026, 8, 14, 7, 1, tzinfo=UTC),
            datetime(2026, 8, 14, 7, 2, tzinfo=UTC),
            "cycle end must align to a whole UTC hour",
        ),
        (
            datetime(2026, 8, 14, 7),
            datetime(2026, 8, 14, 7, tzinfo=UTC),
            "timestamp must be timezone-aware",
        ),
        (
            datetime(2026, 8, 14, 7, tzinfo=UTC),
            datetime(2026, 8, 14, 6, 59, 59, 999999, tzinfo=UTC),
            "collection clock precedes cycle end",
        ),
    ],
)
def test_trial_cycle_timing_rejects_invalid_boundaries(
    cycle_end: datetime, now: datetime, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_trial_cycle_timing(cycle_end, now)


def item_data(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = trial_funding_item().model_dump()
    values.update(overrides)
    return values


def cycle_data(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = trial_funding_cycle().model_dump()
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("venue", "asset", "symbol"),
    [
        (Venue.DYDX, Asset.BTC, "BTC"),
        (Venue.LIGHTER, Asset.ETH, "ETH-USD"),
    ],
)
def test_item_rejects_symbol_mismatched_to_venue_and_asset(
    venue: Venue, asset: Asset, symbol: str
) -> None:
    with pytest.raises(ValidationError, match="symbol does not match venue and asset"):
        LighterDydxFundingItem(**item_data(venue=venue, asset=asset, symbol=symbol))


@pytest.mark.parametrize("venue", [Venue.BYBIT, Venue.HYPERLIQUID])
def test_item_rejects_unsupported_venues_with_a_validation_error(venue: Venue) -> None:
    with pytest.raises(ValidationError, match="venue must be dYdX or Lighter"):
        LighterDydxFundingItem(**item_data(venue=venue, symbol="BTC"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("instrument_source_hashes", ("2" * 64, "1" * 64), "source hashes must be sorted"),
        ("reason_codes", ("Z_REASON", "A_REASON"), "reason codes must be sorted"),
    ],
)
def test_item_rejects_noncanonical_hashes_and_reasons(
    field: str, value: tuple[str, ...], message: str
) -> None:
    values = item_data()
    values[field] = value

    with pytest.raises(ValidationError, match=message):
        LighterDydxFundingItem(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"instrument_observed_at": None},
        {
            "instrument_outcome": TrialInstrumentOutcome.FAILED,
            "instrument_observed_at": None,
            "instrument_source_hashes": (),
            "reason_codes": (),
        },
        {"funding_effective_at": None},
        {"funding_observed_at": None},
        {
            "funding_outcome": TrialFundingOutcome.MISSING_EXPECTED,
            "funding_effective_at": None,
            "reason_codes": (),
        },
        {
            "funding_outcome": TrialFundingOutcome.LATE_NOT_COLLECTED,
            "funding_effective_at": None,
            "funding_observed_at": None,
            "funding_source_hashes": (),
            "reason_codes": (),
        },
    ],
)
def test_item_outcomes_require_consistent_timestamps_hashes_and_reasons(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match=r"outcome|reason codes"):
        LighterDydxFundingItem(**item_data(**overrides))


def test_item_only_accepts_expected_missing_and_exception_class_reason_codes() -> None:
    missing = LighterDydxFundingItem(
        **item_data(
            funding_outcome=TrialFundingOutcome.MISSING_EXPECTED,
            funding_effective_at=None,
            reason_codes=("FUNDING_MISSING_EXPECTED",),
        )
    )
    assert missing.funding_outcome is TrialFundingOutcome.MISSING_EXPECTED

    with pytest.raises(ValidationError, match="reason codes do not match"):
        LighterDydxFundingItem(
            **item_data(
                funding_outcome=TrialFundingOutcome.MISSING_EXPECTED,
                funding_effective_at=None,
                reason_codes=("NO_SETTLEMENT",),
            )
        )

    failures = LighterDydxFundingItem(
        **item_data(
            venue=Venue.LIGHTER,
            symbol="BTC",
            instrument_outcome=TrialInstrumentOutcome.FAILED,
            funding_outcome=TrialFundingOutcome.FAILED,
            instrument_observed_at=None,
            funding_effective_at=None,
            funding_observed_at=None,
            instrument_source_hashes=(),
            funding_source_hashes=(),
            reason_codes=(
                "FUNDING_FAILED:lighter:BTC:TimeoutError",
                "INSTRUMENT_FAILED:lighter:TimeoutError",
            ),
        )
    )
    assert failures.reason_codes == (
        "FUNDING_FAILED:lighter:BTC:TimeoutError",
        "INSTRUMENT_FAILED:lighter:TimeoutError",
    )

    with pytest.raises(ValidationError, match="reason codes do not match"):
        LighterDydxFundingItem(
            **item_data(
                instrument_outcome=TrialInstrumentOutcome.FAILED,
                instrument_observed_at=None,
                instrument_source_hashes=(),
                reason_codes=("INSTRUMENT_FAILED:dydx:TimeoutError:request timed out",),
            )
        )


def test_cycle_requires_venue_order_and_canonical_cartesian_coverage() -> None:
    with pytest.raises(ValidationError, match="venues must be dYdX followed by Lighter"):
        LighterDydxFundingCycle(**cycle_data(venues=(Venue.LIGHTER, Venue.DYDX)))

    items = trial_funding_cycle().items
    with pytest.raises(ValidationError, match="items must be ordered by venue and asset"):
        LighterDydxFundingCycle(**cycle_data(items=tuple(reversed(items))))

    with pytest.raises(ValidationError, match="items must cover every requested venue and asset"):
        LighterDydxFundingCycle(**cycle_data(items=items[:-1], source_hashes=("1" * 64, "2" * 64)))


def test_cycle_rejects_duplicate_pairs_hashes_and_altered_warnings() -> None:
    items = trial_funding_cycle().items
    with pytest.raises(ValidationError, match="items must be ordered by venue and asset"):
        LighterDydxFundingCycle(**cycle_data(items=(*items[:-1], items[0])))

    with pytest.raises(ValidationError, match="source hashes must be sorted and unique"):
        LighterDydxFundingCycle(**cycle_data(source_hashes=("1" * 64, "1" * 64, "2" * 64)))

    warnings = trial_funding_cycle().warnings
    with pytest.raises(ValidationError, match="exact research warnings"):
        LighterDydxFundingCycle(**cycle_data(warnings=("changed", *warnings[1:])))


def test_cycle_requires_consistent_request_and_item_times() -> None:
    with pytest.raises(ValidationError, match="request completion must not precede request start"):
        trial_funding_cycle(request_completed_at=CYCLE_END)

    before_window = list(trial_funding_cycle().items)
    before_window[0] = before_window[0].model_copy(
        update={"instrument_observed_at": CYCLE_END + timedelta(seconds=9)}
    )
    with pytest.raises(ValidationError, match="item observation must not precede request start"):
        trial_funding_cycle(items=tuple(before_window))

    after_window = list(trial_funding_cycle().items)
    after_window[0] = after_window[0].model_copy(
        update={"funding_observed_at": CYCLE_END + timedelta(seconds=21)}
    )
    with pytest.raises(
        ValidationError, match="item observation must not follow request completion"
    ):
        trial_funding_cycle(items=tuple(after_window))

    wrong_effective = list(trial_funding_cycle().items)
    wrong_effective[0] = wrong_effective[0].model_copy(
        update={
            "funding_effective_at": CYCLE_END + timedelta(hours=1),
            "funding_observed_at": CYCLE_END + timedelta(hours=1, seconds=1),
        }
    )
    with pytest.raises(ValidationError, match="funding effective time must equal cycle end"):
        trial_funding_cycle(items=tuple(wrong_effective))


def test_cycle_status_follows_late_then_degraded_component_evidence() -> None:
    degraded = list(trial_funding_cycle().items)
    degraded[0] = degraded[0].model_copy(
        update={
            "funding_outcome": TrialFundingOutcome.MISSING_EXPECTED,
            "funding_effective_at": None,
            "reason_codes": ("FUNDING_MISSING_EXPECTED",),
        }
    )
    assert (
        trial_funding_cycle(items=tuple(degraded), status=TrialFundingCycleStatus.DEGRADED).status
        is TrialFundingCycleStatus.DEGRADED
    )

    late = list(trial_funding_cycle().items)
    late[0] = late[0].model_copy(
        update={"instrument_observed_at": CYCLE_END + timedelta(minutes=5, microseconds=1)}
    )
    assert (
        trial_funding_cycle(
            request_completed_at=CYCLE_END + timedelta(minutes=5, microseconds=1),
            items=tuple(late),
            status=TrialFundingCycleStatus.LATE,
        ).status
        is TrialFundingCycleStatus.LATE
    )

    with pytest.raises(ValidationError, match="cycle status does not match"):
        trial_funding_cycle(status=TrialFundingCycleStatus.DEGRADED)


@given(
    now=st.datetimes(
        min_value=datetime(2026, 1, 1),
        max_value=datetime(2026, 12, 31),
        timezones=st.timezones(),
    ),
    selected=st.sets(st.sampled_from(tuple(Asset)), min_size=1, max_size=3),
    venue_order=st.permutations((Venue.DYDX, Venue.LIGHTER)),
)
@settings(max_examples=30)
def test_cycle_normalizes_aware_times_and_rejects_noncanonical_serialized_order(
    now: datetime, selected: set[Asset], venue_order: tuple[Venue, Venue]
) -> None:
    cycle_end = resolve_current_trial_cycle_end(now)
    assets = tuple(sorted(selected, key=lambda asset: asset.value))
    items = tuple(
        trial_funding_item(venue=venue, asset=asset, cycle_end=cycle_end)
        for venue in (Venue.DYDX, Venue.LIGHTER)
        for asset in assets
    )
    values = cycle_data(
        cycle_end=cycle_end.astimezone(now.tzinfo),
        assets=assets,
        request_started_at=(cycle_end + timedelta(seconds=10)).astimezone(now.tzinfo),
        request_completed_at=(cycle_end + timedelta(seconds=20)).astimezone(now.tzinfo),
        items=items,
        source_hashes=("1" * 64, "2" * 64),
    )
    accepted = LighterDydxFundingCycle(**values)

    assert accepted.cycle_end == cycle_end
    assert accepted.source_hashes == tuple(
        sorted(
            {
                value
                for item in accepted.items
                for value in (*item.instrument_source_hashes, *item.funding_source_hashes)
            }
        )
    )

    reordered = tuple(item for venue in venue_order for item in items if item.venue is venue)
    if reordered != items:
        with pytest.raises(ValidationError, match="items must be ordered by venue and asset"):
            LighterDydxFundingCycle(**dict(values, items=reordered))
