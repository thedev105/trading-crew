from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from polytrading.carry.audit import CarryAuditor
from polytrading.carry.discovery import evaluate_discovery
from polytrading.carry.dossier import evaluate_dossier, load_bundled_dossiers
from polytrading.carry.dossier_models import ContractCompatibilityDossier
from polytrading.domain.models import Asset, Venue, normalize_utc_timestamp
from polytrading.storage.store import DuckDBStore
from polytrading.venues.funding_health import FundingCollectionHealthAuditor
from polytrading.web.models import (
    RESEARCH_WARNING,
    BookCycleSummary,
    CarryEvidenceRow,
    DashboardSnapshot,
    EvidenceCounts,
    FundingCycleSummary,
    MarketEvidenceRow,
    OperationRecipes,
)

_VENUES = (Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX)
_ASSETS = (Asset.BTC, Asset.ETH, Asset.SOL)


def _symbol(venue: Venue, asset: Asset) -> str:
    if venue is Venue.BYBIT:
        return f"{asset.value}USDT"
    if venue is Venue.DYDX:
        return f"{asset.value}-USD"
    return asset.value


class DashboardBuilder:
    def __init__(
        self,
        store: DuckDBStore,
        database_path: Path,
        dossier_catalog_loader: Callable[
            [], tuple[ContractCompatibilityDossier, ...]
        ] = load_bundled_dossiers,
    ) -> None:
        self._store = store
        self._database_path = database_path
        self._dossier_catalog_loader = dossier_catalog_loader

    def build(self, as_of: datetime) -> DashboardSnapshot:
        normalized_as_of = normalize_utc_timestamp(as_of)
        health = FundingCollectionHealthAuditor(self._store).audit(normalized_as_of, 24)
        funding_cycle = self._store.latest_funding_collection_cycle_as_of(normalized_as_of)
        book_cycle = self._store.latest_book_cycle_as_of(normalized_as_of)
        carry = CarryAuditor(
            self._store,
            max_instrument_age=timedelta(days=7),
            max_funding_age=timedelta(days=7),
            max_book_age=timedelta(seconds=30),
            max_book_cycle_skew=timedelta(seconds=1),
        ).audit(normalized_as_of)
        dossiers = tuple(
            dossier
            for dossier in self._dossier_catalog_loader()
            if dossier.observed_at <= normalized_as_of
        )
        dossier_reports = tuple(evaluate_dossier(dossier) for dossier in dossiers)
        legacy_report = next(
            (
                report
                for report in dossier_reports
                if report.dossier_id == "hyperliquid-dydx-core-v1"
            ),
            None,
        )

        return DashboardSnapshot(
            schema_version=1,
            as_of=normalized_as_of,
            database_name=self._database_path.name,
            warning=RESEARCH_WARNING,
            funding_health=health,
            latest_funding_cycle=(
                None
                if funding_cycle is None
                else FundingCycleSummary(
                    schema_version=1,
                    cycle_id=funding_cycle.cycle_id,
                    cycle_end=funding_cycle.cycle_end,
                    request_completed_at=funding_cycle.request_completed_at,
                    status=funding_cycle.status,
                )
            ),
            latest_book_cycle=(
                None
                if book_cycle is None
                else BookCycleSummary(
                    schema_version=1,
                    cycle_id=book_cycle.cycle_id,
                    request_completed_at=book_cycle.request_completed_at,
                    status=book_cycle.status,
                    max_effective_skew_ms=book_cycle.max_effective_skew_ms,
                )
            ),
            compatibility_dossier=legacy_report,
            venue_discovery=(evaluate_discovery(dossier_reports) if dossier_reports else None),
            markets=tuple(
                self._market_row(venue, asset, normalized_as_of)
                for venue in _VENUES
                for asset in _ASSETS
            ),
            carry_rows=tuple(
                CarryEvidenceRow(
                    schema_version=1,
                    asset=row.asset,
                    status=row.status,
                    funding_ready=row.funding_ready,
                    book_ready=row.book_ready,
                    hourly_spread=(
                        None if row.diagnostic is None else row.diagnostic.hourly_spread
                    ),
                    reason_codes=tuple(sorted(set(row.reason_codes))),
                )
                for row in carry.assets
            ),
            evidence_counts=EvidenceCounts(**self._store.evidence_counts_as_of(normalized_as_of)),
            operation_recipes=_operation_recipes(self._database_path),
        )

    def _market_row(self, venue: Venue, asset: Asset, as_of: datetime) -> MarketEvidenceRow:
        symbol = _symbol(venue, asset)
        instrument = self._store.latest_instrument_as_of(venue, symbol, as_of)
        funding = self._store.latest_funding_as_of(venue, symbol, as_of)
        book = self._store.latest_book_as_of(venue, symbol, as_of)
        if book is None:
            best_bid = best_ask = spread_bps = None
            book_effective_at = book_observed_at = None
        else:
            best_bid = book.bids[0].price
            best_ask = book.asks[0].price
            midpoint = (best_ask + best_bid) / Decimal(2)
            spread_bps = (best_ask - best_bid) / midpoint * Decimal(10_000)
            book_effective_at = book.effective_at
            book_observed_at = book.observed_at
        return MarketEvidenceRow(
            schema_version=1,
            venue=venue,
            asset=asset,
            symbol=symbol,
            instrument_observed_at=(None if instrument is None else instrument.observed_at),
            funding_rate=None if funding is None else funding.rate,
            funding_interval_hours=(None if funding is None else funding.interval_hours),
            funding_effective_at=None if funding is None else funding.effective_at,
            funding_observed_at=None if funding is None else funding.observed_at,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_bps=spread_bps,
            book_effective_at=book_effective_at,
            book_observed_at=book_observed_at,
        )


def _operation_recipes(database_path: Path) -> OperationRecipes:
    database = shlex.quote(str(database_path))
    return OperationRecipes(
        collect_public=(
            f".venv/bin/polytrading collect public --venue all --assets BTC,ETH,SOL --db {database}"
        ),
        collect_books_once=(
            ".venv/bin/polytrading collect books --venue all --assets BTC,ETH,SOL --once "
            f"--db {database}"
        ),
        collect_current_funding=(
            f".venv/bin/polytrading collect funding-cycle --current --db {database}"
        ),
        inspect_funding_health=(f".venv/bin/polytrading funding health --hours 24 --db {database}"),
    )


def render_dashboard_json(snapshot: DashboardSnapshot) -> bytes:
    document = _json_value(snapshot)
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(exclude_computed_fields=True))
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
