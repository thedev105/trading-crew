import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from polytrading.carry.audit import AuditStatus
from polytrading.domain.models import Asset, BookLevel, Venue
from polytrading.storage.store import DuckDBStore
from polytrading.web.dashboard import DashboardBuilder, render_dashboard_json
from tests.domain.factories import (
    book_collection_cycle,
    book_snapshot,
    funding_observation,
    instrument_spec,
)

AS_OF = datetime(2026, 8, 13, 12, 6, tzinfo=UTC)
SOURCE_HASH = "a" * 64
FUTURE_HASH = "f" * 64
BOOK_CYCLE_ID = UUID("00000000-0000-0000-0000-000000000a01")


def test_empty_store_snapshot_fails_closed_without_invented_values(tmp_path: Path) -> None:
    path = tmp_path / "empty.duckdb"
    store = DuckDBStore(path)

    snapshot = DashboardBuilder(store, path).build(AS_OF)

    assert snapshot.as_of == AS_OF
    assert snapshot.database_name == "empty.duckdb"
    assert snapshot.funding_health.requested_hours == 24
    assert snapshot.funding_health.missing_boundary_count == 24
    assert snapshot.latest_funding_cycle is None
    assert snapshot.latest_book_cycle is None
    assert len(snapshot.markets) == 9
    assert all(
        row.instrument_observed_at is None and row.funding_rate is None and row.best_bid is None
        for row in snapshot.markets
    )
    assert tuple(row.status for row in snapshot.carry_rows) == (
        AuditStatus.INSUFFICIENT_DATA,
        AuditStatus.INSUFFICIENT_DATA,
        AuditStatus.INSUFFICIENT_DATA,
    )
    assert set(snapshot.evidence_counts.model_dump().values()) == {0}
    store.close()


def test_builder_selects_latest_pre_cutoff_dydx_evidence_and_preserves_native_rate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.duckdb"
    store = DuckDBStore(path)
    instrument = instrument_spec(
        instrument_id="dydx:BTC-USD:linear_perpetual",
        venue=Venue.DYDX,
        symbol="BTC-USD",
        asset=Asset.BTC,
        collateral_asset="USDC",
        pnl_asset="USDC",
        funding_interval_hours=Decimal("1"),
        observed_at=AS_OF - timedelta(minutes=10),
    )
    future_instrument = instrument.model_copy(
        update={"observed_at": AS_OF + timedelta(minutes=1), "source_hash": FUTURE_HASH}
    )
    funding = funding_observation(
        venue=Venue.DYDX,
        symbol="BTC-USD",
        asset=Asset.BTC,
        rate=Decimal("0.0002"),
        interval_hours=Decimal("1"),
        effective_at=AS_OF - timedelta(hours=1),
        observed_at=AS_OF - timedelta(minutes=5),
    )
    future_funding = funding.model_copy(
        update={
            "rate": Decimal("0.9"),
            "effective_at": AS_OF - timedelta(minutes=30),
            "observed_at": AS_OF + timedelta(minutes=1),
            "source_hash": FUTURE_HASH,
        }
    )
    book = book_snapshot(
        cycle_id=BOOK_CYCLE_ID,
        venue=Venue.DYDX,
        symbol="BTC-USD",
        asset=Asset.BTC,
        bids=(BookLevel(price=Decimal("100"), quantity=Decimal("2"), order_count=None),),
        asks=(BookLevel(price=Decimal("101"), quantity=Decimal("3"), order_count=None),),
        effective_at=AS_OF - timedelta(seconds=2),
        observed_at=AS_OF - timedelta(seconds=1),
    )
    cycle = book_collection_cycle(
        cycle_id=BOOK_CYCLE_ID,
        assets=(Asset.BTC,),
        venues=(Venue.DYDX,),
        request_started_at=AS_OF - timedelta(seconds=3),
        request_completed_at=AS_OF - timedelta(seconds=1),
        effective_timestamps=(book.effective_at,),
    )
    for record in (instrument, future_instrument):
        store.append_instrument(record)
    for record in (funding, future_funding):
        store.append_funding(record)
    store.append_book_snapshot(book)
    store.append_book_collection_cycle(cycle)

    snapshot = DashboardBuilder(store, path).build(AS_OF)
    row = next(
        item for item in snapshot.markets if item.venue is Venue.DYDX and item.asset is Asset.BTC
    )

    assert row.symbol == "BTC-USD"
    assert row.instrument_observed_at == instrument.observed_at
    assert row.funding_rate == Decimal("0.0002")
    assert row.funding_interval_hours == Decimal("1")
    assert row.best_bid == Decimal("100")
    assert row.best_ask == Decimal("101")
    assert row.spread_bps == Decimal("99.50248756218905472636815920")
    assert snapshot.latest_book_cycle is not None
    assert snapshot.latest_book_cycle.cycle_id == BOOK_CYCLE_ID
    assert snapshot.evidence_counts.instrument_specs == 1
    assert snapshot.evidence_counts.funding_observations == 1
    store.close()


def test_recipes_shell_quote_database_path_and_json_uses_public_scalars(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "actual.duckdb")
    display_path = Path("/tmp/research data/owner's.duckdb")
    snapshot = DashboardBuilder(store, display_path).build(AS_OF)

    quoted = "'/tmp/research data/owner'\"'\"'s.duckdb'"
    assert quoted in snapshot.operation_recipes.collect_public
    assert quoted in snapshot.operation_recipes.collect_books_once
    document = json.loads(render_dashboard_json(snapshot))
    assert document["as_of"] == "2026-08-13T12:06:00Z"
    assert document["funding_health"]["complete_coverage"] == "0"
    assert document["markets"][0]["venue"] == "bybit"
    assert document["markets"][0]["asset"] == "BTC"
    store.close()
