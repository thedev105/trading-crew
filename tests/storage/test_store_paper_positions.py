from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from polytrading.carry.economics_models import FundingDirection
from polytrading.domain.models import Asset
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from polytrading.trial.paper_models import (
    PAPER_RESEARCH_WARNING,
    PaperCloseReason,
    PaperPosition,
    PaperPositionClosure,
)


def _position(**overrides: object) -> PaperPosition:
    fields = {
        "schema_version": 1,
        "position_id": uuid4(),
        "source_evaluation_id": uuid4(),
        "asset": Asset.BTC,
        "direction": FundingDirection.SHORT_LIGHTER_LONG_DYDX,
        "opened_at": datetime(2026, 8, 17, 12, 0, tzinfo=UTC),
        "base_quantity": Decimal("0.5"),
        "lighter_entry_notional_usd": Decimal("30000"),
        "dydx_entry_notional_usd": Decimal("30010"),
        "lighter_entry_price": Decimal("60000"),
        "dydx_entry_price": Decimal("60020"),
        "opening_book_cycle_id": uuid4(),
        "warning": PAPER_RESEARCH_WARNING,
    }
    fields.update(overrides)
    return PaperPosition(**fields)


def _closure(position_id, **overrides: object) -> PaperPositionClosure:
    fields = {
        "schema_version": 1,
        "position_id": position_id,
        "closed_at": datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        "close_reason": PaperCloseReason.MAX_HORIZON_REACHED,
        "lighter_exit_notional_usd": Decimal("29500"),
        "dydx_exit_notional_usd": Decimal("29600"),
        "lighter_exit_price": Decimal("59000"),
        "dydx_exit_price": Decimal("59200"),
        "closing_book_cycle_id": uuid4(),
        "realized_funding_usd": Decimal("120.50"),
        "realized_pnl_usd": Decimal("-45.25"),
    }
    fields.update(overrides)
    return PaperPositionClosure(**fields)


def test_append_and_read_open_paper_position(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        assert store.append_paper_position(position) is True
        assert store.append_paper_position(position) is False  # idempotent retry
        assert store.open_paper_position_for_asset(Asset.BTC) == position
        assert store.open_paper_position_for_asset(Asset.ETH) is None
        assert store.paper_position(position.position_id) == position
    finally:
        store.close()


def test_append_paper_position_conflict_raises(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)
        conflicting = _position(
            position_id=position.position_id, base_quantity=Decimal("0.6")
        )
        with pytest.raises(ConflictingRecordError):
            store.append_paper_position(conflicting)
    finally:
        store.close()


def test_closing_a_position_removes_it_from_open_lookup(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "test.duckdb")
    try:
        position = _position()
        store.append_paper_position(position)
        closure = _closure(position.position_id)
        assert store.append_paper_position_closure(closure) is True
        assert store.open_paper_position_for_asset(Asset.BTC) is None
        assert store.paper_position_closure(position.position_id) == closure
    finally:
        store.close()
