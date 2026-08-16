from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from polytrading.predictions.dashboard_models import (
    PredictionDashboardSnapshot,
    PredictionEvidenceCounts,
    PredictionOperationRecipes,
)
from polytrading.predictions.domain import MarketRecord, PredictionBookSnapshot, PredictionVenue
from polytrading.predictions.health import PredictionHealthAuditor
from polytrading.predictions.storage.store import PredictionMarketStore

_MAX_MARKETS_SHOWN = 200
_MAX_BOOKS_SHOWN = 24


class PredictionDashboardBuilder:
    def __init__(self, store: PredictionMarketStore, database_path: Path) -> None:
        self._store = store
        self._database_path = database_path

    def build(self, as_of: datetime) -> PredictionDashboardSnapshot:
        health = PredictionHealthAuditor(self._store).audit(as_of)
        markets: list[MarketRecord] = []
        for venue in (PredictionVenue.POLYMARKET, PredictionVenue.KALSHI):
            markets.extend(self._store.markets_as_of(venue, as_of))
        markets.sort(key=lambda market: market.retrieved_at, reverse=True)
        shown_markets = tuple(markets[:_MAX_MARKETS_SHOWN])
        return PredictionDashboardSnapshot(
            schema_version=1,
            as_of=as_of,
            health=health,
            markets=shown_markets,
            books=self._latest_books(shown_markets, as_of),
            evidence_counts=PredictionEvidenceCounts(
                schema_version=1, counts=self._store.evidence_counts_as_of(as_of)
            ),
            recipes=PredictionOperationRecipes(schema_version=1, recipes=self._recipes()),
        )

    def _latest_books(
        self, markets: tuple[MarketRecord, ...], as_of: datetime
    ) -> tuple[PredictionBookSnapshot, ...]:
        books: list[PredictionBookSnapshot] = []
        for market in markets:
            if len(books) >= _MAX_BOOKS_SHOWN:
                break
            for token_id in market.outcome_token_ids or (None,):
                book = self._store.latest_book_as_of(
                    market.venue, market.market_id, token_id, as_of
                )
                if book is not None:
                    books.append(book)
                if len(books) >= _MAX_BOOKS_SHOWN:
                    break
        return tuple(books)

    def _recipes(self) -> tuple[str, ...]:
        db = self._database_path
        return (
            f"polytrading predictions venues status --db {db} --format json",
            f"polytrading predictions collect polymarket --db {db}",
            f"polytrading predictions collect kalshi --db {db}",
            f"polytrading predictions health --db {db} --format json",
        )


def render_prediction_dashboard_json(snapshot: PredictionDashboardSnapshot) -> bytes:
    return json.dumps(
        _json_value(snapshot), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump())
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
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value
