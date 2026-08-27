from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from polytrading.domain.models import Asset, FundingObservation, Venue
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from tests.funding_evidence_seed import bulk_append_funding_evidence
from tests.trial.funding_helpers import trial_funding_cycle

BOUNDARY = datetime(2026, 1, 1, tzinfo=UTC)


def funding_pair(boundary: datetime = BOUNDARY) -> tuple[FundingObservation, ...]:
    observed_at = boundary + timedelta(minutes=1)
    return (
        FundingObservation(
            schema_version=1,
            venue=Venue.DYDX,
            symbol="BTC-USD",
            asset=Asset.BTC,
            rate=Decimal("0.0001"),
            interval_hours=Decimal("1"),
            effective_at=boundary,
            observed_at=observed_at,
            source_hash="1" * 64,
        ),
        FundingObservation(
            schema_version=1,
            venue=Venue.LIGHTER,
            symbol="BTC",
            asset=Asset.BTC,
            rate=Decimal("0.0002"),
            interval_hours=Decimal("1"),
            effective_at=boundary,
            observed_at=observed_at,
            source_hash="2" * 64,
        ),
    )


def test_bulk_funding_evidence_matches_rowwise_storage(tmp_path: Path) -> None:
    observations = funding_pair()
    cycle = trial_funding_cycle(cycle_id=UUID(int=1), cycle_end=BOUNDARY)
    rowwise = DuckDBStore(tmp_path / "rowwise.duckdb")
    with rowwise.transaction():
        for observation in observations:
            rowwise.append_funding(observation)
        rowwise.append_lighter_dydx_funding_cycle(cycle)
    bulk = DuckDBStore(tmp_path / "bulk.duckdb")

    bulk_append_funding_evidence(bulk, observations, (cycle,), tmp_path)

    for query in (
        """
        SELECT venue, symbol, asset, rate, interval_hours,
               epoch_us(effective_at), epoch_us(observed_at), source_hash,
               schema_version, record_hash
        FROM funding_observations ORDER BY venue, symbol
        """,
        """
        SELECT cycle_id, epoch_us(cycle_end), epoch_us(request_completed_at),
               status, CAST(record_json AS VARCHAR), record_hash
        FROM lighter_dydx_funding_cycles ORDER BY cycle_id
        """,
    ):
        assert bulk._connection.execute(query).fetchall() == (
            rowwise._connection.execute(query).fetchall()
        )
    bulk.close()
    rowwise.close()


def test_bulk_funding_evidence_is_idempotent(tmp_path: Path) -> None:
    observation = funding_pair()[0]
    cycle = trial_funding_cycle(cycle_id=UUID(int=2), cycle_end=BOUNDARY)
    store = DuckDBStore(tmp_path / "evidence.duckdb")

    bulk_append_funding_evidence(
        store,
        (observation, observation),
        (cycle, cycle),
        tmp_path,
    )
    bulk_append_funding_evidence(store, (observation,), (cycle,), tmp_path)

    assert store._connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (1,)
    assert store._connection.execute(
        "SELECT count(*) FROM lighter_dydx_funding_cycles"
    ).fetchone() == (1,)
    store.close()


def test_bulk_funding_evidence_conflicts_fail_before_writes(tmp_path: Path) -> None:
    original = funding_pair()[0]
    conflicting = original.model_copy(update={"rate": Decimal("0.0003")})
    untouched = funding_pair(BOUNDARY + timedelta(hours=1))[0]
    store = DuckDBStore(tmp_path / "evidence.duckdb")

    with pytest.raises(ConflictingRecordError, match="funding observation"):
        bulk_append_funding_evidence(
            store,
            (untouched, original, conflicting),
            (),
            tmp_path,
        )

    assert store._connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (0,)
    store.close()


def test_bulk_funding_evidence_rejects_existing_cycle_conflict_before_writes(
    tmp_path: Path,
) -> None:
    original = trial_funding_cycle(cycle_id=UUID(int=3), cycle_end=BOUNDARY)
    conflicting = original.model_copy(
        update={"request_completed_at": original.request_completed_at + timedelta(seconds=1)}
    )
    untouched = funding_pair(BOUNDARY + timedelta(hours=1))[0]
    store = DuckDBStore(tmp_path / "evidence.duckdb")
    store.append_lighter_dydx_funding_cycle(original)

    with pytest.raises(ConflictingRecordError, match="Lighter-dYdX funding cycle"):
        bulk_append_funding_evidence(store, (untouched,), (conflicting,), tmp_path)

    assert store._connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (0,)
    assert store.lighter_dydx_funding_cycles_between(
        original.cycle_end,
        original.cycle_end,
        original.request_completed_at,
    ) == (original,)
    store.close()
