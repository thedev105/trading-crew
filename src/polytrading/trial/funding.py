from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from polytrading.domain.models import (
    Asset,
    FundingObservation,
    InstrumentSpec,
    RawEnvelope,
    Venue,
    normalize_utc_timestamp,
)
from polytrading.storage.store import DuckDBStore
from polytrading.trial.funding_models import (
    TRIAL_FUNDING_POINT_IN_TIME_LAG,
    TRIAL_FUNDING_PROTOCOL_VERSION,
    TRIAL_FUNDING_WARNINGS,
    LighterDydxFundingCycle,
    LighterDydxFundingItem,
    TrialFundingCycleStatus,
    TrialFundingOutcome,
    TrialInstrumentOutcome,
    validate_trial_cycle_timing,
)
from polytrading.venues.public import (
    AdapterBatch,
    AdapterBatchIntegrityError,
    PublicVenueAdapter,
    validate_adapter_batch,
)

_EXPECTED_VENUES = (Venue.DYDX, Venue.LIGHTER)
_EXPECTED_SYMBOL = {
    Venue.DYDX: {asset: f"{asset.value}-USD" for asset in Asset},
    Venue.LIGHTER: {asset: asset.value for asset in Asset},
}


@dataclass(frozen=True)
class PreparedLighterDydxFundingCycle:
    raw: tuple[RawEnvelope, ...]
    instruments: tuple[InstrumentSpec, ...]
    funding: tuple[FundingObservation, ...]
    cycle: LighterDydxFundingCycle


@dataclass(frozen=True)
class _InstrumentResult:
    outcome: TrialInstrumentOutcome
    observed_at: datetime | None
    source_hashes: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class _FundingResult:
    outcome: TrialFundingOutcome
    effective_at: datetime | None
    observed_at: datetime | None
    source_hashes: tuple[str, ...]
    reason_codes: tuple[str, ...]


class LighterDydxFundingCollector:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        cycle_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._clock = clock
        self._cycle_id_factory = cycle_id_factory

    async def prepare_once(
        self,
        adapters: Iterable[PublicVenueAdapter],
        assets: frozenset[Asset],
        cycle_end: datetime,
    ) -> PreparedLighterDydxFundingCycle:
        requested_assets = frozenset(assets)
        if not requested_assets:
            raise ValueError("at least one asset is required")
        ordered_adapters = tuple(sorted(adapters, key=lambda adapter: adapter.venue.value))
        adapter_venues = tuple(adapter.venue for adapter in ordered_adapters)
        if len(set(adapter_venues)) != len(adapter_venues):
            raise ValueError("public adapters must have unique venues")
        if adapter_venues != _EXPECTED_VENUES:
            raise ValueError("both dYdX and Lighter adapters are required")

        request_started_at = normalize_utc_timestamp(self._clock())
        normalized_cycle_end, _, is_late = validate_trial_cycle_timing(
            cycle_end, request_started_at
        )
        if is_late:
            raise ValueError("on-time collector requires collection clock within five minutes")

        ordered_assets = tuple(sorted(requested_assets, key=lambda item: item.value))
        valid_raws: list[RawEnvelope] = []
        valid_instruments: list[InstrumentSpec] = []
        instrument_by_pair: dict[tuple[Venue, Asset], _InstrumentResult] = {}
        instrument_results = await asyncio.gather(
            *(
                adapter.fetch_instruments(requested_assets, request_started_at)
                for adapter in ordered_adapters
            ),
            return_exceptions=True,
        )
        for adapter, result in zip(ordered_adapters, instrument_results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                _record_instrument_failure(
                    instrument_by_pair, adapter.venue, ordered_assets, result
                )
                continue
            try:
                specs = _validate_instrument_batch(
                    result, adapter.venue, requested_assets, request_started_at
                )
                _require_new_raw_identities(result.raw, valid_raws)
            except (TypeError, ValueError, AdapterBatchIntegrityError) as error:
                _record_instrument_failure(instrument_by_pair, adapter.venue, ordered_assets, error)
                continue
            hashes = tuple(sorted({raw.source_hash for raw in result.raw}))
            response_observed_at = max(raw.observed_at for raw in result.raw)
            valid_raws.extend(result.raw)
            valid_instruments.extend(specs)
            for spec in specs:
                instrument_by_pair[(adapter.venue, spec.asset)] = _InstrumentResult(
                    outcome=TrialInstrumentOutcome.CAPTURED,
                    observed_at=response_observed_at,
                    source_hashes=hashes,
                    reason_codes=(),
                )

        calls = tuple((adapter, asset) for adapter in ordered_adapters for asset in ordered_assets)
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
        funding_by_pair: dict[tuple[Venue, Asset], _FundingResult] = {}
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
                    request_started_at=request_started_at,
                )
                _require_new_raw_identities(result.raw, valid_raws)
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
                    outcome=TrialFundingOutcome.CAPTURED,
                    effective_at=observation.effective_at,
                    observed_at=response_observed_at,
                    source_hashes=hashes,
                    reason_codes=(),
                )
            else:
                funding_by_pair[pair] = _FundingResult(
                    outcome=TrialFundingOutcome.MISSING_EXPECTED,
                    effective_at=None,
                    observed_at=response_observed_at,
                    source_hashes=hashes,
                    reason_codes=("FUNDING_MISSING_EXPECTED",),
                )

        request_completed_at = normalize_utc_timestamp(self._clock())
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
        cycle = LighterDydxFundingCycle(
            schema_version=1,
            protocol_version=TRIAL_FUNDING_PROTOCOL_VERSION,
            cycle_id=self._cycle_id_factory(),
            cycle_end=normalized_cycle_end,
            assets=ordered_assets,
            venues=_EXPECTED_VENUES,
            request_started_at=request_started_at,
            request_completed_at=request_completed_at,
            items=items,
            status=_cycle_status(items, normalized_cycle_end),
            source_hashes=_item_source_hashes(items),
            warnings=TRIAL_FUNDING_WARNINGS,
        )
        return PreparedLighterDydxFundingCycle(
            raw=tuple(sorted(valid_raws, key=_raw_sort_key)),
            instruments=tuple(sorted(valid_instruments, key=_instrument_sort_key)),
            funding=tuple(sorted(valid_funding, key=_funding_sort_key)),
            cycle=cycle,
        )


def record_late_lighter_dydx_cycle(
    assets: frozenset[Asset],
    cycle_end: datetime,
    now: datetime,
    cycle_id_factory: Callable[[], UUID] = uuid4,
) -> LighterDydxFundingCycle:
    requested_assets = frozenset(assets)
    if not requested_assets:
        raise ValueError("at least one asset is required")
    normalized_cycle_end, normalized_now, is_late = validate_trial_cycle_timing(cycle_end, now)
    if not is_late:
        raise ValueError("late cycle requires a clock after the five-minute cutoff")
    ordered_assets = tuple(sorted(requested_assets, key=lambda item: item.value))
    items = tuple(
        LighterDydxFundingItem(
            schema_version=1,
            venue=venue,
            asset=asset,
            symbol=_EXPECTED_SYMBOL[venue][asset],
            instrument_outcome=TrialInstrumentOutcome.LATE_NOT_COLLECTED,
            funding_outcome=TrialFundingOutcome.LATE_NOT_COLLECTED,
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
    return LighterDydxFundingCycle(
        schema_version=1,
        protocol_version=TRIAL_FUNDING_PROTOCOL_VERSION,
        cycle_id=cycle_id_factory(),
        cycle_end=normalized_cycle_end,
        assets=ordered_assets,
        venues=_EXPECTED_VENUES,
        request_started_at=normalized_now,
        request_completed_at=normalized_now,
        items=items,
        status=TrialFundingCycleStatus.LATE,
        source_hashes=(),
        warnings=TRIAL_FUNDING_WARNINGS,
    )


def persist_lighter_dydx_funding_cycle(
    store: DuckDBStore, prepared: PreparedLighterDydxFundingCycle
) -> bool:
    _validate_prepared_conservation(prepared)
    with store.transaction() as transaction:
        for raw in prepared.raw:
            transaction.append_raw(raw)
        for instrument in prepared.instruments:
            transaction.append_instrument(instrument)
        for observation in prepared.funding:
            transaction.append_funding(observation)
        return transaction.append_lighter_dydx_funding_cycle(prepared.cycle)


def _validate_instrument_batch(
    batch: AdapterBatch,
    venue: Venue,
    assets: frozenset[Asset],
    request_started_at: datetime,
) -> tuple[InstrumentSpec, ...]:
    if not isinstance(batch, AdapterBatch):
        raise TypeError("instrument response must be an adapter batch")
    if any(type(record) is not InstrumentSpec for record in batch.normalized):
        raise TypeError("instrument batch contains an invalid normalized record")
    specs = tuple(record for record in batch.normalized if type(record) is InstrumentSpec)
    _validate_response_raw(batch, venue, request_started_at)
    if any(record.venue is not venue for record in specs):
        raise ValueError("instrument venue does not match adapter")
    identities = tuple((record.asset, record.symbol) for record in specs)
    expected = tuple(
        (asset, _EXPECTED_SYMBOL[venue][asset])
        for asset in sorted(assets, key=lambda item: item.value)
    )
    if tuple(sorted(identities, key=lambda item: (item[0].value, item[1]))) != expected:
        raise ValueError("instrument batch does not cover requested assets")
    if any(record.funding_interval_hours != Decimal(1) for record in specs):
        raise ValueError("instrument funding interval must be one hour")
    validate_adapter_batch(batch)
    _require_normalized_observation_lineage(batch, specs)
    return tuple(sorted(specs, key=lambda record: record.asset.value))


def _validate_funding_batch(
    batch: AdapterBatch,
    *,
    venue: Venue,
    asset: Asset,
    cycle_end: datetime,
    request_started_at: datetime,
) -> tuple[FundingObservation, ...]:
    if not isinstance(batch, AdapterBatch):
        raise TypeError("funding response must be an adapter batch")
    if any(type(record) is not FundingObservation for record in batch.normalized):
        raise TypeError("funding batch contains an invalid normalized record")
    observations = tuple(
        record for record in batch.normalized if type(record) is FundingObservation
    )
    _validate_response_raw(batch, venue, request_started_at)
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
        if record.interval_hours != Decimal(1):
            raise ValueError("funding interval must be one hour")
    validate_adapter_batch(batch)
    _require_normalized_observation_lineage(batch, observations)
    return observations


def _validate_response_raw(batch: AdapterBatch, venue: Venue, request_started_at: datetime) -> None:
    if not batch.raw:
        raise ValueError("successful adapter batch requires a raw response")
    if len({raw.event_id for raw in batch.raw}) != len(batch.raw):
        raise ValueError("adapter batch contains duplicate raw identities")
    if any(raw.venue is not venue for raw in batch.raw):
        raise ValueError("raw response venue does not match adapter")
    if any(raw.observed_at < request_started_at for raw in batch.raw):
        raise ValueError("raw response observation precedes request start")


def _require_normalized_observation_lineage(
    batch: AdapterBatch,
    records: tuple[InstrumentSpec, ...] | tuple[FundingObservation, ...],
) -> None:
    raw_observations = {(raw.source_hash, raw.observed_at) for raw in batch.raw}
    if any((record.source_hash, record.observed_at) not in raw_observations for record in records):
        raise ValueError("normalized observation time does not match raw lineage")


def _require_new_raw_identities(
    batch_raw: tuple[RawEnvelope, ...], accepted_raw: list[RawEnvelope]
) -> None:
    accepted_ids = {raw.event_id for raw in accepted_raw}
    if any(raw.event_id in accepted_ids for raw in batch_raw):
        raise ValueError("adapter response reuses a raw identity")


def _record_instrument_failure(
    results: dict[tuple[Venue, Asset], _InstrumentResult],
    venue: Venue,
    assets: tuple[Asset, ...],
    error: BaseException,
) -> None:
    reason = f"INSTRUMENT_FAILED:{venue.value}:{type(error).__name__}"
    for asset in assets:
        results[(venue, asset)] = _InstrumentResult(
            outcome=TrialInstrumentOutcome.FAILED,
            observed_at=None,
            source_hashes=(),
            reason_codes=(reason,),
        )


def _funding_failure(venue: Venue, asset: Asset, error: BaseException) -> _FundingResult:
    return _FundingResult(
        outcome=TrialFundingOutcome.FAILED,
        effective_at=None,
        observed_at=None,
        source_hashes=(),
        reason_codes=(f"FUNDING_FAILED:{venue.value}:{asset.value}:{type(error).__name__}",),
    )


def _make_item(
    venue: Venue,
    asset: Asset,
    instrument: _InstrumentResult,
    funding: _FundingResult,
) -> LighterDydxFundingItem:
    return LighterDydxFundingItem(
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
    items: tuple[LighterDydxFundingItem, ...], cycle_end: datetime
) -> TrialFundingCycleStatus:
    cutoff = cycle_end + TRIAL_FUNDING_POINT_IN_TIME_LAG
    if any(
        observed_at is not None and observed_at > cutoff
        for item in items
        for observed_at in (item.instrument_observed_at, item.funding_observed_at)
    ):
        return TrialFundingCycleStatus.LATE
    if any(
        item.instrument_outcome is TrialInstrumentOutcome.FAILED
        or item.funding_outcome
        in (TrialFundingOutcome.MISSING_EXPECTED, TrialFundingOutcome.FAILED)
        for item in items
    ):
        return TrialFundingCycleStatus.DEGRADED
    return TrialFundingCycleStatus.COMPLETE


def _item_source_hashes(items: tuple[LighterDydxFundingItem, ...]) -> tuple[str, ...]:
    return tuple(
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


def _validate_prepared_conservation(prepared: PreparedLighterDydxFundingCycle) -> None:
    raw_lineage = {(raw.venue, raw.source_hash) for raw in prepared.raw}
    raw_hashes = {raw.source_hash for raw in prepared.raw}
    if len({raw.event_id for raw in prepared.raw}) != len(prepared.raw):
        raise ValueError("prepared raw identities are not conserved uniquely")
    if raw_hashes != set(prepared.cycle.source_hashes):
        raise ValueError("prepared raw hashes are not conserved by cycle evidence")
    if any(
        (item.venue, source_hash) not in raw_lineage
        for item in prepared.cycle.items
        for source_hash in (*item.instrument_source_hashes, *item.funding_source_hashes)
    ):
        raise ValueError("prepared raw lineage is not conserved by cycle items")
    normalized = (*prepared.instruments, *prepared.funding)
    if any((record.venue, record.source_hash) not in raw_lineage for record in normalized):
        raise ValueError("prepared normalized lineage is not conserved by raw evidence")

    instrument_by_pair: dict[tuple[Venue, Asset], list[InstrumentSpec]] = {}
    for record in prepared.instruments:
        instrument_by_pair.setdefault((record.venue, record.asset), []).append(record)
    funding_by_pair: dict[tuple[Venue, Asset], list[FundingObservation]] = {}
    for record in prepared.funding:
        funding_by_pair.setdefault((record.venue, record.asset), []).append(record)
    expected_pairs = {(item.venue, item.asset) for item in prepared.cycle.items}
    if set(instrument_by_pair) - expected_pairs or set(funding_by_pair) - expected_pairs:
        raise ValueError("prepared normalized identities are not conserved by cycle evidence")
    for item in prepared.cycle.items:
        instruments = instrument_by_pair.get((item.venue, item.asset), [])
        funding = funding_by_pair.get((item.venue, item.asset), [])
        if (item.instrument_outcome is TrialInstrumentOutcome.CAPTURED) != (len(instruments) == 1):
            raise ValueError("prepared instrument evidence is not conserved by cycle outcome")
        if (item.funding_outcome is TrialFundingOutcome.CAPTURED) != (len(funding) == 1):
            raise ValueError("prepared funding evidence is not conserved by cycle outcome")
        for record in instruments:
            if (
                record.symbol != item.symbol
                or record.source_hash not in item.instrument_source_hashes
            ):
                raise ValueError("prepared instrument evidence is not conserved by cycle item")
        for record in funding:
            if (
                record.symbol != item.symbol
                or record.effective_at != prepared.cycle.cycle_end
                or record.source_hash not in item.funding_source_hashes
            ):
                raise ValueError("prepared funding evidence is not conserved by cycle item")


def _raw_sort_key(raw: RawEnvelope) -> tuple[str, str, str, str]:
    return raw.venue.value, raw.endpoint, raw.source_hash, str(raw.event_id)


def _instrument_sort_key(record: InstrumentSpec) -> tuple[str, str, str, datetime]:
    return record.venue.value, record.asset.value, record.symbol, record.observed_at


def _funding_sort_key(record: FundingObservation) -> tuple[str, str, str, datetime, datetime]:
    return (
        record.venue.value,
        record.asset.value,
        record.symbol,
        record.effective_at,
        record.observed_at,
    )
