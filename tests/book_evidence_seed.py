from __future__ import annotations

import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from polytrading.domain.models import Level2BookSnapshot
from polytrading.storage.store import (
    ConflictingRecordError,
    DuckDBStore,
    _canonical_json,
    _record_hash,
)
from polytrading.venues.synchronized import BookCollectionCycle


def bulk_append_book_evidence(
    store: DuckDBStore,
    evidence: Iterable[tuple[BookCollectionCycle, tuple[Level2BookSnapshot, ...]]],
    directory: Path,
) -> None:
    """COPY validated test book models without changing production append semantics."""
    cycles: dict[UUID, BookCollectionCycle] = {}
    snapshots: dict[tuple[UUID, str, str], Level2BookSnapshot] = {}
    for cycle, cycle_snapshots in evidence:
        _add_exact_record(
            cycles,
            cycle.cycle_id,
            cycle,
            "book collection cycle",
        )
        for snapshot in cycle_snapshots:
            _add_exact_record(
                snapshots,
                (snapshot.cycle_id, snapshot.venue.value, snapshot.symbol),
                snapshot,
                "book snapshot",
            )
    if not cycles and not snapshots:
        return

    existing_cycles = _existing_cycle_hashes(store, tuple(cycles))
    existing_snapshots = _existing_snapshot_hashes(
        store,
        tuple({key[0] for key in snapshots}),
    )
    existing_snapshot_levels = _existing_snapshot_level_rows(
        store,
        tuple({key[0] for key in snapshots}),
    )
    for identity, record in cycles.items():
        if existing_hash := existing_cycles.get(identity):
            _ensure_exact_record("book collection cycle", record, existing_hash)
    for identity, record in snapshots.items():
        if existing_hash := existing_snapshots.get(identity):
            _ensure_exact_record("book snapshot", record, existing_hash)
            if existing_snapshot_levels.get(identity, ()) != _snapshot_level_rows(record):
                raise ConflictingRecordError(
                    "conflicting book snapshot levels for immutable identity"
                )

    new_cycles = tuple(
        record for identity, record in cycles.items() if identity not in existing_cycles
    )
    new_snapshots = tuple(
        record for identity, record in snapshots.items() if identity not in existing_snapshots
    )
    cycle_rows: list[tuple[object, ...]] = []
    snapshot_rows: list[tuple[object, ...]] = []
    level_rows: list[tuple[object, ...]] = []
    for cycle in new_cycles:
        cycle_rows.append(
            (
                cycle.cycle_id,
                cycle.request_completed_at,
                cycle.status,
                _canonical_json(cycle),
                _record_hash(cycle),
            )
        )
    for snapshot in new_snapshots:
        record_hash = _record_hash(snapshot)
        snapshot_rows.append(
            (
                snapshot.cycle_id,
                snapshot.venue.value,
                snapshot.symbol,
                snapshot.asset.value,
                snapshot.depth_limit,
                snapshot.sequence,
                snapshot.effective_at,
                snapshot.observed_at,
                snapshot.source_hash,
                snapshot.schema_version,
                record_hash,
            )
        )
        for side, levels in (("bid", snapshot.bids), ("ask", snapshot.asks)):
            level_rows.extend(
                (
                    snapshot.cycle_id,
                    snapshot.venue.value,
                    snapshot.symbol,
                    snapshot.observed_at,
                    side,
                    level_index,
                    level.price,
                    level.quantity,
                    level.order_count,
                    record_hash,
                )
                for level_index, level in enumerate(levels)
            )

    if not cycle_rows and not snapshot_rows:
        return

    def insert_rows() -> None:
        with tempfile.TemporaryDirectory(dir=directory) as batch_directory:
            batch_path = Path(batch_directory)
            _copy_rows(store, "book_collection_cycles", cycle_rows, batch_path)
            _copy_rows(store, "book_snapshots", snapshot_rows, batch_path)
            _copy_rows(store, "book_levels", level_rows, batch_path)

    if store._in_transaction:
        insert_rows()
    else:
        with store.transaction():
            insert_rows()


def _add_exact_record(
    records: dict[object, object],
    identity: object,
    record: BookCollectionCycle | Level2BookSnapshot,
    label: str,
) -> None:
    existing = records.get(identity)
    if existing is None:
        records[identity] = record
        return
    _ensure_exact_record(label, record, _record_hash(existing))


def _ensure_exact_record(
    label: str,
    record: BookCollectionCycle | Level2BookSnapshot,
    existing_hash: str,
) -> None:
    if _record_hash(record) != existing_hash:
        raise ConflictingRecordError(f"conflicting {label} for immutable identity")


def _existing_cycle_hashes(
    store: DuckDBStore,
    cycle_ids: tuple[UUID, ...],
) -> dict[UUID, str]:
    if not cycle_ids:
        return {}
    rows = store._connection.execute(
        """
        SELECT cycle_id, record_hash FROM book_collection_cycles
        WHERE cycle_id IN (SELECT unnest(?::UUID[]))
        """,
        [list(cycle_ids)],
    ).fetchall()
    return dict(rows)


def _existing_snapshot_hashes(
    store: DuckDBStore,
    cycle_ids: tuple[UUID, ...],
) -> dict[tuple[UUID, str, str], str]:
    if not cycle_ids:
        return {}
    rows = store._connection.execute(
        """
        SELECT cycle_id, venue, symbol, record_hash FROM book_snapshots
        WHERE cycle_id IN (SELECT unnest(?::UUID[]))
        """,
        [list(cycle_ids)],
    ).fetchall()
    return {(row[0], row[1], row[2]): row[3] for row in rows}


def _existing_snapshot_level_rows(
    store: DuckDBStore,
    cycle_ids: tuple[UUID, ...],
) -> dict[tuple[UUID, str, str], tuple[tuple[object, ...], ...]]:
    if not cycle_ids:
        return {}
    rows = store._connection.execute(
        """
        SELECT cycle_id, venue, symbol, epoch_us(observed_at), side, level_index,
               price, quantity, order_count, record_hash
        FROM book_levels
        WHERE cycle_id IN (SELECT unnest(?::UUID[]))
        ORDER BY cycle_id, venue, symbol,
                 CASE side WHEN 'bid' THEN 0 ELSE 1 END, level_index
        """,
        [list(cycle_ids)],
    ).fetchall()
    grouped: dict[tuple[UUID, str, str], list[tuple[object, ...]]] = {}
    for row in rows:
        grouped.setdefault((row[0], row[1], row[2]), []).append(row[3:])
    return {identity: tuple(levels) for identity, levels in grouped.items()}


def _snapshot_level_rows(snapshot: Level2BookSnapshot) -> tuple[tuple[object, ...], ...]:
    record_hash = _record_hash(snapshot)
    observed_at_us = _epoch_us(snapshot.observed_at)
    return tuple(
        (
            observed_at_us,
            side,
            level_index,
            level.price,
            level.quantity,
            level.order_count,
            record_hash,
        )
        for side, levels in (("bid", snapshot.bids), ("ask", snapshot.asks))
        for level_index, level in enumerate(levels)
    )


def _epoch_us(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        delta.days * int(timedelta(days=1).total_seconds()) * 1_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _copy_rows(
    store: DuckDBStore,
    table: str,
    rows: list[tuple[object, ...]],
    directory: Path,
) -> None:
    if not rows:
        return
    path = directory / f"{table}.csv"
    with path.open("w", encoding="utf-8", newline="") as output:
        for row in rows:
            output.write(",".join(_csv_field(value) for value in row))
            output.write("\n")
    quoted_path = path.as_posix().replace("'", "''")
    store._connection.execute(
        f"""
        COPY {table} FROM '{quoted_path}' (
            FORMAT CSV,
            DELIMITER ',',
            QUOTE '"',
            ESCAPE '"',
            NULL '\\N',
            HEADER FALSE,
            ALLOW_QUOTED_NULLS FALSE
        )
        """
    )


def _csv_field(value: object) -> str:
    if value is None:
        return "\\N"
    return f'"{str(value).replace(chr(34), chr(34) * 2)}"'
