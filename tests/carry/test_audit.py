from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.carry.audit import AuditStatus, CarryAuditor
from polytrading.domain.models import Asset, BookLevel, Venue
from polytrading.storage.store import DuckDBStore
from tests.domain.factories import (
    book_collection_cycle,
    book_snapshot,
    funding_observation,
    instrument_spec,
)

AS_OF = datetime(2026, 8, 12, 12, tzinfo=UTC)
OLD_HASH = "1" * 64
FUTURE_HASH = "f" * 64
CYCLE_ID = UUID("00000000-0000-0000-0000-000000000911")
FUTURE_CYCLE_ID = UUID("00000000-0000-0000-0000-000000000912")
VENUES = (Venue.BYBIT, Venue.HYPERLIQUID)
ASSETS = (Asset.BTC, Asset.ETH, Asset.SOL)


def _symbol(venue: Venue, asset: Asset) -> str:
    return f"{asset.value}USDT" if venue is Venue.BYBIT else asset.value


def _compatible_instrument(venue: Venue, asset: Asset, **overrides: object):
    symbol = _symbol(venue, asset)
    values: dict[str, object] = {
        "instrument_id": f"{venue.value}:{symbol}:linear_perpetual",
        "venue": venue,
        "symbol": symbol,
        "asset": asset,
        "index_family": asset.value,
        "oracle_family": asset.value,
        "mark_method": "shared-mark-v1",
        "liquidation_method": "isolated-linear-v1",
        "collateral_asset": "USDT",
        "pnl_asset": "USDT",
        "funding_formula_id": "shared-funding-v1",
        "funding_cap": Decimal("0.003"),
        "funding_interval_hours": Decimal("1"),
        "observed_at": AS_OF - timedelta(minutes=10),
        "source_hash": OLD_HASH,
    }
    values.update(overrides)
    return instrument_spec(**values)


def _funding(venue: Venue, asset: Asset, **overrides: object):
    values: dict[str, object] = {
        "venue": venue,
        "symbol": _symbol(venue, asset),
        "asset": asset,
        "rate": Decimal("0.0001") if venue is Venue.BYBIT else Decimal("0.0002"),
        "interval_hours": Decimal("1"),
        "effective_at": AS_OF - timedelta(hours=1),
        "observed_at": AS_OF - timedelta(minutes=5),
        "source_hash": OLD_HASH,
    }
    values.update(overrides)
    return funding_observation(**values)


def _book(
    venue: Venue,
    asset: Asset,
    cycle_id: UUID = CYCLE_ID,
    effective_at: datetime = AS_OF - timedelta(seconds=3),
):
    base = {
        Asset.BTC: Decimal("65000"),
        Asset.ETH: Decimal("3500"),
        Asset.SOL: Decimal("150"),
    }[asset]
    return book_snapshot(
        cycle_id=cycle_id,
        venue=venue,
        symbol=_symbol(venue, asset),
        asset=asset,
        bids=(
            BookLevel(price=base - 1, quantity=Decimal("2"), order_count=1),
            BookLevel(price=base - 2, quantity=Decimal("3"), order_count=1),
        ),
        asks=(
            BookLevel(price=base + 1, quantity=Decimal("4"), order_count=1),
            BookLevel(price=base + 2, quantity=Decimal("5"), order_count=1),
        ),
        effective_at=effective_at,
        observed_at=AS_OF - timedelta(seconds=2),
        source_hash=OLD_HASH,
    )


def _auditor(store: DuckDBStore, **overrides: timedelta) -> CarryAuditor:
    values = {
        "max_instrument_age": timedelta(hours=1),
        "max_funding_age": timedelta(hours=2),
        "max_book_age": timedelta(seconds=10),
        "max_book_cycle_skew": timedelta(milliseconds=500),
    }
    values.update(overrides)
    return CarryAuditor(store, **values)


def _seed_current_funding(store: DuckDBStore) -> None:
    for asset in ASSETS:
        for venue in VENUES:
            store.append_instrument(_compatible_instrument(venue, asset))
            store.append_instrument(
                _compatible_instrument(
                    venue,
                    asset,
                    observed_at=AS_OF + timedelta(minutes=1),
                    price_tick=Decimal("99"),
                    source_hash=FUTURE_HASH,
                )
            )
            store.append_funding(_funding(venue, asset))
            store.append_funding(
                _funding(
                    venue,
                    asset,
                    rate=Decimal("0.9"),
                    effective_at=AS_OF - timedelta(minutes=30),
                    observed_at=AS_OF + timedelta(minutes=1),
                    source_hash=FUTURE_HASH,
                )
            )


def _seed_complete_books(store: DuckDBStore) -> None:
    books = tuple(_book(venue, asset) for asset in ASSETS for venue in VENUES)
    for book in books:
        store.append_book_snapshot(book)
    store.append_book_collection_cycle(
        book_collection_cycle(
            cycle_id=CYCLE_ID,
            request_started_at=AS_OF - timedelta(seconds=4),
            request_completed_at=AS_OF - timedelta(seconds=1),
            effective_timestamps=tuple(book.effective_at for book in books),
            max_effective_skew_ms=Decimal("0"),
            source_hashes=(OLD_HASH,),
        )
    )


def test_audit_is_point_in_time_stable_and_has_no_trade_surface(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "audit.duckdb")
    _seed_current_funding(store)
    _seed_complete_books(store)

    report = _auditor(store).audit(AS_OF)

    assert tuple(row.asset for row in report.assets) == ASSETS
    assert len(report.assets) == 3
    assert all(row.status is AuditStatus.DIAGNOSTIC_ONLY for row in report.assets)
    assert all(row.funding_ready and row.book_ready for row in report.assets)
    assert all(row.diagnostic is not None for row in report.assets)
    assert all(row.diagnostic.short_hourly_rate == Decimal("0.0002") for row in report.assets)
    assert all(
        evidence.instrument_source_hash == OLD_HASH and evidence.funding_source_hash == OLD_HASH
        for row in report.assets
        for evidence in row.funding_evidence
    )
    dumped = report.model_dump(mode="json")
    forbidden = {
        "proposal",
        "order",
        "quantity",
        "leverage",
        "allocation",
        "expected_profit",
        "recommendation",
        "size",
    }
    assert forbidden.isdisjoint(_all_keys(dumped))
    store.close()


def test_latest_complete_pre_cutoff_cycle_provides_depth_evidence(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "audit.duckdb")
    _seed_current_funding(store)
    _seed_complete_books(store)
    future_books = tuple(
        _book(
            venue,
            asset,
            cycle_id=FUTURE_CYCLE_ID,
            effective_at=AS_OF + timedelta(seconds=2),
        )
        for asset in ASSETS
        for venue in VENUES
    )
    for book in future_books:
        store.append_book_snapshot(
            book.model_copy(update={"observed_at": AS_OF + timedelta(seconds=2)})
        )
    store.append_book_collection_cycle(
        book_collection_cycle(
            cycle_id=FUTURE_CYCLE_ID,
            request_started_at=AS_OF + timedelta(seconds=1),
            request_completed_at=AS_OF + timedelta(seconds=3),
            effective_timestamps=tuple(book.effective_at for book in future_books),
            source_hashes=(FUTURE_HASH,),
        )
    )

    btc = _auditor(store).audit(AS_OF).assets[0]

    assert btc.book_cycle_id == CYCLE_ID
    assert btc.book_cycle_skew_ms == Decimal("0")
    assert tuple(item.venue for item in btc.book_evidence) == VENUES
    assert all(item.book_age_ms == Decimal("3000") for item in btc.book_evidence)
    assert all(item.top_level_spread == Decimal("2") for item in btc.book_evidence)
    assert all(item.common_depth_levels == 2 for item in btc.book_evidence)
    assert all(item.cumulative_bid_notional == Decimal("324992") for item in btc.book_evidence)
    assert all(item.cumulative_ask_notional == Decimal("585014") for item in btc.book_evidence)
    assert "proposed_quantity" not in _all_keys(btc.model_dump(mode="json"))
    store.close()


def test_later_failed_cycle_does_not_hide_prior_complete_book_evidence(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "audit.duckdb")
    _seed_current_funding(store)
    _seed_complete_books(store)
    store.append_book_collection_cycle(
        book_collection_cycle(
            cycle_id=FUTURE_CYCLE_ID,
            request_started_at=AS_OF - timedelta(milliseconds=500),
            request_completed_at=AS_OF - timedelta(milliseconds=100),
            effective_timestamps=(),
            status="failed",
            failure_codes=("hyperliquid:TimeoutError",),
            source_hashes=(),
        )
    )

    btc = _auditor(store).audit(AS_OF).assets[0]

    assert btc.book_ready is True
    assert btc.book_cycle_id == CYCLE_ID
    store.close()


@pytest.mark.parametrize(
    ("mutation", "expected_status", "expected_reason"),
    [
        ("compatibility", AuditStatus.INELIGIBLE, "collateral_mismatch"),
        ("missing", AuditStatus.INSUFFICIENT_DATA, "FUNDING_MISSING:hyperliquid"),
        ("stale", AuditStatus.STALE, "FUNDING_STALE:bybit"),
        ("missing_books", AuditStatus.INSUFFICIENT_DATA, "BOOK_EVIDENCE_MISSING"),
        ("stale_books", AuditStatus.STALE, "BOOK_EVIDENCE_STALE"),
        ("skewed_books", AuditStatus.INSUFFICIENT_DATA, "BOOK_CYCLE_SKEW_EXCEEDED"),
    ],
)
def test_statuses_fail_closed_without_hiding_funding(
    tmp_path: Path,
    mutation: str,
    expected_status: AuditStatus,
    expected_reason: str,
) -> None:
    store = DuckDBStore(tmp_path / "status.duckdb")
    for venue in VENUES:
        if mutation != "missing" or venue is Venue.BYBIT:
            instrument = _compatible_instrument(venue, Asset.BTC)
            if mutation == "compatibility" and venue is Venue.HYPERLIQUID:
                instrument = instrument.model_copy(update={"collateral_asset": "USDC"})
            store.append_instrument(instrument)
            store.append_funding(
                _funding(
                    venue,
                    Asset.BTC,
                    observed_at=AS_OF - timedelta(hours=3)
                    if mutation == "stale" and venue is Venue.BYBIT
                    else AS_OF - timedelta(minutes=5),
                )
            )
    if mutation != "missing_books":
        books = tuple(
            _book(
                venue,
                Asset.BTC,
                effective_at=AS_OF - timedelta(seconds=30)
                if mutation == "stale_books"
                else AS_OF - timedelta(milliseconds=400 if venue is Venue.BYBIT else 0),
            )
            for venue in VENUES
        )
        for book in books:
            store.append_book_snapshot(book)
        store.append_book_collection_cycle(
            book_collection_cycle(
                cycle_id=CYCLE_ID,
                assets=(Asset.BTC,),
                request_started_at=AS_OF - timedelta(seconds=2),
                request_completed_at=AS_OF - timedelta(seconds=1),
                effective_timestamps=tuple(book.effective_at for book in books),
                max_effective_skew_ms=Decimal("400")
                if mutation == "skewed_books"
                else Decimal("0"),
                source_hashes=(OLD_HASH,),
            )
        )

    btc = (
        _auditor(
            store,
            max_book_cycle_skew=timedelta(milliseconds=100)
            if mutation == "skewed_books"
            else timedelta(milliseconds=500),
        )
        .audit(AS_OF)
        .assets[0]
    )

    assert btc.status is expected_status
    assert expected_reason in btc.reason_codes
    if mutation in {"missing_books", "stale_books", "skewed_books"}:
        assert btc.funding_ready is True
        assert btc.diagnostic is not None
        assert btc.book_ready is False
    store.close()


def test_store_point_in_time_readers_exclude_late_known_and_future_effective_data(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "cutoff.duckdb")
    current = _funding(Venue.BYBIT, Asset.BTC)
    late_known = _funding(
        Venue.BYBIT,
        Asset.BTC,
        rate=Decimal("0.9"),
        effective_at=AS_OF - timedelta(minutes=1),
        observed_at=AS_OF + timedelta(seconds=1),
        source_hash=FUTURE_HASH,
    )
    store.append_funding(current)
    store.append_funding(late_known)
    future_effective_book = _book(Venue.BYBIT, Asset.BTC, effective_at=AS_OF + timedelta(seconds=1))
    store.append_book_snapshot(future_effective_book)

    assert store.latest_funding_as_of(Venue.BYBIT, "BTCUSDT", AS_OF) == current
    assert store.latest_book_as_of(Venue.BYBIT, "BTCUSDT", AS_OF) is None
    store.close()


@pytest.mark.parametrize("cycle_case", ["failed", "partial"])
def test_failed_or_partial_cycles_never_become_executable_book_evidence(
    tmp_path: Path, cycle_case: str
) -> None:
    store = DuckDBStore(tmp_path / "cycle.duckdb")
    for venue in VENUES:
        store.append_instrument(_compatible_instrument(venue, Asset.BTC))
        store.append_funding(_funding(venue, Asset.BTC))
    if cycle_case == "partial":
        store.append_book_snapshot(_book(Venue.BYBIT, Asset.BTC))
    store.append_book_collection_cycle(
        book_collection_cycle(
            cycle_id=CYCLE_ID,
            assets=(Asset.BTC,),
            request_started_at=AS_OF - timedelta(seconds=2),
            request_completed_at=AS_OF - timedelta(seconds=1),
            effective_timestamps=(AS_OF - timedelta(seconds=1),),
            status="failed" if cycle_case == "failed" else "complete",
            failure_codes=("hyperliquid:TimeoutError",) if cycle_case == "failed" else (),
        )
    )

    btc = _auditor(store).audit(AS_OF).assets[0]

    assert btc.book_ready is False
    assert btc.book_evidence == ()
    assert "BOOK_EVIDENCE_MISSING" in btc.reason_codes
    store.close()


def test_crossed_book_is_rejected_before_it_can_become_audit_evidence() -> None:
    with pytest.raises(ValidationError, match="must not cross"):
        book_snapshot(
            bids=(BookLevel(price=Decimal("102"), quantity=Decimal("1"), order_count=1),),
            asks=(BookLevel(price=Decimal("101"), quantity=Decimal("1"), order_count=1),),
        )


def test_stale_instrument_and_missing_funding_are_both_reported(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "combined.duckdb")
    store.append_instrument(
        _compatible_instrument(Venue.BYBIT, Asset.BTC, observed_at=AS_OF - timedelta(hours=2))
    )
    store.append_instrument(_compatible_instrument(Venue.HYPERLIQUID, Asset.BTC))
    store.append_funding(_funding(Venue.HYPERLIQUID, Asset.BTC))

    btc = _auditor(store).audit(AS_OF).assets[0]

    assert btc.status is AuditStatus.STALE
    assert "INSTRUMENT_STALE:bybit" in btc.reason_codes
    assert "FUNDING_MISSING:bybit" in btc.reason_codes
    bybit = btc.funding_evidence[0]
    assert bybit.instrument_source_hash == OLD_HASH
    assert bybit.funding_source_hash is None
    assert bybit.hourly_rate is None
    store.close()


def test_incompatible_instruments_remain_ineligible_when_funding_is_missing(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "combined.duckdb")
    store.append_instrument(_compatible_instrument(Venue.BYBIT, Asset.BTC))
    store.append_instrument(
        _compatible_instrument(
            Venue.HYPERLIQUID,
            Asset.BTC,
            collateral_asset="USDC",
            pnl_asset="USDC",
        )
    )
    store.append_funding(_funding(Venue.BYBIT, Asset.BTC))

    btc = _auditor(store).audit(AS_OF).assets[0]

    assert btc.status is AuditStatus.INELIGIBLE
    assert "collateral_mismatch" in btc.reason_codes
    assert "pnl_asset_mismatch" in btc.reason_codes
    assert "FUNDING_MISSING:hyperliquid" in btc.reason_codes
    assert btc.diagnostic is None
    store.close()


def test_present_funding_stays_visible_when_its_instrument_is_missing(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "combined.duckdb")
    store.append_instrument(_compatible_instrument(Venue.BYBIT, Asset.BTC))
    store.append_funding(_funding(Venue.BYBIT, Asset.BTC))
    hyperliquid_funding = _funding(Venue.HYPERLIQUID, Asset.BTC)
    store.append_funding(hyperliquid_funding)

    btc = _auditor(store).audit(AS_OF).assets[0]

    assert btc.status is AuditStatus.INSUFFICIENT_DATA
    hyperliquid = btc.funding_evidence[1]
    assert hyperliquid.instrument_source_hash is None
    assert hyperliquid.instrument_observed_at is None
    assert hyperliquid.funding_source_hash == hyperliquid_funding.source_hash
    assert hyperliquid.funding_effective_at == hyperliquid_funding.effective_at
    assert hyperliquid.funding_observed_at == hyperliquid_funding.observed_at
    assert hyperliquid.hourly_rate == hyperliquid_funding.hourly_rate
    store.close()


def test_high_skew_and_stale_books_report_all_reasons_and_safe_depth(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "combined.duckdb")
    for venue in VENUES:
        store.append_instrument(_compatible_instrument(venue, Asset.BTC))
        store.append_funding(_funding(venue, Asset.BTC))
    books = tuple(
        _book(
            venue,
            Asset.BTC,
            effective_at=AS_OF
            - timedelta(seconds=30, milliseconds=400 if venue is Venue.BYBIT else 0),
        )
        for venue in VENUES
    )
    for book in books:
        store.append_book_snapshot(book)
    store.append_book_collection_cycle(
        book_collection_cycle(
            cycle_id=CYCLE_ID,
            assets=(Asset.BTC,),
            request_started_at=AS_OF - timedelta(seconds=31),
            request_completed_at=AS_OF - timedelta(seconds=29),
            effective_timestamps=tuple(book.effective_at for book in books),
            max_effective_skew_ms=Decimal("400"),
            source_hashes=(OLD_HASH,),
        )
    )

    btc = (
        _auditor(
            store,
            max_book_cycle_skew=timedelta(milliseconds=100),
        )
        .audit(AS_OF)
        .assets[0]
    )

    assert btc.status is AuditStatus.STALE
    assert btc.reason_codes[-2:] == (
        "BOOK_CYCLE_SKEW_EXCEEDED",
        "BOOK_EVIDENCE_STALE",
    )
    assert btc.book_ready is False
    assert len(btc.book_evidence) == 2
    assert all(item.common_depth_levels == 2 for item in btc.book_evidence)
    store.close()


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(map(_all_keys, value.values())), set())
    if isinstance(value, list):
        return set().union(*(map(_all_keys, value)), set())
    return set()
