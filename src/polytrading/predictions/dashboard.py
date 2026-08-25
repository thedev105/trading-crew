from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from itertools import pairwise
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
    ShadowListing,
    ShadowSummary,
)
from polytrading.predictions.domain import MarketRecord, PredictionBookSnapshot, PredictionVenue
from polytrading.predictions.economics_models import ScanReport
from polytrading.predictions.experiments import ShadowExperiment
from polytrading.predictions.health import PredictionHealthAuditor
from polytrading.predictions.proofs_models import ProofArtifact
from polytrading.predictions.shadow_ledger import (
    LedgerPosting,
    ShadowReconciliation,
    proposal_paper_pnl,
    reconciled_event_for,
)
from polytrading.predictions.shadow_models import (
    ShadowEvent,
    ShadowPlan,
    ShadowState,
    derive_current_state,
)
from polytrading.predictions.storage.store import PredictionMarketStore

_MAX_MARKETS_SHOWN = 200
_MAX_BOOKS_SHOWN = 24
_MAX_CANDIDATES_SHOWN = 20
_MAX_PROOFS_SHOWN = 20
_MAX_SCANS_SHOWN = 20
_MAX_SHADOW_SHOWN = 20


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
            shadow=self._shadow_summary(as_of),
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

    def _shadow_summary(self, as_of: datetime) -> ShadowSummary:
        experiments = self._store.verified_shadow_experiments_as_of(as_of)
        experiments_by_proposal: dict[UUID, ShadowExperiment] = {}
        experiments_by_family: dict[str, int] = {}
        for experiment in experiments:
            if experiment.proposal_id in experiments_by_proposal:
                raise ValueError("multiple shadow experiments exist for one proposal")
            experiments_by_proposal[experiment.proposal_id] = experiment
            _increment(experiments_by_family, experiment.family_id)

        listings: list[ShadowListing] = []
        by_current_state: dict[str, int] = {}
        reconciled_pnl = Decimal("0")
        for plan in self._store.verified_shadow_plans_as_of(as_of):
            if plan.observed_at > as_of or plan.information_cutoff > as_of:
                raise ValueError("shadow plan exceeds the dashboard information cutoff")
            events = self._store.verified_shadow_events_for_proposal(plan.proposal_id, as_of)
            if not events:
                raise ValueError("shadow plan is missing its event chain")
            if any(event.occurred_at > as_of for event in events):
                raise ValueError("shadow event exceeds the dashboard information cutoff")
            if any(event.proposal_id != plan.proposal_id for event in events):
                raise ValueError("shadow events do not belong to their plan")
            if len({event.event_id for event in events}) != len(events):
                raise ValueError("shadow event identities must be unique")
            if any(earlier.occurred_at > later.occurred_at for earlier, later in pairwise(events)):
                raise ValueError("shadow event chain must be chronological")
            current_state = derive_current_state(events)
            scenario_id = _scenario_from_execution_events(events)
            reconciliations = self._store.verified_shadow_reconciliations_for_proposal(
                plan.proposal_id, as_of
            )
            if len(reconciliations) > 1:
                raise ValueError("multiple shadow reconciliations exist for one proposal")
            reconciliation = reconciliations[0] if reconciliations else None
            experiment = experiments_by_proposal.pop(plan.proposal_id, None)
            postings = self._store.verified_ledger_postings_for_proposal(plan.proposal_id, as_of)
            paper_pnl = _validated_shadow_pnl(
                plan=plan,
                events=events,
                postings=postings,
                current_state=current_state,
                scenario_id=scenario_id,
                reconciliation=reconciliation,
                experiment=experiment,
            )
            if paper_pnl is not None:
                reconciled_pnl += paper_pnl
            _increment(by_current_state, current_state.value)
            listings.append(
                ShadowListing(
                    schema_version=1,
                    proposal_id=plan.proposal_id,
                    candidate_id=plan.candidate_id,
                    current_state=current_state,
                    scenario_id=scenario_id,
                    quantity=plan.max_quantity,
                    paper_pnl=paper_pnl,
                    observed_at=events[-1].occurred_at,
                )
            )

        if experiments_by_proposal:
            raise ValueError("shadow experiment references an unavailable proposal")

        listings.sort(key=lambda item: (item.observed_at, item.proposal_id), reverse=True)
        by_current_state = dict(sorted(by_current_state.items()))
        experiments_by_family = dict(sorted(experiments_by_family.items()))
        proposals_total = len(listings)
        reconciled_count = by_current_state.get("reconciled", 0)
        return ShadowSummary(
            schema_version=1,
            proposals_total=proposals_total,
            by_terminal_state=by_current_state,
            reconciled_count=reconciled_count,
            reconciled_paper_pnl_usd=reconciled_pnl,
            unreconciled_count=proposals_total - reconciled_count,
            latest=tuple(listings[:_MAX_SHADOW_SHOWN]),
            experiments_by_family=experiments_by_family,
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
            f"polytrading predictions shadow run --db {db} "
            "--trial-family <trial-family> --format json",
            f"polytrading predictions shadow replay --db {db} "
            "--proposal-id <proposal-id> --format json",
        )


def _scenario_from_execution_events(events: tuple[ShadowEvent, ...]) -> str | None:
    execution_states = {
        ShadowState.FIRST_LEG_SIMULATED,
        ShadowState.COMPLETE,
        ShadowState.UNWOUND,
        ShadowState.EXPIRED,
        ShadowState.UNKNOWN,
        ShadowState.RECONCILED,
    }
    execution_events = tuple(event for event in events if event.to_state in execution_states)
    provenance_events = tuple(event for event in events if event.to_state not in execution_states)
    if any(event.scenario_id is not None for event in provenance_events):
        raise ValueError("provenance events cannot define a shadow scenario")
    if not execution_events:
        return None
    scenario_ids = {event.scenario_id for event in execution_events}
    if None in scenario_ids or len(scenario_ids) != 1:
        raise ValueError("shadow execution events require one scenario")
    return next(iter(scenario_ids))


def _validated_shadow_pnl(
    *,
    plan: ShadowPlan,
    events: tuple[ShadowEvent, ...],
    postings: tuple[LedgerPosting, ...],
    current_state: ShadowState,
    scenario_id: str | None,
    reconciliation: ShadowReconciliation | None,
    experiment: ShadowExperiment | None,
) -> Decimal | None:
    execution_terminals = {
        ShadowState.COMPLETE,
        ShadowState.UNWOUND,
        ShadowState.EXPIRED,
        ShadowState.UNKNOWN,
    }
    if current_state is ShadowState.RECONCILED:
        if len(events) < 2 or events[-2].to_state not in execution_terminals:
            raise ValueError("reconciled shadow chain is missing its execution terminal")
        terminal = events[-2]
        if reconciliation is None or experiment is None:
            raise ValueError("reconciled shadow proposal is missing result evidence")
        if (
            not reconciliation.complete
            or reconciliation.terminal_event_id != terminal.event_id
            or reconciliation.terminal_state is not terminal.to_state
            or reconciliation.observed_at != events[-1].occurred_at
            or experiment.terminal_state is not ShadowState.RECONCILED
            or not experiment.reconciled
            or experiment.paper_pnl_usd is None
            or experiment.scenario_id != scenario_id
            or experiment.observed_at != reconciliation.observed_at
        ):
            raise ValueError("reconciled shadow result evidence is inconsistent")
        expected_reconciled_event = reconciled_event_for(plan, events[:-1], reconciliation)
        if events[-1] != expected_reconciled_event:
            raise ValueError("reconciled shadow event is not canonical")
        authoritative_pnl = proposal_paper_pnl(postings, reconciliation, events)
        if authoritative_pnl is None or experiment.paper_pnl_usd != authoritative_pnl:
            raise ValueError("shadow experiment paper P&L does not match the reconciled ledger")
        return authoritative_pnl

    if reconciliation is not None:
        if current_state not in execution_terminals:
            raise ValueError("intermediate shadow state has reconciliation evidence")
        terminal = events[-1]
        if (
            reconciliation.complete
            or reconciliation.terminal_event_id != terminal.event_id
            or reconciliation.terminal_state is not current_state
            or reconciliation.observed_at != terminal.occurred_at
        ):
            raise ValueError("unreconciled shadow evidence is inconsistent")
        if proposal_paper_pnl(postings, reconciliation, events) is not None:
            raise ValueError("unreconciled shadow proposal cannot have authoritative paper P&L")
    elif postings:
        raise ValueError("shadow ledger postings require reconciliation evidence")
    if experiment is not None and (
        reconciliation is None
        or experiment.terminal_state is not current_state
        or experiment.reconciled
        or experiment.paper_pnl_usd is not None
        or experiment.scenario_id != scenario_id
        or experiment.observed_at != reconciliation.observed_at
    ):
        raise ValueError("unreconciled shadow experiment is inconsistent")
    return None


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
