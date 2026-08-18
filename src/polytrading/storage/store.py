from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import duckdb
from pydantic import BaseModel

from polytrading.ai.models import ModelCard, RelationshipCandidateArtifact, RuleExtractionArtifact
from polytrading.domain.models import (
    Asset,
    BookLevel,
    FeeSchedule,
    FundingObservation,
    InstrumentKind,
    InstrumentSpec,
    Level2BookSnapshot,
    MarketSnapshot,
    RawEnvelope,
    Venue,
    normalize_utc_timestamp,
)
from polytrading.ledger.models import JournalTransaction, TrialBalanceRow
from polytrading.research.models import ExperimentRecord
from polytrading.trial.funding_models import LighterDydxFundingCycle
from polytrading.venues.funding_cycle_models import FundingCollectionCycle
from polytrading.venues.synchronized import BookCollectionCycle

if TYPE_CHECKING:
    from polytrading.carry.economics_models import (
        CandidateEconomicsReport,
        LegacyEconomicEvaluationSummary,
    )
    from polytrading.trial.paper_models import PaperPosition, PaperPositionClosure

_MIGRATION_NAME = re.compile(r"(?P<version>[0-9]{3})_[a-z0-9_]+\.sql")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ConflictingRecordError(ValueError):
    """Raised when an immutable identity is retried with different content."""


@dataclass(frozen=True)
class BookSnapshotHeader:
    """Level-free book identity and timing metadata for read-only evidence audits."""

    cycle_id: UUID
    venue: Venue
    symbol: str
    asset: Asset
    effective_at: datetime
    observed_at: datetime
    source_hash: str


def _canonical_json(record: BaseModel) -> str:
    return json.dumps(
        record.model_dump(mode="json", exclude_computed_fields=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _record_hash(record: BaseModel) -> str:
    return sha256(_canonical_json(record).encode()).hexdigest()


def _utc_from_epoch_us(value: int | None) -> datetime | None:
    if value is None:
        return None
    return _UNIX_EPOCH + timedelta(microseconds=value)


class DuckDBStore:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self._connection = duckdb.connect(str(path), read_only=read_only)
        self._in_transaction = False
        try:
            if read_only:
                self._verify_current_schema()
            else:
                self._apply_migrations()
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[DuckDBStore]:
        if self._in_transaction:
            raise RuntimeError("nested transactions are not supported")
        self._connection.execute("BEGIN TRANSACTION")
        self._in_transaction = True
        try:
            yield self
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")
        finally:
            self._in_transaction = False

    def append_raw(self, record: RawEnvelope) -> bool:
        existing = self._connection.execute(
            """
            SELECT event_id, venue, endpoint, epoch_us(venue_timestamp), epoch_us(observed_at),
                   received_monotonic_ns, request_latency_ms, source_version,
                   payload_json, source_hash, schema_version
            FROM raw_envelopes
            WHERE event_id = ?
            """,
            [record.event_id],
        ).fetchone()
        if existing is not None:
            stored = RawEnvelope(
                event_id=existing[0],
                venue=Venue(existing[1]),
                endpoint=existing[2],
                venue_timestamp=_utc_from_epoch_us(existing[3]),
                observed_at=_utc_from_epoch_us(existing[4]),
                received_monotonic_ns=existing[5],
                request_latency_ms=existing[6],
                source_version=existing[7],
                payload_json=existing[8],
                source_hash=existing[9],
                schema_version=existing[10],
            )
            if stored != record:
                raise ConflictingRecordError("conflicting raw envelope for immutable identity")
            return False

        self._connection.execute(
            """
            INSERT INTO raw_envelopes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                record.event_id,
                record.venue.value,
                record.endpoint,
                record.venue_timestamp,
                record.observed_at,
                record.received_monotonic_ns,
                record.request_latency_ms,
                record.source_version,
                record.payload_json,
                record.source_hash,
                record.schema_version,
            ],
        )
        return True

    def append_instrument(self, record: InstrumentSpec) -> bool:
        if self._normalized_retry(
            "instrument spec",
            record,
            "instrument_specs",
            "venue = ? AND symbol = ? AND observed_at = ?",
            [record.venue.value, record.symbol, record.observed_at],
        ):
            return False
        if self._normalized_retry(
            "instrument spec",
            record,
            "instrument_specs",
            "instrument_id = ? AND observed_at = ?",
            [record.instrument_id, record.observed_at],
        ):
            return False
        self._connection.execute(
            """
            INSERT INTO instrument_specs VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                record.instrument_id,
                record.venue.value,
                record.symbol,
                record.asset.value,
                record.kind.value,
                record.contract_multiplier,
                record.index_family,
                record.oracle_family,
                record.mark_method,
                record.liquidation_method,
                record.collateral_asset,
                record.pnl_asset,
                record.funding_formula_id,
                record.funding_cap,
                record.funding_interval_hours,
                record.funding_payment_offset_minutes,
                record.min_notional,
                record.quantity_step,
                record.price_tick,
                record.is_inverse,
                record.is_prelaunch,
                record.observed_at,
                record.source_hash,
                record.schema_version,
                _record_hash(record),
            ],
        )
        return True

    def append_funding(self, record: FundingObservation) -> bool:
        if self._normalized_retry(
            "funding observation",
            record,
            "funding_observations",
            "venue = ? AND symbol = ? AND effective_at = ? AND observed_at = ?",
            [record.venue.value, record.symbol, record.effective_at, record.observed_at],
        ):
            return False
        self._connection.execute(
            "INSERT INTO funding_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.venue.value,
                record.symbol,
                record.asset.value,
                record.rate,
                record.interval_hours,
                record.effective_at,
                record.observed_at,
                record.source_hash,
                record.schema_version,
                _record_hash(record),
            ],
        )
        return True

    def append_market_snapshot(self, record: MarketSnapshot) -> bool:
        if self._normalized_retry(
            "market snapshot",
            record,
            "market_snapshots",
            "venue = ? AND symbol = ? AND effective_at = ? AND observed_at = ?",
            [record.venue.value, record.symbol, record.effective_at, record.observed_at],
        ):
            return False
        self._connection.execute(
            "INSERT INTO market_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.venue.value,
                record.symbol,
                record.asset.value,
                record.bid,
                record.ask,
                record.mark,
                record.index,
                record.open_interest,
                record.effective_at,
                record.observed_at,
                record.source_hash,
                record.schema_version,
                _record_hash(record),
            ],
        )
        return True

    def append_book_snapshot(self, record: Level2BookSnapshot) -> bool:
        if self._in_transaction:
            return self._append_book_snapshot(record)
        with self.transaction():
            return self._append_book_snapshot(record)

    def append_book_collection_cycle(self, record: BookCollectionCycle) -> bool:
        if self._in_transaction:
            return self._append_book_collection_cycle(record)
        with self.transaction():
            return self._append_book_collection_cycle(record)

    def append_funding_collection_cycle(self, record: FundingCollectionCycle) -> bool:
        if self._in_transaction:
            return self._append_funding_collection_cycle(record)
        with self.transaction():
            return self._append_funding_collection_cycle(record)

    def append_lighter_dydx_funding_cycle(self, record: LighterDydxFundingCycle) -> bool:
        if self._in_transaction:
            return self._append_lighter_dydx_funding_cycle(record)
        with self.transaction():
            return self._append_lighter_dydx_funding_cycle(record)

    def append_fee_schedule(self, record: FeeSchedule) -> bool:
        if self._normalized_retry(
            "fee schedule",
            record,
            "fee_schedules",
            "venue = ? AND tier_name = ? AND effective_from = ? AND observed_at = ?",
            [record.venue.value, record.tier_name, record.effective_from, record.observed_at],
        ):
            return False
        self._connection.execute(
            "INSERT INTO fee_schedules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.venue.value,
                record.tier_name,
                record.maker_rate,
                record.taker_rate,
                record.effective_from,
                record.observed_at,
                record.source_url,
                record.source_hash,
                record.schema_version,
                _record_hash(record),
            ],
        )
        return True

    def append_economic_evaluation(self, record: CandidateEconomicsReport) -> bool:
        if self._in_transaction:
            return self._append_economic_evaluation(record)
        with self.transaction():
            return self._append_economic_evaluation(record)

    def append_paper_position(self, record: PaperPosition) -> bool:
        if self._in_transaction:
            return self._append_paper_position(record)
        with self.transaction():
            return self._append_paper_position(record)

    def append_paper_position_closure(self, record: PaperPositionClosure) -> bool:
        if self._in_transaction:
            return self._append_paper_position_closure(record)
        with self.transaction():
            return self._append_paper_position_closure(record)

    def open_paper_position_for_asset(self, asset: Asset) -> PaperPosition | None:
        row = self._connection.execute(
            """
            SELECT CAST(position.record_json AS VARCHAR)
            FROM paper_positions AS position
            LEFT JOIN paper_position_closures AS closure
              ON closure.position_id = position.position_id
            WHERE position.asset = ? AND closure.position_id IS NULL
            """,
            [asset.value],
        ).fetchone()
        if row is None:
            return None
        from polytrading.trial.paper_models import PaperPosition

        return PaperPosition.model_validate_json(row[0])

    def paper_position(self, position_id: UUID) -> PaperPosition | None:
        row = self._connection.execute(
            "SELECT CAST(record_json AS VARCHAR) FROM paper_positions WHERE position_id = ?",
            [position_id],
        ).fetchone()
        if row is None:
            return None
        from polytrading.trial.paper_models import PaperPosition

        return PaperPosition.model_validate_json(row[0])

    def paper_position_closure(self, position_id: UUID) -> PaperPositionClosure | None:
        row = self._connection.execute(
            """
            SELECT CAST(record_json AS VARCHAR) FROM paper_position_closures
            WHERE position_id = ?
            """,
            [position_id],
        ).fetchone()
        if row is None:
            return None
        from polytrading.trial.paper_models import PaperPositionClosure

        return PaperPositionClosure.model_validate_json(row[0])

    def append_experiment(self, record: ExperimentRecord) -> bool:
        if self._normalized_retry(
            "experiment",
            record,
            "experiments",
            "experiment_id = ?",
            [record.experiment_id],
        ):
            return False
        self._connection.execute(
            "INSERT INTO experiments VALUES (?, ?::JSON, ?)",
            [record.experiment_id, _canonical_json(record), _record_hash(record)],
        )
        return True

    def append_model_card(self, record: ModelCard) -> bool:
        record_json = _canonical_json(record)
        existing = self._connection.execute(
            """
            SELECT record_json FROM model_cards
            WHERE model_id = ? AND version = ?
            """,
            [record.model_id, record.version],
        ).fetchone()
        if existing is not None:
            if existing[0] != record_json:
                raise ConflictingRecordError("conflicting model card for immutable identity")
            return False
        self._connection.execute(
            "INSERT INTO model_cards VALUES (?, ?, ?, ?)",
            [record.model_id, record.version, record_json, _record_hash(record)],
        )
        return True

    def get_model_card(self, model_id: str, version: str) -> ModelCard | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM model_cards
            WHERE model_id = ? AND version = ?
            """,
            [model_id, version],
        ).fetchone()
        if row is None:
            return None
        return ModelCard.model_validate_json(row[0])

    def append_ai_artifact(
        self, record: RuleExtractionArtifact | RelationshipCandidateArtifact
    ) -> bool:
        record_json = _canonical_json(record)
        existing = self._connection.execute(
            "SELECT record_json FROM ai_artifacts WHERE artifact_id = ?", [record.artifact_id]
        ).fetchone()
        if existing is not None:
            if existing[0] != record_json:
                raise ConflictingRecordError("conflicting AI artifact for immutable identity")
            return False
        self._connection.execute(
            "INSERT INTO ai_artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.artifact_id,
                type(record).__name__,
                record.model_id,
                record.model_version,
                record.created_at,
                record.expires_at,
                record_json,
                _record_hash(record),
            ],
        )
        return True

    def get_experiment(self, experiment_id: UUID) -> ExperimentRecord | None:
        row = self._connection.execute(
            "SELECT CAST(record_json AS VARCHAR) FROM experiments WHERE experiment_id = ?",
            [experiment_id],
        ).fetchone()
        if row is None:
            return None
        return ExperimentRecord.model_validate_json(row[0])

    def append_journal_transaction(self, record: JournalTransaction) -> bool:
        if self._in_transaction:
            return self._append_journal_transaction(record)
        with self.transaction():
            return self._append_journal_transaction(record)

    def journal_trial_balance(self, as_of: datetime) -> tuple[TrialBalanceRow, ...]:
        normalized_as_of = normalize_utc_timestamp(as_of)
        rows = self._connection.execute(
            """
            SELECT posting.asset, posting.account, SUM(posting.debit), SUM(posting.credit)
            FROM journal_postings AS posting
            JOIN journal_transactions AS transaction
              ON transaction.transaction_id = posting.transaction_id
            WHERE transaction.occurred_at <= ? AND transaction.observed_at <= ?
            GROUP BY posting.asset, posting.account
            ORDER BY posting.asset, posting.account
            """,
            [normalized_as_of, normalized_as_of],
        ).fetchall()
        return tuple(
            TrialBalanceRow(
                asset=row[0],
                account=row[1],
                debit=row[2],
                credit=row[3],
                difference=row[2] - row[3],
            )
            for row in rows
        )

    def latest_instrument_as_of(
        self, venue: Venue, symbol: str, as_of: datetime
    ) -> InstrumentSpec | None:
        normalized_as_of = normalize_utc_timestamp(as_of)
        row = self._connection.execute(
            """
            SELECT instrument_id, venue, symbol, asset, kind, contract_multiplier,
                   index_family, oracle_family, mark_method, liquidation_method,
                   collateral_asset, pnl_asset, funding_formula_id, funding_cap,
                   funding_interval_hours, funding_payment_offset_minutes, min_notional,
                   quantity_step, price_tick, is_inverse, is_prelaunch, epoch_us(observed_at),
                   source_hash, schema_version
            FROM instrument_specs
            WHERE venue = ? AND symbol = ? AND observed_at <= ?
            ORDER BY observed_at DESC, instrument_id, source_hash
            LIMIT 1
            """,
            [venue.value, symbol, normalized_as_of],
        ).fetchone()
        if row is None:
            return None
        return InstrumentSpec(
            instrument_id=row[0],
            venue=Venue(row[1]),
            symbol=row[2],
            asset=Asset(row[3]),
            kind=InstrumentKind(row[4]),
            contract_multiplier=row[5],
            index_family=row[6],
            oracle_family=row[7],
            mark_method=row[8],
            liquidation_method=row[9],
            collateral_asset=row[10],
            pnl_asset=row[11],
            funding_formula_id=row[12],
            funding_cap=row[13],
            funding_interval_hours=row[14],
            funding_payment_offset_minutes=row[15],
            min_notional=row[16],
            quantity_step=row[17],
            price_tick=row[18],
            is_inverse=row[19],
            is_prelaunch=row[20],
            observed_at=_utc_from_epoch_us(row[21]),
            source_hash=row[22],
            schema_version=row[23],
        )

    def latest_book_as_of(
        self, venue: Venue, symbol: str, as_of: datetime
    ) -> Level2BookSnapshot | None:
        normalized_as_of = normalize_utc_timestamp(as_of)
        row = self._connection.execute(
            """
            SELECT book.cycle_id, book.venue, book.symbol, book.asset, book.depth_limit,
                   book.sequence, epoch_us(book.effective_at), epoch_us(book.observed_at),
                   book.source_hash, book.schema_version
            FROM book_snapshots AS book
            LEFT JOIN book_collection_cycles AS cycle ON cycle.cycle_id = book.cycle_id
            WHERE book.venue = ? AND book.symbol = ?
              AND book.effective_at <= ? AND book.observed_at <= ?
              AND (cycle.cycle_id IS NULL OR cycle.status = 'complete')
            ORDER BY book.observed_at DESC, book.effective_at DESC, book.cycle_id
            LIMIT 1
            """,
            [venue.value, symbol, normalized_as_of, normalized_as_of],
        ).fetchone()
        return self._book_snapshot_from_row(row)

    def book_for_cycle_as_of(
        self,
        cycle_id: UUID,
        venue: Venue,
        symbol: str,
        as_of: datetime,
    ) -> Level2BookSnapshot | None:
        normalized_as_of = normalize_utc_timestamp(as_of)
        row = self._connection.execute(
            """
            SELECT cycle_id, venue, symbol, asset, depth_limit, sequence,
                   epoch_us(effective_at), epoch_us(observed_at), source_hash, schema_version
            FROM book_snapshots
            WHERE cycle_id = ? AND venue = ? AND symbol = ?
              AND effective_at <= ? AND observed_at <= ?
            LIMIT 1
            """,
            [cycle_id, venue.value, symbol, normalized_as_of, normalized_as_of],
        ).fetchone()
        return self._book_snapshot_from_row(row)

    def _book_snapshot_from_row(self, row: tuple[Any, ...] | None) -> Level2BookSnapshot | None:
        if row is None:
            return None
        levels = self._connection.execute(
            """
            SELECT side, price, quantity, order_count
            FROM book_levels
            WHERE cycle_id = ? AND venue = ? AND symbol = ? AND epoch_us(observed_at) = ?
            ORDER BY CASE side WHEN 'bid' THEN 0 ELSE 1 END, level_index
            """,
            [row[0], row[1], row[2], row[7]],
        ).fetchall()
        bids = tuple(
            BookLevel(price=level[1], quantity=level[2], order_count=level[3])
            for level in levels
            if level[0] == "bid"
        )
        asks = tuple(
            BookLevel(price=level[1], quantity=level[2], order_count=level[3])
            for level in levels
            if level[0] == "ask"
        )
        return Level2BookSnapshot(
            cycle_id=row[0],
            venue=Venue(row[1]),
            symbol=row[2],
            asset=Asset(row[3]),
            bids=bids,
            asks=asks,
            depth_limit=row[4],
            sequence=row[5],
            effective_at=_utc_from_epoch_us(row[6]),
            observed_at=_utc_from_epoch_us(row[7]),
            source_hash=row[8],
            schema_version=row[9],
        )

    def book_collection_cycles_between(
        self,
        start: datetime,
        end: datetime,
        known_as_of: datetime,
    ) -> tuple[BookCollectionCycle, ...]:
        normalized_start = normalize_utc_timestamp(start)
        normalized_end = normalize_utc_timestamp(end)
        normalized_known_as_of = normalize_utc_timestamp(known_as_of)
        if normalized_start > normalized_end:
            raise ValueError("start must be less than or equal to end")
        if normalized_known_as_of < normalized_end:
            raise ValueError("knowledge cutoff must be greater than or equal to end")
        rows = self._connection.execute(
            """
            SELECT CAST(record_json AS VARCHAR)
            FROM book_collection_cycles
            WHERE request_completed_at <= ?
            ORDER BY request_completed_at, cycle_id
            """,
            [normalized_known_as_of],
        ).fetchall()
        cycles = tuple(BookCollectionCycle.model_validate_json(row[0]) for row in rows)
        return tuple(
            cycle
            for cycle in cycles
            if cycle.effective_timestamps
            and cycle.effective_timestamps[-1] >= normalized_start
            and cycle.effective_timestamps[0] <= normalized_end
        )

    def book_collection_cycles_completed_between(
        self,
        start: datetime,
        end: datetime,
        known_as_of: datetime,
    ) -> tuple[BookCollectionCycle, ...]:
        normalized_start = normalize_utc_timestamp(start)
        normalized_end = normalize_utc_timestamp(end)
        normalized_known_as_of = normalize_utc_timestamp(known_as_of)
        if normalized_start > normalized_end:
            raise ValueError("start must be less than or equal to end")
        if normalized_known_as_of < normalized_end:
            raise ValueError("knowledge cutoff must be greater than or equal to end")
        rows = self._connection.execute(
            """
            SELECT CAST(record_json AS VARCHAR)
            FROM book_collection_cycles
            WHERE request_completed_at >= ?
              AND request_completed_at <= ?
              AND request_completed_at <= ?
            ORDER BY request_completed_at, cycle_id
            """,
            [normalized_start, normalized_end, normalized_known_as_of],
        ).fetchall()
        return tuple(BookCollectionCycle.model_validate_json(row[0]) for row in rows)

    def books_for_cycle(self, cycle_id: UUID) -> tuple[Level2BookSnapshot, ...]:
        rows = self._connection.execute(
            """
            SELECT cycle_id, venue, symbol, asset, depth_limit, sequence,
                   epoch_us(effective_at), epoch_us(observed_at), source_hash, schema_version
            FROM book_snapshots
            WHERE cycle_id = ?
            ORDER BY venue, symbol
            """,
            [cycle_id],
        ).fetchall()
        return tuple(
            snapshot for row in rows if (snapshot := self._book_snapshot_from_row(row)) is not None
        )

    def book_snapshot_headers_for_cycles(
        self,
        cycle_ids: tuple[UUID, ...],
        known_as_of: datetime,
    ) -> tuple[BookSnapshotHeader, ...]:
        """Return cutoff-safe snapshot headers without reading book levels."""
        normalized_known_as_of = normalize_utc_timestamp(known_as_of)
        if not cycle_ids:
            return ()
        rows = self._connection.execute(
            """
            SELECT cycle_id, venue, symbol, asset,
                   epoch_us(effective_at), epoch_us(observed_at), source_hash
            FROM book_snapshots
            WHERE cycle_id IN (SELECT unnest(?::UUID[]))
              AND effective_at <= ?
              AND observed_at <= ?
            ORDER BY cycle_id, venue, symbol
            """,
            [list(cycle_ids), normalized_known_as_of, normalized_known_as_of],
        ).fetchall()
        return tuple(
            BookSnapshotHeader(
                cycle_id=row[0],
                venue=Venue(row[1]),
                symbol=row[2],
                asset=Asset(row[3]),
                effective_at=_utc_from_epoch_us(row[4]),
                observed_at=_utc_from_epoch_us(row[5]),
                source_hash=row[6],
            )
            for row in rows
        )

    def latest_economic_evaluation_as_of(
        self, asset: Asset, as_of: datetime
    ) -> CandidateEconomicsReport | LegacyEconomicEvaluationSummary | None:
        normalized_as_of = normalize_utc_timestamp(as_of)
        row = self._connection.execute(
            """
            SELECT CAST(report_json AS VARCHAR), schema_version
            FROM economic_evaluations
            WHERE asset = ? AND known_as_of <= ? AND evaluated_at <= ?
            ORDER BY evaluated_at DESC, evaluation_id DESC
            LIMIT 1
            """,
            [asset.value, normalized_as_of, normalized_as_of],
        ).fetchone()
        if row is None:
            return None
        from polytrading.carry.economics_models import (
            CandidateEconomicsReport,
            LegacyEconomicEvaluationSummary,
        )

        if row[1] == 2:
            return CandidateEconomicsReport.model_validate_json(row[0])
        if row[1] == 1:
            return LegacyEconomicEvaluationSummary.from_report_json(row[0])
        raise ValueError(f"unsupported economic evaluation schema version: {row[1]}")

    def latest_book_cycle_as_of(self, as_of: datetime) -> BookCollectionCycle | None:
        normalized_as_of = normalize_utc_timestamp(as_of)
        row = self._connection.execute(
            """
            SELECT CAST(record_json AS VARCHAR)
            FROM book_collection_cycles
            WHERE request_completed_at <= ?
            ORDER BY request_completed_at DESC, cycle_id
            LIMIT 1
            """,
            [normalized_as_of],
        ).fetchone()
        if row is None:
            return None
        return BookCollectionCycle.model_validate_json(row[0])

    def latest_complete_book_cycle_as_of(self, as_of: datetime) -> BookCollectionCycle | None:
        normalized_as_of = normalize_utc_timestamp(as_of)
        row = self._connection.execute(
            """
            SELECT CAST(record_json AS VARCHAR)
            FROM book_collection_cycles
            WHERE request_completed_at <= ? AND status = 'complete'
            ORDER BY request_completed_at DESC, cycle_id
            LIMIT 1
            """,
            [normalized_as_of],
        ).fetchone()
        if row is None:
            return None
        return BookCollectionCycle.model_validate_json(row[0])

    def funding_collection_cycles_between(
        self, start: datetime, end: datetime
    ) -> tuple[FundingCollectionCycle, ...]:
        normalized_start = normalize_utc_timestamp(start)
        normalized_end = normalize_utc_timestamp(end)
        if normalized_start > normalized_end:
            raise ValueError("start must be less than or equal to end")
        rows = self._connection.execute(
            """
            SELECT CAST(record_json AS VARCHAR)
            FROM funding_collection_cycles
            WHERE cycle_end >= ? AND cycle_end <= ?
            ORDER BY cycle_end, request_completed_at, cycle_id
            """,
            [normalized_start, normalized_end],
        ).fetchall()
        return tuple(FundingCollectionCycle.model_validate_json(row[0]) for row in rows)

    def latest_funding_collection_cycle_as_of(
        self, as_of: datetime
    ) -> FundingCollectionCycle | None:
        normalized_as_of = normalize_utc_timestamp(as_of)
        row = self._connection.execute(
            """
            SELECT CAST(record_json AS VARCHAR)
            FROM funding_collection_cycles
            WHERE request_completed_at <= ?
            ORDER BY request_completed_at DESC, cycle_end DESC, cycle_id
            LIMIT 1
            """,
            [normalized_as_of],
        ).fetchone()
        return None if row is None else FundingCollectionCycle.model_validate_json(row[0])

    def lighter_dydx_funding_cycles_between(
        self, start: datetime, end: datetime, known_as_of: datetime
    ) -> tuple[LighterDydxFundingCycle, ...]:
        normalized_start = normalize_utc_timestamp(start)
        normalized_end = normalize_utc_timestamp(end)
        normalized_known_as_of = normalize_utc_timestamp(known_as_of)
        if normalized_start > normalized_end:
            raise ValueError("start must be less than or equal to end")
        if normalized_known_as_of < normalized_end:
            raise ValueError("known_as_of must be greater than or equal to end")
        rows = self._connection.execute(
            """
            SELECT CAST(record_json AS VARCHAR)
            FROM lighter_dydx_funding_cycles
            WHERE cycle_end >= ? AND cycle_end <= ? AND request_completed_at <= ?
            ORDER BY cycle_end, request_completed_at, cycle_id
            """,
            [normalized_start, normalized_end, normalized_known_as_of],
        ).fetchall()
        return tuple(LighterDydxFundingCycle.model_validate_json(row[0]) for row in rows)

    def latest_lighter_dydx_funding_cycle_as_of(
        self, as_of: datetime
    ) -> LighterDydxFundingCycle | None:
        normalized_as_of = normalize_utc_timestamp(as_of)
        row = self._connection.execute(
            """
            SELECT CAST(record_json AS VARCHAR)
            FROM lighter_dydx_funding_cycles
            WHERE request_completed_at <= ?
            ORDER BY request_completed_at DESC, cycle_end DESC, cycle_id DESC
            LIMIT 1
            """,
            [normalized_as_of],
        ).fetchone()
        return None if row is None else LighterDydxFundingCycle.model_validate_json(row[0])

    def evidence_counts_as_of(self, as_of: datetime) -> dict[str, int]:
        normalized_as_of = normalize_utc_timestamp(as_of)
        queries = (
            ("raw_envelopes", "SELECT count(*) FROM raw_envelopes WHERE observed_at <= ?"),
            ("instrument_specs", "SELECT count(*) FROM instrument_specs WHERE observed_at <= ?"),
            (
                "funding_observations",
                "SELECT count(*) FROM funding_observations WHERE observed_at <= ?",
            ),
            ("market_snapshots", "SELECT count(*) FROM market_snapshots WHERE observed_at <= ?"),
            ("book_snapshots", "SELECT count(*) FROM book_snapshots WHERE observed_at <= ?"),
            (
                "book_collection_cycles",
                "SELECT count(*) FROM book_collection_cycles WHERE request_completed_at <= ?",
            ),
            (
                "funding_collection_cycles",
                "SELECT count(*) FROM funding_collection_cycles WHERE request_completed_at <= ?",
            ),
            (
                "lighter_dydx_funding_cycles",
                "SELECT count(*) FROM lighter_dydx_funding_cycles WHERE request_completed_at <= ?",
            ),
        )
        return {
            name: int(self._connection.execute(sql, [normalized_as_of]).fetchone()[0])
            for name, sql in queries
        }

    def latest_funding_as_of(
        self, venue: Venue, symbol: str, as_of: datetime
    ) -> FundingObservation | None:
        normalized_as_of = normalize_utc_timestamp(as_of)
        row = self._connection.execute(
            """
            SELECT venue, symbol, asset, rate, interval_hours, epoch_us(effective_at),
                   epoch_us(observed_at), source_hash, schema_version
            FROM funding_observations
            WHERE venue = ? AND symbol = ?
              AND effective_at <= ? AND observed_at <= ?
            ORDER BY effective_at DESC, observed_at DESC, source_hash
            LIMIT 1
            """,
            [venue.value, symbol, normalized_as_of, normalized_as_of],
        ).fetchone()
        if row is None:
            return None
        return FundingObservation(
            venue=Venue(row[0]),
            symbol=row[1],
            asset=Asset(row[2]),
            rate=row[3],
            interval_hours=row[4],
            effective_at=_utc_from_epoch_us(row[5]),
            observed_at=_utc_from_epoch_us(row[6]),
            source_hash=row[7],
            schema_version=row[8],
        )

    def latest_fee_as_of(self, venue: Venue, tier_name: str, as_of: datetime) -> FeeSchedule | None:
        normalized_as_of = normalize_utc_timestamp(as_of)
        row = self._connection.execute(
            """
            SELECT venue, tier_name, maker_rate, taker_rate, epoch_us(effective_from),
                   epoch_us(observed_at), source_url, source_hash, schema_version
            FROM fee_schedules
            WHERE venue = ? AND tier_name = ?
              AND effective_from <= ? AND observed_at <= ?
            ORDER BY effective_from DESC, observed_at DESC
            LIMIT 1
            """,
            [venue.value, tier_name, normalized_as_of, normalized_as_of],
        ).fetchone()
        if row is None:
            return None
        return FeeSchedule(
            venue=Venue(row[0]),
            tier_name=row[1],
            maker_rate=row[2],
            taker_rate=row[3],
            effective_from=_utc_from_epoch_us(row[4]),
            observed_at=_utc_from_epoch_us(row[5]),
            source_url=row[6],
            source_hash=row[7],
            schema_version=row[8],
        )

    def reviewed_fee_schedules_as_of(self, as_of: datetime) -> tuple[FeeSchedule, ...]:
        normalized_as_of = normalize_utc_timestamp(as_of)
        rows = self._connection.execute(
            """
            SELECT venue, tier_name, maker_rate, taker_rate, epoch_us(effective_from),
                   epoch_us(observed_at), source_url, source_hash, schema_version
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY venue, tier_name
                    ORDER BY effective_from DESC, observed_at DESC, source_hash DESC
                ) AS revision_rank
                FROM fee_schedules
                WHERE effective_from <= ? AND observed_at <= ?
            ) AS applicable_fees
            WHERE revision_rank = 1
            ORDER BY venue, tier_name, effective_from, observed_at, source_hash
            """,
            [normalized_as_of, normalized_as_of],
        ).fetchall()
        return tuple(
            FeeSchedule(
                venue=Venue(row[0]),
                tier_name=row[1],
                maker_rate=row[2],
                taker_rate=row[3],
                effective_from=_utc_from_epoch_us(row[4]),
                observed_at=_utc_from_epoch_us(row[5]),
                source_url=row[6],
                source_hash=row[7],
                schema_version=row[8],
            )
            for row in rows
        )

    def funding_between(
        self, venue: Venue, symbol: str, start: datetime, end: datetime
    ) -> tuple[FundingObservation, ...]:
        normalized_start = normalize_utc_timestamp(start)
        normalized_end = normalize_utc_timestamp(end)
        if normalized_start > normalized_end:
            raise ValueError("start must be less than or equal to end")
        rows = self._connection.execute(
            """
            SELECT venue, symbol, asset, rate, interval_hours, epoch_us(effective_at),
                   epoch_us(observed_at), source_hash, schema_version
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY effective_at
                    ORDER BY observed_at DESC, source_hash DESC
                ) AS revision_rank
                FROM funding_observations
                WHERE venue = ? AND symbol = ?
                  AND effective_at >= ? AND effective_at <= ?
                  AND observed_at <= ?
            ) AS known_revisions
            WHERE revision_rank = 1
            ORDER BY effective_at, observed_at, source_hash
            """,
            [venue.value, symbol, normalized_start, normalized_end, normalized_end],
        ).fetchall()
        return tuple(
            FundingObservation(
                venue=Venue(row[0]),
                symbol=row[1],
                asset=Asset(row[2]),
                rate=row[3],
                interval_hours=row[4],
                effective_at=_utc_from_epoch_us(row[5]),
                observed_at=_utc_from_epoch_us(row[6]),
                source_hash=row[7],
                schema_version=row[8],
            )
            for row in rows
        )

    def funding_revisions_between(
        self,
        venue: Venue,
        symbol: str,
        start: datetime,
        end: datetime,
        known_as_of: datetime,
    ) -> tuple[FundingObservation, ...]:
        normalized_start = normalize_utc_timestamp(start)
        normalized_end = normalize_utc_timestamp(end)
        normalized_known_as_of = normalize_utc_timestamp(known_as_of)
        if normalized_start > normalized_end:
            raise ValueError("start must be less than or equal to end")
        if normalized_known_as_of < normalized_end:
            raise ValueError("known_as_of must be greater than or equal to end")
        rows = self._connection.execute(
            """
            SELECT venue, symbol, asset, rate, interval_hours, epoch_us(effective_at),
                   epoch_us(observed_at), source_hash, schema_version
            FROM funding_observations
            WHERE venue = ? AND symbol = ?
              AND effective_at > ? AND effective_at <= ?
              AND observed_at <= ?
            ORDER BY effective_at, observed_at, source_hash
            """,
            [venue.value, symbol, normalized_start, normalized_end, normalized_known_as_of],
        ).fetchall()
        return tuple(
            FundingObservation(
                venue=Venue(row[0]),
                symbol=row[1],
                asset=Asset(row[2]),
                rate=row[3],
                interval_hours=row[4],
                effective_at=_utc_from_epoch_us(row[5]),
                observed_at=_utc_from_epoch_us(row[6]),
                source_hash=row[7],
                schema_version=row[8],
            )
            for row in rows
        )

    def _append_book_snapshot(self, record: Level2BookSnapshot) -> bool:
        if self._normalized_retry(
            "book snapshot",
            record,
            "book_snapshots",
            "cycle_id = ? AND venue = ? AND symbol = ?",
            [record.cycle_id, record.venue.value, record.symbol],
        ):
            return False
        record_hash = _record_hash(record)
        self._connection.execute(
            "INSERT INTO book_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.cycle_id,
                record.venue.value,
                record.symbol,
                record.asset.value,
                record.depth_limit,
                record.sequence,
                record.effective_at,
                record.observed_at,
                record.source_hash,
                record.schema_version,
                record_hash,
            ],
        )
        for side, side_levels in (("bid", record.bids), ("ask", record.asks)):
            for index, level in enumerate(side_levels):
                self._connection.execute(
                    "INSERT INTO book_levels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        record.cycle_id,
                        record.venue.value,
                        record.symbol,
                        record.observed_at,
                        side,
                        index,
                        level.price,
                        level.quantity,
                        level.order_count,
                        record_hash,
                    ],
                )
        return True

    def _append_economic_evaluation(self, record: CandidateEconomicsReport) -> bool:
        if self._normalized_retry(
            "economic evaluation",
            record,
            "economic_evaluations",
            "evaluation_id = ?",
            [record.evaluation_id],
        ):
            return False
        self._connection.execute(
            """
            INSERT INTO economic_evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?::JSON, ?, ?)
            """,
            [
                record.evaluation_id,
                record.asset.value,
                record.known_as_of,
                record.evaluated_at,
                record.decision.value,
                None if record.direction is None else record.direction.value,
                record.policy_hash,
                _canonical_json(record),
                record.schema_version,
                _record_hash(record),
            ],
        )
        return True

    def _append_paper_position(self, record: PaperPosition) -> bool:
        if self._normalized_retry(
            "paper position",
            record,
            "paper_positions",
            "position_id = ?",
            [record.position_id],
        ):
            return False
        self._connection.execute(
            "INSERT INTO paper_positions VALUES (?, ?, ?, ?, ?::JSON, ?, ?)",
            [
                record.position_id,
                record.source_evaluation_id,
                record.asset.value,
                record.opened_at,
                _canonical_json(record),
                record.schema_version,
                _record_hash(record),
            ],
        )
        return True

    def _append_paper_position_closure(self, record: PaperPositionClosure) -> bool:
        if self._normalized_retry(
            "paper position closure",
            record,
            "paper_position_closures",
            "position_id = ?",
            [record.position_id],
        ):
            return False
        self._connection.execute(
            "INSERT INTO paper_position_closures VALUES (?, ?, ?, ?::JSON, ?, ?)",
            [
                record.position_id,
                record.closed_at,
                record.close_reason.value,
                _canonical_json(record),
                record.schema_version,
                _record_hash(record),
            ],
        )
        return True

    def _append_book_collection_cycle(self, record: BookCollectionCycle) -> bool:
        if self._normalized_retry(
            "book collection cycle",
            record,
            "book_collection_cycles",
            "cycle_id = ?",
            [record.cycle_id],
        ):
            return False
        self._connection.execute(
            "INSERT INTO book_collection_cycles VALUES (?, ?, ?, ?::JSON, ?)",
            [
                record.cycle_id,
                record.request_completed_at,
                record.status,
                _canonical_json(record),
                _record_hash(record),
            ],
        )
        return True

    def _append_funding_collection_cycle(self, record: FundingCollectionCycle) -> bool:
        if self._normalized_retry(
            "funding collection cycle",
            record,
            "funding_collection_cycles",
            "cycle_id = ?",
            [record.cycle_id],
        ):
            return False
        self._connection.execute(
            "INSERT INTO funding_collection_cycles VALUES (?, ?, ?, ?, ?::JSON, ?)",
            [
                record.cycle_id,
                record.cycle_end,
                record.request_completed_at,
                record.status.value,
                _canonical_json(record),
                _record_hash(record),
            ],
        )
        return True

    def _append_lighter_dydx_funding_cycle(self, record: LighterDydxFundingCycle) -> bool:
        if self._normalized_retry(
            "Lighter-dYdX funding cycle",
            record,
            "lighter_dydx_funding_cycles",
            "cycle_id = ?",
            [record.cycle_id],
        ):
            return False
        self._connection.execute(
            "INSERT INTO lighter_dydx_funding_cycles VALUES (?, ?, ?, ?, ?::JSON, ?)",
            [
                record.cycle_id,
                record.cycle_end,
                record.request_completed_at,
                record.status.value,
                _canonical_json(record),
                _record_hash(record),
            ],
        )
        return True

    def _append_journal_transaction(self, record: JournalTransaction) -> bool:
        if self._normalized_retry(
            "journal transaction",
            record,
            "journal_transactions",
            "transaction_id = ?",
            [record.transaction_id],
        ):
            return False
        record_hash = _record_hash(record)
        self._connection.execute(
            """
            INSERT INTO journal_transactions (
                transaction_id, occurred_at, observed_at, description, evidence_ids,
                schema_version, record_hash
            ) VALUES (?, ?, ?, ?, ?::JSON, ?, ?)
            """,
            [
                record.transaction_id,
                record.occurred_at,
                record.observed_at,
                record.description,
                json.dumps(record.evidence_ids, ensure_ascii=False, separators=(",", ":")),
                record.schema_version,
                record_hash,
            ],
        )
        for index, posting in enumerate(record.postings):
            self._connection.execute(
                "INSERT INTO journal_postings VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    record.transaction_id,
                    index,
                    posting.account,
                    posting.asset,
                    posting.debit,
                    posting.credit,
                    record_hash,
                ],
            )
        return True

    def _normalized_retry(
        self,
        label: str,
        record: BaseModel,
        table: str,
        identity_sql: str,
        parameters: list[Any],
    ) -> bool:
        # table/identity_sql are hardcoded literals from internal callers, never
        # externally-influenced identifiers — this is not safe if that changes.
        existing = self._connection.execute(
            f"SELECT record_hash FROM {table} WHERE {identity_sql}", parameters
        ).fetchone()
        if existing is None:
            return False
        self._ensure_exact_retry(label, record, existing[0])
        return True

    @staticmethod
    def _ensure_exact_retry(label: str, record: BaseModel, existing_hash: str) -> None:
        if _record_hash(record) != existing_hash:
            raise ConflictingRecordError(f"conflicting {label} for immutable identity")

    def _apply_migrations(self) -> None:
        migrations = self._migration_entries()
        versions = [version for version, _ in migrations]

        self._connection.execute("BEGIN TRANSACTION")
        try:
            applied = self._applied_migration_versions(versions)
            for version, migration in migrations[len(applied) :]:
                self._connection.execute(migration.read_text(encoding="utf-8"))
                self._connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)", [version, datetime.now(UTC)]
                )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def _verify_current_schema(self) -> None:
        versions = [version for version, _ in self._migration_entries()]
        applied = self._applied_migration_versions(versions)
        if applied != versions:
            raise RuntimeError(
                f"read-only store requires current schema {versions}; found {applied}"
            )

    @staticmethod
    def _migration_entries() -> list[tuple[int, Any]]:
        migration_root = resources.files("polytrading.storage.schema")
        migrations: list[tuple[int, Any]] = []
        for entry in migration_root.iterdir():
            match = _MIGRATION_NAME.fullmatch(entry.name)
            if match is not None:
                migrations.append((int(match.group("version")), entry))
        migrations.sort(key=lambda migration: migration[0])
        versions = [version for version, _ in migrations]
        if versions != list(range(1, len(versions) + 1)):
            raise RuntimeError(f"migration versions must be contiguous from 1; found {versions}")
        return migrations

    def _applied_migration_versions(self, known_versions: list[int]) -> list[int]:
        has_migration_table = bool(
            self._connection.execute(
                """
                SELECT count(*)
                FROM information_schema.tables
                WHERE table_schema = 'main' AND table_name = 'schema_migrations'
                """
            ).fetchone()[0]
        )
        applied = (
            [
                row[0]
                for row in self._connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
            if has_migration_table
            else []
        )
        if has_migration_table and not applied:
            raise RuntimeError("schema_migrations exists without an applied version")
        if applied != list(range(1, len(applied) + 1)) or any(
            version not in known_versions for version in applied
        ):
            raise RuntimeError(f"applied migration versions are not a known prefix: {applied}")
        return applied
