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

import duckdb
from pydantic import BaseModel

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
from polytrading.predictions.manifest import VenueManifest

_MIGRATION_NAME = re.compile(r"(?P<version>[0-9]{3})_[a-z0-9_]+\.sql")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


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
