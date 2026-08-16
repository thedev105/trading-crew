from __future__ import annotations

from datetime import datetime
from uuid import UUID

from polytrading.predictions.domain import MarketRecord, PredictionVenue, RuleVersion
from polytrading.predictions.storage.store import PredictionMarketStore


class PredictionRegistry:
    """Typed read-only queries over the immutable market/rule registry.

    Per spec section 11.2, this layer performs no ranking, scoring, matching, or text-
    similarity logic of any kind; it only exposes exact, point-in-time-safe reads.
    """

    def __init__(self, store: PredictionMarketStore) -> None:
        self._store = store

    def market_as_of(
        self, venue: PredictionVenue, market_id: str, as_of: datetime
    ) -> MarketRecord | None:
        matches = [
            market
            for market in self._store.markets_as_of(venue, as_of)
            if market.market_id == market_id
        ]
        return matches[0] if matches else None

    def rule_history(
        self, venue: PredictionVenue, market_id: str, as_of: datetime
    ) -> tuple[RuleVersion, ...]:
        return tuple(
            version
            for version in self._store.rule_versions_for_market(market_id, as_of)
            if version.venue is venue
        )

    def markets_by_venue_as_of(
        self, venue: PredictionVenue, as_of: datetime
    ) -> tuple[MarketRecord, ...]:
        return self._store.markets_as_of(venue, as_of)

    def has_rule_changed_since(
        self,
        venue: PredictionVenue,
        market_id: str,
        known_rule_version_id: UUID,
        as_of: datetime,
    ) -> bool:
        history = self.rule_history(venue, market_id, as_of)
        if not history:
            return False
        return history[-1].rule_version_id != known_rule_version_id
