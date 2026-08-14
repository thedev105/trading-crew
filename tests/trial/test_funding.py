from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from polytrading.domain.models import (
    Asset,
    FundingObservation,
    InstrumentKind,
    InstrumentSpec,
    RawEnvelope,
    Venue,
)
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from polytrading.trial.funding import (
    LighterDydxFundingCollector,
    PreparedLighterDydxFundingCycle,
    persist_lighter_dydx_funding_cycle,
    record_late_lighter_dydx_cycle,
)
from polytrading.trial.funding_models import (
    LighterDydxFundingCycle,
    TrialFundingCycleStatus,
    TrialFundingOutcome,
)
from polytrading.venues.public import AdapterBatch
from tests.trial.funding_helpers import CYCLE_END, CYCLE_ID

ASSETS = frozenset(Asset)


def _symbol(venue: Venue, asset: Asset) -> str:
    return f"{asset.value}-USD" if venue is Venue.DYDX else asset.value


def _raw(venue: Venue, label: str, observed_at: datetime, event_int: int) -> RawEnvelope:
    payload = f'{{"label":"{label}","venue":"{venue.value}"}}'
    return RawEnvelope(
        schema_version=1,
        event_id=UUID(int=event_int),
        venue=venue,
        endpoint=f"/{venue.value}/{label}",
        venue_timestamp=None,
        observed_at=observed_at,
        received_monotonic_ns=event_int,
        request_latency_ms=Decimal("1"),
        source_version="fixture-v1",
        payload_json=payload,
        source_hash=hashlib.sha256(payload.encode()).hexdigest(),
    )


def _instrument(
    venue: Venue,
    asset: Asset,
    observed_at: datetime,
    source_hash: str,
) -> InstrumentSpec:
    symbol = _symbol(venue, asset)
    return InstrumentSpec(
        schema_version=1,
        instrument_id=f"{venue.value}:{symbol}",
        venue=venue,
        symbol=symbol,
        asset=asset,
        kind=InstrumentKind.LINEAR_PERPETUAL,
        contract_multiplier=Decimal("1"),
        index_family=None,
        oracle_family=None,
        mark_method=None,
        liquidation_method=None,
        collateral_asset="USDC",
        pnl_asset="USDC",
        funding_formula_id=None,
        funding_cap=None,
        funding_interval_hours=Decimal("1"),
        funding_payment_offset_minutes=None,
        min_notional=None,
        quantity_step=Decimal("0.001"),
        price_tick=Decimal("0.1"),
        is_inverse=False,
        is_prelaunch=False,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def _funding(
    venue: Venue,
    asset: Asset,
    effective_at: datetime,
    observed_at: datetime,
    source_hash: str,
) -> FundingObservation:
    return FundingObservation(
        schema_version=1,
        venue=venue,
        symbol=_symbol(venue, asset),
        asset=asset,
        rate=Decimal("0.0001"),
        interval_hours=Decimal("1"),
        effective_at=effective_at,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def _instrument_batch(
    venue: Venue,
    observed_at: datetime,
    event_int: int,
    *,
    assets: frozenset[Asset] = ASSETS,
) -> AdapterBatch:
    raw = _raw(venue, "instruments", observed_at, event_int)
    return AdapterBatch(
        raw=(raw,),
        normalized=tuple(
            _instrument(venue, asset, observed_at, raw.source_hash)
            for asset in sorted(assets, key=lambda item: item.value)
        ),
    )


def _funding_batch(
    venue: Venue,
    asset: Asset,
    observed_at: datetime,
    event_int: int,
    *,
    effective_at: datetime = CYCLE_END,
    include_record: bool = True,
) -> AdapterBatch:
    raw = _raw(venue, f"funding-{asset.value}", observed_at, event_int)
    return AdapterBatch(
        raw=(raw,),
        normalized=(
            (_funding(venue, asset, effective_at, observed_at, raw.source_hash),)
            if include_record
            else ()
        ),
    )


class FakeAdapter:
    def __init__(
        self,
        venue: Venue,
        *,
        instrument_result: AdapterBatch | BaseException | None = None,
        funding_results: dict[Asset, AdapterBatch | BaseException] | None = None,
    ) -> None:
        offset = 100 if venue is Venue.DYDX else 200
        self.venue = venue
        self.instrument_result = instrument_result or _instrument_batch(
            venue, CYCLE_END + timedelta(seconds=11), offset
        )
        self.funding_results = funding_results or {
            asset: _funding_batch(
                venue,
                asset,
                CYCLE_END + timedelta(seconds=12),
                offset + index + 1,
            )
            for index, asset in enumerate(sorted(ASSETS, key=lambda item: item.value))
        }
        self.instrument_calls: list[tuple[frozenset[Asset], datetime]] = []
        self.funding_calls: list[tuple[Asset, datetime, datetime, datetime]] = []

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.instrument_calls.append((assets, observed_at))
        if isinstance(self.instrument_result, BaseException):
            raise self.instrument_result
        return self.instrument_result

    async def fetch_funding_history(
        self, asset: Asset, start: datetime, end: datetime, observed_at: datetime
    ) -> AdapterBatch:
        self.funding_calls.append((asset, start, end, observed_at))
        result = self.funding_results[asset]
        if isinstance(result, BaseException):
            raise result
        return result

    async def fetch_market_snapshots(self, *args: object, **kwargs: object) -> AdapterBatch:
        raise AssertionError("unexpected market request")

    async def fetch_order_books(self, *args: object, **kwargs: object) -> AdapterBatch:
        raise AssertionError("unexpected book request")


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


def _collect(
    adapters: tuple[FakeAdapter, FakeAdapter],
    *,
    clock: SequenceClock | None = None,
) -> PreparedLighterDydxFundingCycle:
    return asyncio.run(
        LighterDydxFundingCollector(
            clock=clock
            or SequenceClock(CYCLE_END + timedelta(seconds=10), CYCLE_END + timedelta(seconds=20)),
            cycle_id_factory=lambda: CYCLE_ID,
        ).prepare_once(adapters, ASSETS, CYCLE_END)
    )


def complete_prepared_cycle() -> PreparedLighterDydxFundingCycle:
    return _collect((FakeAdapter(Venue.DYDX), FakeAdapter(Venue.LIGHTER)))


def test_late_trial_cycle_uses_no_adapter_and_has_all_missed_items() -> None:
    cycle = record_late_lighter_dydx_cycle(
        frozenset(Asset),
        CYCLE_END,
        CYCLE_END + timedelta(minutes=5, microseconds=1),
        cycle_id_factory=lambda: CYCLE_ID,
    )
    assert cycle.status is TrialFundingCycleStatus.LATE
    assert all(
        item.funding_outcome is TrialFundingOutcome.LATE_NOT_COLLECTED for item in cycle.items
    )


def test_late_trial_cycle_rejects_asset_subset() -> None:
    with pytest.raises(ValueError, match="BTC, ETH, and SOL"):
        record_late_lighter_dydx_cycle(
            frozenset({Asset.BTC}),
            CYCLE_END,
            CYCLE_END + timedelta(minutes=5, microseconds=1),
        )


def test_collector_requests_every_exact_lighter_dydx_boundary() -> None:
    adapters = (FakeAdapter(Venue.DYDX), FakeAdapter(Venue.LIGHTER))
    prepared = asyncio.run(
        LighterDydxFundingCollector(
            clock=SequenceClock(
                CYCLE_END + timedelta(seconds=10), CYCLE_END + timedelta(seconds=20)
            ),
            cycle_id_factory=lambda: CYCLE_ID,
        ).prepare_once(adapters, frozenset(Asset), CYCLE_END)
    )

    assert [call[1:3] for adapter in adapters for call in adapter.funding_calls] == [
        (CYCLE_END, CYCLE_END),
    ] * 6
    assert prepared.cycle.status is TrialFundingCycleStatus.COMPLETE


def test_collector_rejects_asset_subset_before_adapter_requests() -> None:
    adapters = (FakeAdapter(Venue.DYDX), FakeAdapter(Venue.LIGHTER))

    with pytest.raises(ValueError, match="BTC, ETH, and SOL"):
        asyncio.run(
            LighterDydxFundingCollector(
                clock=SequenceClock(
                    CYCLE_END + timedelta(seconds=10), CYCLE_END + timedelta(seconds=20)
                ),
                cycle_id_factory=lambda: CYCLE_ID,
            ).prepare_once(adapters, frozenset({Asset.BTC}), CYCLE_END)
        )

    assert all(adapter.instrument_calls == [] for adapter in adapters)
    assert all(adapter.funding_calls == [] for adapter in adapters)


def test_empty_funding_responses_are_missing_expected_for_both_venues() -> None:
    adapters = tuple(
        FakeAdapter(
            venue,
            funding_results={
                asset: _funding_batch(
                    venue,
                    asset,
                    CYCLE_END + timedelta(seconds=12),
                    offset + index,
                    include_record=False,
                )
                for index, asset in enumerate(sorted(ASSETS, key=lambda item: item.value))
            },
        )
        for venue, offset in ((Venue.DYDX, 300), (Venue.LIGHTER, 400))
    )

    prepared = _collect(adapters)  # type: ignore[arg-type]

    assert prepared.funding == ()
    assert prepared.cycle.status is TrialFundingCycleStatus.DEGRADED
    assert all(
        item.funding_outcome is TrialFundingOutcome.MISSING_EXPECTED
        for item in prepared.cycle.items
    )


def test_one_funding_exception_does_not_erase_other_valid_evidence() -> None:
    dydx = FakeAdapter(Venue.DYDX)
    dydx.funding_results[Asset.ETH] = TimeoutError("public timeout")

    prepared = _collect((dydx, FakeAdapter(Venue.LIGHTER)))

    failed = next(
        item
        for item in prepared.cycle.items
        if item.venue is Venue.DYDX and item.asset is Asset.ETH
    )
    assert failed.funding_outcome is TrialFundingOutcome.FAILED
    assert failed.reason_codes == ("FUNDING_FAILED:dydx:ETH:TimeoutError",)
    assert len(prepared.funding) == 5
    assert (
        sum(item.funding_outcome is TrialFundingOutcome.CAPTURED for item in prepared.cycle.items)
        == 5
    )


def test_non_batch_adapter_response_is_scoped_as_failure() -> None:
    dydx = FakeAdapter(Venue.DYDX)
    dydx.instrument_result = object()  # type: ignore[assignment]

    prepared = _collect((dydx, FakeAdapter(Venue.LIGHTER)))

    assert all(
        item.instrument_outcome.value == "failed"
        for item in prepared.cycle.items
        if item.venue is Venue.DYDX
    )
    assert all(
        item.reason_codes == ("INSTRUMENT_FAILED:dydx:TypeError",)
        for item in prepared.cycle.items
        if item.venue is Venue.DYDX
    )


def test_response_timestamp_before_request_start_is_scoped_as_failure() -> None:
    dydx = FakeAdapter(
        Venue.DYDX,
        instrument_result=_instrument_batch(
            Venue.DYDX,
            CYCLE_END + timedelta(seconds=5),
            555,
        ),
    )

    prepared = _collect((dydx, FakeAdapter(Venue.LIGHTER)))

    assert all(
        item.instrument_outcome.value == "failed"
        for item in prepared.cycle.items
        if item.venue is Venue.DYDX
    )
    assert len(prepared.instruments) == 3


@pytest.mark.parametrize("phase", ["instrument", "funding"])
def test_cancellation_propagates(phase: str) -> None:
    dydx = FakeAdapter(Venue.DYDX)
    if phase == "instrument":
        dydx.instrument_result = asyncio.CancelledError()
    else:
        dydx.funding_results[Asset.BTC] = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _collect((dydx, FakeAdapter(Venue.LIGHTER)))


@pytest.mark.parametrize(
    "mutation",
    ["wrong_symbol", "wrong_asset", "wrong_boundary", "wrong_interval", "duplicate"],
)
def test_malformed_funding_batch_fails_only_its_item(mutation: str) -> None:
    dydx = FakeAdapter(Venue.DYDX)
    original = dydx.funding_results[Asset.BTC]
    assert isinstance(original, AdapterBatch)
    row = original.normalized[0]
    assert isinstance(row, FundingObservation)
    updates: dict[str, object] = {}
    if mutation == "wrong_symbol":
        updates["symbol"] = "BTC"
    elif mutation == "wrong_asset":
        updates["asset"] = Asset.ETH
        updates["symbol"] = "ETH-USD"
    elif mutation == "wrong_boundary":
        updates["effective_at"] = CYCLE_END - timedelta(hours=1)
    elif mutation == "wrong_interval":
        updates["interval_hours"] = Decimal("8")
    changed = row.model_copy(update=updates)
    normalized = (changed, changed) if mutation == "duplicate" else (changed,)
    dydx.funding_results[Asset.BTC] = AdapterBatch(raw=original.raw, normalized=normalized)

    prepared = _collect((dydx, FakeAdapter(Venue.LIGHTER)))

    item = prepared.cycle.items[0]
    assert (item.venue, item.asset) == (Venue.DYDX, Asset.BTC)
    assert item.funding_outcome is TrialFundingOutcome.FAILED
    assert item.funding_source_hashes == ()
    assert len(prepared.funding) == 5


def test_missing_raw_lineage_rejects_successful_batch() -> None:
    lighter = FakeAdapter(Venue.LIGHTER)
    original = lighter.funding_results[Asset.SOL]
    assert isinstance(original, AdapterBatch)
    row = original.normalized[0]
    assert isinstance(row, FundingObservation)
    lighter.funding_results[Asset.SOL] = AdapterBatch(
        raw=original.raw,
        normalized=(row.model_copy(update={"source_hash": "f" * 64}),),
    )

    prepared = _collect((FakeAdapter(Venue.DYDX), lighter))

    item = next(
        item
        for item in prepared.cycle.items
        if item.venue is Venue.LIGHTER and item.asset is Asset.SOL
    )
    assert item.funding_outcome is TrialFundingOutcome.FAILED
    assert all(raw.event_id != original.raw[0].event_id for raw in prepared.raw)


def test_response_after_cutoff_is_preserved_and_marks_cycle_late() -> None:
    lighter = FakeAdapter(Venue.LIGHTER)
    late = CYCLE_END + timedelta(minutes=5, microseconds=1)
    lighter.funding_results[Asset.SOL] = _funding_batch(Venue.LIGHTER, Asset.SOL, late, 999)

    prepared = _collect(
        (FakeAdapter(Venue.DYDX), lighter),
        clock=SequenceClock(CYCLE_END + timedelta(seconds=10), late),
    )

    assert prepared.cycle.status is TrialFundingCycleStatus.LATE
    assert prepared.cycle.items[-1].funding_observed_at == late
    assert any(raw.event_id == UUID(int=999) for raw in prepared.raw)


def test_prepared_output_is_invariant_to_adapter_and_response_order() -> None:
    dydx = FakeAdapter(Venue.DYDX)
    lighter = FakeAdapter(Venue.LIGHTER)
    for adapter in (dydx, lighter):
        result = adapter.instrument_result
        assert isinstance(result, AdapterBatch)
        adapter.instrument_result = AdapterBatch(
            raw=tuple(reversed(result.raw)), normalized=tuple(reversed(result.normalized))
        )

    forward = _collect((lighter, dydx))
    reverse = _collect((dydx, lighter))

    assert forward == reverse


def test_prepared_cycle_persists_raw_normalized_and_cycle_atomically(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "trial.duckdb")
    prepared = complete_prepared_cycle()

    assert persist_lighter_dydx_funding_cycle(store, prepared) is True
    assert store.lighter_dydx_funding_cycles_between(
        CYCLE_END, CYCLE_END, prepared.cycle.request_completed_at
    ) == (prepared.cycle,)
    assert (
        len(
            store.funding_revisions_between(
                Venue.DYDX,
                "BTC-USD",
                CYCLE_END - timedelta(hours=1),
                CYCLE_END,
                prepared.cycle.request_completed_at,
            )
        )
        == 1
    )
    store.close()


def test_persistence_rolls_back_every_table_when_cycle_append_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rollback.duckdb"
    store = DuckDBStore(database)
    prepared = complete_prepared_cycle()

    def fail(_record: LighterDydxFundingCycle) -> bool:
        raise RuntimeError("cycle append failed")

    monkeypatch.setattr(store, "append_lighter_dydx_funding_cycle", fail)
    with pytest.raises(RuntimeError, match="cycle append failed"):
        persist_lighter_dydx_funding_cycle(store, prepared)
    store.close()

    with duckdb.connect(str(database), read_only=True) as connection:
        assert tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "raw_envelopes",
                "instrument_specs",
                "funding_observations",
                "lighter_dydx_funding_cycles",
            )
        ) == (0, 0, 0, 0)


def test_persistence_exact_retry_is_idempotent_and_conflict_rolls_back(tmp_path: Path) -> None:
    database = tmp_path / "idempotent.duckdb"
    store = DuckDBStore(database)
    prepared = complete_prepared_cycle()
    assert persist_lighter_dydx_funding_cycle(store, prepared) is True
    assert persist_lighter_dydx_funding_cycle(store, prepared) is False
    counts_before = store.evidence_counts_as_of(prepared.cycle.request_completed_at)
    conflict = PreparedLighterDydxFundingCycle(
        raw=prepared.raw,
        instruments=prepared.instruments,
        funding=prepared.funding,
        cycle=prepared.cycle.model_copy(update={"status": TrialFundingCycleStatus.DEGRADED}),
    )
    with pytest.raises((ConflictingRecordError, ValueError)):
        persist_lighter_dydx_funding_cycle(store, conflict)
    assert store.evidence_counts_as_of(prepared.cycle.request_completed_at) == counts_before
    store.close()


def test_persistence_rejects_prepared_evidence_not_conserved_by_cycle(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "invalid-prepared.duckdb")
    prepared = complete_prepared_cycle()
    invalid = PreparedLighterDydxFundingCycle(
        raw=prepared.raw[:-1],
        instruments=prepared.instruments,
        funding=prepared.funding,
        cycle=prepared.cycle,
    )

    with pytest.raises(ValueError, match="conserved"):
        persist_lighter_dydx_funding_cycle(store, invalid)
    assert (
        store.evidence_counts_as_of(prepared.cycle.request_completed_at)[
            "lighter_dydx_funding_cycles"
        ]
        == 0
    )
    store.close()


def test_persistence_rejects_conserved_asset_subset_before_transaction(tmp_path: Path) -> None:
    complete = complete_prepared_cycle()
    items = tuple(item for item in complete.cycle.items if item.asset is Asset.BTC)
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
    cycle = LighterDydxFundingCycle(
        schema_version=1,
        protocol_version=complete.cycle.protocol_version,
        cycle_id=complete.cycle.cycle_id,
        cycle_end=complete.cycle.cycle_end,
        assets=(Asset.BTC,),
        venues=complete.cycle.venues,
        request_started_at=complete.cycle.request_started_at,
        request_completed_at=complete.cycle.request_completed_at,
        items=items,
        status=complete.cycle.status,
        source_hashes=source_hashes,
        warnings=complete.cycle.warnings,
    )
    prepared = PreparedLighterDydxFundingCycle(
        raw=tuple(raw for raw in complete.raw if raw.source_hash in source_hashes),
        instruments=tuple(record for record in complete.instruments if record.asset is Asset.BTC),
        funding=tuple(record for record in complete.funding if record.asset is Asset.BTC),
        cycle=cycle,
    )
    store = DuckDBStore(tmp_path / "subset.duckdb")

    with pytest.raises(ValueError, match="BTC, ETH, and SOL"):
        persist_lighter_dydx_funding_cycle(store, prepared)
    assert (
        store.evidence_counts_as_of(cycle.request_completed_at)["lighter_dydx_funding_cycles"] == 0
    )
    store.close()

    claimed_full = PreparedLighterDydxFundingCycle(
        raw=prepared.raw,
        instruments=prepared.instruments,
        funding=prepared.funding,
        cycle=cycle.model_copy(
            update={"assets": tuple(sorted(Asset, key=lambda asset: asset.value))}
        ),
    )
    claimed_store = DuckDBStore(tmp_path / "claimed-full-subset.duckdb")
    with pytest.raises(ValueError, match="six venue-asset items"):
        persist_lighter_dydx_funding_cycle(claimed_store, claimed_full)
    claimed_store.close()


@pytest.mark.parametrize("record_type", ["instrument", "funding"])
def test_persistence_rejects_normalized_timestamp_not_conserved_by_raw_and_item(
    tmp_path: Path, record_type: str
) -> None:
    complete = complete_prepared_cycle()
    records = complete.instruments if record_type == "instrument" else complete.funding
    first = records[0]
    mutated = first.model_copy(update={"observed_at": first.observed_at + timedelta(seconds=1)})
    prepared = PreparedLighterDydxFundingCycle(
        raw=complete.raw,
        instruments=(mutated, *complete.instruments[1:])
        if record_type == "instrument"
        else complete.instruments,
        funding=(mutated, *complete.funding[1:]) if record_type == "funding" else complete.funding,
        cycle=complete.cycle,
    )
    database = tmp_path / f"{record_type}-timestamp-lineage.duckdb"
    store = DuckDBStore(database)

    with pytest.raises(ValueError, match="observation timestamp"):
        persist_lighter_dydx_funding_cycle(store, prepared)
    store.close()
    with duckdb.connect(str(database), read_only=True) as connection:
        assert tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "raw_envelopes",
                "instrument_specs",
                "funding_observations",
                "lighter_dydx_funding_cycles",
            )
        ) == (0, 0, 0, 0)


def test_persistence_rejects_timestamp_replay_conserved_by_raw_but_not_item(
    tmp_path: Path,
) -> None:
    complete = complete_prepared_cycle()
    first = complete.funding[0]
    replayed_at = first.observed_at + timedelta(seconds=1)
    replayed_raw = tuple(
        raw.model_copy(update={"observed_at": replayed_at})
        if raw.venue is first.venue
        and raw.source_hash == first.source_hash
        and raw.observed_at == first.observed_at
        else raw
        for raw in complete.raw
    )
    replayed_funding = first.model_copy(update={"observed_at": replayed_at})
    prepared = PreparedLighterDydxFundingCycle(
        raw=replayed_raw,
        instruments=complete.instruments,
        funding=(replayed_funding, *complete.funding[1:]),
        cycle=complete.cycle,
    )
    store = DuckDBStore(tmp_path / "item-timestamp-lineage.duckdb")

    with pytest.raises(ValueError, match="cycle item"):
        persist_lighter_dydx_funding_cycle(store, prepared)
    assert (
        store.evidence_counts_as_of(complete.cycle.request_completed_at)[
            "lighter_dydx_funding_cycles"
        ]
        == 0
    )
    store.close()


def test_persistence_rejects_missing_outcome_raw_hash_swapped_across_venues(
    tmp_path: Path,
) -> None:
    adapters = []
    for venue, event_int in ((Venue.DYDX, 810), (Venue.LIGHTER, 820)):
        adapter = FakeAdapter(venue)
        adapter.funding_results[Asset.BTC] = _funding_batch(
            venue,
            Asset.BTC,
            CYCLE_END + timedelta(seconds=12),
            event_int,
            include_record=False,
        )
        adapters.append(adapter)
    prepared = _collect(tuple(adapters))  # type: ignore[arg-type]
    items = list(prepared.cycle.items)
    dydx_hashes = items[0].funding_source_hashes
    lighter_hashes = items[3].funding_source_hashes
    items[0] = items[0].model_copy(update={"funding_source_hashes": lighter_hashes})
    items[3] = items[3].model_copy(update={"funding_source_hashes": dydx_hashes})
    invalid = PreparedLighterDydxFundingCycle(
        raw=prepared.raw,
        instruments=prepared.instruments,
        funding=prepared.funding,
        cycle=prepared.cycle.model_copy(update={"items": tuple(items)}),
    )
    store = DuckDBStore(tmp_path / "cross-venue.duckdb")

    with pytest.raises(ValueError, match="conserved"):
        persist_lighter_dydx_funding_cycle(store, invalid)
    store.close()
