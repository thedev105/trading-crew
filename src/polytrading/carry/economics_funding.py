from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from itertools import pairwise
from typing import Literal, cast

from polytrading.carry.economics_models import FundingDirection
from polytrading.domain.models import normalize_utc_timestamp

HoldingDays = Literal[7, 14, 28]


@dataclass(frozen=True)
class FundingHorizonStatistics:
    holding_days: HoldingDays
    complete_window_count: int
    percentile_05_sum: Decimal
    maximum_drawdown: Decimal


def _require_decimal_values(values: tuple[Decimal, ...]) -> None:
    if not values:
        raise ValueError("values must not be empty")
    if any(not isinstance(value, Decimal) for value in values):
        raise TypeError("funding values must be Decimal instances")
    if any(not value.is_finite() for value in values):
        raise ValueError("funding values must be finite")


def exact_median(values: tuple[Decimal, ...]) -> Decimal:
    _require_decimal_values(values)
    ordered = tuple(sorted(values))
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def select_direction(values: tuple[Decimal, ...]) -> FundingDirection | None:
    median = exact_median(values)
    if median > 0:
        return FundingDirection.SHORT_LIGHTER_LONG_DYDX
    if median < 0:
        return FundingDirection.SHORT_DYDX_LONG_LIGHTER
    return None


def orient_funding(values: tuple[Decimal, ...], direction: FundingDirection) -> tuple[Decimal, ...]:
    _require_decimal_values(values)
    if direction is FundingDirection.SHORT_LIGHTER_LONG_DYDX:
        return values
    if direction is FundingDirection.SHORT_DYDX_LONG_LIGHTER:
        return tuple(-value for value in values)
    raise ValueError("unsupported funding direction")


def nearest_rank(values: tuple[Decimal, ...], percentile: Decimal) -> Decimal:
    _require_decimal_values(values)
    if not isinstance(percentile, Decimal):
        raise TypeError("percentile must be a Decimal instance")
    if not percentile.is_finite() or percentile <= 0 or percentile > 1:
        raise ValueError("percentile must be finite and in (0, 1]")
    rank = int((percentile * Decimal(len(values))).to_integral_value(rounding=ROUND_CEILING))
    return sorted(values)[rank - 1]


def _validate_holding_days(holding_days: int) -> HoldingDays:
    if holding_days not in (7, 14, 28):
        raise ValueError("holding days must be 7, 14, or 28")
    return cast(HoldingDays, holding_days)


def _validated_rows(
    rows: tuple[tuple[datetime, Decimal], ...],
) -> tuple[tuple[datetime, Decimal], ...]:
    values = tuple(value for _, value in rows)
    _require_decimal_values(values)
    normalized = tuple((normalize_utc_timestamp(timestamp), value) for timestamp, value in rows)
    if any(right[0] <= left[0] for left, right in pairwise(normalized)):
        raise ValueError("funding rows must use strict timestamp order")
    return normalized


def _contiguous_chunks(
    rows: tuple[tuple[datetime, Decimal], ...],
) -> tuple[tuple[tuple[datetime, Decimal], ...], ...]:
    chunks: list[list[tuple[datetime, Decimal]]] = []
    for row in rows:
        if not chunks or row[0] - chunks[-1][-1][0] != timedelta(hours=1):
            chunks.append([row])
        else:
            chunks[-1].append(row)
    return tuple(tuple(chunk) for chunk in chunks)


def rolling_funding_sums(
    rows: tuple[tuple[datetime, Decimal], ...], holding_days: HoldingDays
) -> tuple[Decimal, ...]:
    normalized = _validated_rows(rows)
    window_size = _validate_holding_days(holding_days) * 24
    results: list[Decimal] = []
    for chunk in _contiguous_chunks(normalized):
        if len(chunk) < window_size:
            continue
        running = sum((value for _, value in chunk[:window_size]), Decimal(0))
        results.append(running)
        for index in range(window_size, len(chunk)):
            running += chunk[index][1] - chunk[index - window_size][1]
            results.append(running)
    return tuple(results)


def maximum_funding_drawdown(
    rows: tuple[tuple[datetime, Decimal], ...], holding_days: HoldingDays
) -> Decimal:
    normalized = _validated_rows(rows)
    maximum_hours = _validate_holding_days(holding_days) * 24
    maximum = Decimal(0)
    for chunk in _contiguous_chunks(normalized):
        prefix = [Decimal(0)]
        for _, value in chunk:
            prefix.append(prefix[-1] + value)
        for end in range(1, len(prefix)):
            earliest_start = max(0, end - maximum_hours)
            peak = max(prefix[earliest_start:end])
            maximum = max(maximum, peak - prefix[end])
    return maximum


def funding_horizon_statistics(
    rows: tuple[tuple[datetime, Decimal], ...], holding_days: HoldingDays
) -> FundingHorizonStatistics:
    normalized_holding_days = _validate_holding_days(holding_days)
    sums = rolling_funding_sums(rows, normalized_holding_days)
    if not sums:
        raise ValueError(f"at least one complete {holding_days}-day funding window is required")
    return FundingHorizonStatistics(
        holding_days=normalized_holding_days,
        complete_window_count=len(sums),
        percentile_05_sum=nearest_rank(sums, Decimal("0.05")),
        maximum_drawdown=maximum_funding_drawdown(rows, normalized_holding_days),
    )
