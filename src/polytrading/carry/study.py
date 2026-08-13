from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from polytrading.carry.study_models import (
    AvailabilityClass,
    CoverageSummary,
    IncompleteBlock,
    PairedFundingBlock,
)
from polytrading.domain.models import Asset, FundingObservation, Venue, normalize_utc_timestamp

PROTOCOL_VERSION = "hl-bybit-funding-persistence-v1"
COMMON_BLOCK_HOURS = 8
POINT_IN_TIME_MAX_LAG = timedelta(minutes=5)
HOLDING_DAYS = (7, 14, 28)
MIN_PAIRED_COVERAGE = Decimal("0.99")

_BLOCK_DURATION = timedelta(hours=COMMON_BLOCK_HOURS)
_BLOCK_MICROSECONDS = COMMON_BLOCK_HOURS * 60 * 60 * 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EXPECTED_SYMBOL = {
    Venue.BYBIT: {asset: f"{asset.value}USDT" for asset in Asset},
    Venue.HYPERLIQUID: {asset: asset.value for asset in Asset},
}


@dataclass(frozen=True)
class _Settlement:
    effective_at: datetime
    observed_at: datetime
    rate: Decimal
    interval_hours: Decimal


@dataclass(frozen=True)
class _PreparedStudy:
    paired_blocks: tuple[PairedFundingBlock, ...]
    coverage: CoverageSummary
    availability: AvailabilityClass
    source_hashes: tuple[str, ...]


def _prepare_blocks(
    *,
    asset: Asset,
    start: datetime,
    end: datetime,
    known_as_of: datetime,
    bybit_rows: tuple[FundingObservation, ...],
    hyperliquid_rows: tuple[FundingObservation, ...],
) -> _PreparedStudy:
    normalized_start, normalized_end, normalized_known_as_of = _validate_window(
        start, end, known_as_of
    )
    bybit, bybit_hashes = _prepare_settlements(
        bybit_rows,
        venue=Venue.BYBIT,
        asset=asset,
        start=normalized_start,
        end=normalized_end,
        known_as_of=normalized_known_as_of,
    )
    hyperliquid, hyperliquid_hashes = _prepare_settlements(
        hyperliquid_rows,
        venue=Venue.HYPERLIQUID,
        asset=asset,
        start=normalized_start,
        end=normalized_end,
        known_as_of=normalized_known_as_of,
    )
    bybit_blocks = _group_by_block(bybit, normalized_start)
    hyperliquid_blocks = _group_by_block(hyperliquid, normalized_start)
    requested_blocks = (normalized_end - normalized_start) // _BLOCK_DURATION
    block_ends = tuple(
        normalized_start + _BLOCK_DURATION * index for index in range(1, requested_blocks + 1)
    )

    paired: list[PairedFundingBlock] = []
    incomplete: list[IncompleteBlock] = []
    bybit_complete = 0
    hyperliquid_complete = 0
    included_settlements: list[_Settlement] = []
    for block_end in block_ends:
        bybit_values = bybit_blocks.get(block_end, ())
        hyperliquid_values = hyperliquid_blocks.get(block_end, ())
        bybit_rate, bybit_is_complete = _summarize_native_block(bybit_values)
        hyperliquid_rate, hyperliquid_is_complete = _summarize_native_block(hyperliquid_values)
        bybit_complete += int(bybit_is_complete)
        hyperliquid_complete += int(hyperliquid_is_complete)
        if bybit_is_complete and hyperliquid_is_complete:
            paired.append(
                PairedFundingBlock(
                    schema_version=1,
                    block_start=block_end - _BLOCK_DURATION,
                    block_end=block_end,
                    bybit_rate=bybit_rate,
                    hyperliquid_rate=hyperliquid_rate,
                    spread=hyperliquid_rate - bybit_rate,
                )
            )
            included_settlements.extend(bybit_values)
            included_settlements.extend(hyperliquid_values)
        else:
            reasons = []
            if not bybit_is_complete:
                reasons.append("BYBIT_INTERVAL_UNDERFILLED")
            if not hyperliquid_is_complete:
                reasons.append("HYPERLIQUID_INTERVAL_UNDERFILLED")
            incomplete.append(
                IncompleteBlock(
                    schema_version=1,
                    block_end=block_end,
                    reason_codes=tuple(sorted(reasons)),
                )
            )

    paired_rows = tuple(paired)
    coverage = CoverageSummary(
        schema_version=1,
        requested_blocks=requested_blocks,
        bybit_complete_blocks=bybit_complete,
        hyperliquid_complete_blocks=hyperliquid_complete,
        paired_complete_blocks=len(paired_rows),
        coverage_ratio=Decimal(len(paired_rows)) / Decimal(requested_blocks),
        first_paired_at=paired_rows[0].block_end if paired_rows else None,
        last_paired_at=paired_rows[-1].block_end if paired_rows else None,
        incomplete_blocks=tuple(incomplete),
    )
    availability = _classify_availability(tuple(included_settlements))
    return _PreparedStudy(
        paired_blocks=paired_rows,
        coverage=coverage,
        availability=availability,
        source_hashes=tuple(sorted(bybit_hashes | hyperliquid_hashes)),
    )


def _validate_window(
    start: datetime, end: datetime, known_as_of: datetime
) -> tuple[datetime, datetime, datetime]:
    normalized_start = normalize_utc_timestamp(start)
    normalized_end = normalize_utc_timestamp(end)
    normalized_known_as_of = normalize_utc_timestamp(known_as_of)
    if normalized_start >= normalized_end:
        raise ValueError("study start must precede end")
    if not _is_block_aligned(normalized_start) or not _is_block_aligned(normalized_end):
        raise ValueError("study boundaries must align to eight-hour UTC epoch blocks")
    if normalized_known_as_of < normalized_end:
        raise ValueError("known_as_of must not precede end")
    return normalized_start, normalized_end, normalized_known_as_of


def _is_block_aligned(value: datetime) -> bool:
    delta = value - _EPOCH
    total_microseconds = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return total_microseconds % _BLOCK_MICROSECONDS == 0


def _prepare_settlements(
    rows: tuple[FundingObservation, ...],
    *,
    venue: Venue,
    asset: Asset,
    start: datetime,
    end: datetime,
    known_as_of: datetime,
) -> tuple[tuple[_Settlement, ...], set[str]]:
    grouped: dict[datetime, list[FundingObservation]] = {}
    source_hashes: set[str] = set()
    expected_symbol = _EXPECTED_SYMBOL[venue][asset]
    for row in rows:
        if row.venue is not venue:
            raise ValueError("funding venue does not match input stream")
        if row.asset is not asset:
            raise ValueError("funding asset does not match study asset")
        if row.symbol != expected_symbol:
            raise ValueError("funding symbol does not match study asset and venue")
        if row.effective_at <= start or row.effective_at > end:
            raise ValueError("funding settlement is outside requested study window")
        if row.observed_at > known_as_of:
            raise ValueError("funding observation is after knowledge cutoff")
        if row.observed_at < row.effective_at:
            raise ValueError("funding observation precedes settlement")
        grouped.setdefault(row.effective_at, []).append(row)
        source_hashes.add(row.source_hash)

    settlements: list[_Settlement] = []
    for effective_at, revisions in sorted(grouped.items()):
        economic_values = {(row.rate, row.interval_hours) for row in revisions}
        if len(economic_values) != 1:
            raise ValueError("conflicting funding revisions for immutable settlement")
        rate, interval_hours = economic_values.pop()
        settlements.append(
            _Settlement(
                effective_at=effective_at,
                observed_at=min(row.observed_at for row in revisions),
                rate=rate,
                interval_hours=interval_hours,
            )
        )
    return tuple(settlements), source_hashes


def _group_by_block(
    settlements: tuple[_Settlement, ...], start: datetime
) -> dict[datetime, tuple[_Settlement, ...]]:
    grouped: dict[datetime, list[_Settlement]] = {}
    for settlement in settlements:
        elapsed = settlement.effective_at - start
        elapsed_microseconds = (
            elapsed.days * 86_400 + elapsed.seconds
        ) * 1_000_000 + elapsed.microseconds
        block_index = (elapsed_microseconds + _BLOCK_MICROSECONDS - 1) // _BLOCK_MICROSECONDS
        block_end = start + _BLOCK_DURATION * block_index
        grouped.setdefault(block_end, []).append(settlement)
    return {key: tuple(value) for key, value in grouped.items()}


def _summarize_native_block(values: tuple[_Settlement, ...]) -> tuple[Decimal, bool]:
    interval_hours = sum((value.interval_hours for value in values), Decimal(0))
    if interval_hours > Decimal(COMMON_BLOCK_HOURS):
        raise ValueError("native funding intervals exceed eight-hour block")
    rate = sum((value.rate for value in values), Decimal(0))
    return rate, interval_hours == Decimal(COMMON_BLOCK_HOURS)


def _classify_availability(values: tuple[_Settlement, ...]) -> AvailabilityClass:
    if not values:
        return AvailabilityClass.INSUFFICIENT_DATA
    if all(value.observed_at - value.effective_at <= POINT_IN_TIME_MAX_LAG for value in values):
        return AvailabilityClass.POINT_IN_TIME
    return AvailabilityClass.HISTORICAL_RECONSTRUCTION
