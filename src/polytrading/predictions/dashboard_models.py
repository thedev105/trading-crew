from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from polytrading.predictions.candidates_models import CandidateDisposition, RelationshipType
from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookSnapshot,
    PredictionRecord,
    PredictionVenue,
)
from polytrading.predictions.health import PredictionHealthReport


class PredictionEvidenceCounts(PredictionRecord):
    schema_version: Literal[1]
    counts: dict[str, int]


class PredictionOperationRecipes(PredictionRecord):
    schema_version: Literal[1]
    recipes: tuple[str, ...]


class CandidateListing(PredictionRecord):
    schema_version: Literal[1]
    candidate_id: UUID
    relationship_type: RelationshipType
    venues: tuple[PredictionVenue, ...]
    disposition: CandidateDisposition
    provenance_kind: str
    unresolved_field_count: int
    observed_at: datetime


class CandidateSummary(PredictionRecord):
    schema_version: Literal[1]
    total: int
    by_relationship_type: dict[str, int]
    by_disposition: dict[str, int]
    by_provenance_kind: dict[str, int]
    latest: tuple[CandidateListing, ...]


class PredictionDashboardSnapshot(PredictionRecord):
    schema_version: Literal[1]
    as_of: datetime
    health: PredictionHealthReport
    markets: tuple[MarketRecord, ...]
    books: tuple[PredictionBookSnapshot, ...]
    evidence_counts: PredictionEvidenceCounts
    recipes: PredictionOperationRecipes
    candidates: CandidateSummary
