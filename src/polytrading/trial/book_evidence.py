from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from polytrading.carry.economics_execution import PairedBookObservation
from polytrading.domain.models import Asset, Venue, normalize_utc_timestamp
from polytrading.storage.store import DuckDBStore
from polytrading.venues.synchronized import BookCollectionCycle

_HOUR = timedelta(hours=1)
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_VENUES = (Venue.DYDX, Venue.LIGHTER)
_SYMBOLS = {
    Asset.BTC: {Venue.DYDX: "BTC-USD", Venue.LIGHTER: "BTC"},
    Asset.ETH: {Venue.DYDX: "ETH-USD", Venue.LIGHTER: "ETH"},
    Asset.SOL: {Venue.DYDX: "SOL-USD", Venue.LIGHTER: "SOL"},
}


@dataclass(frozen=True)
class EligibleTrialBookPair:
    cycle: BookCollectionCycle
    pair: PairedBookObservation


def _duration_microseconds(value: timedelta) -> int:
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def _duration_seconds(value: timedelta) -> Decimal:
    return Decimal(_duration_microseconds(value)) / Decimal(1_000_000)


def _duration_milliseconds(value: timedelta) -> Decimal:
    return Decimal(_duration_microseconds(value)) / Decimal(1_000)


def _require_limit(value: Decimal, label: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{label} must be a Decimal instance")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative")
    return value


def _limit_timedelta(value: Decimal) -> timedelta:
    microseconds = int((value * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))
    try:
        return timedelta(microseconds=microseconds)
    except OverflowError as error:
        raise ValueError("book evidence limit is outside the supported datetime range") from error


def _select_hourly_trial_books_from_eligible(
    eligible: tuple[EligibleTrialBookPair, ...],
    start: datetime,
    end: datetime,
    maximum_age_seconds: Decimal,
) -> tuple[EligibleTrialBookPair, ...]:
    ordered = tuple(
        sorted(eligible, key=lambda item: (item.cycle.request_completed_at, item.cycle.cycle_id))
    )
    selected: list[EligibleTrialBookPair] = []
    boundary = start + _HOUR
    while boundary <= end:
        representative = next(
            (
                item
                for item in reversed(ordered)
                if item.cycle.request_completed_at <= boundary
                and _duration_seconds(boundary - item.cycle.request_completed_at)
                <= maximum_age_seconds
                and item.pair.dydx.observed_at <= boundary
                and item.pair.lighter.observed_at <= boundary
            ),
            None,
        )
        if representative is not None:
            selected.append(
                EligibleTrialBookPair(
                    cycle=representative.cycle,
                    pair=PairedBookObservation(
                        effective_at=boundary,
                        lighter=representative.pair.lighter,
                        dydx=representative.pair.dydx,
                    ),
                )
            )
        boundary += _HOUR
    return tuple(selected)


def eligible_lighter_dydx_book_pair(
    store: DuckDBStore,
    cycle: BookCollectionCycle,
    asset: Asset,
    known_as_of: datetime,
    maximum_skew_ms: Decimal,
) -> EligibleTrialBookPair | None:
    """Return an exact detached Lighter/dYdX pair known by the cutoff."""
    normalized_cutoff = normalize_utc_timestamp(known_as_of)
    skew_limit = _require_limit(maximum_skew_ms, "maximum skew")
    if (
        cycle.status != "complete"
        or asset not in cycle.assets
        or not set(_VENUES).issubset(cycle.venues)
        or not cycle.source_hashes
        or cycle.max_effective_skew_ms > skew_limit
        or cycle.request_completed_at > normalized_cutoff
    ):
        return None

    books = store.books_for_cycle(cycle.cycle_id)
    selected = {
        venue: tuple(item for item in books if item.venue is venue and item.asset is asset)
        for venue in _VENUES
    }
    if any(len(rows) != 1 for rows in selected.values()):
        return None
    dydx = selected[Venue.DYDX][0]
    lighter = selected[Venue.LIGHTER][0]
    if (
        dydx.symbol != _SYMBOLS[asset][Venue.DYDX]
        or lighter.symbol != _SYMBOLS[asset][Venue.LIGHTER]
        or not dydx.source_hash
        or not lighter.source_hash
        or dydx.source_hash not in cycle.source_hashes
        or lighter.source_hash not in cycle.source_hashes
        or dydx.observed_at > cycle.request_completed_at
        or lighter.observed_at > cycle.request_completed_at
        or dydx.effective_at > cycle.request_completed_at
        or lighter.effective_at > cycle.request_completed_at
        or _duration_milliseconds(abs(dydx.effective_at - lighter.effective_at)) > skew_limit
    ):
        return None
    return EligibleTrialBookPair(
        cycle=cycle,
        pair=PairedBookObservation(
            effective_at=max(dydx.effective_at, lighter.effective_at),
            lighter=lighter,
            dydx=dydx,
        ),
    )


def select_hourly_trial_books(
    store: DuckDBStore,
    asset: Asset,
    start: datetime,
    end: datetime,
    known_as_of: datetime,
    maximum_age_seconds: Decimal,
    maximum_skew_ms: Decimal,
) -> tuple[EligibleTrialBookPair, ...]:
    """Select one cutoff-safe representative for each UTC hour in ``(start, end]``."""
    normalized_start = normalize_utc_timestamp(start)
    normalized_end = normalize_utc_timestamp(end)
    normalized_cutoff = normalize_utc_timestamp(known_as_of)
    age_limit = _require_limit(maximum_age_seconds, "maximum age")
    skew_limit = _require_limit(maximum_skew_ms, "maximum skew")
    if normalized_start > normalized_end:
        raise ValueError("start must be less than or equal to end")
    if normalized_cutoff < normalized_end:
        raise ValueError("knowledge cutoff must be greater than or equal to end")
    if any((normalized_start.minute, normalized_start.second, normalized_start.microsecond)) or any(
        (normalized_end.minute, normalized_end.second, normalized_end.microsecond)
    ):
        raise ValueError("hourly book window must align to whole UTC hours")

    earliest_query = max(
        _UNIX_EPOCH,
        normalized_start - _limit_timedelta(age_limit) - _limit_timedelta(skew_limit / 1_000),
    )
    cycles = store.book_collection_cycles_completed_between(
        earliest_query,
        normalized_end,
        normalized_cutoff,
    )
    eligible = tuple(
        item
        for cycle in cycles
        if (
            item := eligible_lighter_dydx_book_pair(
                store, cycle, asset, normalized_cutoff, skew_limit
            )
        )
        is not None
    )
    return _select_hourly_trial_books_from_eligible(
        eligible,
        normalized_start,
        normalized_end,
        age_limit,
    )
