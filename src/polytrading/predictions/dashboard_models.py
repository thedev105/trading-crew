from __future__ import annotations

from datetime import datetime
from typing import Literal

from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookSnapshot,
    PredictionRecord,
)
from polytrading.predictions.health import PredictionHealthReport


class PredictionEvidenceCounts(PredictionRecord):
    schema_version: Literal[1]
    counts: dict[str, int]


class PredictionOperationRecipes(PredictionRecord):
    schema_version: Literal[1]
    recipes: tuple[str, ...]


class PredictionDashboardSnapshot(PredictionRecord):
    schema_version: Literal[1]
    as_of: datetime
    health: PredictionHealthReport
    markets: tuple[MarketRecord, ...]
    books: tuple[PredictionBookSnapshot, ...]
    evidence_counts: PredictionEvidenceCounts
    recipes: PredictionOperationRecipes
