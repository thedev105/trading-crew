from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from polytrading.domain.models import (
    Asset,
    FundingObservation,
    InstrumentSpec,
    RawEnvelope,
    Venue,
    normalize_utc_timestamp,
)
from polytrading.venues.funding_cycle_models import (
    FUNDING_CYCLE_PROTOCOL_VERSION,
    FUNDING_CYCLE_WARNINGS,
    FUNDING_POINT_IN_TIME_LAG,
    FundingCaptureOutcome,
    FundingCollectionCycle,
    FundingCycleItem,
    FundingCycleStatus,
    InstrumentCaptureOutcome,
    validate_cycle_timing,
)
from polytrading.venues.public import (
    AdapterBatch,
    AdapterBatchIntegrityError,
    PublicVenueAdapter,
    validate_adapter_batch,
)
from polytrading.venues.recorder import PublicRecordStore, append_normalized

_EXPECTED_SYMBOL = {
    Venue.BYBIT: {asset: f"{asset.value}USDT" for asset in Asset},
    Venue.HYPERLIQUID: {asset: asset.value for asset in Asset},
}
_EXPECTED_VENUES = (Venue.BYBIT, Venue.HYPERLIQUID)


class FundingCycleStore(PublicRecordStore, Protocol):
    def transaction(self) -> AbstractContextManager[FundingCycleStore]: ...

    def latest_instrument_as_of(
        self, venue: Venue, symbol: str, as_of: datetime
    ) -> InstrumentSpec | None: ...

    def append_funding_collection_cycle(self, record: FundingCollectionCycle) -> bool: ...


@dataclass(frozen=True)
class _InstrumentResult:
    outcome: InstrumentCaptureOutcome
    observed_at: datetime | None
    source_hashes: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class _FundingResult:
    outcome: FundingCaptureOutcome
    effective_at: datetime | None
    observed_at: datetime | None
    source_hashes: tuple[str, ...]
    reason_codes: tuple[str, ...]


class PointInTimeFundingCollector:
    def __init__(
        self,
        store: FundingCycleStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        cycle_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._clock = clock
        self._cycle_id_factory = cycle_id_factory

    async def collect_once(
        self,
        adapters: Iterable[PublicVenueAdapter],
        assets: frozenset[Asset],
        cycle_end: datetime,
    ) -> FundingCollectionCycle:
        requested_assets = frozenset(assets)
        if not requested_assets:
            raise ValueError("at least one asset is required")
        ordered_adapters = tuple(sorted(adapters, key=lambda adapter: adapter.venue.value))
        adapter_venues = tuple(adapter.venue for adapter in ordered_adapters)
        if len(set(adapter_venues)) != len(adapter_venues):
            raise ValueError("public adapters must have unique venues")
        if adapter_venues != _EXPECTED_VENUES:
            raise ValueError("both Bybit and Hyperliquid adapters are required")

        request_started_at = normalize_utc_timestamp(self._clock())
        normalized_cycle_end, _, is_late = validate_cycle_timing(
            cycle_end, request_started_at
        )
        if is_late:
            raise ValueError("on-time collector requires collection clock within five minutes")

        instrument_results = await asyncio.gather(
            *(
                adapter.fetch_instruments(requested_assets, request_started_at)
                for adapter in ordered_adapters
            ),
            return_exceptions=True,
        )
        instrument_by_pair: dict[tuple[Venue, Asset], _InstrumentResult] = {}
        valid_raws: list[RawEnvelope] = []
        valid_instruments: list[InstrumentSpec] = []
        for adapter, result in zip(ordered_adapters, instrument_results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                reason = f"INSTRUMENT_FAILED:{adapter.venue.value}:{_failure_token(result)}"
                for asset in requested_assets:
                    instrument_by_pair[(adapter.venue, asset)] = _InstrumentResult(
                        outcome=InstrumentCaptureOutcome.FAILED,
                        observed_at=None,
                        source_hashes=(),
                        reason_codes=(reason,),
                    )
                continue
            try:
                specs = _validate_instrument_batch(result, adapter.venue, requested_assets)
            except (TypeError, ValueError, AdapterBatchIntegrityError) as error:
                reason = f"INSTRUMENT_FAILED:{adapter.venue.value}:{_failure_token(error)}"
                for asset in requested_assets:
                    instrument_by_pair[(adapter.venue, asset)] = _InstrumentResult(
                        outcome=InstrumentCaptureOutcome.FAILED,
                        observed_at=None,
                        source_hashes=(),
                        reason_codes=(reason,),
                    )
                continue
            hashes = tuple(sorted({raw.source_hash for raw in result.raw}))
            response_observed_at = max(raw.observed_at for raw in result.raw)
            valid_raws.extend(result.raw)
            valid_instruments.extend(specs)
            for spec in specs:
                instrument_by_pair[(adapter.venue, spec.asset)] = _InstrumentResult(
                    outcome=InstrumentCaptureOutcome.CAPTURED,
                    observed_at=response_observed_at,
                    source_hashes=hashes,
                    reason_codes=(),
                )

        calls: list[tuple[PublicVenueAdapter, Asset]] = []
        funding_by_pair: dict[tuple[Venue, Asset], _FundingResult] = {}
        for adapter in ordered_adapters:
            for asset in sorted(requested_assets, key=lambda item: item.value):
                if adapter.venue is Venue.BYBIT and self._store.latest_instrument_as_of(
                    Venue.BYBIT,
                    _EXPECTED_SYMBOL[Venue.BYBIT][asset],
                    normalized_cycle_end,
                ) is None:
                    funding_by_pair[(adapter.venue, asset)] = _FundingResult(
                        outcome=FundingCaptureOutcome.BOOTSTRAP_REQUIRED,
                        effective_at=None,
                        observed_at=None,
                        source_hashes=(),
                        reason_codes=("BYBIT_INSTRUMENT_BOOTSTRAP_REQUIRED",),
                    )
                    continue
                calls.append((adapter, asset))

        funding_results = await asyncio.gather(
            *(
                adapter.fetch_funding_history(
                    asset,
                    normalized_cycle_end,
                    normalized_cycle_end,
                    request_started_at,
                )
                for adapter, asset in calls
            ),
            return_exceptions=True,
        )
        valid_funding: list[FundingObservation] = []
        for (adapter, asset), result in zip(calls, funding_results, strict=True):
            pair = (adapter.venue, asset)
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                funding_by_pair[pair] = _funding_failure(adapter.venue, asset, result)
                continue
            try:
                observations = _validate_funding_batch(
                    result,
                    venue=adapter.venue,
                    asset=asset,
                    cycle_end=normalized_cycle_end,
                )
            except (TypeError, ValueError, AdapterBatchIntegrityError) as error:
                funding_by_pair[pair] = _funding_failure(adapter.venue, asset, error)
                continue
            hashes = tuple(sorted({raw.source_hash for raw in result.raw}))
            response_observed_at = max(raw.observed_at for raw in result.raw)
            valid_raws.extend(result.raw)
            if observations:
                observation = observations[0]
                valid_funding.append(observation)
                funding_by_pair[pair] = _FundingResult(
                    outcome=FundingCaptureOutcome.CAPTURED,
                    effective_at=observation.effective_at,
                    observed_at=response_observed_at,
                    source_hashes=hashes,
                    reason_codes=(),
                )
            elif adapter.venue is Venue.HYPERLIQUID:
                funding_by_pair[pair] = _FundingResult(
                    outcome=FundingCaptureOutcome.MISSING_EXPECTED,
                    effective_at=None,
                    observed_at=response_observed_at,
                    source_hashes=hashes,
                    reason_codes=("FUNDING_MISSING_EXPECTED",),
                )
            else:
                funding_by_pair[pair] = _FundingResult(
                    outcome=FundingCaptureOutcome.NO_SETTLEMENT,
                    effective_at=None,
                    observed_at=response_observed_at,
                    source_hashes=hashes,
                    reason_codes=(),
                )

        request_completed_at = normalize_utc_timestamp(self._clock())
        ordered_assets = tuple(sorted(requested_assets, key=lambda item: item.value))
        items = tuple(
            _make_item(
                venue,
                asset,
                instrument_by_pair[(venue, asset)],
                funding_by_pair[(venue, asset)],
            )
            for venue in _EXPECTED_VENUES
            for asset in ordered_assets
        )
        status = _cycle_status(items, normalized_cycle_end)
        source_hashes = tuple(
            sorted(
                {
                    source_hash
                    for item in items
                    for source_hash in (
                        *item.instrument_source_hashes,
                        *item.funding_source_hashes,
                    )
                }
            )
        )
        cycle = FundingCollectionCycle(
            schema_version=1,
            protocol_version=FUNDING_CYCLE_PROTOCOL_VERSION,
            cycle_id=self._cycle_id_factory(),
            cycle_end=normalized_cycle_end,
            assets=ordered_assets,
            venues=_EXPECTED_VENUES,
            request_started_at=request_started_at,
            request_completed_at=request_completed_at,
            items=items,
            status=status,
            source_hashes=source_hashes,
            warnings=FUNDING_CYCLE_WARNINGS,
        )

        raws_by_id = {raw.event_id: raw for raw in valid_raws}
        ordered_raws = tuple(
            sorted(
                raws_by_id.values(),
                key=lambda raw: (
                    raw.venue.value,
                    raw.endpoint,
                    raw.source_hash,
                    str(raw.event_id),
                ),
            )
        )
        ordered_instruments = tuple(
            sorted(
                valid_instruments,
                key=lambda record: (
                    record.venue.value,
                    record.asset.value,
                    record.symbol,
                    record.observed_at,
                ),
            )
        )
        ordered_funding = tuple(
            sorted(
                valid_funding,
                key=lambda record: (
                    record.venue.value,
                    record.asset.value,
                    record.symbol,
                    record.effective_at,
                    record.observed_at,
                ),
            )
        )
        with self._store.transaction() as transaction:
            for raw in ordered_raws:
                transaction.append_raw(raw)
            for normalized in (*ordered_instruments, *ordered_funding):
                append_normalized(transaction, normalized)
            transaction.append_funding_collection_cycle(cycle)
        return cycle


def record_late_funding_cycle(
    store: FundingCycleStore,
    assets: frozenset[Asset],
    cycle_end: datetime,
    now: datetime,
    *,
    cycle_id_factory: Callable[[], UUID] = uuid4,
) -> FundingCollectionCycle:
    requested_assets = frozenset(assets)
    if not requested_assets:
        raise ValueError("at least one asset is required")
    normalized_cycle_end, normalized_now, is_late = validate_cycle_timing(cycle_end, now)
    if not is_late:
        raise ValueError("late cycle requires a clock after the five-minute cutoff")
    ordered_assets = tuple(sorted(requested_assets, key=lambda item: item.value))
    items = tuple(
        FundingCycleItem(
            schema_version=1,
            venue=venue,
            asset=asset,
            symbol=_EXPECTED_SYMBOL[venue][asset],
            instrument_outcome=InstrumentCaptureOutcome.LATE_NOT_COLLECTED,
            funding_outcome=FundingCaptureOutcome.LATE_NOT_COLLECTED,
            instrument_observed_at=None,
            funding_effective_at=None,
            funding_observed_at=None,
            instrument_source_hashes=(),
            funding_source_hashes=(),
            reason_codes=("COLLECTION_WINDOW_MISSED",),
        )
        for venue in _EXPECTED_VENUES
        for asset in ordered_assets
    )
    cycle = FundingCollectionCycle(
        schema_version=1,
        protocol_version=FUNDING_CYCLE_PROTOCOL_VERSION,
        cycle_id=cycle_id_factory(),
        cycle_end=normalized_cycle_end,
        assets=ordered_assets,
        venues=_EXPECTED_VENUES,
        request_started_at=normalized_now,
        request_completed_at=normalized_now,
        items=items,
        status=FundingCycleStatus.LATE,
        source_hashes=(),
        warnings=FUNDING_CYCLE_WARNINGS,
    )
    store.append_funding_collection_cycle(cycle)
    return cycle


def _validate_instrument_batch(
    batch: AdapterBatch,
    venue: Venue,
    assets: frozenset[Asset],
) -> tuple[InstrumentSpec, ...]:
    if any(type(record) is not InstrumentSpec for record in batch.normalized):
        raise TypeError("instrument batch contains an invalid normalized record")
    specs = tuple(record for record in batch.normalized if type(record) is InstrumentSpec)
    if any(record.venue is not venue for record in specs):
        raise ValueError("instrument venue does not match adapter")
    _require_response_raw(batch)
    identities = tuple((record.asset, record.symbol) for record in specs)
    expected = tuple(
        (asset, _EXPECTED_SYMBOL[venue][asset])
        for asset in sorted(assets, key=lambda item: item.value)
    )
    if tuple(sorted(identities, key=lambda item: (item[0].value, item[1]))) != expected:
        raise ValueError("instrument batch does not cover requested assets")
    validate_adapter_batch(batch)
    _require_normalized_observation_lineage(batch, specs)
    return tuple(sorted(specs, key=lambda record: record.asset.value))


def _validate_funding_batch(
    batch: AdapterBatch,
    *,
    venue: Venue,
    asset: Asset,
    cycle_end: datetime,
) -> tuple[FundingObservation, ...]:
    if any(type(record) is not FundingObservation for record in batch.normalized):
        raise TypeError("funding batch contains an invalid normalized record")
    observations = tuple(
        record for record in batch.normalized if type(record) is FundingObservation
    )
    _require_response_raw(batch)
    if len(observations) > 1:
        raise ValueError("funding batch contains more than one exact-boundary record")
    for record in observations:
        if (
            record.venue is not venue
            or record.asset is not asset
            or record.symbol != _EXPECTED_SYMBOL[venue][asset]
        ):
            raise ValueError("funding identity does not match request")
        if record.effective_at != cycle_end:
            raise ValueError("funding effective time does not match cycle end")
    validate_adapter_batch(batch)
    _require_normalized_observation_lineage(batch, observations)
    return observations


def _require_response_raw(batch: AdapterBatch) -> None:
    if not batch.raw:
        raise ValueError("successful adapter batch requires a raw response")


def _require_normalized_observation_lineage(
    batch: AdapterBatch,
    records: tuple[InstrumentSpec, ...] | tuple[FundingObservation, ...],
) -> None:
    raw_observations = {
        (raw.source_hash, raw.observed_at) for raw in batch.raw
    }
    if any((record.source_hash, record.observed_at) not in raw_observations for record in records):
        raise ValueError("normalized observation time does not match raw lineage")


def _failure_token(error: BaseException) -> str:
    return error.code if isinstance(error, AdapterBatchIntegrityError) else type(error).__name__


def _funding_failure(venue: Venue, asset: Asset, error: BaseException) -> _FundingResult:
    return _FundingResult(
        outcome=FundingCaptureOutcome.FAILED,
        effective_at=None,
        observed_at=None,
        source_hashes=(),
        reason_codes=(
            f"FUNDING_FAILED:{venue.value}:{asset.value}:{_failure_token(error)}",
        ),
    )


def _make_item(
    venue: Venue,
    asset: Asset,
    instrument: _InstrumentResult,
    funding: _FundingResult,
) -> FundingCycleItem:
    return FundingCycleItem(
        schema_version=1,
        venue=venue,
        asset=asset,
        symbol=_EXPECTED_SYMBOL[venue][asset],
        instrument_outcome=instrument.outcome,
        funding_outcome=funding.outcome,
        instrument_observed_at=instrument.observed_at,
        funding_effective_at=funding.effective_at,
        funding_observed_at=funding.observed_at,
        instrument_source_hashes=instrument.source_hashes,
        funding_source_hashes=funding.source_hashes,
        reason_codes=tuple(sorted(set(instrument.reason_codes + funding.reason_codes))),
    )


def _cycle_status(
    items: tuple[FundingCycleItem, ...], cycle_end: datetime
) -> FundingCycleStatus:
    cutoff = cycle_end + FUNDING_POINT_IN_TIME_LAG
    if any(
        (item.instrument_observed_at is not None and item.instrument_observed_at > cutoff)
        or (item.funding_observed_at is not None and item.funding_observed_at > cutoff)
        for item in items
    ):
        return FundingCycleStatus.LATE
    if any(
        item.instrument_outcome is not InstrumentCaptureOutcome.CAPTURED
        or item.funding_outcome
        in (
            FundingCaptureOutcome.MISSING_EXPECTED,
            FundingCaptureOutcome.BOOTSTRAP_REQUIRED,
            FundingCaptureOutcome.FAILED,
        )
        for item in items
    ):
        return FundingCycleStatus.DEGRADED
    return FundingCycleStatus.COMPLETE
