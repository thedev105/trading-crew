import shutil
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb
import pytest

from polytrading.carry.dossier_models import DossierStatus
from polytrading.carry.economics_assembler import EconomicsEvidenceAssembler
from polytrading.domain.models import Asset, FeeSchedule, FundingObservation, Venue
from polytrading.storage.store import DuckDBStore
from tests.carry.test_economics_models import KNOWN_AS_OF, STUDY_END, policy
from tests.domain.factories import (
    book_collection_cycle,
    book_snapshot,
    instrument_spec,
)

DYDX_FUNDING_HASH = "1" * 64
LIGHTER_FUNDING_HASH = "2" * 64
DYDX_BOOK_HASH = "3" * 64
LIGHTER_BOOK_HASH = "4" * 64
DYDX_INSTRUMENT_HASH = "5" * 64
LIGHTER_INSTRUMENT_HASH = "6" * 64


def seed_complete_database(path: Path) -> None:
    item = policy()
    store = DuckDBStore(path)
    training_start = item.study_end - timedelta(days=90)
    evaluation_start = item.study_end - timedelta(days=60)
    with store.transaction():
        store.append_instrument(
            instrument_spec(
                instrument_id="dydx:BTC-USD:linear_perpetual",
                venue=Venue.DYDX,
                symbol="BTC-USD",
                asset=Asset.BTC,
                contract_multiplier=Decimal("1"),
                index_family="BTC",
                oracle_family="BTC",
                collateral_asset="USDC",
                pnl_asset="USDC",
                funding_interval_hours=Decimal("1"),
                min_notional=Decimal("5"),
                quantity_step=Decimal("0.001"),
                is_inverse=False,
                is_prelaunch=False,
                observed_at=training_start,
                source_hash=DYDX_INSTRUMENT_HASH,
            )
        )
        store.append_instrument(
            instrument_spec(
                instrument_id="lighter:BTC:linear_perpetual",
                venue=Venue.LIGHTER,
                symbol="BTC",
                asset=Asset.BTC,
                contract_multiplier=Decimal("1"),
                index_family="BTC",
                oracle_family="BTC",
                collateral_asset="USDC",
                pnl_asset="USDC",
                funding_interval_hours=Decimal("1"),
                min_notional=Decimal("5"),
                quantity_step=Decimal("0.001"),
                is_inverse=False,
                is_prelaunch=False,
                observed_at=training_start,
                source_hash=LIGHTER_INSTRUMENT_HASH,
            )
        )
        store.append_fee_schedule(
            FeeSchedule(
                schema_version=1,
                venue=Venue.DYDX,
                tier_name="reviewed-tier",
                maker_rate=Decimal("0"),
                taker_rate=Decimal("0.0005"),
                effective_from=training_start,
                observed_at=STUDY_END,
                source_url="https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx",
                source_hash="f" * 64,
            )
        )
        store.append_fee_schedule(
            FeeSchedule(
                schema_version=1,
                venue=Venue.LIGHTER,
                tier_name="reviewed-tier",
                maker_rate=Decimal("0"),
                taker_rate=Decimal("0"),
                effective_from=training_start,
                observed_at=STUDY_END,
                source_url="https://docs.lighter.xyz/trading/trading-fees",
                source_hash="0" * 64,
            )
        )
        for hour in range(1, 90 * 24 + 1):
            effective_at = training_start + timedelta(hours=hour)
            observed_at = effective_at + timedelta(minutes=1)
            store.append_funding(
                FundingObservation(
                    schema_version=1,
                    venue=Venue.DYDX,
                    symbol="BTC-USD",
                    asset=Asset.BTC,
                    rate=Decimal("0.0001"),
                    interval_hours=Decimal("1"),
                    effective_at=effective_at,
                    observed_at=observed_at,
                    source_hash=DYDX_FUNDING_HASH,
                )
            )
            store.append_funding(
                FundingObservation(
                    schema_version=1,
                    venue=Venue.LIGHTER,
                    symbol="BTC",
                    asset=Asset.BTC,
                    rate=Decimal("0.0002"),
                    interval_hours=Decimal("1"),
                    effective_at=effective_at,
                    observed_at=observed_at,
                    source_hash=LIGHTER_FUNDING_HASH,
                )
            )
        for hour in range(1, 60 * 24 + 1):
            boundary = evaluation_start + timedelta(hours=hour)
            append_book_pair(store, 10_000 + hour, boundary - timedelta(minutes=1))
        append_book_pair(store, 20_001, KNOWN_AS_OF - timedelta(seconds=10))
        append_book_pair(store, 20_002, KNOWN_AS_OF - timedelta(seconds=5))
    store.close()


def append_book_pair(store: DuckDBStore, identity: int, completed_at) -> None:
    cycle_id = UUID(int=identity)
    dydx_effective = completed_at - timedelta(milliseconds=200)
    lighter_effective = completed_at - timedelta(milliseconds=100)
    cycle = book_collection_cycle(
        cycle_id=cycle_id,
        assets=(Asset.BTC,),
        venues=(Venue.DYDX, Venue.LIGHTER),
        request_started_at=completed_at - timedelta(seconds=1),
        request_completed_at=completed_at,
        effective_timestamps=(dydx_effective, lighter_effective),
        max_effective_skew_ms=Decimal("100"),
        source_hashes=(DYDX_BOOK_HASH, LIGHTER_BOOK_HASH),
    )
    store.append_book_collection_cycle(cycle)
    store.append_book_snapshot(
        book_snapshot(
            cycle_id=cycle_id,
            venue=Venue.DYDX,
            symbol="BTC-USD",
            asset=Asset.BTC,
            effective_at=dydx_effective,
            observed_at=completed_at,
            source_hash=DYDX_BOOK_HASH,
        )
    )
    store.append_book_snapshot(
        book_snapshot(
            cycle_id=cycle_id,
            venue=Venue.LIGHTER,
            symbol="BTC",
            asset=Asset.BTC,
            effective_at=lighter_effective,
            observed_at=completed_at,
            source_hash=LIGHTER_BOOK_HASH,
        )
    )


@pytest.fixture(scope="module")
def complete_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("economics-assembler") / "complete.duckdb"
    seed_complete_database(path)
    return path


def copied_database(tmp_path: Path, complete_database: Path) -> Path:
    path = tmp_path / "research.duckdb"
    shutil.copyfile(complete_database, path)
    return path


def test_complete_assembly_uses_exact_windows_tiers_books_and_lineage(
    tmp_path: Path, complete_database: Path
) -> None:
    path = copied_database(tmp_path, complete_database)
    store = DuckDBStore(path, read_only=True)

    result = EconomicsEvidenceAssembler(store).assemble(policy())

    assert result.reason_codes == ()
    assert result.bundle is not None
    assert result.bundle.training_start == STUDY_END - timedelta(days=90)
    assert result.bundle.training_end == STUDY_END - timedelta(days=60)
    assert result.bundle.evaluation_end == STUDY_END
    assert result.bundle.dossier.status is DossierStatus.MODEL_REQUIRED
    assert tuple(item.venue for item in result.bundle.instruments) == (
        Venue.DYDX,
        Venue.LIGHTER,
    )
    assert tuple(item.venue for item in result.bundle.fees) == (Venue.DYDX, Venue.LIGHTER)
    assert len(result.bundle.funding_pairs) == 2160
    assert len(result.bundle.hourly_books) == 1440
    assert len(result.bundle.dense_books) == 1442
    evaluation_start = STUDY_END - timedelta(days=60)
    assert result.bundle.hourly_books[0].effective_at == evaluation_start + timedelta(hours=1)
    assert result.bundle.hourly_books[0].lighter.effective_at == (
        evaluation_start + timedelta(hours=1, minutes=-1, milliseconds=-100)
    )
    assert result.bundle.latest_books.effective_at == KNOWN_AS_OF - timedelta(
        seconds=5, milliseconds=100
    )
    assert result.coverage.training_funding_coverage == 1
    assert result.coverage.evaluation_funding_coverage == 1
    assert result.coverage.book_coverage == 1
    assert result.coverage.latency_sample_count == 1
    assert result.source_hashes == tuple(sorted(set(result.source_hashes)))
    assert result.source_hashes == result.bundle.source_hashes
    store.close()


def test_training_coverage_passes_above_99_percent_and_fails_below_it(
    tmp_path: Path, complete_database: Path
) -> None:
    path = copied_database(tmp_path, complete_database)
    training_start = STUDY_END - timedelta(days=90)

    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """
            DELETE FROM funding_observations
            WHERE venue = 'lighter' AND effective_at > ? AND effective_at <= ?
            """,
            [training_start, training_start + timedelta(hours=7)],
        )
    store = DuckDBStore(path, read_only=True)
    above = EconomicsEvidenceAssembler(store).assemble(policy())
    assert above.bundle is not None
    assert above.coverage.paired_training_hours == 713
    store.close()

    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "DELETE FROM funding_observations WHERE venue = 'lighter' AND effective_at = ?",
            [training_start + timedelta(hours=8)],
        )
    store = DuckDBStore(path, read_only=True)
    below = EconomicsEvidenceAssembler(store).assemble(policy())
    assert below.bundle is None
    assert "FUNDING_TRAINING_COVERAGE_INSUFFICIENT" in below.reason_codes
    assert below.coverage.paired_training_hours == 712
    store.close()


def test_missing_fee_returns_no_partial_bundle_and_future_records_do_not_leak(
    tmp_path: Path, complete_database: Path
) -> None:
    path = copied_database(tmp_path, complete_database)
    store = DuckDBStore(path)
    before = EconomicsEvidenceAssembler(store).assemble(policy())
    store.append_fee_schedule(
        FeeSchedule(
            schema_version=1,
            venue=Venue.DYDX,
            tier_name="reviewed-tier",
            maker_rate=Decimal("0"),
            taker_rate=Decimal("0.9"),
            effective_from=KNOWN_AS_OF + timedelta(microseconds=1),
            observed_at=KNOWN_AS_OF + timedelta(microseconds=1),
            source_url="https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx",
            source_hash="9" * 64,
        )
    )
    after = EconomicsEvidenceAssembler(store).assemble(policy())
    assert after == before
    store.close()

    with duckdb.connect(str(path)) as connection:
        connection.execute("DELETE FROM fee_schedules WHERE venue = 'lighter'")
    store = DuckDBStore(path, read_only=True)
    missing = EconomicsEvidenceAssembler(store).assemble(policy())
    assert missing.bundle is None
    assert missing.reason_codes == ("FEE_LIGHTER_MISSING",)
    assert LIGHTER_BOOK_HASH in missing.source_hashes
    store.close()


def test_missing_dense_samples_and_stale_latest_book_fail_closed(
    tmp_path: Path, complete_database: Path
) -> None:
    path = copied_database(tmp_path, complete_database)
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "DELETE FROM book_levels WHERE cycle_id IN (?, ?)",
            [UUID(int=20_001), UUID(int=20_002)],
        )
        connection.execute(
            "DELETE FROM book_snapshots WHERE cycle_id IN (?, ?)",
            [UUID(int=20_001), UUID(int=20_002)],
        )
        connection.execute(
            "DELETE FROM book_collection_cycles WHERE cycle_id IN (?, ?)",
            [UUID(int=20_001), UUID(int=20_002)],
        )
    store = DuckDBStore(path, read_only=True)

    result = EconomicsEvidenceAssembler(store).assemble(policy())

    assert result.bundle is None
    assert "BOOK_LATEST_STALE" in result.reason_codes
    assert "LATENCY_SAMPLES_MISSING" in result.reason_codes
    store.close()


def test_invalid_instrument_funding_and_hourly_book_evidence_accumulate_reasons(
    tmp_path: Path, complete_database: Path
) -> None:
    path = copied_database(tmp_path, complete_database)
    evaluation_start = STUDY_END - timedelta(days=60)
    with duckdb.connect(str(path)) as connection:
        connection.execute("DELETE FROM instrument_specs WHERE venue = 'dydx'")
        connection.execute(
            """
            UPDATE funding_observations SET interval_hours = 8
            WHERE venue = 'lighter' AND effective_at = ?
            """,
            [evaluation_start + timedelta(hours=1)],
        )
        connection.execute(
            """
            CREATE TEMP TABLE removed_cycles AS
            SELECT cycle_id FROM book_collection_cycles
            ORDER BY request_completed_at LIMIT 15
            """
        )
        connection.execute(
            "DELETE FROM book_levels WHERE cycle_id IN (SELECT cycle_id FROM removed_cycles)"
        )
        connection.execute(
            "DELETE FROM book_snapshots WHERE cycle_id IN (SELECT cycle_id FROM removed_cycles)"
        )
        connection.execute(
            "DELETE FROM book_collection_cycles "
            "WHERE cycle_id IN (SELECT cycle_id FROM removed_cycles)"
        )
    store = DuckDBStore(path, read_only=True)

    result = EconomicsEvidenceAssembler(store).assemble(policy())

    assert result.bundle is None
    assert "INSTRUMENT_DYDX_MISSING" in result.reason_codes
    assert "FUNDING_INTERVAL_OR_IDENTITY_INVALID" in result.reason_codes
    assert "BOOK_COVERAGE_INSUFFICIENT" in result.reason_codes
    assert result.coverage.paired_book_hours == 1425
    store.close()
