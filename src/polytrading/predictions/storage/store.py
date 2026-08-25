from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb
from pydantic import BaseModel

from polytrading.predictions.attestations import RuleAttestation
from polytrading.predictions.candidates_models import CandidateRelationship
from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookSnapshot,
    PredictionFeeRate,
    PredictionRawEnvelope,
    PredictionVenue,
    RuleVersion,
    TradeRecord,
)
from polytrading.predictions.economics_models import ScanReport
from polytrading.predictions.experiments import ShadowExperiment, TrialFamily
from polytrading.predictions.manifest import VenueManifest
from polytrading.predictions.proofs_models import ProofArtifact
from polytrading.predictions.shadow_ledger import LedgerPosting, ShadowReconciliation
from polytrading.predictions.shadow_models import ShadowEvent, ShadowPlan

_MIGRATION_NAME = re.compile(r"(?P<version>[0-9]{3})_[a-z0-9_]+\.sql")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FAMILY_SENTINEL_TABLE = "prediction_raw_envelopes"


def _utc_from_epoch_us(value: int | None) -> datetime | None:
    if value is None:
        return None
    return _UNIX_EPOCH + timedelta(microseconds=value)


class ConflictingRecordError(ValueError):
    """Raised when an immutable prediction-market identity is retried with different content."""


def _canonical_json(record: BaseModel) -> str:
    return json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _record_hash(record: BaseModel) -> str:
    return sha256(_canonical_json(record).encode()).hexdigest()


def _verified_record[RecordT: BaseModel](
    row: tuple[Any, ...] | None,
    record_type: type[RecordT],
    label: str,
) -> RecordT | None:
    if row is None:
        return None
    record = record_type.model_validate_json(row[0])
    if _record_hash(record) != row[1]:
        raise ConflictingRecordError(f"stored {label} failed its immutable record hash")
    return record


def _verified_shadow_experiments(rows: list[tuple[Any, ...]]) -> tuple[ShadowExperiment, ...]:
    records: list[ShadowExperiment] = []
    for row in rows:
        record = _verified_record((row[8], row[9]), ShadowExperiment, "shadow experiment")
        if record is None:
            continue
        indexed = (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            _utc_from_epoch_us(row[6]),
            _utc_from_epoch_us(row[7]),
        )
        decoded = (
            record.experiment_id,
            record.family_id,
            record.proposal_id,
            record.scenario_id,
            record.terminal_state.value,
            record.reconciled,
            record.as_of,
            record.observed_at,
        )
        if indexed != decoded:
            raise ConflictingRecordError("stored shadow experiment indexed columns do not match")
        records.append(record)
    return tuple(sorted(records, key=lambda record: (record.observed_at, record.experiment_id)))


class PredictionMarketStore:
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
    def transaction(self) -> Iterator[PredictionMarketStore]:
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

    # -- append --------------------------------------------------------

    def append_raw(self, record: PredictionRawEnvelope) -> bool:
        existing = self._connection.execute(
            """
            SELECT event_id, venue, endpoint, epoch_us(venue_timestamp), epoch_us(observed_at),
                   received_monotonic_ns, request_latency_ms, source_version,
                   payload_json, source_hash, schema_version
            FROM prediction_raw_envelopes
            WHERE event_id = ?
            """,
            [record.event_id],
        ).fetchone()
        if existing is not None:
            stored = PredictionRawEnvelope(
                schema_version=existing[10],
                event_id=existing[0],
                venue=PredictionVenue(existing[1]),
                endpoint=existing[2],
                venue_timestamp=_utc_from_epoch_us(existing[3]),
                observed_at=_utc_from_epoch_us(existing[4]),
                received_monotonic_ns=existing[5],
                request_latency_ms=existing[6],
                source_version=existing[7],
                payload_json=existing[8],
                source_hash=existing[9],
            )
            if stored != record:
                raise ConflictingRecordError("conflicting raw envelope for immutable identity")
            return False
        self._connection.execute(
            """
            INSERT INTO prediction_raw_envelopes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def append_venue_manifest(self, record: VenueManifest) -> bool:
        return self._append_keyed(
            table="venue_manifests",
            record=record,
            label="venue manifest",
            where="venue = ? AND reviewed_at = ?",
            key_params=[record.venue.value, record.reviewed_at],
            insert_columns="venue, reviewed_at, record_json, record_hash",
            insert_params=[record.venue.value, record.reviewed_at],
        )

    def append_market(self, record: MarketRecord) -> bool:
        return self._append_keyed(
            table="markets",
            record=record,
            label="market",
            where="venue = ? AND market_id = ? AND rule_version_id = ?",
            key_params=[record.venue.value, record.market_id, record.rule_version_id],
            insert_columns=(
                "venue, market_id, rule_version_id, retrieved_at, record_json, record_hash"
            ),
            insert_params=[
                record.venue.value,
                record.market_id,
                record.rule_version_id,
                record.retrieved_at,
            ],
        )

    def append_rule_version(self, record: RuleVersion) -> bool:
        return self._append_keyed(
            table="rule_versions",
            record=record,
            label="rule version",
            where="rule_version_id = ?",
            key_params=[record.rule_version_id],
            insert_columns=(
                "rule_version_id, venue, market_id, effective_at, record_json, record_hash"
            ),
            insert_params=[
                record.rule_version_id,
                record.venue.value,
                record.market_id,
                record.effective_at,
            ],
        )

    def append_trade(self, record: TradeRecord) -> bool:
        return self._append_keyed(
            table="trades",
            record=record,
            label="trade",
            where="venue = ? AND market_id = ? AND trade_id = ?",
            key_params=[record.venue.value, record.market_id, record.trade_id],
            insert_columns=(
                "venue, market_id, trade_id, effective_at, observed_at, record_json, record_hash"
            ),
            insert_params=[
                record.venue.value,
                record.market_id,
                record.trade_id,
                record.effective_at,
                record.observed_at,
            ],
        )

    def append_book_snapshot(self, record: PredictionBookSnapshot) -> bool:
        return self._append_keyed(
            table="prediction_books",
            record=record,
            label="book snapshot",
            where=(
                "cycle_id = ? AND venue = ? AND market_id = ? "
                "AND outcome_token_id IS NOT DISTINCT FROM ?"
            ),
            key_params=[
                record.cycle_id,
                record.venue.value,
                record.market_id,
                record.outcome_token_id,
            ],
            insert_columns=(
                "cycle_id, venue, market_id, outcome_token_id, observed_at, "
                "record_json, record_hash"
            ),
            insert_params=[
                record.cycle_id,
                record.venue.value,
                record.market_id,
                record.outcome_token_id,
                record.observed_at,
            ],
        )

    def append_fee_rate(self, record: PredictionFeeRate) -> bool:
        return self._append_keyed(
            table="prediction_fee_rates",
            record=record,
            label="fee rate",
            where="venue = ? AND market_id IS NOT DISTINCT FROM ? AND observed_at = ?",
            key_params=[record.venue.value, record.market_id, record.observed_at],
            insert_columns="venue, market_id, observed_at, record_json, record_hash",
            insert_params=[record.venue.value, record.market_id, record.observed_at],
        )

    def append_candidate_relationship(self, record: CandidateRelationship) -> bool:
        return self._append_keyed(
            table="candidate_relationships",
            record=record,
            label="candidate relationship",
            where="candidate_id = ?",
            key_params=[record.candidate_id],
            insert_columns=(
                "candidate_id, relationship_type, trial_family_id, observed_at, "
                "information_cutoff, record_json, record_hash"
            ),
            insert_params=[
                record.candidate_id,
                record.relationship_type.value,
                record.trial_family_id,
                record.observed_at,
                record.information_cutoff,
            ],
        )

    def append_rule_attestation(self, record: RuleAttestation) -> bool:
        return self._append_keyed(
            table="rule_attestations",
            record=record,
            label="rule attestation",
            where="attestation_id = ?",
            key_params=[record.attestation_id],
            insert_columns=(
                "attestation_id, venue, market_id, rule_version_id, reviewed_at, "
                "record_json, record_hash"
            ),
            insert_params=[
                record.attestation_id,
                record.venue.value,
                record.market_id,
                record.rule_version_id,
                record.reviewed_at,
            ],
        )

    def append_proof_artifact(self, record: ProofArtifact) -> bool:
        return self._append_keyed(
            table="proof_artifacts",
            record=record,
            label="proof artifact",
            where="proof_id = ?",
            key_params=[record.proof_id],
            insert_columns=(
                "proof_id, candidate_id, template, status, observed_at, "
                "information_cutoff, record_json, record_hash"
            ),
            insert_params=[
                record.proof_id,
                record.candidate_id,
                record.template,
                record.status,
                record.observed_at,
                record.information_cutoff,
            ],
        )

    def append_scan_report(self, record: ScanReport) -> bool:
        return self._append_keyed(
            table="scan_reports",
            record=record,
            label="scan report",
            where="report_id = ?",
            key_params=[record.report_id],
            insert_columns=(
                "report_id, candidate_id, decision, as_of, observed_at, record_json, record_hash"
            ),
            insert_params=[
                record.report_id,
                record.candidate_id,
                record.decision,
                record.as_of,
                record.observed_at,
            ],
        )

    def append_shadow_plan(self, record: ShadowPlan) -> bool:
        return self._append_keyed(
            table="shadow_plans",
            record=record,
            label="shadow plan",
            where="proposal_id = ?",
            key_params=[record.proposal_id],
            insert_columns=(
                "proposal_id, candidate_id, observed_at, information_cutoff, "
                "record_json, record_hash"
            ),
            insert_params=[
                record.proposal_id,
                record.candidate_id,
                record.observed_at,
                record.information_cutoff,
            ],
        )

    def append_shadow_event(self, record: ShadowEvent) -> bool:
        existing_sequence = self._connection.execute(
            """
            SELECT event_id FROM shadow_events
            WHERE proposal_id = ? AND sequence = ?
            """,
            [record.proposal_id, record.sequence],
        ).fetchone()
        if existing_sequence is not None and existing_sequence[0] != record.event_id:
            raise ConflictingRecordError("conflicting shadow event for proposal sequence")
        return self._append_keyed(
            table="shadow_events",
            record=record,
            label="shadow event",
            where="event_id = ?",
            key_params=[record.event_id],
            insert_columns="event_id, proposal_id, sequence, occurred_at, record_json, record_hash",
            insert_params=[
                record.event_id,
                record.proposal_id,
                record.sequence,
                record.occurred_at,
            ],
        )

    def append_ledger_posting(self, record: LedgerPosting) -> bool:
        record = LedgerPosting.model_validate(record.model_dump())
        return self._append_keyed(
            table="shadow_ledger_postings",
            record=record,
            label="shadow ledger posting",
            where="posting_id = ?",
            key_params=[record.posting_id],
            insert_columns=(
                "posting_id, proposal_id, event_id, occurred_at, record_json, record_hash"
            ),
            insert_params=[
                record.posting_id,
                record.proposal_id,
                record.event_id,
                record.occurred_at,
            ],
        )

    def append_reconciliation(self, record: ShadowReconciliation) -> bool:
        record = ShadowReconciliation.model_validate(record.model_dump())
        return self._append_keyed(
            table="shadow_reconciliations",
            record=record,
            label="shadow reconciliation",
            where="reconciliation_id = ?",
            key_params=[record.reconciliation_id],
            insert_columns="reconciliation_id, proposal_id, observed_at, record_json, record_hash",
            insert_params=[record.reconciliation_id, record.proposal_id, record.observed_at],
        )

    def append_trial_family(self, record: TrialFamily) -> bool:
        record = TrialFamily.model_validate(record.model_dump())
        return self._append_keyed(
            table="trial_families",
            record=record,
            label="trial family",
            where="family_id = ? AND preregistered_at = ?",
            key_params=[record.family_id, record.preregistered_at],
            insert_columns="family_id, preregistered_at, record_json, record_hash",
            insert_params=[record.family_id, record.preregistered_at],
        )

    def append_shadow_experiment(self, record: ShadowExperiment) -> bool:
        record = ShadowExperiment.model_validate(record.model_dump())
        return self._append_keyed(
            table="shadow_experiments",
            record=record,
            label="shadow experiment",
            where="experiment_id = ?",
            key_params=[record.experiment_id],
            insert_columns=(
                "experiment_id, family_id, proposal_id, scenario_id, terminal_state, "
                "reconciled, as_of, observed_at, record_json, record_hash"
            ),
            insert_params=[
                record.experiment_id,
                record.family_id,
                record.proposal_id,
                record.scenario_id,
                record.terminal_state.value,
                record.reconciled,
                record.as_of,
                record.observed_at,
            ],
        )

    def _append_keyed(
        self,
        *,
        table: str,
        record: BaseModel,
        label: str,
        where: str,
        key_params: list[Any],
        insert_columns: str,
        insert_params: list[Any],
    ) -> bool:
        # table/where/insert_columns are hardcoded literals from internal callers,
        # never externally-influenced identifiers — this is not safe if that changes.
        existing = self._connection.execute(
            f"SELECT record_hash FROM {table} WHERE {where}", key_params
        ).fetchone()
        if existing is not None:
            if _record_hash(record) != existing[0]:
                raise ConflictingRecordError(f"conflicting {label} for immutable identity")
            return False
        placeholders = ", ".join("?" for _ in range(len(insert_params) + 2))
        self._connection.execute(
            f"INSERT INTO {table} ({insert_columns}) VALUES ({placeholders})",
            [*insert_params, _canonical_json(record), _record_hash(record)],
        )
        return True

    # -- cutoff-safe reads ----------------------------------------------

    def latest_venue_manifest_as_of(
        self, venue: PredictionVenue, as_of: datetime
    ) -> VenueManifest | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM venue_manifests
            WHERE venue = ? AND reviewed_at <= ?
            ORDER BY reviewed_at DESC
            LIMIT 1
            """,
            [venue.value, as_of],
        ).fetchone()
        return None if row is None else VenueManifest.model_validate_json(row[0])

    def markets_as_of(self, venue: PredictionVenue, as_of: datetime) -> tuple[MarketRecord, ...]:
        # A correlated subquery evaluated once per candidate row does not scale to a
        # real market catalog (confirmed 2026-08-16: ~70s against ~79k Kalshi markets).
        # A single windowed pass over the venue's rows is equivalent -- each market_id's
        # rows are its distinct rule_version_id revisions, so the newest retrieved_at at
        # or before the cutoff is exactly the same row the prior per-row subquery picked.
        rows = self._connection.execute(
            """
            SELECT record_json FROM (
                SELECT
                    market_id,
                    record_json,
                    ROW_NUMBER() OVER (
                        PARTITION BY market_id ORDER BY retrieved_at DESC
                    ) AS rank
                FROM markets
                WHERE venue = ? AND retrieved_at <= ?
            )
            WHERE rank = 1
            ORDER BY market_id
            """,
            [venue.value, as_of],
        ).fetchall()
        return tuple(MarketRecord.model_validate_json(row[0]) for row in rows)

    def verified_markets_as_of(
        self, venue: PredictionVenue, as_of: datetime
    ) -> tuple[MarketRecord, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json, record_hash FROM (
                SELECT
                    market_id,
                    record_json,
                    record_hash,
                    ROW_NUMBER() OVER (
                        PARTITION BY market_id ORDER BY retrieved_at DESC
                    ) AS rank
                FROM markets
                WHERE venue = ? AND retrieved_at <= ?
            )
            WHERE rank = 1
            ORDER BY market_id
            """,
            [venue.value, as_of],
        ).fetchall()
        return tuple(
            record
            for row in rows
            if (record := _verified_record(row, MarketRecord, "market")) is not None
        )

    def rule_version_by_id(self, rule_version_id: UUID) -> RuleVersion | None:
        row = self._connection.execute(
            "SELECT record_json FROM rule_versions WHERE rule_version_id = ?",
            [rule_version_id],
        ).fetchone()
        return None if row is None else RuleVersion.model_validate_json(row[0])

    def rule_versions_for_market(self, market_id: str, as_of: datetime) -> tuple[RuleVersion, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM rule_versions
            WHERE market_id = ? AND effective_at <= ?
            ORDER BY effective_at
            """,
            [market_id, as_of],
        ).fetchall()
        return tuple(RuleVersion.model_validate_json(row[0]) for row in rows)

    def verified_rule_versions_for_market(
        self, market_id: str, as_of: datetime
    ) -> tuple[RuleVersion, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json, record_hash FROM rule_versions
            WHERE market_id = ? AND effective_at <= ?
            ORDER BY effective_at
            """,
            [market_id, as_of],
        ).fetchall()
        return tuple(
            record
            for row in rows
            if (record := _verified_record(row, RuleVersion, "rule version")) is not None
        )

    def latest_book_as_of(
        self,
        venue: PredictionVenue,
        market_id: str,
        outcome_token_id: str | None,
        as_of: datetime,
    ) -> PredictionBookSnapshot | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM prediction_books
            WHERE venue = ? AND market_id = ?
              AND outcome_token_id IS NOT DISTINCT FROM ?
              AND observed_at <= ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [venue.value, market_id, outcome_token_id, as_of],
        ).fetchone()
        return None if row is None else PredictionBookSnapshot.model_validate_json(row[0])

    def book_snapshot_by_source_hash(
        self,
        venue: PredictionVenue,
        market_id: str,
        outcome_token_id: str | None,
        source_hash: str,
        as_of: datetime,
    ) -> PredictionBookSnapshot | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM prediction_books
            WHERE venue = ? AND market_id = ?
              AND outcome_token_id IS NOT DISTINCT FROM ?
              AND observed_at <= ?
              AND json_extract_string(record_json, '$.source_hash') = ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [venue.value, market_id, outcome_token_id, as_of, source_hash],
        ).fetchone()
        return None if row is None else PredictionBookSnapshot.model_validate_json(row[0])

    def verified_book_snapshot_by_source_hash(
        self,
        venue: PredictionVenue,
        market_id: str,
        outcome_token_id: str | None,
        source_hash: str,
        as_of: datetime,
    ) -> PredictionBookSnapshot | None:
        row = self._connection.execute(
            """
            SELECT record_json, record_hash FROM prediction_books
            WHERE venue = ? AND market_id = ?
              AND outcome_token_id IS NOT DISTINCT FROM ?
              AND observed_at <= ?
              AND json_extract_string(record_json, '$.source_hash') = ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [venue.value, market_id, outcome_token_id, as_of, source_hash],
        ).fetchone()
        return _verified_record(row, PredictionBookSnapshot, "book snapshot")

    def latest_book_observed_at_for_venue(
        self, venue: PredictionVenue, as_of: datetime
    ) -> datetime | None:
        # Cast to VARCHAR rather than fetching TIMESTAMPTZ directly: duckdb's Python
        # binding requires the optional pytz package to convert a native TIMESTAMPTZ
        # result column, which this project does not depend on -- every other reader
        # in this store avoids this by only ever fetching JSON text columns.
        row = self._connection.execute(
            """
            SELECT CAST(observed_at AS VARCHAR) FROM prediction_books
            WHERE venue = ? AND observed_at <= ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [venue.value, as_of],
        ).fetchone()
        return None if row is None else datetime.fromisoformat(row[0]).astimezone(UTC)

    def trades_between(
        self,
        venue: PredictionVenue,
        market_id: str,
        start: datetime,
        end: datetime,
        known_as_of: datetime,
    ) -> tuple[TradeRecord, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM trades
            WHERE venue = ? AND market_id = ?
              AND effective_at >= ? AND effective_at <= ?
              AND observed_at <= ?
            ORDER BY effective_at
            """,
            [venue.value, market_id, start, end, known_as_of],
        ).fetchall()
        return tuple(TradeRecord.model_validate_json(row[0]) for row in rows)

    def latest_fee_rate_as_of(
        self, venue: PredictionVenue, market_id: str | None, as_of: datetime
    ) -> PredictionFeeRate | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM prediction_fee_rates
            WHERE venue = ? AND market_id IS NOT DISTINCT FROM ? AND observed_at <= ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [venue.value, market_id, as_of],
        ).fetchone()
        return None if row is None else PredictionFeeRate.model_validate_json(row[0])

    def fee_rate_by_source_hash(
        self,
        venue: PredictionVenue,
        market_id: str | None,
        source_hash: str,
        as_of: datetime,
    ) -> PredictionFeeRate | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM prediction_fee_rates
            WHERE venue = ? AND market_id IS NOT DISTINCT FROM ?
              AND observed_at <= ?
              AND json_extract_string(record_json, '$.source_hash') = ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [venue.value, market_id, as_of, source_hash],
        ).fetchone()
        return None if row is None else PredictionFeeRate.model_validate_json(row[0])

    def verified_fee_rate_by_source_hash(
        self,
        venue: PredictionVenue,
        market_id: str | None,
        source_hash: str,
        as_of: datetime,
    ) -> PredictionFeeRate | None:
        rows = self._connection.execute(
            """
            SELECT venue, market_id, epoch_us(observed_at), record_json, record_hash
            FROM prediction_fee_rates
            WHERE (
                    (venue = ? AND market_id IS NOT DISTINCT FROM ?)
                    OR (
                        json_extract_string(record_json, '$.venue') = ?
                        AND json_extract_string(record_json, '$.market_id')
                            IS NOT DISTINCT FROM ?
                    )
                  )
              AND (
                    observed_at <= ?
                    OR CAST(
                        json_extract_string(record_json, '$.observed_at') AS TIMESTAMPTZ
                    ) <= ?
                  )
              AND json_extract_string(record_json, '$.source_hash') = ?
            ORDER BY observed_at DESC
            """,
            [venue.value, market_id, venue.value, market_id, as_of, as_of, source_hash],
        ).fetchall()
        records: list[PredictionFeeRate] = []
        for row in rows:
            record = _verified_record((row[3], row[4]), PredictionFeeRate, "fee rate")
            if record is None:
                continue
            indexed = (row[0], row[1], _utc_from_epoch_us(row[2]))
            decoded = (record.venue.value, record.market_id, record.observed_at)
            if indexed != decoded:
                raise ConflictingRecordError("stored fee rate indexed columns do not match")
            records.append(record)
        if not records:
            return None
        return max(records, key=lambda record: record.observed_at)

    def existing_candidate_ids(self) -> frozenset[UUID]:
        """Every ``candidate_id`` already persisted, with no ``as_of`` cutoff.

        Regenerating candidates at a later ``--as-of`` reproduces the same deterministic
        ``candidate_id`` but with a different ``observed_at``/``information_cutoff``
        (the new as_of). Callers must use this to skip re-appending an id that already
        exists rather than calling ``append_candidate_relationship`` and relying on it
        to raise ``ConflictingRecordError`` -- the first-observed record must stand
        unappended-over, not conflict the whole persisting transaction.
        """
        rows = self._connection.execute(
            "SELECT candidate_id FROM candidate_relationships"
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def existing_scan_report_ids(self) -> frozenset[UUID]:
        """Every ``report_id`` already persisted, with no ``as_of`` cutoff.

        Mirrors ``existing_candidate_ids``: ``deterministic_scan_report_id`` excludes
        ``observed_at``, so a report_id is stable for a given (candidate, decision,
        evidence, as_of) tuple. Callers must use this to pre-check and skip an id that
        already exists rather than relying on ``append_scan_report`` to raise
        ``ConflictingRecordError`` on a same-content retry -- that never actually
        happens here (identical content always hashes to the identical id), but a
        pre-fetched set lets a batch scan skip already-known reports without one
        round-trip query per candidate.
        """
        rows = self._connection.execute("SELECT report_id FROM scan_reports").fetchall()
        return frozenset(row[0] for row in rows)

    def candidate_relationships_as_of(self, as_of: datetime) -> tuple[CandidateRelationship, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM candidate_relationships
            WHERE observed_at <= ?
            ORDER BY observed_at, candidate_id
            """,
            [as_of],
        ).fetchall()
        return tuple(CandidateRelationship.model_validate_json(row[0]) for row in rows)

    def candidate_relationship_by_id(
        self, candidate_id: UUID, as_of: datetime
    ) -> CandidateRelationship | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM candidate_relationships
            WHERE candidate_id = ? AND observed_at <= ?
            LIMIT 1
            """,
            [candidate_id, as_of],
        ).fetchone()
        return None if row is None else CandidateRelationship.model_validate_json(row[0])

    def verified_candidate_relationship_by_id(
        self, candidate_id: UUID, as_of: datetime
    ) -> CandidateRelationship | None:
        row = self._connection.execute(
            """
            SELECT record_json, record_hash FROM candidate_relationships
            WHERE candidate_id = ? AND observed_at <= ?
            LIMIT 1
            """,
            [candidate_id, as_of],
        ).fetchone()
        return _verified_record(row, CandidateRelationship, "candidate relationship")

    def latest_attestation_for_rule_version(
        self, rule_version_id: UUID, as_of: datetime
    ) -> RuleAttestation | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM rule_attestations
            WHERE rule_version_id = ? AND reviewed_at <= ?
            ORDER BY reviewed_at DESC
            LIMIT 1
            """,
            [rule_version_id, as_of],
        ).fetchone()
        return None if row is None else RuleAttestation.model_validate_json(row[0])

    def proof_artifacts_for_candidate(
        self, candidate_id: UUID, as_of: datetime
    ) -> tuple[ProofArtifact, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM proof_artifacts
            WHERE candidate_id = ? AND observed_at <= ?
            ORDER BY observed_at, proof_id
            """,
            [candidate_id, as_of],
        ).fetchall()
        return tuple(ProofArtifact.model_validate_json(row[0]) for row in rows)

    def latest_proof_for_candidate(
        self, candidate_id: UUID, as_of: datetime
    ) -> ProofArtifact | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM proof_artifacts
            WHERE candidate_id = ? AND observed_at <= ?
            ORDER BY observed_at DESC, proof_id DESC
            LIMIT 1
            """,
            [candidate_id, as_of],
        ).fetchone()
        return None if row is None else ProofArtifact.model_validate_json(row[0])

    def proof_artifact_by_id(self, proof_id: UUID, as_of: datetime) -> ProofArtifact | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM proof_artifacts
            WHERE proof_id = ? AND observed_at <= ?
            LIMIT 1
            """,
            [proof_id, as_of],
        ).fetchone()
        return None if row is None else ProofArtifact.model_validate_json(row[0])

    def verified_proof_artifact_by_id(
        self, proof_id: UUID, as_of: datetime
    ) -> ProofArtifact | None:
        row = self._connection.execute(
            """
            SELECT record_json, record_hash FROM proof_artifacts
            WHERE proof_id = ? AND observed_at <= ?
            LIMIT 1
            """,
            [proof_id, as_of],
        ).fetchone()
        return _verified_record(row, ProofArtifact, "proof artifact")

    def proof_artifacts_as_of(self, as_of: datetime) -> tuple[ProofArtifact, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM proof_artifacts
            WHERE observed_at <= ?
            ORDER BY observed_at, proof_id
            """,
            [as_of],
        ).fetchall()
        return tuple(ProofArtifact.model_validate_json(row[0]) for row in rows)

    def scan_reports_as_of(self, as_of: datetime) -> tuple[ScanReport, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM scan_reports
            WHERE observed_at <= ?
            ORDER BY observed_at, report_id
            """,
            [as_of],
        ).fetchall()
        return tuple(ScanReport.model_validate_json(row[0]) for row in rows)

    def verified_scan_reports_as_of(self, as_of: datetime) -> tuple[ScanReport, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json, record_hash FROM scan_reports
            WHERE observed_at <= ?
            ORDER BY observed_at, report_id
            """,
            [as_of],
        ).fetchall()
        return tuple(
            record
            for row in rows
            if (record := _verified_record(row, ScanReport, "scan report")) is not None
        )

    def scan_report_by_id(self, report_id: UUID, as_of: datetime) -> ScanReport | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM scan_reports
            WHERE report_id = ? AND observed_at <= ?
            LIMIT 1
            """,
            [report_id, as_of],
        ).fetchone()
        return None if row is None else ScanReport.model_validate_json(row[0])

    def verified_scan_report_by_id(self, report_id: UUID, as_of: datetime) -> ScanReport | None:
        row = self._connection.execute(
            """
            SELECT record_json, record_hash FROM scan_reports
            WHERE report_id = ? AND observed_at <= ?
            LIMIT 1
            """,
            [report_id, as_of],
        ).fetchone()
        return _verified_record(row, ScanReport, "scan report")

    def shadow_plans_as_of(self, as_of: datetime) -> tuple[ShadowPlan, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM shadow_plans
            WHERE observed_at <= ?
            ORDER BY observed_at, proposal_id
            """,
            [as_of],
        ).fetchall()
        return tuple(ShadowPlan.model_validate_json(row[0]) for row in rows)

    def verified_shadow_plans_as_of(self, as_of: datetime) -> tuple[ShadowPlan, ...]:
        rows = self._connection.execute(
            """
            SELECT proposal_id, candidate_id, epoch_us(observed_at),
                   epoch_us(information_cutoff), record_json, record_hash
            FROM shadow_plans
            WHERE observed_at <= ?
               OR CAST(
                    json_extract_string(record_json, '$.observed_at') AS TIMESTAMPTZ
                  ) <= ?
            ORDER BY observed_at, proposal_id
            """,
            [as_of, as_of],
        ).fetchall()
        records: list[ShadowPlan] = []
        for row in rows:
            record = _verified_record((row[4], row[5]), ShadowPlan, "shadow plan")
            if record is None:
                continue
            indexed = (
                row[0],
                row[1],
                _utc_from_epoch_us(row[2]),
                _utc_from_epoch_us(row[3]),
            )
            decoded = (
                record.proposal_id,
                record.candidate_id,
                record.observed_at,
                record.information_cutoff,
            )
            if indexed != decoded:
                raise ConflictingRecordError("stored shadow plan indexed columns do not match")
            records.append(record)
        return tuple(sorted(records, key=lambda record: (record.observed_at, record.proposal_id)))

    def shadow_plan_by_proposal(self, proposal_id: UUID) -> ShadowPlan | None:
        row = self._connection.execute(
            "SELECT record_json FROM shadow_plans WHERE proposal_id = ?", [proposal_id]
        ).fetchone()
        return None if row is None else ShadowPlan.model_validate_json(row[0])

    def verified_shadow_plan_by_proposal(self, proposal_id: UUID) -> ShadowPlan | None:
        row = self._connection.execute(
            "SELECT record_json, record_hash FROM shadow_plans WHERE proposal_id = ?",
            [proposal_id],
        ).fetchone()
        return _verified_record(row, ShadowPlan, "shadow plan")

    def shadow_events_for_proposal(
        self, proposal_id: UUID, as_of: datetime
    ) -> tuple[ShadowEvent, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM shadow_events
            WHERE proposal_id = ? AND occurred_at <= ?
            ORDER BY sequence
            """,
            [proposal_id, as_of],
        ).fetchall()
        return tuple(ShadowEvent.model_validate_json(row[0]) for row in rows)

    def verified_shadow_events_for_proposal(
        self, proposal_id: UUID, as_of: datetime
    ) -> tuple[ShadowEvent, ...]:
        rows = self._connection.execute(
            """
            SELECT event_id, proposal_id, sequence, epoch_us(occurred_at),
                   record_json, record_hash
            FROM shadow_events
            WHERE (
                    proposal_id = ?
                    OR json_extract_string(record_json, '$.proposal_id') = ?
                  )
              AND (
                    occurred_at <= ?
                    OR CAST(
                        json_extract_string(record_json, '$.occurred_at') AS TIMESTAMPTZ
                    ) <= ?
                  )
            ORDER BY sequence
            """,
            [proposal_id, str(proposal_id), as_of, as_of],
        ).fetchall()
        records: list[ShadowEvent] = []
        for row in rows:
            record = _verified_record((row[4], row[5]), ShadowEvent, "shadow event")
            if record is None:
                continue
            indexed = (row[0], row[1], row[2], _utc_from_epoch_us(row[3]))
            decoded = (
                record.event_id,
                record.proposal_id,
                record.sequence,
                record.occurred_at,
            )
            if indexed != decoded:
                raise ConflictingRecordError("stored shadow event indexed columns do not match")
            records.append(record)
        return tuple(sorted(records, key=lambda record: record.sequence))

    def ledger_postings_for_proposal(
        self, proposal_id: UUID, as_of: datetime
    ) -> tuple[LedgerPosting, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM shadow_ledger_postings
            WHERE proposal_id = ? AND occurred_at <= ?
            ORDER BY occurred_at, event_id, posting_id
            """,
            [proposal_id, as_of],
        ).fetchall()
        return tuple(LedgerPosting.model_validate_json(row[0]) for row in rows)

    def verified_ledger_postings_for_proposal(
        self, proposal_id: UUID, as_of: datetime
    ) -> tuple[LedgerPosting, ...]:
        rows = self._connection.execute(
            """
            SELECT posting_id, proposal_id, event_id, epoch_us(occurred_at),
                   record_json, record_hash
            FROM shadow_ledger_postings
            WHERE (
                    proposal_id = ?
                    OR json_extract_string(record_json, '$.proposal_id') = ?
                  )
              AND (
                    occurred_at <= ?
                    OR CAST(
                        json_extract_string(record_json, '$.occurred_at') AS TIMESTAMPTZ
                    ) <= ?
                  )
            ORDER BY occurred_at, event_id, posting_id
            """,
            [proposal_id, str(proposal_id), as_of, as_of],
        ).fetchall()
        records: list[LedgerPosting] = []
        for row in rows:
            record = _verified_record((row[4], row[5]), LedgerPosting, "shadow ledger posting")
            if record is None:
                continue
            indexed = (row[0], row[1], row[2], _utc_from_epoch_us(row[3]))
            decoded = (
                record.posting_id,
                record.proposal_id,
                record.event_id,
                record.occurred_at,
            )
            if indexed != decoded:
                raise ConflictingRecordError(
                    "stored shadow ledger posting indexed columns do not match"
                )
            records.append(record)
        return tuple(
            sorted(
                records,
                key=lambda record: (record.occurred_at, record.event_id, record.posting_id),
            )
        )

    def latest_reconciliation_for_proposal(
        self, proposal_id: UUID, as_of: datetime
    ) -> ShadowReconciliation | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM shadow_reconciliations
            WHERE proposal_id = ? AND observed_at <= ?
            ORDER BY observed_at DESC, reconciliation_id DESC
            LIMIT 1
            """,
            [proposal_id, as_of],
        ).fetchone()
        return None if row is None else ShadowReconciliation.model_validate_json(row[0])

    def verified_shadow_reconciliations_for_proposal(
        self, proposal_id: UUID, as_of: datetime
    ) -> tuple[ShadowReconciliation, ...]:
        rows = self._connection.execute(
            """
            SELECT reconciliation_id, proposal_id, epoch_us(observed_at),
                   record_json, record_hash
            FROM shadow_reconciliations
            WHERE (
                    proposal_id = ?
                    OR json_extract_string(record_json, '$.proposal_id') = ?
                  )
              AND (
                    observed_at <= ?
                    OR CAST(
                        json_extract_string(record_json, '$.observed_at') AS TIMESTAMPTZ
                    ) <= ?
                  )
            ORDER BY observed_at, reconciliation_id
            """,
            [proposal_id, str(proposal_id), as_of, as_of],
        ).fetchall()
        records: list[ShadowReconciliation] = []
        for row in rows:
            record = _verified_record(
                (row[3], row[4]), ShadowReconciliation, "shadow reconciliation"
            )
            if record is None:
                continue
            indexed = (row[0], row[1], _utc_from_epoch_us(row[2]))
            decoded = (
                record.reconciliation_id,
                record.proposal_id,
                record.observed_at,
            )
            if indexed != decoded:
                raise ConflictingRecordError(
                    "stored shadow reconciliation indexed columns do not match"
                )
            records.append(record)
        return tuple(
            sorted(records, key=lambda record: (record.observed_at, record.reconciliation_id))
        )

    def trial_family_by_id(self, family_id: str, as_of: datetime) -> TrialFamily | None:
        row = self._connection.execute(
            """
            SELECT record_json FROM trial_families
            WHERE family_id = ? AND preregistered_at <= ?
            ORDER BY preregistered_at DESC
            LIMIT 1
            """,
            [family_id, as_of],
        ).fetchone()
        return None if row is None else TrialFamily.model_validate_json(row[0])

    def verified_trial_family_by_id(self, family_id: str, as_of: datetime) -> TrialFamily | None:
        row = self._connection.execute(
            """
            SELECT record_json, record_hash FROM trial_families
            WHERE family_id = ? AND preregistered_at <= ?
            ORDER BY preregistered_at DESC
            LIMIT 1
            """,
            [family_id, as_of],
        ).fetchone()
        return _verified_record(row, TrialFamily, "trial family")

    def trial_families_as_of(self, as_of: datetime) -> tuple[TrialFamily, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM trial_families
            WHERE preregistered_at <= ?
            ORDER BY preregistered_at, family_id
            """,
            [as_of],
        ).fetchall()
        return tuple(TrialFamily.model_validate_json(row[0]) for row in rows)

    def shadow_experiments_as_of(self, as_of: datetime) -> tuple[ShadowExperiment, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM shadow_experiments
            WHERE as_of <= ? AND observed_at <= ?
            ORDER BY observed_at, experiment_id
            """,
            [as_of, as_of],
        ).fetchall()
        return tuple(ShadowExperiment.model_validate_json(row[0]) for row in rows)

    def verified_shadow_experiments_as_of(self, as_of: datetime) -> tuple[ShadowExperiment, ...]:
        rows = self._connection.execute(
            """
            SELECT experiment_id, family_id, proposal_id, scenario_id, terminal_state,
                   reconciled, epoch_us(as_of), epoch_us(observed_at), record_json, record_hash
            FROM shadow_experiments
            WHERE (as_of <= ? AND observed_at <= ?)
               OR (
                    CAST(json_extract_string(record_json, '$.as_of') AS TIMESTAMPTZ) <= ?
                    AND CAST(
                        json_extract_string(record_json, '$.observed_at') AS TIMESTAMPTZ
                    ) <= ?
                  )
            ORDER BY observed_at, experiment_id
            """,
            [as_of, as_of, as_of, as_of],
        ).fetchall()
        return _verified_shadow_experiments(rows)

    def verified_shadow_experiments_for_proposal(
        self, proposal_id: UUID, as_of: datetime
    ) -> tuple[ShadowExperiment, ...]:
        rows = self._connection.execute(
            """
            SELECT experiment_id, family_id, proposal_id, scenario_id, terminal_state,
                   reconciled, epoch_us(as_of), epoch_us(observed_at), record_json, record_hash
            FROM shadow_experiments
            WHERE (
                    proposal_id = ?
                    OR json_extract_string(record_json, '$.proposal_id') = ?
                  )
              AND (
                    (as_of <= ? AND observed_at <= ?)
                    OR (
                        CAST(json_extract_string(record_json, '$.as_of') AS TIMESTAMPTZ) <= ?
                        AND CAST(
                            json_extract_string(record_json, '$.observed_at') AS TIMESTAMPTZ
                        ) <= ?
                    )
                  )
            ORDER BY observed_at, experiment_id
            """,
            [proposal_id, str(proposal_id), as_of, as_of, as_of, as_of],
        ).fetchall()
        return _verified_shadow_experiments(rows)

    def shadow_experiments_for_family(
        self, family_id: str, as_of: datetime
    ) -> tuple[ShadowExperiment, ...]:
        rows = self._connection.execute(
            """
            SELECT record_json FROM shadow_experiments
            WHERE family_id = ? AND as_of <= ? AND observed_at <= ?
            ORDER BY observed_at, experiment_id
            """,
            [family_id, as_of, as_of],
        ).fetchall()
        return tuple(ShadowExperiment.model_validate_json(row[0]) for row in rows)

    def evidence_counts_as_of(self, as_of: datetime) -> dict[str, int]:
        counts: dict[str, tuple[str, str]] = {
            "prediction_raw_envelopes": ("prediction_raw_envelopes", "observed_at"),
            "venue_manifests": ("venue_manifests", "reviewed_at"),
            "markets": ("markets", "retrieved_at"),
            "rule_versions": ("rule_versions", "effective_at"),
            "trades": ("trades", "observed_at"),
            "prediction_books": ("prediction_books", "observed_at"),
            "prediction_fee_rates": ("prediction_fee_rates", "observed_at"),
        }
        result: dict[str, int] = {}
        for name, (table, cutoff_column) in counts.items():
            # table/cutoff_column come from the hardcoded `counts` mapping above,
            # never externally-influenced identifiers — this is not safe if that changes.
            result[name] = self._connection.execute(
                f"SELECT count(*) FROM {table} WHERE {cutoff_column} <= ?", [as_of]
            ).fetchone()[0]
        return result

    # -- migrations ------------------------------------------------------

    def _apply_migrations(self) -> None:
        migrations = self._migration_entries()
        self._connection.execute("BEGIN TRANSACTION")
        try:
            self._require_prediction_market_database()
            applied = self._applied_migration_versions([version for version, _ in migrations])
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
        self._require_prediction_market_database()
        applied = self._applied_migration_versions(versions)
        if applied != versions:
            raise RuntimeError(
                f"read-only prediction-market store requires current schema {versions}; "
                f"found {applied}"
            )

    @staticmethod
    def _migration_entries() -> list[tuple[int, Any]]:
        migration_root = resources.files("polytrading.predictions.storage.schema")
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
        has_migration_table = self._has_table("schema_migrations")
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

    def _require_prediction_market_database(self) -> None:
        if self._has_table("schema_migrations") and not self._has_table(_FAMILY_SENTINEL_TABLE):
            raise RuntimeError(
                "prediction-market store cannot open a non-prediction-market database"
            )

    def _has_table(self, table_name: str) -> bool:
        return bool(
            self._connection.execute(
                """
                SELECT count(*)
                FROM information_schema.tables
                WHERE table_schema = 'main' AND table_name = ?
                """,
                [table_name],
            ).fetchone()[0]
        )
