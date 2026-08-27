from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from importlib import resources
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from uuid import UUID

import duckdb
from pydantic import BaseModel, ValidationError

from polytrading.predictions.attestations import RuleAttestation
from polytrading.predictions.candidates_models import CandidateRelationship
from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookSnapshot,
    PredictionFeeRate,
    PredictionRawEnvelope,
    PredictionRecord,
    PredictionVenue,
    RuleVersion,
    TradeRecord,
)
from polytrading.predictions.economics_models import ScanReport
from polytrading.predictions.execution.ledger import AuthoritativeTradeEconomics
from polytrading.predictions.execution.models import (
    ActivationEvidence,
    ExecutionIntent,
    KillSwitchEvent,
    LiveExecutionPlan,
    LiveLedgerPosting,
    LiveReconciliation,
    ProtocolConformanceResult,
    SignedOrderEnvelope,
    VenueOrderEvent,
    VenueOrderState,
    VenueTradeEvent,
    VenueTradeState,
    canonical_execution_hash,
)
from polytrading.predictions.experiments import ShadowExperiment, TrialFamily
from polytrading.predictions.manifest import VenueManifest
from polytrading.predictions.proofs_models import ProofArtifact
from polytrading.predictions.shadow_ledger import LedgerPosting, ShadowReconciliation
from polytrading.predictions.shadow_models import ShadowEvent, ShadowPlan

_MIGRATION_NAME = re.compile(r"(?P<version>[0-9]{3})_[a-z0-9_]+\.sql")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FAMILY_SENTINEL_TABLE = "prediction_raw_envelopes"
_MAX_VERIFIED_LIVE_ACCOUNT_RECORDS = 10_000


def _utc_from_epoch_us(value: int | None) -> datetime | None:
    if value is None:
        return None
    return _UNIX_EPOCH + timedelta(microseconds=value)


def _epoch_us(value: datetime) -> int:
    return (value - _UNIX_EPOCH) // timedelta(microseconds=1)


def _order_event_sort_key(record: VenueOrderEvent) -> tuple[datetime, bool, int, UUID]:
    return (
        record.received_at,
        record.sequence_number is None,
        record.sequence_number if record.sequence_number is not None else 0,
        record.event_id,
    )


class ConflictingRecordError(ValueError):
    """Raised when an immutable prediction-market identity is retried with different content."""


class _ExecutionOperationClaim(PredictionRecord):
    claim_key: str
    intent_id: UUID
    account_fingerprint: str
    operation: Literal["SUBMIT_INTENT", "FIRST_FILL_REVALIDATION"]
    occurrence_hash: str
    claimed_at: datetime


def _canonical_json(record: BaseModel) -> str:
    return json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _record_hash(record: BaseModel) -> str:
    return sha256(_canonical_json(record).encode()).hexdigest()


def _authoritative_trade_economics_from_json(payload: str) -> AuthoritativeTradeEconomics:
    raw = json.loads(payload)
    if type(raw) is not dict:
        raise ValueError("TRADE_ECONOMICS_INVALID") from None
    values = dict(raw)
    values["intent_id"] = UUID(values["intent_id"])
    for field in (
        "price",
        "size",
        "fee",
        "cash_quantum",
        "position_quantum",
        "realized_pnl",
    ):
        if values.get(field) is not None:
            values[field] = Decimal(values[field])
    values["balance_evidence_hashes"] = tuple(values["balance_evidence_hashes"])
    values["occurred_at"] = datetime.fromisoformat(values["occurred_at"])
    values["information_cutoff"] = datetime.fromisoformat(values["information_cutoff"])
    values["trade_state"] = VenueTradeState(values["trade_state"])
    values["settlement_state"] = VenueTradeState(values["settlement_state"])
    return AuthoritativeTradeEconomics.model_validate(values, strict=True)


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
        self._path = path
        self._read_only = read_only
        self._connection = duckdb.connect(str(path), read_only=read_only)
        self._in_transaction = False
        self._execution_claim_lock = Lock()
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

    def _claim_execution_operation(
        self,
        *,
        intent: ExecutionIntent,
        operation: Literal["SUBMIT_INTENT", "FIRST_FILL_REVALIDATION"],
        occurrence_hash: str,
        claimed_at: datetime,
    ) -> bool:
        if (
            type(intent) is not ExecutionIntent
            or type(occurrence_hash) is not str
            or len(occurrence_hash) != 64
            or any(character not in "0123456789abcdef" for character in occurrence_hash)
            or type(claimed_at) is not datetime
            or claimed_at.tzinfo is None
            or claimed_at.utcoffset() is None
        ):
            raise ValueError("EXECUTION_OPERATION_CLAIM_INVALID") from None
        claimed_at = claimed_at.astimezone(UTC)
        claim_key = sha256(f"{operation}|{intent.intent_id}".encode()).hexdigest()
        claim = _ExecutionOperationClaim(
            claim_key=claim_key,
            intent_id=intent.intent_id,
            account_fingerprint=intent.account_fingerprint,
            operation=operation,
            occurrence_hash=occurrence_hash,
            claimed_at=claimed_at,
        )

        with self._execution_claim_lock:
            if self._in_transaction:
                raise RuntimeError("execution operation claim requires its own transaction")
            if self._read_only:
                raise RuntimeError("execution operation claim requires a writable store")
            with duckdb.connect(str(self._path)) as claim_connection:
                try:
                    claim_connection.execute(
                        """
                        INSERT INTO execution_operation_claims (
                            claim_key, intent_id, account_fingerprint, operation,
                            occurrence_hash, claimed_at, record_json, record_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            claim.claim_key,
                            claim.intent_id,
                            claim.account_fingerprint,
                            claim.operation,
                            claim.occurrence_hash,
                            claim.claimed_at,
                            _canonical_json(claim),
                            canonical_execution_hash(claim),
                        ],
                    )
                    return True
                except duckdb.Error as error:
                    existing = self._verified_execution_operation_claim(
                        claim_key,
                        connection=claim_connection,
                    )
                    if existing is None:
                        raise ConflictingRecordError(
                            "execution operation claim insertion conflicted without "
                            "a verified winner"
                        ) from error
                    if (
                        existing.intent_id != claim.intent_id
                        or existing.account_fingerprint != claim.account_fingerprint
                        or existing.operation != claim.operation
                        or existing.occurrence_hash != claim.occurrence_hash
                    ):
                        raise ConflictingRecordError(
                            "conflicting execution operation claim for immutable identity"
                        ) from error
                    return False

    def _verified_execution_operation_claim(
        self,
        claim_key: str,
        *,
        connection: duckdb.DuckDBPyConnection | None = None,
    ) -> _ExecutionOperationClaim | None:
        selected = self._connection if connection is None else connection
        rows = selected.execute(
            """
            SELECT claim_key, CAST(intent_id AS VARCHAR), account_fingerprint,
                   operation, occurrence_hash, epoch_us(claimed_at), record_json, record_hash
            FROM execution_operation_claims
            WHERE claim_key = ? OR TRY(json_extract_string(record_json, '$.claim_key')) = ?
            """,
            [claim_key, claim_key],
        ).fetchall()
        records: list[_ExecutionOperationClaim] = []
        for row in rows:
            try:
                record = _ExecutionOperationClaim.model_validate_json(row[-2])
            except (TypeError, ValueError) as error:
                raise ConflictingRecordError(
                    "stored execution operation claim is invalid"
                ) from error
            if canonical_execution_hash(record) != row[-1]:
                raise ConflictingRecordError(
                    "stored execution operation claim failed its immutable record hash"
                )
            if tuple(row[:-2]) != (
                record.claim_key,
                str(record.intent_id),
                record.account_fingerprint,
                record.operation,
                record.occurrence_hash,
                _epoch_us(record.claimed_at),
            ):
                raise ConflictingRecordError(
                    "stored execution operation claim indexed columns do not match"
                )
            if record.claim_key == claim_key:
                records.append(record)
        if len(records) > 1:
            raise ConflictingRecordError("duplicate execution operation claim identity")
        return records[0] if records else None

    def claim_execution_intent_submission(
        self,
        intent: ExecutionIntent,
        claimed_at: datetime,
    ) -> bool:
        """Durably claim the one submission mutation for an immutable intent."""

        return self._claim_execution_operation(
            intent=intent,
            operation="SUBMIT_INTENT",
            occurrence_hash=intent.intent_fingerprint,
            claimed_at=claimed_at,
        )

    def claim_execution_first_fill(
        self,
        intent: ExecutionIntent,
        event: VenueOrderEvent,
        claimed_at: datetime,
    ) -> bool:
        """Durably claim the first-fill revalidation for an exact correlated event."""

        if (
            type(event) is not VenueOrderEvent
            or event.intent_id != intent.intent_id
            or event.normalized_state
            not in {VenueOrderState.PARTIALLY_FILLED, VenueOrderState.FILLED}
        ):
            raise ValueError("EXECUTION_FIRST_FILL_CLAIM_INVALID") from None
        return self._claim_execution_operation(
            intent=intent,
            operation="FIRST_FILL_REVALIDATION",
            occurrence_hash=canonical_execution_hash(event),
            claimed_at=claimed_at,
        )

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

    def append_execution_intent(self, record: ExecutionIntent) -> bool:
        record = self._validated_execution_record(
            record, ExecutionIntent, "execution_intents", "intent_id", record.intent_id
        )
        return self._append_hashed_record(
            table="execution_intents",
            identity_column="intent_id",
            identity=record.intent_id,
            columns=("intent_id", "plan_id", "account_fingerprint", "created_at", "deadline"),
            values=(
                record.intent_id,
                record.plan_id,
                record.account_fingerprint,
                record.created_at,
                record.deadline,
            ),
            record=record,
        )

    def append_live_execution_plan(self, record: LiveExecutionPlan) -> bool:
        record = self._validated_execution_record(
            record, LiveExecutionPlan, "live_execution_plans", "plan_id", record.plan_id
        )
        return self._append_hashed_record(
            table="live_execution_plans",
            identity_column="plan_id",
            identity=record.plan_id,
            columns=(
                "plan_id",
                "proposal_id",
                "account_fingerprint",
                "observed_at",
                "information_cutoff",
            ),
            values=(
                record.plan_id,
                record.proposal_id,
                record.account_fingerprint,
                record.observed_at,
                record.information_cutoff,
            ),
            record=record,
        )

    def append_signed_order_envelope(self, record: SignedOrderEnvelope) -> bool:
        record = self._validated_execution_record(
            record, SignedOrderEnvelope, "signed_order_envelopes", "intent_id", record.intent_id
        )
        return self._append_hashed_record(
            table="signed_order_envelopes",
            identity_column="intent_id",
            identity=record.intent_id,
            columns=("intent_id",),
            values=(record.intent_id,),
            record=record,
        )

    def append_venue_order_event(self, record: VenueOrderEvent) -> bool:
        record = self._validated_execution_record(
            record, VenueOrderEvent, "venue_order_events", "event_id", record.event_id
        )
        return self._append_hashed_record(
            table="venue_order_events",
            identity_column="event_id",
            identity=record.event_id,
            columns=("event_id", "intent_id", "received_at"),
            values=(record.event_id, record.intent_id, record.received_at),
            record=record,
        )

    def append_venue_trade_event(self, record: VenueTradeEvent) -> bool:
        record = self._validated_execution_record(
            record, VenueTradeEvent, "venue_trade_events", "trade_event_id", record.trade_event_id
        )
        return self._append_hashed_record(
            table="venue_trade_events",
            identity_column="trade_event_id",
            identity=record.trade_event_id,
            columns=("trade_event_id", "intent_id", "received_at"),
            values=(record.trade_event_id, record.intent_id, record.received_at),
            record=record,
        )

    def append_authoritative_trade_economics(self, record: AuthoritativeTradeEconomics) -> bool:
        try:
            exact = AuthoritativeTradeEconomics.model_validate(
                record.model_dump(mode="python"), strict=True
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("TRADE_ECONOMICS_INVALID") from error
        existing = self._verified_authoritative_trade_economics_identity(exact)
        if existing:
            if len(existing) != 1 or existing[0] != exact:
                raise ConflictingRecordError(
                    "conflicting authoritative_trade_economics record for immutable identity"
                ) from None
            return False
        try:
            self._connection.execute(
                """
                INSERT INTO authoritative_trade_economics (
                    economics_fingerprint, account_fingerprint, intent_id,
                    venue_order_id, venue_trade_id, trade_state, settlement_state,
                    occurred_at, information_cutoff, record_json, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    exact.economics_fingerprint,
                    exact.account_fingerprint,
                    exact.intent_id,
                    exact.venue_order_id,
                    exact.venue_trade_id,
                    exact.trade_state.value,
                    exact.settlement_state.value,
                    exact.occurred_at,
                    exact.information_cutoff,
                    _canonical_json(exact),
                    _record_hash(exact),
                ],
            )
        except duckdb.Error as error:
            winner = self._verified_authoritative_trade_economics_identity(exact)
            if len(winner) == 1 and winner[0] == exact:
                return False
            raise ConflictingRecordError(
                "authoritative_trade_economics insertion conflicted without a verified winner"
            ) from error
        return True

    def append_live_ledger_posting(self, record: LiveLedgerPosting) -> bool:
        record = self._validated_execution_record(
            record, LiveLedgerPosting, "live_ledger_postings", "posting_id", record.posting_id
        )
        return self._append_hashed_record(
            table="live_ledger_postings",
            identity_column="posting_id",
            identity=record.posting_id,
            columns=("posting_id", "account_fingerprint", "intent_id", "occurred_at"),
            values=(
                record.posting_id,
                record.account_fingerprint,
                record.intent_id,
                record.occurred_at,
            ),
            record=record,
        )

    def append_live_reconciliation(self, record: LiveReconciliation) -> bool:
        record = self._validated_execution_record(
            record,
            LiveReconciliation,
            "live_reconciliations",
            "reconciliation_id",
            record.reconciliation_id,
        )
        return self._append_hashed_record(
            table="live_reconciliations",
            identity_column="reconciliation_id",
            identity=record.reconciliation_id,
            columns=("reconciliation_id", "account_fingerprint", "observed_at"),
            values=(record.reconciliation_id, record.account_fingerprint, record.observed_at),
            record=record,
        )

    def append_kill_switch_event(self, record: KillSwitchEvent) -> bool:
        record = self._validated_execution_record(
            record, KillSwitchEvent, "execution_kill_events", "kill_event_id", record.kill_event_id
        )
        return self._append_hashed_record(
            table="execution_kill_events",
            identity_column="kill_event_id",
            identity=record.kill_event_id,
            columns=("kill_event_id", "scope", "occurred_at"),
            values=(record.kill_event_id, record.scope, record.occurred_at),
            record=record,
        )

    def append_activation_evidence(self, record: ActivationEvidence) -> bool:
        record = self._validated_execution_record(
            record,
            ActivationEvidence,
            "activation_evidence",
            "activation_evidence_id",
            record.activation_evidence_id,
        )
        return self._append_hashed_record(
            table="activation_evidence",
            identity_column="activation_evidence_id",
            identity=record.activation_evidence_id,
            columns=("activation_evidence_id", "capability_digest", "verified_at", "expires_at"),
            values=(
                record.activation_evidence_id,
                record.capability_digest,
                record.verified_at,
                record.expires_at,
            ),
            record=record,
        )

    def append_protocol_conformance_result(self, record: ProtocolConformanceResult) -> bool:
        record = self._validated_execution_record(
            record,
            ProtocolConformanceResult,
            "protocol_conformance_results",
            "conformance_result_id",
            record.conformance_result_id,
        )
        return self._append_hashed_record(
            table="protocol_conformance_results",
            identity_column="conformance_result_id",
            identity=record.conformance_result_id,
            columns=("conformance_result_id", "observed_at"),
            values=(record.conformance_result_id, record.observed_at),
            record=record,
        )

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

    def _validated_execution_record[RecordT: BaseModel](
        self,
        record: BaseModel,
        model: type[RecordT],
        table: str,
        identity_column: str,
        identity: UUID,
    ) -> RecordT:
        try:
            return model.model_validate(record.model_dump())
        except ValidationError:
            existing = self._connection.execute(
                f"SELECT record_hash FROM {table} WHERE {identity_column} = ?", [identity]
            ).fetchone()
            if existing is not None and canonical_execution_hash(record) != existing[0]:
                raise ConflictingRecordError(
                    f"conflicting {table} record for immutable identity {identity}"
                ) from None
            raise

    def _append_hashed_record(
        self,
        *,
        table: str,
        identity_column: str,
        identity: UUID,
        columns: tuple[str, ...],
        values: tuple[Any, ...],
        record: BaseModel,
    ) -> bool:
        existing = self._connection.execute(
            f"SELECT record_hash FROM {table} WHERE {identity_column} = ?", [identity]
        ).fetchone()
        record_hash = canonical_execution_hash(record)
        if existing is not None:
            if record_hash != existing[0]:
                raise ConflictingRecordError(
                    f"conflicting {table} record for immutable identity {identity}"
                )
            return False
        insert_columns = ", ".join((*columns, "record_json", "record_hash"))
        placeholders = ", ".join("?" for _ in range(len(columns) + 2))
        self._connection.execute(
            f"INSERT INTO {table} ({insert_columns}) VALUES ({placeholders})",
            [*values, _canonical_json(record), record_hash],
        )
        return True

    def _verified_records[RecordT: BaseModel](
        self,
        *,
        table: str,
        model: type[RecordT],
        candidate_where: str,
        candidate_parameters: tuple[Any, ...],
        index_columns: tuple[str, ...],
        indexed_values: Callable[[RecordT], tuple[Any, ...]],
        matches: Callable[[RecordT], bool],
        sort_key: Callable[[RecordT], Any],
        reverse: bool = False,
        decode: Callable[[str], RecordT] | None = None,
        maximum_records: int | None = None,
    ) -> tuple[RecordT, ...]:
        # SQL narrows candidates only. Every returned row is strictly re-parsed, hashed,
        # and compared to its indexed identity/account/timestamps before model semantics
        # decide whether it belongs in the result.
        selected_indexes = ", ".join(index_columns)
        limit = "" if maximum_records is None else f" LIMIT {maximum_records + 1}"
        rows = self._connection.execute(
            f"SELECT {selected_indexes}, record_json, record_hash "
            f"FROM {table} WHERE {candidate_where}{limit}",
            candidate_parameters,
        ).fetchall()
        if maximum_records is not None and len(rows) > maximum_records:
            raise ConflictingRecordError(f"stored {table} exceeds the verified record limit")
        records: list[RecordT] = []
        for row in rows:
            try:
                record = model.model_validate_json(row[-2]) if decode is None else decode(row[-2])
            except (TypeError, ValueError) as error:
                raise ConflictingRecordError(f"stored {table} record is invalid") from error
            if _record_hash(record) != row[-1]:
                raise ConflictingRecordError(f"stored {table} failed its immutable record hash")
            if tuple(row[:-2]) != indexed_values(record):
                raise ConflictingRecordError(f"stored {table} indexed columns do not match")
            if matches(record):
                records.append(record)
        return tuple(sorted(records, key=sort_key, reverse=reverse))

    def verified_live_execution_account_fingerprints(
        self,
        as_of: datetime,
    ) -> tuple[str, ...]:
        """Discover account history only after complete relational revalidation."""

        plans = self._verified_records(
            table="live_execution_plans",
            model=LiveExecutionPlan,
            candidate_where=(
                "(observed_at <= ? OR TRY_CAST(TRY(json_extract_string(record_json, "
                "'$.observed_at')) AS TIMESTAMPTZ) <= ?) AND "
                "(information_cutoff <= ? OR TRY_CAST(TRY(json_extract_string(record_json, "
                "'$.information_cutoff')) AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(as_of, as_of, as_of, as_of),
            index_columns=(
                "CAST(plan_id AS VARCHAR)",
                "CAST(proposal_id AS VARCHAR)",
                "account_fingerprint",
                "epoch_us(observed_at)",
                "epoch_us(information_cutoff)",
            ),
            indexed_values=lambda record: (
                str(record.plan_id),
                str(record.proposal_id),
                record.account_fingerprint,
                _epoch_us(record.observed_at),
                _epoch_us(record.information_cutoff),
            ),
            matches=lambda record: (
                record.observed_at <= as_of and record.information_cutoff <= as_of
            ),
            sort_key=lambda record: (record.observed_at, record.plan_id),
            maximum_records=_MAX_VERIFIED_LIVE_ACCOUNT_RECORDS,
        )
        intents = self._verified_records(
            table="execution_intents",
            model=ExecutionIntent,
            candidate_where=(
                "created_at <= ? OR TRY_CAST(TRY(json_extract_string(record_json, "
                "'$.created_at')) AS TIMESTAMPTZ) <= ?"
            ),
            candidate_parameters=(as_of, as_of),
            index_columns=(
                "CAST(intent_id AS VARCHAR)",
                "CAST(plan_id AS VARCHAR)",
                "account_fingerprint",
                "epoch_us(created_at)",
                "epoch_us(deadline)",
            ),
            indexed_values=lambda record: (
                str(record.intent_id),
                str(record.plan_id),
                record.account_fingerprint,
                _epoch_us(record.created_at),
                _epoch_us(record.deadline),
            ),
            matches=lambda record: record.created_at <= as_of,
            sort_key=lambda record: (record.created_at, record.intent_id),
            maximum_records=_MAX_VERIFIED_LIVE_ACCOUNT_RECORDS,
        )
        economics = self._verified_records(
            table="authoritative_trade_economics",
            model=AuthoritativeTradeEconomics,
            candidate_where=(
                "(occurred_at <= ? OR TRY_CAST(TRY(json_extract_string(record_json, "
                "'$.occurred_at')) AS TIMESTAMPTZ) <= ?) AND "
                "(information_cutoff <= ? OR TRY_CAST(TRY(json_extract_string(record_json, "
                "'$.information_cutoff')) AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(as_of, as_of, as_of, as_of),
            index_columns=(
                "economics_fingerprint",
                "account_fingerprint",
                "CAST(intent_id AS VARCHAR)",
                "venue_order_id",
                "venue_trade_id",
                "trade_state",
                "settlement_state",
                "epoch_us(occurred_at)",
                "epoch_us(information_cutoff)",
            ),
            indexed_values=self._authoritative_economics_indexed_values,
            matches=lambda record: (
                record.occurred_at <= as_of and record.information_cutoff <= as_of
            ),
            sort_key=lambda record: (
                record.occurred_at,
                record.venue_trade_id,
                record.economics_fingerprint,
            ),
            decode=_authoritative_trade_economics_from_json,
            maximum_records=_MAX_VERIFIED_LIVE_ACCOUNT_RECORDS,
        )
        postings = self._verified_records(
            table="live_ledger_postings",
            model=LiveLedgerPosting,
            candidate_where=(
                "occurred_at <= ? OR TRY_CAST(TRY(json_extract_string(record_json, "
                "'$.occurred_at')) AS TIMESTAMPTZ) <= ?"
            ),
            candidate_parameters=(as_of, as_of),
            index_columns=(
                "CAST(posting_id AS VARCHAR)",
                "account_fingerprint",
                "CAST(intent_id AS VARCHAR)",
                "epoch_us(occurred_at)",
            ),
            indexed_values=lambda record: (
                str(record.posting_id),
                record.account_fingerprint,
                None if record.intent_id is None else str(record.intent_id),
                _epoch_us(record.occurred_at),
            ),
            matches=lambda record: record.occurred_at <= as_of,
            sort_key=lambda record: (record.occurred_at, record.posting_id),
            maximum_records=_MAX_VERIFIED_LIVE_ACCOUNT_RECORDS,
        )
        reconciliations = self._verified_records(
            table="live_reconciliations",
            model=LiveReconciliation,
            candidate_where=(
                "observed_at <= ? OR TRY_CAST(TRY(json_extract_string(record_json, "
                "'$.observed_at')) AS TIMESTAMPTZ) <= ?"
            ),
            candidate_parameters=(as_of, as_of),
            index_columns=(
                "CAST(reconciliation_id AS VARCHAR)",
                "account_fingerprint",
                "epoch_us(observed_at)",
            ),
            indexed_values=lambda record: (
                str(record.reconciliation_id),
                record.account_fingerprint,
                _epoch_us(record.observed_at),
            ),
            matches=lambda record: record.observed_at <= as_of,
            sort_key=lambda record: (record.observed_at, record.reconciliation_id),
            maximum_records=_MAX_VERIFIED_LIVE_ACCOUNT_RECORDS,
        )

        plans_by_id = {record.plan_id: record for record in plans}
        intents_by_id = {record.intent_id: record for record in intents}
        accounts = {record.account_fingerprint for record in plans}
        for intent in intents:
            plan = plans_by_id.get(intent.plan_id)
            if plan is None:
                raise ConflictingRecordError("orphan execution intent has no visible plan")
            if plan.account_fingerprint != intent.account_fingerprint:
                raise ConflictingRecordError("execution plan and intent account mismatch")
            accounts.add(intent.account_fingerprint)
        for record in economics:
            intent = intents_by_id.get(record.intent_id)
            if intent is None:
                raise ConflictingRecordError("orphan trade economics has no visible intent")
            if intent.account_fingerprint != record.account_fingerprint:
                raise ConflictingRecordError("trade economics and intent account mismatch")
            accounts.add(record.account_fingerprint)
        for posting in postings:
            intent = None if posting.intent_id is None else intents_by_id.get(posting.intent_id)
            if intent is None:
                raise ConflictingRecordError("orphan live ledger posting has no visible intent")
            if intent.account_fingerprint != posting.account_fingerprint:
                raise ConflictingRecordError("live ledger posting and intent account mismatch")
            accounts.add(posting.account_fingerprint)
        for reconciliation in reconciliations:
            if reconciliation.account_fingerprint not in accounts:
                raise ConflictingRecordError(
                    "orphan live reconciliation has no visible execution account"
                )
        return tuple(sorted(accounts))

    @staticmethod
    def _authoritative_economics_indexed_values(
        record: AuthoritativeTradeEconomics,
    ) -> tuple[Any, ...]:
        return (
            record.economics_fingerprint,
            record.account_fingerprint,
            str(record.intent_id),
            record.venue_order_id,
            record.venue_trade_id,
            record.trade_state.value,
            record.settlement_state.value,
            _epoch_us(record.occurred_at),
            _epoch_us(record.information_cutoff),
        )

    def _verified_authoritative_trade_economics_identity(
        self,
        record: AuthoritativeTradeEconomics,
    ) -> tuple[AuthoritativeTradeEconomics, ...]:
        return self._verified_records(
            table="authoritative_trade_economics",
            model=AuthoritativeTradeEconomics,
            candidate_where=(
                "economics_fingerprint = ? OR "
                "TRY(json_extract_string(record_json, '$.economics_fingerprint')) = ? OR "
                "((account_fingerprint = ? OR TRY(json_extract_string(record_json, "
                "'$.account_fingerprint')) = ?) AND "
                "(venue_trade_id = ? OR TRY(json_extract_string(record_json, "
                "'$.venue_trade_id')) = ?))"
            ),
            candidate_parameters=(
                record.economics_fingerprint,
                record.economics_fingerprint,
                record.account_fingerprint,
                record.account_fingerprint,
                record.venue_trade_id,
                record.venue_trade_id,
            ),
            index_columns=(
                "economics_fingerprint",
                "account_fingerprint",
                "CAST(intent_id AS VARCHAR)",
                "venue_order_id",
                "venue_trade_id",
                "trade_state",
                "settlement_state",
                "epoch_us(occurred_at)",
                "epoch_us(information_cutoff)",
            ),
            indexed_values=self._authoritative_economics_indexed_values,
            matches=lambda candidate: (
                candidate.economics_fingerprint == record.economics_fingerprint
                or (
                    candidate.account_fingerprint == record.account_fingerprint
                    and candidate.venue_trade_id == record.venue_trade_id
                )
            ),
            sort_key=lambda candidate: (
                candidate.occurred_at,
                candidate.venue_order_id,
                candidate.venue_trade_id,
                candidate.economics_fingerprint,
            ),
            decode=_authoritative_trade_economics_from_json,
        )

    def verified_authoritative_trade_economics_for_account(
        self,
        account_fingerprint: str,
        as_of: datetime,
    ) -> tuple[AuthoritativeTradeEconomics, ...]:
        return self._verified_authoritative_trade_economics(
            identity_column="account_fingerprint",
            identity_json_field="account_fingerprint",
            identity=account_fingerprint,
            as_of=as_of,
            matches=lambda record: record.account_fingerprint == account_fingerprint,
        )

    def verified_authoritative_trade_economics_for_intent(
        self,
        intent_id: UUID,
        as_of: datetime,
    ) -> tuple[AuthoritativeTradeEconomics, ...]:
        return self._verified_authoritative_trade_economics(
            identity_column="intent_id",
            identity_json_field="intent_id",
            identity=intent_id,
            as_of=as_of,
            matches=lambda record: record.intent_id == intent_id,
        )

    def _verified_authoritative_trade_economics(
        self,
        *,
        identity_column: str,
        identity_json_field: str,
        identity: str | UUID,
        as_of: datetime,
        matches: Callable[[AuthoritativeTradeEconomics], bool],
    ) -> tuple[AuthoritativeTradeEconomics, ...]:
        return self._verified_records(
            table="authoritative_trade_economics",
            model=AuthoritativeTradeEconomics,
            candidate_where=(
                f"({identity_column} = ? OR TRY(json_extract_string(record_json, "
                f"'$.{identity_json_field}')) = ?) AND "
                "(occurred_at <= ? OR TRY_CAST(TRY(json_extract_string(record_json, "
                "'$.occurred_at')) AS TIMESTAMPTZ) <= ?) AND "
                "(information_cutoff <= ? OR TRY_CAST(TRY(json_extract_string(record_json, "
                "'$.information_cutoff')) AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(
                identity,
                str(identity),
                as_of,
                as_of,
                as_of,
                as_of,
            ),
            index_columns=(
                "economics_fingerprint",
                "account_fingerprint",
                "CAST(intent_id AS VARCHAR)",
                "venue_order_id",
                "venue_trade_id",
                "trade_state",
                "settlement_state",
                "epoch_us(occurred_at)",
                "epoch_us(information_cutoff)",
            ),
            indexed_values=self._authoritative_economics_indexed_values,
            matches=lambda record: (
                matches(record)
                and record.occurred_at <= as_of
                and record.information_cutoff <= as_of
            ),
            sort_key=lambda record: (
                record.occurred_at,
                record.venue_order_id,
                record.venue_trade_id,
                record.economics_fingerprint,
            ),
            decode=_authoritative_trade_economics_from_json,
        )

    def verified_live_execution_plan(
        self, plan_id: UUID, as_of: datetime | None = None
    ) -> LiveExecutionPlan | None:
        records = self._verified_records(
            table="live_execution_plans",
            model=LiveExecutionPlan,
            candidate_where=(
                "(plan_id = ? OR json_extract_string(record_json, '$.plan_id') = ?) "
                "AND (observed_at <= ? OR CAST(json_extract_string(record_json, "
                "'$.observed_at') AS TIMESTAMPTZ) <= ?)"
                if as_of is not None
                else "plan_id = ? OR json_extract_string(record_json, '$.plan_id') = ?"
            ),
            candidate_parameters=(plan_id, str(plan_id), as_of, as_of)
            if as_of is not None
            else (plan_id, str(plan_id)),
            index_columns=(
                "CAST(plan_id AS VARCHAR)",
                "CAST(proposal_id AS VARCHAR)",
                "account_fingerprint",
                "epoch_us(observed_at)",
                "epoch_us(information_cutoff)",
            ),
            indexed_values=lambda record: (
                str(record.plan_id),
                str(record.proposal_id),
                record.account_fingerprint,
                _epoch_us(record.observed_at),
                _epoch_us(record.information_cutoff),
            ),
            matches=lambda record: (
                record.plan_id == plan_id
                and (
                    as_of is None
                    or (record.observed_at <= as_of and record.information_cutoff <= as_of)
                )
            ),
            sort_key=lambda record: (record.observed_at, record.plan_id),
        )
        return records[0] if records else None

    def verified_live_execution_plans_for_account(
        self,
        account_fingerprint: str,
        as_of: datetime,
    ) -> tuple[LiveExecutionPlan, ...]:
        """Return all strictly verified account plans visible at one cutoff."""

        return self._verified_records(
            table="live_execution_plans",
            model=LiveExecutionPlan,
            candidate_where=(
                "(account_fingerprint = ? OR json_extract_string(record_json, "
                "'$.account_fingerprint') = ?) AND (observed_at <= ? OR "
                "CAST(json_extract_string(record_json, '$.observed_at') AS TIMESTAMPTZ) <= ?) "
                "AND (information_cutoff <= ? OR CAST(json_extract_string(record_json, "
                "'$.information_cutoff') AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(
                account_fingerprint,
                account_fingerprint,
                as_of,
                as_of,
                as_of,
                as_of,
            ),
            index_columns=(
                "CAST(plan_id AS VARCHAR)",
                "CAST(proposal_id AS VARCHAR)",
                "account_fingerprint",
                "epoch_us(observed_at)",
                "epoch_us(information_cutoff)",
            ),
            indexed_values=lambda record: (
                str(record.plan_id),
                str(record.proposal_id),
                record.account_fingerprint,
                _epoch_us(record.observed_at),
                _epoch_us(record.information_cutoff),
            ),
            matches=lambda record: (
                record.account_fingerprint == account_fingerprint
                and record.observed_at <= as_of
                and record.information_cutoff <= as_of
            ),
            sort_key=lambda record: (record.observed_at, record.plan_id),
        )

    def verified_execution_intent(
        self, intent_id: UUID, as_of: datetime | None = None
    ) -> ExecutionIntent | None:
        records = self._verified_records(
            table="execution_intents",
            model=ExecutionIntent,
            candidate_where=(
                "(intent_id = ? OR json_extract_string(record_json, '$.intent_id') = ?) "
                "AND (created_at <= ? OR CAST(json_extract_string(record_json, "
                "'$.created_at') AS TIMESTAMPTZ) <= ?) "
                "AND (deadline >= ? OR CAST(json_extract_string(record_json, "
                "'$.deadline') AS TIMESTAMPTZ) >= ?)"
                if as_of is not None
                else "intent_id = ? OR json_extract_string(record_json, '$.intent_id') = ?"
            ),
            candidate_parameters=(intent_id, str(intent_id), as_of, as_of, as_of, as_of)
            if as_of is not None
            else (intent_id, str(intent_id)),
            index_columns=(
                "CAST(intent_id AS VARCHAR)",
                "CAST(plan_id AS VARCHAR)",
                "account_fingerprint",
                "epoch_us(created_at)",
                "epoch_us(deadline)",
            ),
            indexed_values=lambda record: (
                str(record.intent_id),
                str(record.plan_id),
                record.account_fingerprint,
                _epoch_us(record.created_at),
                _epoch_us(record.deadline),
            ),
            matches=lambda record: (
                record.intent_id == intent_id
                and (as_of is None or (record.created_at <= as_of and record.deadline >= as_of))
            ),
            sort_key=lambda record: (record.created_at, record.intent_id),
        )
        return records[0] if records else None

    def verified_execution_intents_for_plan(
        self, plan_id: UUID, as_of: datetime
    ) -> tuple[ExecutionIntent, ...]:
        return self._verified_records(
            table="execution_intents",
            model=ExecutionIntent,
            candidate_where=(
                "(plan_id = ? OR json_extract_string(record_json, '$.plan_id') = ?) "
                "AND (created_at <= ? OR CAST(json_extract_string(record_json, "
                "'$.created_at') AS TIMESTAMPTZ) <= ?) "
                "AND (deadline >= ? OR CAST(json_extract_string(record_json, "
                "'$.deadline') AS TIMESTAMPTZ) >= ?)"
            ),
            candidate_parameters=(plan_id, str(plan_id), as_of, as_of, as_of, as_of),
            index_columns=(
                "CAST(intent_id AS VARCHAR)",
                "CAST(plan_id AS VARCHAR)",
                "account_fingerprint",
                "epoch_us(created_at)",
                "epoch_us(deadline)",
            ),
            indexed_values=lambda record: (
                str(record.intent_id),
                str(record.plan_id),
                record.account_fingerprint,
                _epoch_us(record.created_at),
                _epoch_us(record.deadline),
            ),
            matches=lambda record: (
                record.plan_id == plan_id
                and record.created_at <= as_of
                and record.deadline >= as_of
            ),
            sort_key=lambda record: (record.created_at, record.intent_id),
        )

    def verified_execution_intent_history_for_plan(
        self,
        plan_id: UUID,
        as_of: datetime,
    ) -> tuple[ExecutionIntent, ...]:
        """Return verified intent history without dropping expired unresolved work."""

        return self._verified_records(
            table="execution_intents",
            model=ExecutionIntent,
            candidate_where=(
                "(plan_id = ? OR json_extract_string(record_json, '$.plan_id') = ?) "
                "AND (created_at <= ? OR CAST(json_extract_string(record_json, "
                "'$.created_at') AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(plan_id, str(plan_id), as_of, as_of),
            index_columns=(
                "CAST(intent_id AS VARCHAR)",
                "CAST(plan_id AS VARCHAR)",
                "account_fingerprint",
                "epoch_us(created_at)",
                "epoch_us(deadline)",
            ),
            indexed_values=lambda record: (
                str(record.intent_id),
                str(record.plan_id),
                record.account_fingerprint,
                _epoch_us(record.created_at),
                _epoch_us(record.deadline),
            ),
            matches=lambda record: record.plan_id == plan_id and record.created_at <= as_of,
            sort_key=lambda record: (record.created_at, record.intent_id),
        )

    def verified_execution_intent_history_for_account(
        self,
        account_fingerprint: str,
        as_of: datetime,
    ) -> tuple[ExecutionIntent, ...]:
        """Return verified account intent history independently of plan discovery."""

        return self._verified_records(
            table="execution_intents",
            model=ExecutionIntent,
            candidate_where=(
                "(account_fingerprint = ? OR TRY(json_extract_string(record_json, "
                "'$.account_fingerprint')) = ?) AND (created_at <= ? OR "
                "TRY_CAST(TRY(json_extract_string(record_json, '$.created_at')) "
                "AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(account_fingerprint, account_fingerprint, as_of, as_of),
            index_columns=(
                "CAST(intent_id AS VARCHAR)",
                "CAST(plan_id AS VARCHAR)",
                "account_fingerprint",
                "epoch_us(created_at)",
                "epoch_us(deadline)",
            ),
            indexed_values=lambda record: (
                str(record.intent_id),
                str(record.plan_id),
                record.account_fingerprint,
                _epoch_us(record.created_at),
                _epoch_us(record.deadline),
            ),
            matches=lambda record: (
                record.account_fingerprint == account_fingerprint and record.created_at <= as_of
            ),
            sort_key=lambda record: (record.created_at, record.intent_id),
        )

    def verified_signed_order_envelope(
        self, intent_id: UUID, as_of: datetime | None = None
    ) -> SignedOrderEnvelope | None:
        candidate_where = "intent_id = ? OR json_extract_string(record_json, '$.intent_id') = ?"
        candidate_parameters: tuple[Any, ...] = (intent_id, str(intent_id))
        if as_of is not None:
            candidate_where = f"({candidate_where}) AND persisted_at <= ?"
            candidate_parameters += (as_of,)
        records = self._verified_records(
            table="signed_order_envelopes",
            model=SignedOrderEnvelope,
            candidate_where=candidate_where,
            candidate_parameters=candidate_parameters,
            index_columns=("CAST(intent_id AS VARCHAR)",),
            indexed_values=lambda record: (str(record.intent_id),),
            matches=lambda record: record.intent_id == intent_id,
            sort_key=lambda record: record.intent_id,
        )
        return records[0] if records else None

    def verified_venue_order_events_for_intent(
        self, intent_id: UUID, as_of: datetime
    ) -> tuple[VenueOrderEvent, ...]:
        return self._verified_records(
            table="venue_order_events",
            model=VenueOrderEvent,
            candidate_where=(
                "(intent_id = ? OR json_extract_string(record_json, '$.intent_id') = ?) "
                "AND (received_at <= ? OR CAST(json_extract_string(record_json, "
                "'$.received_at') AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(intent_id, str(intent_id), as_of, as_of),
            index_columns=(
                "CAST(event_id AS VARCHAR)",
                "CAST(intent_id AS VARCHAR)",
                "epoch_us(received_at)",
            ),
            indexed_values=lambda record: (
                str(record.event_id),
                None if record.intent_id is None else str(record.intent_id),
                _epoch_us(record.received_at),
            ),
            matches=lambda record: record.intent_id == intent_id and record.received_at <= as_of,
            sort_key=_order_event_sort_key,
        )

    def latest_order_state(
        self, intent_id: UUID, as_of: datetime | None = None
    ) -> VenueOrderEvent | None:
        candidate_where = "intent_id = ? OR json_extract_string(record_json, '$.intent_id') = ?"
        candidate_parameters: tuple[Any, ...] = (intent_id, str(intent_id))
        if as_of is not None:
            candidate_where = (
                f"({candidate_where}) AND (received_at <= ? OR "
                "CAST(json_extract_string(record_json, '$.received_at') AS TIMESTAMPTZ) <= ?)"
            )
            candidate_parameters += (as_of, as_of)
        records = self._verified_records(
            table="venue_order_events",
            model=VenueOrderEvent,
            candidate_where=candidate_where,
            candidate_parameters=candidate_parameters,
            index_columns=(
                "CAST(event_id AS VARCHAR)",
                "CAST(intent_id AS VARCHAR)",
                "epoch_us(received_at)",
            ),
            indexed_values=lambda record: (
                str(record.event_id),
                None if record.intent_id is None else str(record.intent_id),
                _epoch_us(record.received_at),
            ),
            matches=lambda record: (
                record.intent_id == intent_id and (as_of is None or record.received_at <= as_of)
            ),
            sort_key=_order_event_sort_key,
            reverse=True,
        )
        return records[0] if records else None

    def verified_venue_trade_events_for_intent(
        self, intent_id: UUID, as_of: datetime
    ) -> tuple[VenueTradeEvent, ...]:
        return self._verified_records(
            table="venue_trade_events",
            model=VenueTradeEvent,
            candidate_where=(
                "(intent_id = ? OR json_extract_string(record_json, '$.intent_id') = ?) "
                "AND (received_at <= ? OR CAST(json_extract_string(record_json, "
                "'$.received_at') AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(intent_id, str(intent_id), as_of, as_of),
            index_columns=(
                "CAST(trade_event_id AS VARCHAR)",
                "CAST(intent_id AS VARCHAR)",
                "epoch_us(received_at)",
            ),
            indexed_values=lambda record: (
                str(record.trade_event_id),
                None if record.intent_id is None else str(record.intent_id),
                _epoch_us(record.received_at),
            ),
            matches=lambda record: record.intent_id == intent_id and record.received_at <= as_of,
            sort_key=lambda record: (record.received_at, record.trade_event_id),
        )

    def verified_live_ledger_postings_for_account(
        self, account_fingerprint: str, as_of: datetime
    ) -> tuple[LiveLedgerPosting, ...]:
        return self._verified_records(
            table="live_ledger_postings",
            model=LiveLedgerPosting,
            candidate_where=(
                "(account_fingerprint = ? OR json_extract_string(record_json, "
                "'$.account_fingerprint') = ?) AND (occurred_at <= ? OR "
                "CAST(json_extract_string(record_json, '$.occurred_at') AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(account_fingerprint, account_fingerprint, as_of, as_of),
            index_columns=(
                "CAST(posting_id AS VARCHAR)",
                "account_fingerprint",
                "CAST(intent_id AS VARCHAR)",
                "epoch_us(occurred_at)",
            ),
            indexed_values=lambda record: (
                str(record.posting_id),
                record.account_fingerprint,
                None if record.intent_id is None else str(record.intent_id),
                _epoch_us(record.occurred_at),
            ),
            matches=lambda record: (
                record.account_fingerprint == account_fingerprint and record.occurred_at <= as_of
            ),
            sort_key=lambda record: (record.occurred_at, record.posting_id),
        )

    def verified_live_reconciliations_for_account(
        self, account_fingerprint: str, as_of: datetime
    ) -> tuple[LiveReconciliation, ...]:
        return self._verified_records(
            table="live_reconciliations",
            model=LiveReconciliation,
            candidate_where=(
                "(account_fingerprint = ? OR json_extract_string(record_json, "
                "'$.account_fingerprint') = ?) AND (observed_at <= ? OR "
                "CAST(json_extract_string(record_json, '$.observed_at') AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(account_fingerprint, account_fingerprint, as_of, as_of),
            index_columns=(
                "CAST(reconciliation_id AS VARCHAR)",
                "account_fingerprint",
                "epoch_us(observed_at)",
            ),
            indexed_values=lambda record: (
                str(record.reconciliation_id),
                record.account_fingerprint,
                _epoch_us(record.observed_at),
            ),
            matches=lambda record: (
                record.account_fingerprint == account_fingerprint and record.observed_at <= as_of
            ),
            sort_key=lambda record: (record.observed_at, record.reconciliation_id),
        )

    def verified_kill_switch_events(
        self, scope: str, as_of: datetime
    ) -> tuple[KillSwitchEvent, ...]:
        return self._verified_records(
            table="execution_kill_events",
            model=KillSwitchEvent,
            candidate_where=(
                "(scope = ? OR json_extract_string(record_json, '$.scope') = ?) "
                "AND (occurred_at <= ? OR CAST(json_extract_string(record_json, "
                "'$.occurred_at') AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(scope, scope, as_of, as_of),
            index_columns=(
                "CAST(kill_event_id AS VARCHAR)",
                "scope",
                "epoch_us(occurred_at)",
            ),
            indexed_values=lambda record: (
                str(record.kill_event_id),
                record.scope,
                _epoch_us(record.occurred_at),
            ),
            matches=lambda record: record.scope == scope and record.occurred_at <= as_of,
            sort_key=lambda record: (record.occurred_at, record.kill_event_id),
        )

    def verified_activation_evidence(
        self, capability_digest: str, as_of: datetime
    ) -> ActivationEvidence | None:
        records = self._verified_records(
            table="activation_evidence",
            model=ActivationEvidence,
            candidate_where=(
                "(capability_digest = ? OR json_extract_string(record_json, "
                "'$.capability_digest') = ?) AND (verified_at <= ? OR "
                "CAST(json_extract_string(record_json, '$.verified_at') AS TIMESTAMPTZ) <= ?)"
            ),
            candidate_parameters=(capability_digest, capability_digest, as_of, as_of),
            index_columns=(
                "CAST(activation_evidence_id AS VARCHAR)",
                "capability_digest",
                "epoch_us(verified_at)",
                "epoch_us(expires_at)",
            ),
            indexed_values=lambda record: (
                str(record.activation_evidence_id),
                record.capability_digest,
                _epoch_us(record.verified_at),
                None if record.expires_at is None else _epoch_us(record.expires_at),
            ),
            matches=lambda record: (
                record.capability_digest == capability_digest and record.verified_at <= as_of
            ),
            sort_key=lambda record: (record.verified_at, record.activation_evidence_id),
            reverse=True,
        )
        return records[0] if records else None

    def verified_protocol_conformance_results(
        self, as_of: datetime
    ) -> tuple[ProtocolConformanceResult, ...]:
        return self._verified_records(
            table="protocol_conformance_results",
            model=ProtocolConformanceResult,
            candidate_where=(
                "observed_at <= ? OR CAST(json_extract_string(record_json, "
                "'$.observed_at') AS TIMESTAMPTZ) <= ?"
            ),
            candidate_parameters=(as_of, as_of),
            index_columns=("CAST(conformance_result_id AS VARCHAR)", "epoch_us(observed_at)"),
            indexed_values=lambda record: (
                str(record.conformance_result_id),
                _epoch_us(record.observed_at),
            ),
            matches=lambda record: record.observed_at <= as_of,
            sort_key=lambda record: (record.observed_at, record.conformance_result_id),
        )

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
            ORDER BY observed_at DESC
            """,
            [venue.value, market_id, venue.value, market_id, as_of, as_of],
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
        logical_identities = [
            (record.venue, record.market_id, record.observed_at) for record in records
        ]
        if len(set(logical_identities)) != len(logical_identities):
            raise ConflictingRecordError("multiple fee rates share one logical identity")
        matches = [record for record in records if record.source_hash == source_hash]
        if not matches:
            return None
        return max(matches, key=lambda record: record.observed_at)

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

    def verified_trial_family_for_registration(
        self,
        family_id: str,
        preregistered_at: datetime,
    ) -> TrialFamily | None:
        """Resolve one registration identity only after verifying the complete registry.

        A lookup by indexed columns alone can miss a row whose index was corrupted while its
        immutable JSON still decodes to the requested identity. Registration therefore verifies
        every stored row, rejects duplicate decoded identities, and compares each decoded identity
        with both indexed columns before deciding that an identity is absent.
        """
        rows = self._connection.execute(
            """
            SELECT family_id, epoch_us(preregistered_at), record_json, record_hash
            FROM trial_families
            ORDER BY preregistered_at, family_id
            """
        ).fetchall()
        verified: list[tuple[tuple[str, datetime | None], TrialFamily]] = []
        for row in rows:
            try:
                record = _verified_record((row[2], row[3]), TrialFamily, "trial family")
            except ConflictingRecordError:
                raise
            except (TypeError, ValueError) as error:
                raise ConflictingRecordError("stored trial family record is invalid") from error
            if record is None:  # pragma: no cover - rows cannot contain NULL record_json
                continue
            verified.append(((row[0], _utc_from_epoch_us(row[1])), record))

        decoded_identities = [(record.family_id, record.preregistered_at) for _, record in verified]
        if len(set(decoded_identities)) != len(decoded_identities):
            raise ConflictingRecordError("multiple trial families share one logical identity")
        for indexed, record in verified:
            decoded = (record.family_id, record.preregistered_at)
            if indexed != decoded:
                raise ConflictingRecordError("stored trial family indexed columns do not match")

        identity = (family_id, preregistered_at)
        matches = [
            record
            for _, record in verified
            if identity == (record.family_id, record.preregistered_at)
        ]
        return matches[0] if matches else None

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
