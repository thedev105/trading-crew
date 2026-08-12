from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from polytrading.carry.compatibility import compare_contracts
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
    instrument_source_hash: str | None
    instrument_observed_at: datetime | None
    funding_source_hash: str | None
    funding_effective_at: datetime | None
    funding_observed_at: datetime | None
    hourly_rate: Decimal | None


class BookEvidence(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    symbol: str
    source_hash: str
    effective_at: datetime
    observed_at: datetime
    book_age_ms: Decimal
    top_level_spread: Decimal
    common_depth_levels: int | None
    cumulative_bid_notional: Decimal | None
    cumulative_ask_notional: Decimal | None


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
        instruments: dict[Venue, InstrumentSpec] = {}
        fundings: dict[Venue, FundingObservation] = {}
        funding_evidence: list[FundingEvidence] = []
        funding_record_is_stale = False

        for venue in _VENUES:
            symbol = _symbol(venue, asset)
            instrument = self._store.latest_instrument_as_of(venue, symbol, as_of)
            funding = self._store.latest_funding_as_of(venue, symbol, as_of)
            if instrument is None:
                reason_codes.append(f"INSTRUMENT_MISSING:{venue.value}")
            else:
                instruments[venue] = instrument
                if as_of - instrument.observed_at > self._max_instrument_age:
                    reason_codes.append(f"INSTRUMENT_STALE:{venue.value}")
                    funding_record_is_stale = True
            if funding is None:
                reason_codes.append(f"FUNDING_MISSING:{venue.value}")
            else:
                fundings[venue] = funding
                funding_stale = (
                    as_of - funding.effective_at > self._max_funding_age
                    or as_of - funding.observed_at > self._max_funding_age
                )
                if funding_stale:
                    reason_codes.append(f"FUNDING_STALE:{venue.value}")
                    funding_record_is_stale = True
            funding_evidence.append(
                FundingEvidence(
                    schema_version=1,
                    venue=venue,
                    symbol=symbol,
                    instrument_source_hash=(
                        instrument.source_hash if instrument is not None else None
                    ),
                    instrument_observed_at=(
                        instrument.observed_at if instrument is not None else None
                    ),
                    funding_source_hash=funding.source_hash if funding is not None else None,
                    funding_effective_at=(funding.effective_at if funding is not None else None),
                    funding_observed_at=(funding.observed_at if funding is not None else None),
                    hourly_rate=funding.hourly_rate if funding is not None else None,
                )
            )

        compatibility = (
            compare_contracts(instruments[_VENUES[0]], instruments[_VENUES[1]])
            if len(instruments) == 2
            else None
        )
        compatibility_reasons = list(compatibility.reasons) if compatibility is not None else []
        reason_codes.extend(compatibility_reasons)
        diagnostic = (
            compare_latest_funding(
                fundings[_VENUES[0]],
                instruments[_VENUES[0]],
                fundings[_VENUES[1]],
                instruments[_VENUES[1]],
                as_of,
            )
            if len(instruments) == 2 and len(fundings) == 2
            else None
        )

        book_cycle_id, book_cycle_skew_ms, book_evidence, book_reasons = self._book_evidence(
            asset, as_of, cycle
        )
        reason_codes.extend(book_reasons)

        selected_record_is_stale = funding_record_is_stale or "BOOK_EVIDENCE_STALE" in book_reasons
        funding_ready = len(instruments) == 2 and len(fundings) == 2 and not funding_record_is_stale
        book_ready = not book_reasons
        if compatibility_reasons:
            status = AuditStatus.INELIGIBLE
        elif selected_record_is_stale:
            status = AuditStatus.STALE
        elif len(instruments) != 2 or len(fundings) != 2 or not book_ready:
            status = AuditStatus.INSUFFICIENT_DATA
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
    ) -> tuple[UUID | None, Decimal | None, tuple[BookEvidence, ...], tuple[str, ...]]:
        from polytrading.venues.synchronized import BookCollectionCycle

        if not isinstance(cycle, BookCollectionCycle):
            return None, None, (), ("BOOK_EVIDENCE_MISSING",)
        skew_ms = cycle.max_effective_skew_ms
        if (
            cycle.status != "complete"
            or asset not in cycle.assets
            or any(venue not in cycle.venues for venue in _VENUES)
        ):
            return cycle.cycle_id, skew_ms, (), ("BOOK_EVIDENCE_MISSING",)

        books = tuple(
            self._store.book_for_cycle_as_of(cycle.cycle_id, venue, _symbol(venue, asset), as_of)
            for venue in _VENUES
        )
        selected_books = tuple(book for book in books if book is not None)
        complete_pair = len(selected_books) == len(_VENUES)
        common_depth = (
            min(
                20,
                *(len(side) for book in selected_books for side in (book.bids, book.asks)),
            )
            if complete_pair
            else None
        )
        evidence = tuple(_summarize_book(book, as_of, common_depth) for book in selected_books)
        is_stale = any(
            as_of - book.effective_at > self._max_book_age
            or as_of - book.observed_at > self._max_book_age
            for book in selected_books
        )
        reasons: list[str] = []
        if not complete_pair:
            reasons.append("BOOK_EVIDENCE_MISSING")
        if _milliseconds(self._max_book_cycle_skew) < skew_ms:
            reasons.append("BOOK_CYCLE_SKEW_EXCEEDED")
        if is_stale:
            reasons.append("BOOK_EVIDENCE_STALE")
        return cycle.cycle_id, skew_ms, evidence, tuple(reasons)


def _symbol(venue: Venue, asset: Asset) -> str:
    return f"{asset.value}USDT" if venue is Venue.BYBIT else asset.value


def _milliseconds(value: timedelta) -> Decimal:
    return Decimal(value.days * 86_400_000 + value.seconds * 1_000) + (
        Decimal(value.microseconds) / Decimal(1_000)
    )


def _summarize_book(
    book: Level2BookSnapshot, as_of: datetime, common_depth: int | None
) -> BookEvidence:
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
        cumulative_bid_notional=(
            sum(
                (level.price * level.quantity for level in book.bids[:common_depth]),
                Decimal(0),
            )
            if common_depth is not None
            else None
        ),
        cumulative_ask_notional=(
            sum(
                (level.price * level.quantity for level in book.asks[:common_depth]),
                Decimal(0),
            )
            if common_depth is not None
            else None
        ),
    )
