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
)

_MIGRATION_NAME = re.compile(r"(?P<version>[0-9]{3})_[a-z0-9_]+\.sql")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class ConflictingRecordError(ValueError):
    """Raised when an immutable identity is retried with different content."""


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
    def __init__(self, path: Path) -> None:
        self._connection = duckdb.connect(str(path))
        self._in_transaction = False
        try:
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
                   CAST(payload_json AS VARCHAR), source_hash, schema_version
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
            self._ensure_exact_retry("raw envelope", record, _record_hash(stored))
            return False

        self._connection.execute(
            """
            INSERT INTO raw_envelopes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?::JSON, ?, ?)
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

    def latest_instrument_as_of(
        self, venue: Venue, symbol: str, as_of: datetime
    ) -> InstrumentSpec | None:
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
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [venue.value, symbol, as_of],
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
        row = self._connection.execute(
            """
            SELECT cycle_id, venue, symbol, asset, depth_limit, sequence,
                   epoch_us(effective_at), epoch_us(observed_at), source_hash, schema_version
            FROM book_snapshots
            WHERE venue = ? AND symbol = ? AND observed_at <= ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            [venue.value, symbol, as_of],
        ).fetchone()
        if row is None:
            return None
        levels = self._connection.execute(
            """
            SELECT side, price, quantity, order_count
            FROM book_levels
            WHERE cycle_id = ? AND epoch_us(observed_at) = ?
            ORDER BY CASE side WHEN 'bid' THEN 0 ELSE 1 END, level_index
            """,
            [row[0], row[7]],
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

    def latest_fee_as_of(
        self, venue: Venue, tier_name: str, as_of: datetime
    ) -> FeeSchedule | None:
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
            [venue.value, tier_name, as_of, as_of],
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

    def funding_between(
        self, venue: Venue, symbol: str, start: datetime, end: datetime
    ) -> tuple[FundingObservation, ...]:
        rows = self._connection.execute(
            """
            SELECT venue, symbol, asset, rate, interval_hours, epoch_us(effective_at),
                   epoch_us(observed_at), source_hash, schema_version
            FROM funding_observations
            WHERE venue = ? AND symbol = ? AND effective_at >= ? AND effective_at <= ?
            ORDER BY effective_at, observed_at
            """,
            [venue.value, symbol, start, end],
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
            "cycle_id = ? AND observed_at = ?",
            [record.cycle_id, record.observed_at],
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
                    "INSERT INTO book_levels VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        record.cycle_id,
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

    def _normalized_retry(
        self,
        label: str,
        record: BaseModel,
        table: str,
        identity_sql: str,
        parameters: list[Any],
    ) -> bool:
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

        self._connection.execute("BEGIN TRANSACTION")
        try:
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
                version not in versions for version in applied
            ):
                raise RuntimeError(f"applied migration versions are not a known prefix: {applied}")
            for version, migration in migrations[len(applied) :]:
                self._connection.execute(migration.read_text(encoding="utf-8"))
                self._connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)", [version, datetime.now(UTC)]
                )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
