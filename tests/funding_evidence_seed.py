from __future__ import annotations

import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from polytrading.domain.models import FundingObservation
from polytrading.storage.store import (
    ConflictingRecordError,
    DuckDBStore,
    _canonical_json,
    _record_hash,
)
from polytrading.trial.funding_models import LighterDydxFundingCycle

FundingIdentity = tuple[str, str, int, int]


def bulk_append_funding_evidence(
    store: DuckDBStore,
    observations: Iterable[FundingObservation],
    cycles: Iterable[LighterDydxFundingCycle],
    directory: Path,
) -> None:
    """COPY validated test funding models without changing append semantics."""
    unique_observations: dict[FundingIdentity, FundingObservation] = {}
    unique_cycles: dict[UUID, LighterDydxFundingCycle] = {}
    for observation in observations:
        identity = (
            observation.venue.value,
            observation.symbol,
            _epoch_us(observation.effective_at),
            _epoch_us(observation.observed_at),
        )
        _add_exact_record(unique_observations, identity, observation, "funding observation")
    for cycle in cycles:
        _add_exact_record(
            unique_cycles,
            cycle.cycle_id,
            cycle,
            "Lighter-dYdX funding cycle",
        )
    if not unique_observations and not unique_cycles:
        return

    existing_observations = {
        (row[0], row[1], row[2], row[3]): row[4]
        for row in store._connection.execute(
            """
            SELECT venue, symbol, epoch_us(effective_at), epoch_us(observed_at), record_hash
            FROM funding_observations
            """
        ).fetchall()
    }
    existing_cycles = dict(
        store._connection.execute(
            "SELECT cycle_id, record_hash FROM lighter_dydx_funding_cycles"
        ).fetchall()
    )
    for identity, observation in unique_observations.items():
        existing_hash = existing_observations.get(identity)
        if existing_hash is not None:
            _ensure_exact_record("funding observation", observation, existing_hash)
    for identity, cycle in unique_cycles.items():
        existing_hash = existing_cycles.get(identity)
        if existing_hash is not None:
            _ensure_exact_record("Lighter-dYdX funding cycle", cycle, existing_hash)

    observation_rows = [
        (
            observation.venue.value,
            observation.symbol,
            observation.asset.value,
            observation.rate,
            observation.interval_hours,
            observation.effective_at,
            observation.observed_at,
            observation.source_hash,
            observation.schema_version,
            _record_hash(observation),
        )
        for identity, observation in unique_observations.items()
        if identity not in existing_observations
    ]
    cycle_rows = [
        (
            cycle.cycle_id,
            cycle.cycle_end,
            cycle.request_completed_at,
            cycle.status.value,
            _canonical_json(cycle),
            _record_hash(cycle),
        )
        for identity, cycle in unique_cycles.items()
        if identity not in existing_cycles
    ]

    def insert_rows() -> None:
        with tempfile.TemporaryDirectory(dir=directory) as batch_directory:
            batch_path = Path(batch_directory)
            _copy_rows(store, "funding_observations", observation_rows, batch_path)
            _copy_rows(store, "lighter_dydx_funding_cycles", cycle_rows, batch_path)

    if store._in_transaction:
        insert_rows()
    else:
        with store.transaction():
            insert_rows()


def _add_exact_record[Identity, Record: BaseModel](
    records: dict[Identity, Record],
    identity: Identity,
    record: Record,
    label: str,
) -> None:
    existing = records.get(identity)
    if existing is None:
        records[identity] = record
        return
    _ensure_exact_record(label, record, _record_hash(existing))


def _ensure_exact_record(label: str, record: BaseModel, existing_hash: str) -> None:
    if _record_hash(record) != existing_hash:
        raise ConflictingRecordError(f"conflicting {label} for immutable identity")


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
            NEW_LINE '\\n',
            ALLOW_QUOTED_NULLS FALSE
        )
        """
    )


def _csv_field(value: object) -> str:
    if value is None:
        return "\\N"
    return f'"{str(value).replace(chr(34), chr(34) * 2)}"'
