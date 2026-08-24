from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from polytrading.predictions.candidates_models import CandidateRelationship
from polytrading.predictions.dashboard_models import (
    CandidateListing,
    CandidateSummary,
    PredictionDashboardSnapshot,
    PredictionEvidenceCounts,
    PredictionOperationRecipes,
    ProofListing,
    ProofSummary,
    ScanListing,
    ScanSummary,
)
from polytrading.predictions.domain import MarketRecord, PredictionBookSnapshot, PredictionVenue
from polytrading.predictions.economics_models import ScanReport
from polytrading.predictions.health import PredictionHealthAuditor
from polytrading.predictions.proofs_models import ProofArtifact
from polytrading.predictions.storage.store import PredictionMarketStore

_MAX_MARKETS_SHOWN = 200
_MAX_BOOKS_SHOWN = 24
_MAX_CANDIDATES_SHOWN = 20
_MAX_PROOFS_SHOWN = 20
_MAX_SCANS_SHOWN = 20


class PredictionDashboardBuilder:
    def __init__(self, store: PredictionMarketStore, database_path: Path) -> None:
        self._store = store
        self._database_path = database_path

    def build(self, as_of: datetime) -> PredictionDashboardSnapshot:
        health = PredictionHealthAuditor(self._store).audit(as_of)
        markets: list[MarketRecord] = []
        for venue in PredictionVenue:
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
            candidates=self._candidate_summary(as_of),
            proofs=self._proof_summary(as_of),
            scans=self._scan_summary(as_of),
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

    def _candidate_summary(self, as_of: datetime) -> CandidateSummary:
        candidates = self._store.candidate_relationships_as_of(as_of)
        by_relationship_type: dict[str, int] = {}
        by_disposition: dict[str, int] = {}
        by_provenance_kind: dict[str, int] = {}
        for candidate in candidates:
            _increment(by_relationship_type, candidate.relationship_type.value)
            _increment(by_disposition, candidate.disposition.value)
            _increment(by_provenance_kind, candidate.provenance.kind)

        # `candidates` is ordered oldest-first (Task 5's store contract); the panel wants
        # the most recently observed candidates first, capped at _MAX_CANDIDATES_SHOWN.
        newest_first = tuple(reversed(candidates[-_MAX_CANDIDATES_SHOWN:]))
        latest = tuple(_candidate_listing(candidate) for candidate in newest_first)

        return CandidateSummary(
            schema_version=1,
            total=len(candidates),
            by_relationship_type=by_relationship_type,
            by_disposition=by_disposition,
            by_provenance_kind=by_provenance_kind,
            latest=latest,
        )

    def _proof_summary(self, as_of: datetime) -> ProofSummary:
        proofs = self._store.proof_artifacts_as_of(as_of)
        by_status: dict[str, int] = {}
        by_template: dict[str, int] = {}
        for proof in proofs:
            _increment(by_status, proof.status)
            _increment(by_template, proof.template)

        # Same newest-first/cap-at-20 contract as `_candidate_summary` above: the
        # store returns proofs oldest-first, so reverse the newest slice for display.
        newest_first = tuple(reversed(proofs[-_MAX_PROOFS_SHOWN:]))
        latest = tuple(_proof_listing(proof) for proof in newest_first)

        return ProofSummary(
            schema_version=1,
            total=len(proofs),
            by_status=by_status,
            by_template=by_template,
            latest=latest,
        )

    def _scan_summary(self, as_of: datetime) -> ScanSummary:
        reports = self._store.scan_reports_as_of(as_of)
        by_decision: dict[str, int] = {}
        for report in reports:
            _increment(by_decision, report.decision)

        newest_first = tuple(reversed(reports[-_MAX_SCANS_SHOWN:]))
        latest = tuple(_scan_listing(report) for report in newest_first)

        return ScanSummary(
            schema_version=1,
            total=len(reports),
            by_decision=by_decision,
            latest=latest,
        )

    def _recipes(self) -> tuple[str, ...]:
        db = self._database_path
        return (
            f"polytrading predictions venues status --db {db} --format json",
            f"polytrading predictions collect polymarket --db {db}",
            f"polytrading predictions collect kalshi --db {db}",
            f"polytrading predictions collect limitless --db {db}",
            f"polytrading predictions collect polymarket --db {db} --books 5",
            f"polytrading predictions health --db {db} --format json",
            f"polytrading predictions candidates --db {db} "
            "--venues polymarket,kalshi,limitless --format json",
            f"polytrading predictions attest --db {db} --input attestation.json",
            f"polytrading predictions prove --db {db} --candidate-id <candidate-id> --format json",
            f"polytrading predictions scan --db {db} --format json",
        )


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _candidate_listing(candidate: CandidateRelationship) -> CandidateListing:
    return CandidateListing(
        schema_version=1,
        candidate_id=candidate.candidate_id,
        relationship_type=candidate.relationship_type,
        venues=tuple(dict.fromkeys(leg.venue for leg in candidate.legs)),
        disposition=candidate.disposition,
        provenance_kind=candidate.provenance.kind,
        unresolved_field_count=len(candidate.unresolved_fields),
        observed_at=candidate.observed_at,
    )


def _proof_listing(proof: ProofArtifact) -> ProofListing:
    return ProofListing(
        schema_version=1,
        proof_id=proof.proof_id,
        candidate_id=proof.candidate_id,
        template=proof.template,
        status=proof.status,
        rejection_reason=proof.rejection_reason,
        minimum_basket_payout=proof.minimum_basket_payout,
        observed_at=proof.observed_at,
    )


def _scan_listing(report: ScanReport) -> ScanListing:
    economics = report.economics
    return ScanListing(
        schema_version=1,
        candidate_id=report.candidate_id,
        decision=report.decision,
        reason=report.reason,
        surplus=None if economics is None else economics.conservative_surplus_usd,
        capacity=None if economics is None else economics.capacity_usd_at_current_depth,
        as_of=report.as_of,
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
