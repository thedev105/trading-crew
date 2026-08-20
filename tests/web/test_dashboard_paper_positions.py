from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from polytrading.domain.models import Asset
from polytrading.storage.store import DuckDBStore
from polytrading.trial.paper_execution import funding_accrual_transaction
from polytrading.web.dashboard import DashboardBuilder
from tests.storage.test_store_paper_positions import _closure, _position


def test_dashboard_snapshot_includes_empty_paper_positions(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        snapshot = DashboardBuilder(store, tmp_path / "test.duckdb").build(
            datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
        )
        assert snapshot.paper_position_rows == ()
    finally:
        store.close()


def test_open_position_reports_accrued_funding_as_current_pnl_and_cumulative_points(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)

        first_accrual = funding_accrual_transaction(
            position=position,
            effective_at=position.opened_at + timedelta(hours=1),
            lighter_rate=Decimal("0.0001"),
            dydx_rate=Decimal("0.00005"),
        )
        second_accrual = funding_accrual_transaction(
            position=position,
            effective_at=position.opened_at + timedelta(hours=2),
            lighter_rate=Decimal("-0.0002"),
            dydx_rate=Decimal("0.0001"),
        )
        assert first_accrual is not None
        assert second_accrual is not None
        store.append_journal_transaction(first_accrual)
        store.append_journal_transaction(second_accrual)

        as_of = position.opened_at + timedelta(hours=3)
        snapshot = DashboardBuilder(store, tmp_path / "test.duckdb").build(as_of)

        assert len(snapshot.paper_position_rows) == 1
        row = snapshot.paper_position_rows[0]
        assert row.position_id == position.position_id
        assert row.status == "OPEN"
        assert row.closed_at is None
        expected_total = store.paper_position_realized_funding(position.position_id)
        assert row.current_pnl_usd == expected_total

        assert len(row.hourly_pnl_points) == 2
        first_point, second_point = row.hourly_pnl_points
        assert first_point[0] < second_point[0]
        # points must be a running cumulative sum, not per-hour deltas
        first_delta = next(
            posting.debit - posting.credit
            for posting in first_accrual.postings
            if posting.account == "paper:cash"
        )
        second_delta = next(
            posting.debit - posting.credit
            for posting in second_accrual.postings
            if posting.account == "paper:cash"
        )
        assert first_point[1] == first_delta
        assert second_point[1] == first_delta + second_delta
        assert second_point[1] == expected_total
    finally:
        store.close()


def test_closed_position_reports_stored_realized_pnl_without_recomputing(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)

        accrual = funding_accrual_transaction(
            position=position,
            effective_at=position.opened_at + timedelta(hours=1),
            lighter_rate=Decimal("0.0001"),
            dydx_rate=Decimal("0.00005"),
        )
        assert accrual is not None
        store.append_journal_transaction(accrual)

        closure = _closure(position.position_id)
        store.append_paper_position_closure(closure)

        # The closure's realized_pnl_usd deliberately does not equal the raw
        # funding sum (it also folds in trading P&L) — this proves the
        # dashboard uses the stored closure value directly rather than
        # recomputing it from postings.
        funding_total = store.paper_position_realized_funding(position.position_id)
        assert closure.realized_pnl_usd != funding_total

        as_of = closure.closed_at + timedelta(hours=1)
        snapshot = DashboardBuilder(store, tmp_path / "test.duckdb").build(as_of)

        assert len(snapshot.paper_position_rows) == 1
        row = snapshot.paper_position_rows[0]
        assert row.status == "CLOSED_MAX_HORIZON_REACHED"
        assert row.closed_at == closure.closed_at
        assert row.current_pnl_usd == closure.realized_pnl_usd
        assert len(row.hourly_pnl_points) == 1
    finally:
        store.close()


def test_sequential_positions_on_same_asset_do_not_leak_pnl_or_points(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position_a = _position()
        store.append_paper_position(position_a)
        accrual_a = funding_accrual_transaction(
            position=position_a,
            effective_at=position_a.opened_at + timedelta(hours=1),
            lighter_rate=Decimal("0.0001"),
            dydx_rate=Decimal("0.00005"),
        )
        assert accrual_a is not None
        store.append_journal_transaction(accrual_a)
        closure_a = _closure(
            position_a.position_id,
            closed_at=position_a.opened_at + timedelta(hours=2),
        )
        store.append_paper_position_closure(closure_a)

        position_b = _position(
            position_id=uuid4(),
            opened_at=position_a.opened_at + timedelta(days=1),
        )
        store.append_paper_position(position_b)

        as_of = position_b.opened_at + timedelta(hours=1)
        snapshot = DashboardBuilder(store, tmp_path / "test.duckdb").build(as_of)

        rows_by_id = {row.position_id: row for row in snapshot.paper_position_rows}
        assert set(rows_by_id) == {position_a.position_id, position_b.position_id}

        row_b = rows_by_id[position_b.position_id]
        assert row_b.status == "OPEN"
        assert row_b.current_pnl_usd == Decimal(0)
        assert row_b.hourly_pnl_points == ()

        row_a = rows_by_id[position_a.position_id]
        assert row_a.asset == Asset.BTC
        assert row_a.status == "CLOSED_MAX_HORIZON_REACHED"
    finally:
        store.close()
