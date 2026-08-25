from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from polytrading.domain.models import BookLevel, Venue
from polytrading.storage.store import (
    ConflictingRecordError,
    DuckDBStore,
    _canonical_json,
    _record_hash,
)
from tests.book_evidence_seed import bulk_append_book_evidence
from tests.domain.factories import book_collection_cycle, book_snapshot


def test_bulk_copy_preserves_literal_null_marker_and_real_nulls(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "evidence.duckdb")
    cycle = book_collection_cycle(
        cycle_id=UUID(int=1),
        status="failed",
        failure_codes=("\\N",),
    )
    literal = book_snapshot(
        cycle_id=cycle.cycle_id,
        symbol="\\N",
        sequence="\\N",
        bids=(BookLevel(price=Decimal("100"), quantity=Decimal("2"), order_count=None),),
        asks=(BookLevel(price=Decimal("101"), quantity=Decimal("3"), order_count=1),),
    )
    nullable = book_snapshot(
        cycle_id=cycle.cycle_id,
        venue=Venue.HYPERLIQUID,
        symbol="nullable",
        sequence=None,
        bids=(BookLevel(price=Decimal("99"), quantity=Decimal("4"), order_count=2),),
        asks=(BookLevel(price=Decimal("102"), quantity=Decimal("5"), order_count=None),),
    )

    bulk_append_book_evidence(store, ((cycle, (literal, nullable)),), tmp_path)

    assert store._connection.execute(
        "SELECT CAST(record_json AS VARCHAR), record_hash FROM book_collection_cycles"
    ).fetchone() == (_canonical_json(cycle), _record_hash(cycle))
    assert store._connection.execute(
        """
        SELECT symbol, sequence, record_hash
        FROM book_snapshots ORDER BY venue, symbol
        """
    ).fetchall() == [
        ("\\N", "\\N", _record_hash(literal)),
        ("nullable", None, _record_hash(nullable)),
    ]
    assert store._connection.execute(
        """
        SELECT symbol, side, level_index, price, quantity, order_count, record_hash
        FROM book_levels ORDER BY symbol, side, level_index
        """
    ).fetchall() == [
        ("\\N", "ask", 0, Decimal("101"), Decimal("3"), 1, _record_hash(literal)),
        ("\\N", "bid", 0, Decimal("100"), Decimal("2"), None, _record_hash(literal)),
        ("nullable", "ask", 0, Decimal("102"), Decimal("5"), None, _record_hash(nullable)),
        ("nullable", "bid", 0, Decimal("99"), Decimal("4"), 2, _record_hash(nullable)),
    ]
    store.close()


def test_bulk_copy_empty_is_a_noop_and_cycle_only_evidence_is_valid(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "evidence.duckdb")
    cycle = book_collection_cycle(cycle_id=UUID(int=2))

    bulk_append_book_evidence(store, (), tmp_path)
    bulk_append_book_evidence(store, ((cycle, ()),), tmp_path)

    assert store._connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (
        1,
    )
    assert store._connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (0,)
    assert store._connection.execute("SELECT count(*) FROM book_levels").fetchone() == (0,)
    store.close()


def test_bulk_copy_exact_duplicates_are_idempotent_within_and_across_calls(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "evidence.duckdb")
    cycle = book_collection_cycle(cycle_id=UUID(int=3))
    snapshot = book_snapshot(cycle_id=cycle.cycle_id)
    evidence = (cycle, (snapshot,))

    bulk_append_book_evidence(store, (evidence, evidence), tmp_path)
    bulk_append_book_evidence(store, (evidence,), tmp_path)

    assert store._connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (
        1,
    )
    assert store._connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (1,)
    assert store._connection.execute("SELECT count(*) FROM book_levels").fetchone() == (4,)
    store.close()


def test_bulk_copy_conflicts_fail_before_writes_and_preserve_existing_rows(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "evidence.duckdb")
    untouched = book_collection_cycle(cycle_id=UUID(int=4))
    original = book_collection_cycle(cycle_id=UUID(int=5))
    conflicting_cycle = original.model_copy(update={"status": "failed", "failure_codes": ("X",)})

    with pytest.raises(ConflictingRecordError, match="book collection cycle"):
        bulk_append_book_evidence(
            store,
            ((untouched, ()), (original, ()), (conflicting_cycle, ())),
            tmp_path,
        )
    assert store._connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (
        0,
    )

    snapshot = book_snapshot(cycle_id=original.cycle_id)
    bulk_append_book_evidence(store, ((original, (snapshot,)),), tmp_path)
    conflicting_snapshot = snapshot.model_copy(update={"sequence": "different"})
    with pytest.raises(ConflictingRecordError, match="book snapshot"):
        bulk_append_book_evidence(store, ((original, (conflicting_snapshot,)),), tmp_path)

    assert store.books_for_cycle(original.cycle_id) == (snapshot,)
    assert store._connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (
        1,
    )
    assert store._connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (1,)
    assert store._connection.execute("SELECT count(*) FROM book_levels").fetchone() == (4,)
    store.close()


@pytest.mark.parametrize(
    "statements",
    (
        ("DELETE FROM book_levels WHERE cycle_id = ? AND side = 'ask'",),
        ("DELETE FROM book_levels WHERE cycle_id = ? AND side = 'bid' AND level_index = 0",),
        (
            "UPDATE book_levels SET quantity = 999 "
            "WHERE cycle_id = ? AND side = 'bid' AND level_index = 0",
        ),
        (
            "UPDATE book_levels SET price = 1 "
            "WHERE cycle_id = ? AND side = 'bid' AND level_index = 0",
        ),
        (
            "UPDATE book_levels SET order_count = 999 "
            "WHERE cycle_id = ? AND side = 'bid' AND level_index = 0",
        ),
        (
            "UPDATE book_levels SET side = 'ask', level_index = 99 "
            "WHERE cycle_id = ? AND side = 'bid' AND level_index = 0",
        ),
        (
            "UPDATE book_levels SET level_index = 99 "
            "WHERE cycle_id = ? AND side = 'bid' AND level_index = 0",
        ),
        (
            "UPDATE book_levels SET record_hash = 'ffffffffffffffffffffffffffffffff"
            "ffffffffffffffffffffffffffffffff' "
            "WHERE cycle_id = ? AND side = 'bid' AND level_index = 0",
        ),
        (
            "UPDATE book_levels SET observed_at = observed_at + INTERVAL 1 MICROSECOND "
            "WHERE cycle_id = ? AND side = 'bid' AND level_index = 0",
        ),
        (
            """
            INSERT INTO book_levels
            SELECT cycle_id, venue, symbol, observed_at, side, 99,
                   price, quantity, order_count, record_hash
            FROM book_levels
            WHERE cycle_id = ? AND side = 'bid' AND level_index = 0
            """,
        ),
        (
            """
            UPDATE book_levels
            SET price = CASE level_index WHEN 0 THEN 64999.9 ELSE 65000.123456789123456 END
            WHERE cycle_id = ? AND side = 'bid'
            """,
        ),
    ),
    ids=(
        "missing-asks",
        "missing-bid",
        "quantity",
        "price",
        "order-count",
        "side",
        "index",
        "hash",
        "observed-at",
        "extra-duplicate-content",
        "reordered-prices",
    ),
)
def test_existing_snapshot_retry_rejects_any_child_level_divergence_before_writes(
    tmp_path: Path,
    statements: tuple[str, ...],
) -> None:
    store = DuckDBStore(tmp_path / "evidence.duckdb")
    existing_cycle = book_collection_cycle(cycle_id=UUID(int=10))
    existing_snapshot = book_snapshot(cycle_id=existing_cycle.cycle_id)
    bulk_append_book_evidence(store, ((existing_cycle, (existing_snapshot,)),), tmp_path)
    for statement in statements:
        store._connection.execute(statement, [existing_cycle.cycle_id])

    new_cycle = book_collection_cycle(cycle_id=UUID(int=11))
    new_snapshot = book_snapshot(cycle_id=new_cycle.cycle_id)
    with pytest.raises(ConflictingRecordError, match="book snapshot levels"):
        bulk_append_book_evidence(
            store,
            (
                (new_cycle, (new_snapshot,)),
                (existing_cycle, (existing_snapshot,)),
            ),
            tmp_path,
        )

    assert store._connection.execute(
        "SELECT count(*) FROM book_collection_cycles WHERE cycle_id = ?",
        [new_cycle.cycle_id],
    ).fetchone() == (0,)
    assert store._connection.execute(
        "SELECT count(*) FROM book_snapshots WHERE cycle_id = ?",
        [new_cycle.cycle_id],
    ).fetchone() == (0,)
    store.close()


def test_bulk_copy_out_of_range_first_level_raises_and_rolls_back_all_rows(
    tmp_path: Path,
) -> None:
    rowwise = DuckDBStore(tmp_path / "rowwise.duckdb")
    cycle = book_collection_cycle(cycle_id=UUID(int=20))
    invalid = book_snapshot(
        cycle_id=cycle.cycle_id,
        bids=(
            BookLevel(
                price=Decimal("100"),
                quantity=Decimal("2"),
                order_count=2**63,
            ),
        ),
        asks=(BookLevel(price=Decimal("101"), quantity=Decimal("3"), order_count=1),),
    )
    with pytest.raises(duckdb.ConversionException):
        rowwise.append_book_snapshot(invalid)
    assert rowwise._connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (0,)
    assert rowwise._connection.execute("SELECT count(*) FROM book_levels").fetchone() == (0,)
    rowwise.close()

    bulk = DuckDBStore(tmp_path / "bulk.duckdb")
    valid_cycle = book_collection_cycle(cycle_id=UUID(int=21))
    valid = book_snapshot(cycle_id=valid_cycle.cycle_id)
    with pytest.raises(duckdb.ConversionException):
        bulk_append_book_evidence(
            bulk,
            ((cycle, (invalid,)), (valid_cycle, (valid,))),
            tmp_path,
        )

    assert bulk._connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (
        0,
    )
    assert bulk._connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (0,)
    assert bulk._connection.execute("SELECT count(*) FROM book_levels").fetchone() == (0,)
    bulk.close()
