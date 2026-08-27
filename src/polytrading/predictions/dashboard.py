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
    EvidenceStatus,
    ExecutionReadinessSummary,
    ExecutionTimelineEntry,
    LiveLedgerSummary,
    MarketAtlasOpportunity,
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
from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookSnapshot,
    PredictionFeeRate,
    PredictionVenue,
    normalize_utc_timestamp,
)
from polytrading.predictions.economics_models import ScanReport
from polytrading.predictions.execution.authority import UnavailableProductionCapabilityVerifier
from polytrading.predictions.execution.kill_switch import derive_kill_state
from polytrading.predictions.execution.models import canonical_execution_hash
from polytrading.predictions.experiments import ShadowExperiment
from polytrading.predictions.health import PredictionHealthAuditor
from polytrading.predictions.manifest import evaluate_execution_gate
from polytrading.predictions.polymarket_execution import (
    POLYMARKET_PROTOCOL_VERSION,
    load_protocol_snapshot,
    verify_protocol_sources,
)
from polytrading.predictions.proofs_models import ProofArtifact
from polytrading.predictions.shadow_ledger import (
    LedgerPosting,
    ShadowReconciliation,
    postings_for_events,
    proposal_paper_pnl,
    reconcile_proposal,
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
_MAX_OPPORTUNITIES_SHOWN = 200
_MAX_EXECUTION_TIMELINE_SHOWN = 500
_MAX_SAFETY_RECORDS = 10_000


class PredictionDashboardBuilder:
    def __init__(self, store: PredictionMarketStore, database_path: Path) -> None:
        self._store = store
        self._database_path = database_path

    def build(self, as_of: datetime) -> PredictionDashboardSnapshot:
        cutoff = normalize_utc_timestamp(as_of)
        with self._store.transaction():
            return self._build_at_cutoff(cutoff)

    def _build_at_cutoff(self, as_of: datetime) -> PredictionDashboardSnapshot:
        health = PredictionHealthAuditor(self._store).audit(as_of)
        markets: list[MarketRecord] = []
        for venue in PredictionVenue:
            markets.extend(self._store.markets_as_of(venue, as_of))
        markets.sort(key=lambda market: market.retrieved_at, reverse=True)
        shown_markets = tuple(markets[:_MAX_MARKETS_SHOWN])
        (
            execution_readiness,
            opportunities,
            execution_timeline,
            live_ledger,
            evidence_status,
        ) = self._live_execution_views(as_of)
        return PredictionDashboardSnapshot.finalize(
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
            execution_readiness=execution_readiness,
            opportunities=opportunities,
            execution_timeline=execution_timeline,
            live_ledger=live_ledger,
            evidence_status=evidence_status,
        )

    def _live_execution_views(
        self, as_of: datetime
    ) -> tuple[
        ExecutionReadinessSummary,
        tuple[MarketAtlasOpportunity, ...],
        tuple[ExecutionTimelineEntry, ...],
        LiveLedgerSummary,
        EvidenceStatus,
    ]:
        accounts = self._store.verified_live_execution_account_fingerprints(as_of)
        if len(accounts) > _MAX_SAFETY_RECORDS:
            raise ValueError("dashboard account limit exceeded")

        protocol_snapshot = load_protocol_snapshot()
        protocol = verify_protocol_sources(protocol_snapshot)
        expected_fixture_hashes = tuple(
            sorted(item.sha256 for item in protocol_snapshot.fixture_hashes)
        )
        expected_source_hashes = tuple(
            sorted(item.normalized_content_sha256 for item in protocol_snapshot.sources)
        )
        conformance = self._store.verified_protocol_conformance_results(as_of)
        if len(conformance) > _MAX_SAFETY_RECORDS:
            raise ValueError("dashboard conformance record limit exceeded")
        latest_conformance = conformance[-1] if conformance else None
        conformance_bound = (
            latest_conformance is not None
            and latest_conformance.result == "CONFORMANT"
            and latest_conformance.fixture_hashes == expected_fixture_hashes
            and latest_conformance.source_hashes == expected_source_hashes
            and protocol.state == "CURRENT"
        )
        conformance_result = "CONFORMANT" if conformance_bound else "PROTOCOL_REVIEW_REQUIRED"

        manifest = self._store.verified_latest_venue_manifest_as_of(
            PredictionVenue.POLYMARKET, as_of
        )
        if manifest is not None and manifest.implementation_state.value != "LIVE_DISABLED":
            raise ValueError("database manifest contradicts shipped LIVE_DISABLED posture")
        manifest_gate = evaluate_execution_gate(manifest, venue=PredictionVenue.POLYMARKET)
        capability = UnavailableProductionCapabilityVerifier().verify(
            capability_bundle=b"", now=as_of
        )
        if capability.allowed or capability.reason != "CAPABILITY_VERIFIER_NOT_CONFIGURED":
            raise ValueError("production capability posture is not fail closed")

        timeline: list[ExecutionTimelineEntry] = []
        posting_count = 0
        reconciliations = []
        all_kill_events = self._store.verified_kill_switch_events_as_of(as_of)
        latest_kill = max(
            all_kill_events,
            key=lambda event: (event.occurred_at, event.kill_event_id),
            default=None,
        )
        for account in accounts:
            plans = self._store.verified_live_execution_plans_for_account(account, as_of)
            intents = self._store.verified_execution_intent_history_for_account(account, as_of)
            postings = self._store.verified_live_ledger_postings_for_account(account, as_of)
            account_reconciliations = self._store.verified_live_reconciliations_for_account(
                account, as_of
            )
            kill_events = self._store.verified_kill_switch_events(account, as_of)
            _require_bounded_safety_records(
                plans=plans,
                intents=intents,
                postings=postings,
                reconciliations=account_reconciliations,
                kill_events=kill_events,
            )
            plan_ids = {plan.plan_id for plan in plans}
            intent_ids = {intent.intent_id for intent in intents}
            posting_ids = {posting.posting_id for posting in postings}
            if any(intent.plan_id not in plan_ids for intent in intents):
                raise ValueError("execution intent references an unavailable plan")
            if any(
                posting.intent_id is not None and posting.intent_id not in intent_ids
                for posting in postings
            ):
                raise ValueError("live posting references an unavailable intent")
            if any(
                not set(reconciliation.expected_posting_ids).issubset(posting_ids)
                for reconciliation in account_reconciliations
            ):
                raise ValueError("reconciliation references unavailable postings")

            posting_count += len(postings)
            reconciliations.extend(account_reconciliations)
            kill_state = derive_kill_state(kill_events, production=True)
            if not kill_state.engaged:
                raise ValueError("production execution must remain killed")

            for plan in plans:
                timeline.append(
                    _timeline_entry(
                        as_of,
                        "plan",
                        plan.plan_id,
                        plan.observed_at,
                        "PLANNED",
                        None,
                        False,
                        plan,
                    )
                )
            for intent in intents:
                timeline.append(
                    _timeline_entry(
                        as_of,
                        "intent",
                        intent.intent_id,
                        intent.created_at,
                        "INTENT_RECORDED",
                        None,
                        False,
                        intent,
                    )
                )
                orders = self._store.verified_venue_order_events_for_intent(intent.intent_id, as_of)
                trades = self._store.verified_venue_trade_events_for_intent(intent.intent_id, as_of)
                if len(orders) + len(trades) > _MAX_SAFETY_RECORDS:
                    raise ValueError("dashboard venue event limit exceeded")
                timeline.extend(
                    _timeline_entry(
                        as_of,
                        "order",
                        event.event_id,
                        event.received_at,
                        event.normalized_state.value,
                        None,
                        event.normalized_state.value == "RECONCILED",
                        event,
                    )
                    for event in orders
                )
                timeline.extend(
                    _timeline_entry(
                        as_of,
                        "trade",
                        event.trade_event_id,
                        event.received_at,
                        event.normalized_state.value,
                        None,
                        event.normalized_state.value == "CONFIRMED",
                        event,
                    )
                    for event in trades
                )
            timeline.extend(
                _timeline_entry(
                    as_of,
                    "kill",
                    event.kill_event_id,
                    event.occurred_at,
                    "ENGAGED",
                    "KILL_EVENT_RECORDED",
                    False,
                    event,
                )
                for event in kill_events
            )
            timeline.extend(
                _timeline_entry(
                    as_of,
                    "reconciliation",
                    reconciliation.reconciliation_id,
                    reconciliation.observed_at,
                    "COMPLETE" if reconciliation.complete else "INCOMPLETE",
                    None if reconciliation.complete else "RECONCILIATION_ACTION_REQUIRED",
                    reconciliation.complete,
                    reconciliation,
                )
                for reconciliation in account_reconciliations
            )

        account_scopes = set(accounts)
        timeline.extend(
            _timeline_entry(
                as_of,
                "kill",
                event.kill_event_id,
                event.occurred_at,
                "ENGAGED",
                "KILL_EVENT_RECORDED",
                False,
                event,
            )
            for event in all_kill_events
            if event.scope not in account_scopes
        )

        if len(timeline) > _MAX_SAFETY_RECORDS:
            raise ValueError("dashboard execution timeline limit exceeded")
        timeline.sort(key=lambda item: (item.occurred_at, item.record_id), reverse=True)

        unmet_gates = {"CAPABILITY_VERIFIER_NOT_CONFIGURED", "EXECUTION_KILL_ENGAGED"}
        if not manifest_gate.allowed and manifest_gate.reason is not None:
            unmet_gates.add(manifest_gate.reason)
        if protocol.state != "CURRENT":
            unmet_gates.add("PROTOCOL_REVIEW_REQUIRED")
        if conformance_result != "CONFORMANT":
            unmet_gates.add("PROTOCOL_CONFORMANCE_REQUIRED")
        unmet = tuple(sorted(unmet_gates))
        source_hashes = set(expected_fixture_hashes)
        source_hashes.update(expected_source_hashes)
        if manifest is not None:
            source_hashes.update(manifest.source_hashes)

        complete_count = sum(record.complete for record in reconciliations)
        readiness = ExecutionReadinessSummary(
            schema_version=1,
            as_of=as_of,
            implementation_state="LIVE_DISABLED",
            protocol_state=protocol.state,
            conformance_result=conformance_result,
            conformance_observed_at=(
                None if latest_conformance is None else latest_conformance.observed_at
            ),
            kill_engaged=True,
            kill_trigger=None if latest_kill is None else "KILL_EVENT_RECORDED",
            production_capability_available=False,
            live_action_available=False,
            unmet_gates=unmet,
        )
        ledger = LiveLedgerSummary(
            schema_version=1,
            as_of=as_of,
            posting_count=posting_count,
            reconciliation_count=len(reconciliations),
            complete_reconciliation_count=complete_count,
            incomplete_reconciliation_count=len(reconciliations) - complete_count,
            pnl_publishable=False,
            realized_pnl_usd=None,
        )
        evidence = EvidenceStatus(
            schema_version=1,
            as_of=as_of,
            protocol_version=POLYMARKET_PROTOCOL_VERSION,
            protocol_state=protocol.state,
            manifest_state=("MISSING" if manifest is None else manifest.implementation_state.value),
            conformance_result=conformance_result,
            conformance_observed_at=(
                None if latest_conformance is None else latest_conformance.observed_at
            ),
            account_count=len(accounts),
            source_hashes=tuple(sorted(source_hashes)),
            unmet_activation_gates=unmet,
        )
        return (
            readiness,
            self._market_atlas_opportunities(as_of),
            tuple(timeline[:_MAX_EXECUTION_TIMELINE_SHOWN]),
            ledger,
            evidence,
        )

    def _market_atlas_opportunities(self, as_of: datetime) -> tuple[MarketAtlasOpportunity, ...]:
        candidates = self._store.verified_candidate_relationships_as_of(as_of)
        proofs = self._store.verified_proof_artifacts_as_of(as_of)
        reports = self._store.verified_scan_reports_as_of(as_of)
        if max(len(candidates), len(proofs), len(reports)) > _MAX_SAFETY_RECORDS:
            raise ValueError("dashboard opportunity evidence limit exceeded")
        candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
        if len(candidates_by_id) != len(candidates):
            raise ValueError("duplicate candidate identity in opportunity evidence")
        if any(proof.candidate_id not in candidates_by_id for proof in proofs):
            raise ValueError("proof references an unavailable candidate")
        if any(report.candidate_id not in candidates_by_id for report in reports):
            raise ValueError("scan report references an unavailable candidate")
        proofs_by_id = {proof.proof_id: proof for proof in proofs}
        latest_proof = {proof.candidate_id: proof for proof in proofs}
        latest_report = {report.candidate_id: report for report in reports}
        opportunities = []
        for candidate in candidates:
            report = latest_report.get(candidate.candidate_id)
            if report is None:
                proof = latest_proof.get(candidate.candidate_id)
            elif report.proof_id is None:
                proof = None
            else:
                proof = proofs_by_id.get(report.proof_id)
                if proof is None or proof.candidate_id != report.candidate_id:
                    raise ValueError("scan report references an unavailable proof")
            economics = None if report is None else report.economics
            evidence_records = tuple(
                record for record in (candidate, proof, report) if record is not None
            )
            opportunities.append(
                MarketAtlasOpportunity(
                    schema_version=1,
                    as_of=as_of,
                    candidate_id=candidate.candidate_id,
                    proof_id=None if proof is None else proof.proof_id,
                    relationship_type=candidate.relationship_type,
                    decision=None if report is None else report.decision,
                    conservative_surplus_usd=(
                        None if economics is None else economics.conservative_surplus_usd
                    ),
                    capacity_usd=(
                        None if economics is None else economics.capacity_usd_at_current_depth
                    ),
                    reconciled=False,
                    evidence_hashes=tuple(
                        sorted(canonical_execution_hash(record) for record in evidence_records)
                    ),
                )
            )
        return tuple(reversed(opportunities[-_MAX_OPPORTUNITIES_SHOWN:]))

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
        candidates = self._store.verified_candidate_relationships_as_of(as_of)
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
        proofs = self._store.verified_proof_artifacts_as_of(as_of)
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
        reports = self._store.verified_scan_reports_as_of(as_of)
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
        plans = self._store.verified_shadow_plans_as_of(as_of)
        proposal_ids = [plan.proposal_id for plan in plans]
        if len(set(proposal_ids)) != len(proposal_ids):
            raise ValueError("duplicate shadow plan proposal identity")

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
        for plan in plans:
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
            fees = _verified_shadow_fees(self._store, plan)
            postings = self._store.verified_ledger_postings_for_proposal(plan.proposal_id, as_of)
            expected_postings = postings_for_events(plan, events, fees)
            if sorted(postings, key=lambda posting: posting.posting_id) != sorted(
                expected_postings, key=lambda posting: posting.posting_id
            ):
                raise ValueError("shadow ledger postings do not match visible fills")
            paper_pnl = _validated_shadow_pnl(
                plan=plan,
                events=events,
                postings=expected_postings,
                fees=fees,
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


def _require_bounded_safety_records(**families: tuple[object, ...]) -> None:
    for name, records in families.items():
        if len(records) > _MAX_SAFETY_RECORDS:
            raise ValueError(f"dashboard {name} limit exceeded")


def _timeline_entry(
    as_of: datetime,
    kind: str,
    record_id: object,
    occurred_at: datetime,
    state: str,
    reason_code: str | None,
    reconciled: bool,
    record: BaseModel,
) -> ExecutionTimelineEntry:
    return ExecutionTimelineEntry.model_validate(
        {
            "schema_version": 1,
            "as_of": as_of,
            "kind": kind,
            "record_id": record_id,
            "occurred_at": occurred_at,
            "state": state,
            "reason_code": reason_code,
            "reconciled": reconciled,
            "evidence_hashes": (canonical_execution_hash(record),),
        },
        strict=True,
    )


def _validated_shadow_pnl(
    *,
    plan: ShadowPlan,
    events: tuple[ShadowEvent, ...],
    postings: tuple[LedgerPosting, ...],
    fees: dict[int, PredictionFeeRate],
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
        expected_reconciliation = reconcile_proposal(plan, events[:-1], postings, fees)
        if reconciliation != expected_reconciliation:
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
        expected_reconciliation = reconcile_proposal(plan, events, postings, fees)
        if reconciliation != expected_reconciliation:
            raise ValueError("unreconciled shadow evidence is inconsistent")
        if proposal_paper_pnl(postings, reconciliation, events) is not None:
            raise ValueError("unreconciled shadow proposal cannot have authoritative paper P&L")
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


def _verified_shadow_fees(
    store: PredictionMarketStore, plan: ShadowPlan
) -> dict[int, PredictionFeeRate]:
    result: dict[int, PredictionFeeRate] = {}
    for leg in plan.legs:
        matches: list[PredictionFeeRate] = []
        market_ids = (leg.market_id, None) if leg.market_id is not None else (None,)
        for source_hash in plan.frozen_hashes:
            for market_id in market_ids:
                fee = store.verified_fee_rate_by_source_hash(
                    leg.venue,
                    market_id,
                    source_hash,
                    plan.information_cutoff,
                )
                if fee is not None and fee not in matches:
                    matches.append(fee)
        if len(matches) != 1:
            raise ValueError("shadow plan must cite exactly one frozen fee evidence row per leg")
        result[leg.leg_index] = matches[0]
    return result


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


def build_prediction_dashboard_snapshot(
    database_path: Path, *, now: datetime
) -> PredictionDashboardSnapshot:
    cutoff = normalize_utc_timestamp(now)
    store = PredictionMarketStore(database_path, read_only=True)
    try:
        return PredictionDashboardBuilder(store, database_path).build(cutoff)
    finally:
        store.close()


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
