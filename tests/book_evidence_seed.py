from __future__ import annotations

import csv
import tempfile
from collections.abc import Iterable
from pathlib import Path

from polytrading.domain.models import Level2BookSnapshot
from polytrading.storage.store import DuckDBStore, _canonical_json, _record_hash
from polytrading.venues.synchronized import BookCollectionCycle


def bulk_append_book_evidence(
    store: DuckDBStore,
    evidence: Iterable[tuple[BookCollectionCycle, tuple[Level2BookSnapshot, ...]]],
    directory: Path,
) -> None:
    """COPY validated test book models without changing production append semantics."""
    cycle_rows: list[tuple[object, ...]] = []
    snapshot_rows: list[tuple[object, ...]] = []
    level_rows: list[tuple[object, ...]] = []
    for cycle, snapshots in evidence:
        cycle_rows.append(
            (
                cycle.cycle_id,
                cycle.request_completed_at,
                cycle.status,
                _canonical_json(cycle),
                _record_hash(cycle),
            )
        )
        for snapshot in snapshots:
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

    with tempfile.TemporaryDirectory(dir=directory) as batch_directory:
        batch_path = Path(batch_directory)
        _copy_rows(store, "book_collection_cycles", cycle_rows, batch_path)
        _copy_rows(store, "book_snapshots", snapshot_rows, batch_path)
        _copy_rows(store, "book_levels", level_rows, batch_path)


def _copy_rows(
    store: DuckDBStore,
    table: str,
    rows: list[tuple[object, ...]],
    directory: Path,
) -> None:
    path = directory / f"{table}.csv"
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerows(tuple("\\N" if value is None else value for value in row) for row in rows)
    quoted_path = path.as_posix().replace("'", "''")
    store._connection.execute(f"COPY {table} FROM '{quoted_path}' (FORMAT CSV, NULL '\\N')")
