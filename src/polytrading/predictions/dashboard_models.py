from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.predictions.candidates_models import CandidateDisposition, RelationshipType
from polytrading.predictions.domain import (
    MarketRecord,
    PredictionBookSnapshot,
    PredictionRecord,
    PredictionVenue,
    Sha256,
)
from polytrading.predictions.economics_models import ScanDecision
from polytrading.predictions.health import PredictionHealthReport
from polytrading.predictions.proofs_models import ProofRejectionReason, ProofStatus
from polytrading.predictions.shadow_models import ShadowState

NonNegativeCount = Annotated[int, Field(ge=0)]
FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]


class DashboardDomain(StrEnum):
    OVERVIEW = "overview"
    MARKETS = "markets"
    EXECUTION = "execution"
    LEDGER = "ledger"
    EVIDENCE = "evidence"


class ExecutionReadinessSummary(PredictionRecord):
    schema_version: Literal[1]
    as_of: datetime
    implementation_state: Literal["LIVE_DISABLED"]
    protocol_state: Literal["CURRENT", "PROTOCOL_REVIEW_REQUIRED"]
    conformance_result: NonEmptyString
    conformance_observed_at: datetime | None
    kill_engaged: Literal[True]
    kill_trigger: NonEmptyString | None
    production_capability_available: Literal[False]
    live_action_available: Literal[False]
    unmet_gates: tuple[NonEmptyString, ...]

    @field_validator("conformance_observed_at")
    @classmethod
    def _conformance_timestamp_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("unmet_gates")
    @classmethod
    def _unmet_gates_are_sorted_unique(
        cls, value: tuple[NonEmptyString, ...]
    ) -> tuple[NonEmptyString, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("unmet_gates must be sorted and unique")
        return value


class MarketAtlasOpportunity(PredictionRecord):
    schema_version: Literal[1]
    as_of: datetime
    candidate_id: UUID
    proof_id: UUID | None
    relationship_type: RelationshipType
    decision: ScanDecision | None
    conservative_surplus_usd: FiniteDecimal | None
    capacity_usd: FiniteDecimal | None
    reconciled: bool
    evidence_hashes: tuple[Sha256, ...]

    @field_validator("evidence_hashes")
    @classmethod
    def _opportunity_hashes_sorted_unique(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("evidence_hashes must be sorted and unique")
        return value


class ExecutionTimelineEntry(PredictionRecord):
    schema_version: Literal[1]
    as_of: datetime
    kind: Literal["plan", "intent", "order", "trade", "kill", "reconciliation"]
    record_id: NonEmptyString
    occurred_at: datetime
    state: NonEmptyString
    reason_code: NonEmptyString | None
    reconciled: bool
    evidence_hashes: tuple[Sha256, ...]

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("evidence_hashes")
    @classmethod
    def _timeline_hashes_sorted_unique(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("evidence_hashes must be sorted and unique")
        return value


class LiveLedgerSummary(PredictionRecord):
    schema_version: Literal[1]
    as_of: datetime
    posting_count: NonNegativeCount
    reconciliation_count: NonNegativeCount
    complete_reconciliation_count: NonNegativeCount
    incomplete_reconciliation_count: NonNegativeCount
    pnl_publishable: bool
    realized_pnl_usd: FiniteDecimal | None

    @model_validator(mode="after")
    def _ledger_totals_are_coherent(self) -> LiveLedgerSummary:
        if (
            self.complete_reconciliation_count + self.incomplete_reconciliation_count
            != self.reconciliation_count
        ):
            raise ValueError("reconciliation counts must sum to reconciliation_count")
        if self.pnl_publishable != (self.realized_pnl_usd is not None):
            raise ValueError("realized P&L is present exactly when publishable")
        return self


class EvidenceStatus(PredictionRecord):
    schema_version: Literal[1]
    as_of: datetime
    protocol_version: NonEmptyString
    protocol_state: Literal["CURRENT", "PROTOCOL_REVIEW_REQUIRED"]
    manifest_state: NonEmptyString
    conformance_result: NonEmptyString
    conformance_observed_at: datetime | None
    account_count: NonNegativeCount
    source_hashes: tuple[Sha256, ...]
    unmet_activation_gates: tuple[NonEmptyString, ...]

    @field_validator("conformance_observed_at")
    @classmethod
    def _evidence_timestamp_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("source_hashes", "unmet_activation_gates")
    @classmethod
    def _evidence_tuples_sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("evidence tuples must be sorted and unique")
        return value


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


class ProofListing(PredictionRecord):
    schema_version: Literal[1]
    proof_id: UUID
    candidate_id: UUID
    template: str
    status: ProofStatus
    rejection_reason: ProofRejectionReason | None
    minimum_basket_payout: Decimal | None
    observed_at: datetime


class ProofSummary(PredictionRecord):
    schema_version: Literal[1]
    total: int
    by_status: dict[str, int]
    by_template: dict[str, int]
    latest: tuple[ProofListing, ...]


class ScanListing(PredictionRecord):
    schema_version: Literal[1]
    candidate_id: UUID
    decision: ScanDecision
    reason: str
    surplus: Decimal | None
    capacity: Decimal | None
    as_of: datetime


class ScanSummary(PredictionRecord):
    schema_version: Literal[1]
    total: int
    by_decision: dict[str, int]
    latest: tuple[ScanListing, ...]


class ShadowListing(PredictionRecord):
    schema_version: Literal[1]
    proposal_id: UUID
    candidate_id: UUID
    current_state: ShadowState
    scenario_id: NonEmptyString | None
    quantity: PositiveDecimal
    paper_pnl: FiniteDecimal | None
    observed_at: datetime

    @model_validator(mode="after")
    def _require_pnl_only_for_reconciled_rows(self) -> ShadowListing:
        if (self.current_state is ShadowState.RECONCILED) != (self.paper_pnl is not None):
            raise ValueError("paper P&L is present exactly for reconciled rows")
        return self


class ShadowSummary(PredictionRecord):
    schema_version: Literal[1]
    proposals_total: NonNegativeCount
    by_terminal_state: dict[str, NonNegativeCount]
    reconciled_count: NonNegativeCount
    reconciled_paper_pnl_usd: FiniteDecimal
    unreconciled_count: NonNegativeCount
    latest: Annotated[tuple[ShadowListing, ...], Field(max_length=20)]
    experiments_by_family: dict[str, NonNegativeCount]

    @field_validator("by_terminal_state", "experiments_by_family")
    @classmethod
    def _require_sorted_nonblank_mapping(
        cls, value: dict[str, NonNegativeCount]
    ) -> dict[str, NonNegativeCount]:
        if any(not key.strip() for key in value):
            raise ValueError("summary keys must not be blank")
        if tuple(value) != tuple(sorted(value)):
            raise ValueError("summary mappings must be sorted")
        return value

    @field_validator("by_terminal_state")
    @classmethod
    def _require_known_shadow_states(
        cls, value: dict[str, NonNegativeCount]
    ) -> dict[str, NonNegativeCount]:
        known_states = {state.value for state in ShadowState}
        if not set(value).issubset(known_states):
            raise ValueError("terminal-state counts must use shadow state values")
        return value

    @field_validator("latest")
    @classmethod
    def _require_unique_newest_first(
        cls, value: tuple[ShadowListing, ...]
    ) -> tuple[ShadowListing, ...]:
        value = tuple(ShadowListing.model_validate(item.model_dump()) for item in value)
        proposal_ids = tuple(item.proposal_id for item in value)
        if len(set(proposal_ids)) != len(proposal_ids):
            raise ValueError("latest proposals must be unique")
        expected = tuple(
            sorted(value, key=lambda item: (item.observed_at, item.proposal_id), reverse=True)
        )
        if value != expected:
            raise ValueError("latest proposals must be newest first")
        return value

    @model_validator(mode="after")
    def _require_consistent_counts(self) -> ShadowSummary:
        if sum(self.by_terminal_state.values()) != self.proposals_total:
            raise ValueError("terminal-state counts must sum to proposals_total")
        if self.reconciled_count + self.unreconciled_count != self.proposals_total:
            raise ValueError("reconciled and unreconciled counts must sum to proposals_total")
        if self.by_terminal_state.get(ShadowState.RECONCILED.value, 0) != self.reconciled_count:
            raise ValueError("reconciled_count must match current-state counts")
        latest_counts: dict[str, int] = {}
        for listing in self.latest:
            key = listing.current_state.value
            latest_counts[key] = latest_counts.get(key, 0) + 1
        if any(
            count > self.by_terminal_state.get(state, 0) for state, count in latest_counts.items()
        ):
            raise ValueError("latest rows must be represented by current-state counts")
        if self.reconciled_count == 0 and self.reconciled_paper_pnl_usd != 0:
            raise ValueError("no reconciled proposals requires zero aggregate paper P&L")
        if len(self.latest) == self.proposals_total:
            listed_pnl = sum(
                (listing.paper_pnl for listing in self.latest if listing.paper_pnl is not None),
                Decimal("0"),
            )
            if listed_pnl != self.reconciled_paper_pnl_usd:
                raise ValueError("aggregate paper P&L must match complete latest rows")
        return self


class PredictionDashboardSnapshot(PredictionRecord):
    schema_version: Literal[1]
    revision_id: Sha256
    as_of: datetime
    health: PredictionHealthReport
    markets: tuple[MarketRecord, ...]
    books: tuple[PredictionBookSnapshot, ...]
    evidence_counts: PredictionEvidenceCounts
    recipes: PredictionOperationRecipes
    candidates: CandidateSummary
    proofs: ProofSummary
    scans: ScanSummary
    shadow: ShadowSummary
    execution_readiness: ExecutionReadinessSummary
    opportunities: Annotated[tuple[MarketAtlasOpportunity, ...], Field(max_length=200)]
    execution_timeline: Annotated[tuple[ExecutionTimelineEntry, ...], Field(max_length=500)]
    live_ledger: LiveLedgerSummary
    evidence_status: EvidenceStatus

    def cutoff_bound_sections(self) -> tuple[PredictionRecord, ...]:
        return (
            self.execution_readiness,
            *self.opportunities,
            *self.execution_timeline,
            self.live_ledger,
            self.evidence_status,
        )

    @model_validator(mode="after")
    def _one_coherent_cutoff(self) -> PredictionDashboardSnapshot:
        if any(section.as_of != self.as_of for section in self.cutoff_bound_sections()):
            raise ValueError("dashboard sections must share one as_of cutoff")
        if any(item.occurred_at > self.as_of for item in self.execution_timeline):
            raise ValueError("execution timeline cannot exceed the dashboard cutoff")
        return self
