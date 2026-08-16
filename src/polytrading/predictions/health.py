from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from polytrading.predictions.domain import PredictionRecord, PredictionVenue
from polytrading.predictions.manifest import ManifestGateDecision, evaluate_collection_gate
from polytrading.predictions.storage.store import PredictionMarketStore

NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

PREDICTION_HEALTH_WARNINGS = (
    "Research only: this report measures public evidence collection, not expected returns.",
    "No structural-opportunity proof, economics, or shadow-execution decision is made here.",
    "No credentials, accounts, balances, positions, orders, fills, or transfers were accessed.",
)

# Named thresholds for prediction-market book staleness. Chosen independently of the
# perpetual-futures 30-second/5-minute constants (venues.public.py) since this system's
# book-collection cadence and evidence requirements have not yet been established; these
# are a deliberately conservative starting point for increment 1, not a validated cadence.
_BOOK_STALE_AFTER_SECONDS = Decimal(300)
_BOOK_DEGRADED_AFTER_SECONDS = Decimal(3600)


class VenueEvidenceStatus(StrEnum):
    NOT_COLLECTED = "NOT_COLLECTED"
    STALE = "STALE"
    DEGRADED = "DEGRADED"
    CURRENT = "CURRENT"


class VenueHealth(PredictionRecord):
    schema_version: Literal[1]
    venue: PredictionVenue
    collection_gate: ManifestGateDecision
    market_count: NonNegativeInt
    latest_market_retrieved_at: datetime | None
    latest_book_observed_at: datetime | None
    latest_book_age_seconds: NonNegativeDecimal | None
    status: VenueEvidenceStatus
    reason_codes: tuple[str, ...]


class PredictionHealthReport(PredictionRecord):
    schema_version: Literal[1]
    as_of: datetime
    venues: tuple[VenueHealth, ...]
    warnings: tuple[str, str, str]


class PredictionHealthAuditor:
    def __init__(self, store: PredictionMarketStore) -> None:
        self._store = store

    def audit(self, as_of: datetime) -> PredictionHealthReport:
        venues = tuple(
            self._venue_health(venue, as_of)
            for venue in (PredictionVenue.POLYMARKET, PredictionVenue.KALSHI)
        )
        return PredictionHealthReport(
            schema_version=1,
            as_of=as_of,
            venues=venues,
            warnings=PREDICTION_HEALTH_WARNINGS,
        )

    def _venue_health(self, venue: PredictionVenue, as_of: datetime) -> VenueHealth:
        manifest = self._store.latest_venue_manifest_as_of(venue, as_of)
        gate = evaluate_collection_gate(manifest, venue=venue)
        markets = self._store.markets_as_of(venue, as_of)
        latest_market_retrieved_at = max((market.retrieved_at for market in markets), default=None)

        latest_book_observed_at: datetime | None = None
        latest_book_age_seconds: Decimal | None = None
        for market in markets:
            token_ids = market.outcome_token_ids or (None,)
            for token_id in token_ids:
                book = self._store.latest_book_as_of(venue, market.market_id, token_id, as_of)
                if book is not None and (
                    latest_book_observed_at is None or book.observed_at > latest_book_observed_at
                ):
                    latest_book_observed_at = book.observed_at

        if latest_book_observed_at is not None:
            latest_book_age_seconds = Decimal((as_of - latest_book_observed_at).total_seconds())

        status, reasons = _classify(
            gate=gate,
            market_count=len(markets),
            latest_book_age_seconds=latest_book_age_seconds,
        )
        return VenueHealth(
            schema_version=1,
            venue=venue,
            collection_gate=gate,
            market_count=len(markets),
            latest_market_retrieved_at=latest_market_retrieved_at,
            latest_book_observed_at=latest_book_observed_at,
            latest_book_age_seconds=latest_book_age_seconds,
            status=status,
            reason_codes=reasons,
        )


def _classify(
    *,
    gate: ManifestGateDecision,
    market_count: int,
    latest_book_age_seconds: Decimal | None,
) -> tuple[VenueEvidenceStatus, tuple[str, ...]]:
    if not gate.allowed:
        return VenueEvidenceStatus.NOT_COLLECTED, (f"COLLECTION_GATE:{gate.reason}",)
    if market_count == 0 or latest_book_age_seconds is None:
        return VenueEvidenceStatus.NOT_COLLECTED, ("NO_EVIDENCE_COLLECTED",)
    if latest_book_age_seconds > _BOOK_DEGRADED_AFTER_SECONDS:
        return VenueEvidenceStatus.DEGRADED, ("BOOK_STALE",)
    if latest_book_age_seconds > _BOOK_STALE_AFTER_SECONDS:
        return VenueEvidenceStatus.STALE, ("BOOK_STALE",)
    return VenueEvidenceStatus.CURRENT, ()
