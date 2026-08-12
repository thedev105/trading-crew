from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from polytrading.carry.models import FundingSpreadDiagnostic
from polytrading.carry.normalize import compare_latest_funding
from polytrading.domain.models import (
    Asset,
    FundingObservation,
    InstrumentSpec,
    Level2BookSnapshot,
    StrictRecord,
    Venue,
    normalize_utc_timestamp,
)
from polytrading.storage.store import DuckDBStore

_ASSETS = (Asset.BTC, Asset.ETH, Asset.SOL)
_VENUES = (Venue.BYBIT, Venue.HYPERLIQUID)


class AuditStatus(StrEnum):
    INELIGIBLE = "INELIGIBLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    STALE = "STALE"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"


class FundingEvidence(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    symbol: str
    instrument_source_hash: str
    instrument_observed_at: datetime
    funding_source_hash: str
    funding_effective_at: datetime
    funding_observed_at: datetime
    hourly_rate: Decimal


class BookEvidence(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    symbol: str
    source_hash: str
    effective_at: datetime
    observed_at: datetime
    book_age_ms: Decimal
    top_level_spread: Decimal
    common_depth_levels: int
    cumulative_bid_notional: Decimal
    cumulative_ask_notional: Decimal


class AssetCarryAudit(StrictRecord):
    schema_version: Literal[1]
    asset: Asset
    status: AuditStatus
    reason_codes: tuple[str, ...]
    funding_ready: bool
    book_ready: bool
    funding_evidence: tuple[FundingEvidence, ...]
    diagnostic: FundingSpreadDiagnostic | None
    forecast_status: Literal["not_evaluated"] = "not_evaluated"
    book_cycle_id: UUID | None
    book_cycle_skew_ms: Decimal | None
    book_evidence: tuple[BookEvidence, ...]


class CarryAuditReport(StrictRecord):
    schema_version: Literal[1]
    as_of: datetime
    assets: tuple[AssetCarryAudit, ...]


class CarryAuditor:
    def __init__(
        self,
        store: DuckDBStore,
        max_instrument_age: timedelta,
        max_funding_age: timedelta,
        max_book_age: timedelta,
        max_book_cycle_skew: timedelta,
    ) -> None:
        limits = (
            max_instrument_age,
            max_funding_age,
            max_book_age,
            max_book_cycle_skew,
        )
        if any(limit < timedelta(0) for limit in limits):
            raise ValueError("audit age and skew limits must be nonnegative")
        self._store = store
        self._max_instrument_age = max_instrument_age
        self._max_funding_age = max_funding_age
        self._max_book_age = max_book_age
        self._max_book_cycle_skew = max_book_cycle_skew

    def audit(self, as_of: datetime) -> CarryAuditReport:
        """Return BTC, ETH, and SOL diagnostics in that stable order."""
        normalized_as_of = normalize_utc_timestamp(as_of)
        cycle = self._store.latest_complete_book_cycle_as_of(normalized_as_of)
        rows = tuple(self._audit_asset(asset, normalized_as_of, cycle) for asset in _ASSETS)
        return CarryAuditReport(schema_version=1, as_of=normalized_as_of, assets=rows)

    def _audit_asset(self, asset: Asset, as_of: datetime, cycle: object) -> AssetCarryAudit:
        reason_codes: list[str] = []
        pairs: list[tuple[FundingObservation, InstrumentSpec]] = []
        funding_evidence: list[FundingEvidence] = []
        funding_is_stale = False

        for venue in _VENUES:
            symbol = _symbol(venue, asset)
            instrument = self._store.latest_instrument_as_of(venue, symbol, as_of)
            funding = self._store.latest_funding_as_of(venue, symbol, as_of)
            if instrument is None:
                reason_codes.append(f"INSTRUMENT_MISSING:{venue.value}")
            if funding is None:
                reason_codes.append(f"FUNDING_MISSING:{venue.value}")
            if instrument is None or funding is None:
                continue

            instrument_stale = as_of - instrument.observed_at > self._max_instrument_age
            funding_stale = (
                as_of - funding.effective_at > self._max_funding_age
                or as_of - funding.observed_at > self._max_funding_age
            )
            if instrument_stale:
                reason_codes.append(f"INSTRUMENT_STALE:{venue.value}")
            if funding_stale:
                reason_codes.append(f"FUNDING_STALE:{venue.value}")
            funding_is_stale = funding_is_stale or instrument_stale or funding_stale
            pairs.append((funding, instrument))
            funding_evidence.append(
                FundingEvidence(
                    schema_version=1,
                    venue=venue,
                    symbol=symbol,
                    instrument_source_hash=instrument.source_hash,
                    instrument_observed_at=instrument.observed_at,
                    funding_source_hash=funding.source_hash,
                    funding_effective_at=funding.effective_at,
                    funding_observed_at=funding.observed_at,
                    hourly_rate=funding.hourly_rate,
                )
            )

        diagnostic = (
            compare_latest_funding(*pairs[0], *pairs[1], as_of) if len(pairs) == 2 else None
        )
        compatibility_reasons = (
            list(diagnostic.compatibility.reasons) if diagnostic is not None else []
        )
        reason_codes.extend(compatibility_reasons)

        book_cycle_id, book_cycle_skew_ms, book_evidence, book_reason = self._book_evidence(
            asset, as_of, cycle
        )
        if book_reason is not None:
            reason_codes.append(book_reason)

        funding_ready = len(pairs) == 2 and not funding_is_stale
        book_ready = book_reason is None
        if compatibility_reasons:
            status = AuditStatus.INELIGIBLE
        elif len(pairs) != 2:
            status = AuditStatus.INSUFFICIENT_DATA
        elif funding_is_stale:
            status = AuditStatus.STALE
        elif not book_ready:
            status = (
                AuditStatus.STALE
                if book_reason == "BOOK_EVIDENCE_STALE"
                else AuditStatus.INSUFFICIENT_DATA
            )
        else:
            status = AuditStatus.DIAGNOSTIC_ONLY

        return AssetCarryAudit(
            schema_version=1,
            asset=asset,
            status=status,
            reason_codes=tuple(reason_codes),
            funding_ready=funding_ready,
            book_ready=book_ready,
            funding_evidence=tuple(funding_evidence),
            diagnostic=diagnostic,
            book_cycle_id=book_cycle_id,
            book_cycle_skew_ms=book_cycle_skew_ms,
            book_evidence=book_evidence,
        )

    def _book_evidence(
        self, asset: Asset, as_of: datetime, cycle: object
    ) -> tuple[UUID | None, Decimal | None, tuple[BookEvidence, ...], str | None]:
        from polytrading.venues.synchronized import BookCollectionCycle

        if not isinstance(cycle, BookCollectionCycle):
            return None, None, (), "BOOK_EVIDENCE_MISSING"
        skew_ms = cycle.max_effective_skew_ms
        if cycle.status == "skew_exceeds_research_target" or (
            _milliseconds(self._max_book_cycle_skew) < skew_ms
        ):
            return cycle.cycle_id, skew_ms, (), "BOOK_CYCLE_SKEW_EXCEEDED"
        if (
            cycle.status != "complete"
            or asset not in cycle.assets
            or any(venue not in cycle.venues for venue in _VENUES)
        ):
            return cycle.cycle_id, skew_ms, (), "BOOK_EVIDENCE_MISSING"

        books = tuple(
            self._store.book_for_cycle_as_of(cycle.cycle_id, venue, _symbol(venue, asset), as_of)
            for venue in _VENUES
        )
        if any(book is None for book in books):
            return cycle.cycle_id, skew_ms, (), "BOOK_EVIDENCE_MISSING"
        selected_books = tuple(book for book in books if book is not None)
        common_depth = min(
            20,
            *(len(side) for book in selected_books for side in (book.bids, book.asks)),
        )
        evidence = tuple(_summarize_book(book, as_of, common_depth) for book in selected_books)
        is_stale = any(
            as_of - book.effective_at > self._max_book_age
            or as_of - book.observed_at > self._max_book_age
            for book in selected_books
        )
        return (
            cycle.cycle_id,
            skew_ms,
            evidence,
            "BOOK_EVIDENCE_STALE" if is_stale else None,
        )


def _symbol(venue: Venue, asset: Asset) -> str:
    return f"{asset.value}USDT" if venue is Venue.BYBIT else asset.value


def _milliseconds(value: timedelta) -> Decimal:
    return Decimal(value.days * 86_400_000 + value.seconds * 1_000) + (
        Decimal(value.microseconds) / Decimal(1_000)
    )


def _summarize_book(book: Level2BookSnapshot, as_of: datetime, common_depth: int) -> BookEvidence:
    return BookEvidence(
        schema_version=1,
        venue=book.venue,
        symbol=book.symbol,
        source_hash=book.source_hash,
        effective_at=book.effective_at,
        observed_at=book.observed_at,
        book_age_ms=_milliseconds(as_of - book.effective_at),
        top_level_spread=book.asks[0].price - book.bids[0].price,
        common_depth_levels=common_depth,
        cumulative_bid_notional=sum(
            (level.price * level.quantity for level in book.bids[:common_depth]), Decimal(0)
        ),
        cumulative_ask_notional=sum(
            (level.price * level.quantity for level in book.asks[:common_depth]), Decimal(0)
        ),
    )
