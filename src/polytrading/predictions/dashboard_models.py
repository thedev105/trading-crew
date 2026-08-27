from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

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
ActivationGateCode = Literal[
    "AUTOMATED_USE_RESTRICTED",
    "CAPABILITY_VERIFIER_NOT_CONFIGURED",
    "COLLECTION_NOT_PERMITTED",
    "EXECUTION_KILL_ENGAGED",
    "JURISDICTION_BLOCKED",
    "JURISDICTION_UNREVIEWED",
    "LIVE_NOT_ELIGIBLE",
    "MANIFEST_NOT_FOUND",
    "PROTOCOL_CONFORMANCE_REQUIRED",
    "PROTOCOL_REVIEW_REQUIRED",
]
TimelineState = Literal[
    "ACK_DELAYED",
    "ACK_LIVE_UNEXPECTED",
    "ACK_MATCHED",
    "CANCEL_PENDING",
    "CANCELLED",
    "COMPLETE",
    "CONFIRMED",
    "ENGAGED",
    "FAILED",
    "FILLED",
    "INCOMPLETE",
    "INTENT_RECORDED",
    "MATCHED",
    "MATCHED_NOT_BROADCASTED",
    "MINED",
    "PARTIALLY_FILLED",
    "PLANNED",
    "RECONCILED",
    "REJECTED",
    "RETRYING",
    "SIGNED",
    "SUBMITTING",
    "UNKNOWN",
]


class FrozenDict(dict[str, Any]):
    """A detached JSON-object-compatible mapping with no mutation surface."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("dashboard mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            object.__setattr__(value, name, _deep_freeze(getattr(value, name)))
        return value
    if isinstance(value, Mapping):
        return FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_deep_freeze(item) for item in value)
    return value


class DashboardRecord(PredictionRecord):
    """Dashboard publication boundary with validated copies and immutable children."""

    @model_validator(mode="after")
    def _freeze_publication_tree(self) -> Self:
        return _deep_freeze(self)

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        del _fields_set
        return cls.model_validate(values, strict=True)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        del deep
        values = self.model_dump(mode="python", round_trip=True)
        if update is not None:
            values.update(dict(update))
        return type(self).model_validate(values, strict=True)


class DashboardDomain(StrEnum):
    OVERVIEW = "overview"
    MARKETS = "markets"
    EXECUTION = "execution"
    LEDGER = "ledger"
    EVIDENCE = "evidence"


class ExecutionReadinessSummary(DashboardRecord):
    schema_version: Literal[1]
    as_of: datetime
    implementation_state: Literal["LIVE_DISABLED"]
    protocol_state: Literal["CURRENT", "PROTOCOL_REVIEW_REQUIRED"]
    conformance_result: Literal["CONFORMANT", "PROTOCOL_REVIEW_REQUIRED"]
    conformance_observed_at: datetime | None
    kill_engaged: Literal[True]
    kill_trigger: Literal["KILL_EVENT_RECORDED"] | None
    production_capability_available: Literal[False]
    live_action_available: Literal[False]
    unmet_gates: tuple[ActivationGateCode, ...]

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


class MarketAtlasOpportunity(DashboardRecord):
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


class ExecutionTimelineEntry(DashboardRecord):
    schema_version: Literal[1]
    as_of: datetime
    kind: Literal["plan", "intent", "order", "trade", "kill", "reconciliation"]
    record_id: UUID
    occurred_at: datetime
    state: TimelineState
    reason_code: Literal["KILL_EVENT_RECORDED", "RECONCILIATION_ACTION_REQUIRED"] | None
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


class LiveLedgerSummary(DashboardRecord):
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


class EvidenceStatus(DashboardRecord):
    schema_version: Literal[1]
    as_of: datetime
    protocol_version: Literal["polymarket-clob-2026-08-25-v1"]
    protocol_state: Literal["CURRENT", "PROTOCOL_REVIEW_REQUIRED"]
    manifest_state: Literal["MISSING", "LIVE_DISABLED"]
    conformance_result: Literal["CONFORMANT", "PROTOCOL_REVIEW_REQUIRED"]
    conformance_observed_at: datetime | None
    account_count: NonNegativeCount
    source_hashes: tuple[Sha256, ...]
    unmet_activation_gates: tuple[ActivationGateCode, ...]

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


class PredictionEvidenceCounts(DashboardRecord):
    schema_version: Literal[1]
    counts: dict[str, int]


class PredictionOperationRecipes(DashboardRecord):
    schema_version: Literal[1]
    recipes: tuple[str, ...]


class CandidateListing(DashboardRecord):
    schema_version: Literal[1]
    candidate_id: UUID
    relationship_type: RelationshipType
    venues: tuple[PredictionVenue, ...]
    disposition: CandidateDisposition
    provenance_kind: str
    unresolved_field_count: int
    observed_at: datetime


class CandidateSummary(DashboardRecord):
    schema_version: Literal[1]
    total: int
    by_relationship_type: dict[str, int]
    by_disposition: dict[str, int]
    by_provenance_kind: dict[str, int]
    latest: tuple[CandidateListing, ...]


class ProofListing(DashboardRecord):
    schema_version: Literal[1]
    proof_id: UUID
    candidate_id: UUID
    template: str
    status: ProofStatus
    rejection_reason: ProofRejectionReason | None
    minimum_basket_payout: Decimal | None
    observed_at: datetime


class ProofSummary(DashboardRecord):
    schema_version: Literal[1]
    total: int
    by_status: dict[str, int]
    by_template: dict[str, int]
    latest: tuple[ProofListing, ...]


class ScanListing(DashboardRecord):
    schema_version: Literal[1]
    candidate_id: UUID
    decision: ScanDecision
    reason: str
    surplus: Decimal | None
    capacity: Decimal | None
    as_of: datetime


class ScanSummary(DashboardRecord):
    schema_version: Literal[1]
    total: int
    by_decision: dict[str, int]
    latest: tuple[ScanListing, ...]


class ShadowListing(DashboardRecord):
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


class ShadowSummary(DashboardRecord):
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


class _PredictionDashboardContent(DashboardRecord):
    schema_version: Literal[1]
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
    def _one_coherent_cutoff(self) -> _PredictionDashboardContent:
        if any(section.as_of != self.as_of for section in self.cutoff_bound_sections()):
            raise ValueError("dashboard sections must share one as_of cutoff")
        if any(item.occurred_at > self.as_of for item in self.execution_timeline):
            raise ValueError("execution timeline cannot exceed the dashboard cutoff")
        return self


class PredictionDashboardSnapshot(_PredictionDashboardContent):
    revision_id: Sha256

    def deterministic_revision_id(self) -> Sha256:
        canonical = json.dumps(
            self.model_dump(mode="json", exclude={"revision_id"}),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256(canonical).hexdigest()

    @classmethod
    def finalize(cls, **values: Any) -> PredictionDashboardSnapshot:
        if "revision_id" in values:
            raise ValueError("finalization computes revision_id")
        content = _PredictionDashboardContent.model_validate(values, strict=True)
        canonical = json.dumps(
            content.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return cls.model_validate(
            {
                **content.model_dump(mode="python", round_trip=True),
                "revision_id": sha256(canonical).hexdigest(),
            },
            strict=True,
        )

    @model_validator(mode="after")
    def _revision_matches_content(self) -> PredictionDashboardSnapshot:
        if self.revision_id != self.deterministic_revision_id():
            raise ValueError("dashboard revision does not match snapshot content")
        return self
