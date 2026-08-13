from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise

from polytrading.carry.study_models import (
    OMITTED_COSTS,
    PROTOCOL_VERSION,
    AvailabilityClass,
    CarryPersistenceReport,
    CoverageSummary,
    DistributionSummary,
    HoldingWindowSummary,
    IncompleteBlock,
    MonthlyContribution,
    PairedFundingBlock,
    StudyDecision,
    StudyStatistics,
)
from polytrading.domain.models import Asset, FundingObservation, Venue, normalize_utc_timestamp
from polytrading.storage.store import DuckDBStore

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


class CarryPersistenceStudy:
    def __init__(self, store: DuckDBStore) -> None:
        self._store = store

    def run(
        self,
        asset: Asset,
        start: datetime,
        end: datetime,
        known_as_of: datetime,
    ) -> CarryPersistenceReport:
        bybit_symbol = _EXPECTED_SYMBOL[Venue.BYBIT][asset]
        hyperliquid_symbol = _EXPECTED_SYMBOL[Venue.HYPERLIQUID][asset]
        bybit_rows = self._store.funding_revisions_between(
            Venue.BYBIT, bybit_symbol, start, end, known_as_of
        )
        hyperliquid_rows = self._store.funding_revisions_between(
            Venue.HYPERLIQUID, hyperliquid_symbol, start, end, known_as_of
        )
        return _build_report(
            asset=asset,
            start=start,
            end=end,
            known_as_of=known_as_of,
            bybit_rows=bybit_rows,
            hyperliquid_rows=hyperliquid_rows,
        )


def _build_report(
    *,
    asset: Asset,
    start: datetime,
    end: datetime,
    known_as_of: datetime,
    bybit_rows: tuple[FundingObservation, ...],
    hyperliquid_rows: tuple[FundingObservation, ...],
) -> CarryPersistenceReport:
    normalized_start = normalize_utc_timestamp(start)
    normalized_end = normalize_utc_timestamp(end)
    normalized_known_as_of = normalize_utc_timestamp(known_as_of)
    prepared = _prepare_blocks(
        asset=asset,
        start=normalized_start,
        end=normalized_end,
        known_as_of=normalized_known_as_of,
        bybit_rows=bybit_rows,
        hyperliquid_rows=hyperliquid_rows,
    )
    insufficient_reasons = _insufficiency_reasons(prepared, normalized_start, normalized_end)
    holding_values = {
        holding_days: _holding_window_sums(prepared.paired_blocks, holding_days)
        for holding_days in HOLDING_DAYS
    }
    for holding_days, values in holding_values.items():
        if len(values) < 2:
            insufficient_reasons.append(
                f"HOLDING_WINDOW_{holding_days}_DAY_HAS_FEWER_THAN_TWO_SAMPLES"
            )

    if insufficient_reasons:
        return _make_report(
            asset=asset,
            start=normalized_start,
            end=normalized_end,
            known_as_of=normalized_known_as_of,
            prepared=prepared,
            statistics=None,
            decision=StudyDecision.INSUFFICIENT_DATA,
            decision_reasons=tuple(sorted(set(insufficient_reasons))),
        )

    statistics = _statistics(prepared.paired_blocks, holding_values)
    failure_reasons = _failure_reasons(statistics)
    if failure_reasons:
        decision = StudyDecision.REPLICATION_FAILED
    elif prepared.availability is AvailabilityClass.POINT_IN_TIME:
        decision = StudyDecision.NET_FORWARD_GATE_REQUIRED
    else:
        decision = StudyDecision.FORWARD_TEST_REQUIRED
    return _make_report(
        asset=asset,
        start=normalized_start,
        end=normalized_end,
        known_as_of=normalized_known_as_of,
        prepared=prepared,
        statistics=statistics,
        decision=decision,
        decision_reasons=tuple(sorted(failure_reasons)),
    )


def _make_report(
    *,
    asset: Asset,
    start: datetime,
    end: datetime,
    known_as_of: datetime,
    prepared: _PreparedStudy,
    statistics: StudyStatistics | None,
    decision: StudyDecision,
    decision_reasons: tuple[str, ...],
) -> CarryPersistenceReport:
    return CarryPersistenceReport(
        schema_version=1,
        protocol_version=PROTOCOL_VERSION,
        asset=asset,
        start=start,
        end=end,
        known_as_of=known_as_of,
        availability=prepared.availability,
        coverage=prepared.coverage,
        statistics=statistics,
        decision=decision,
        decision_reasons=decision_reasons,
        source_hashes=prepared.source_hashes,
        economic_basis="gross_funding_only",
        omitted_costs=OMITTED_COSTS,
    )


def _insufficiency_reasons(prepared: _PreparedStudy, start: datetime, end: datetime) -> list[str]:
    reasons: list[str] = []
    if prepared.availability is AvailabilityClass.INSUFFICIENT_DATA:
        reasons.append("NO_PAIRED_BLOCKS")
    if prepared.coverage.coverage_ratio < MIN_PAIRED_COVERAGE:
        reasons.append("PAIRED_COVERAGE_BELOW_99_PERCENT")
    duration = end - start
    if prepared.availability is AvailabilityClass.POINT_IN_TIME and duration < timedelta(days=90):
        reasons.append("POINT_IN_TIME_WINDOW_BELOW_90_DAYS")
    if (
        prepared.availability is AvailabilityClass.HISTORICAL_RECONSTRUCTION
        and duration < timedelta(days=365)
    ):
        reasons.append("HISTORICAL_WINDOW_BELOW_365_DAYS")
    return reasons


def _failure_reasons(statistics: StudyStatistics) -> list[str]:
    reasons: list[str] = []
    if statistics.block_distribution.median <= 0:
        reasons.append("BLOCK_MEDIAN_NON_POSITIVE")
    for summary in statistics.holding_windows:
        if summary.distribution.percentile_05 <= 0:
            reasons.append(f"HOLDING_WINDOW_{summary.holding_days}_DAY_LOWER_TAIL_NON_POSITIVE")
    if statistics.cumulative_without_best_month <= 0:
        reasons.append("BEST_MONTH_CONCENTRATION")
    return reasons


def _statistics(
    blocks: tuple[PairedFundingBlock, ...],
    holding_values: dict[int, tuple[Decimal, ...]],
) -> StudyStatistics:
    spreads = tuple(block.spread for block in blocks)
    sign_persistence, sign_reversals = _sign_behavior(spreads)
    monthly_totals: dict[str, Decimal] = {}
    for block in blocks:
        month = block.block_end.strftime("%Y-%m")
        monthly_totals[month] = monthly_totals.get(month, Decimal(0)) + block.spread
    monthly = tuple(
        MonthlyContribution(schema_version=1, month=month, gross_funding=amount)
        for month, amount in sorted(monthly_totals.items())
    )
    cumulative = sum(spreads, Decimal(0))
    best_month = max(item.gross_funding for item in monthly)
    holding_windows = tuple(
        HoldingWindowSummary(
            schema_version=1,
            holding_days=holding_days,
            block_count=holding_days * 3,
            distribution=_distribution(holding_values[holding_days]),
        )
        for holding_days in HOLDING_DAYS
    )
    return StudyStatistics(
        schema_version=1,
        block_distribution=_distribution(spreads),
        sign_persistence=sign_persistence,
        sign_reversals=sign_reversals,
        longest_adverse_run=_longest_adverse_run(spreads),
        cumulative_gross_funding=cumulative,
        maximum_drawdown=_maximum_drawdown(spreads),
        gross_annualized_mean=_mean(spreads) * Decimal(3 * 365),
        monthly_contributions=monthly,
        cumulative_without_best_month=cumulative - best_month,
        holding_windows=holding_windows,
    )


def _distribution(values: tuple[Decimal, ...]) -> DistributionSummary:
    if not values:
        raise ValueError("distribution requires at least one value")
    count = len(values)
    positive_count = sum(value > 0 for value in values)
    zero_count = sum(value == 0 for value in values)
    negative_count = sum(value < 0 for value in values)
    return DistributionSummary(
        schema_version=1,
        count=count,
        mean=_mean(values),
        median=_nearest_rank(values, Decimal("0.50")),
        percentile_05=_nearest_rank(values, Decimal("0.05")),
        percentile_95=_nearest_rank(values, Decimal("0.95")),
        minimum=min(values),
        maximum=max(values),
        positive_count=positive_count,
        zero_count=zero_count,
        negative_count=negative_count,
        positive_fraction=Decimal(positive_count) / Decimal(count),
        zero_fraction=Decimal(zero_count) / Decimal(count),
        negative_fraction=Decimal(negative_count) / Decimal(count),
    )


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _nearest_rank(values: tuple[Decimal, ...], probability: Decimal) -> Decimal:
    if not values:
        raise ValueError("nearest-rank percentile requires at least one value")
    if probability <= 0 or probability > 1:
        raise ValueError("nearest-rank probability must be within (0, 1]")
    rank = int((Decimal(len(values)) * probability).to_integral_value(rounding=ROUND_CEILING))
    return tuple(sorted(values))[rank - 1]


def _maximum_drawdown(values: tuple[Decimal, ...]) -> Decimal:
    cumulative = Decimal(0)
    peak = Decimal(0)
    maximum = Decimal(0)
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _longest_adverse_run(values: tuple[Decimal, ...]) -> int:
    longest = 0
    current = 0
    for value in values:
        current = current + 1 if value < 0 else 0
        longest = max(longest, current)
    return longest


def _sign_behavior(values: tuple[Decimal, ...]) -> tuple[Decimal | None, int]:
    previous: int | None = None
    transitions = 0
    persistent = 0
    reversals = 0
    for value in values:
        if value == 0:
            previous = None
            continue
        sign = 1 if value > 0 else -1
        if previous is not None:
            transitions += 1
            if sign == previous:
                persistent += 1
            else:
                reversals += 1
        previous = sign
    persistence = Decimal(persistent) / Decimal(transitions) if transitions else None
    return persistence, reversals


def _holding_window_sums(
    blocks: tuple[PairedFundingBlock, ...], holding_days: int
) -> tuple[Decimal, ...]:
    block_count = holding_days * 3
    values: list[Decimal] = []
    for start_index in range(len(blocks) - block_count + 1):
        window = blocks[start_index : start_index + block_count]
        if all(
            right.block_end - left.block_end == _BLOCK_DURATION for left, right in pairwise(window)
        ):
            values.append(sum((block.spread for block in window), Decimal(0)))
    return tuple(values)


def _summarize_holding_windows(
    blocks: tuple[PairedFundingBlock, ...], holding_days: int
) -> HoldingWindowSummary:
    return HoldingWindowSummary(
        schema_version=1,
        holding_days=holding_days,
        block_count=holding_days * 3,
        distribution=_distribution(_holding_window_sums(blocks, holding_days)),
    )


def _prepare_blocks(
    *,
    asset: Asset,
    start: datetime,
    end: datetime,
    known_as_of: datetime,
    bybit_rows: tuple[FundingObservation, ...],
    hyperliquid_rows: tuple[FundingObservation, ...],
) -> _PreparedStudy:
    normalized_start, normalized_end, normalized_known_as_of = validate_study_window(
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


def validate_study_window(
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
