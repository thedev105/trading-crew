from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.predictions.domain import (
    PredictionRecord,
    PredictionVenue,
    Sha256,
    normalize_utc_timestamp,
)

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

# Fixed namespace for deterministic shadow-proposal identity (UUIDv5). It is deliberately
# distinct from candidate, proof, and scan-report identities so their content spaces cannot
# collide even if they happen to serialize to the same JSON.
_SHADOW_PROPOSAL_IDENTITY_NAMESPACE = UUID("95d2e83f-b961-4adc-b2e3-c8a1f46fd50f")


class ShadowState(StrEnum):
    DISCOVERED = "discovered"
    PROOF_VALIDATED = "proof_validated"
    ECONOMICS_VALIDATED = "economics_validated"
    SHADOW_PLANNED = "shadow_planned"
    FIRST_LEG_SIMULATED = "first_leg_simulated"
    COMPLETE = "complete"
    UNWOUND = "unwound"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"


ALLOWED_TRANSITIONS: frozenset[tuple[ShadowState, ShadowState]] = frozenset(
    {
        (ShadowState.DISCOVERED, ShadowState.PROOF_VALIDATED),
        (ShadowState.PROOF_VALIDATED, ShadowState.ECONOMICS_VALIDATED),
        (ShadowState.ECONOMICS_VALIDATED, ShadowState.SHADOW_PLANNED),
        (ShadowState.SHADOW_PLANNED, ShadowState.FIRST_LEG_SIMULATED),
        (ShadowState.FIRST_LEG_SIMULATED, ShadowState.COMPLETE),
        (ShadowState.FIRST_LEG_SIMULATED, ShadowState.UNWOUND),
        (ShadowState.FIRST_LEG_SIMULATED, ShadowState.EXPIRED),
        (ShadowState.FIRST_LEG_SIMULATED, ShadowState.UNKNOWN),
        (ShadowState.COMPLETE, ShadowState.RECONCILED),
        (ShadowState.UNWOUND, ShadowState.RECONCILED),
        (ShadowState.EXPIRED, ShadowState.RECONCILED),
        (ShadowState.UNKNOWN, ShadowState.RECONCILED),
    }
)


class ShadowLegPlan(PredictionRecord):
    leg_index: Annotated[int, Field(ge=0)]
    venue: PredictionVenue
    market_id: str
    outcome_token_id: str | None
    sequence_position: Annotated[int, Field(ge=0)]
    limit_price_levels: tuple[tuple[Decimal, Decimal], ...]
    max_quantity: PositiveDecimal

    @field_validator("limit_price_levels")
    @classmethod
    def _require_positive_limit_price_levels(
        cls, value: tuple[tuple[Decimal, Decimal], ...]
    ) -> tuple[tuple[Decimal, Decimal], ...]:
        if not value or any(price <= 0 or size <= 0 for price, size in value):
            raise ValueError("limit_price_levels must contain positive price and size pairs")
        return value


class ShadowPlan(PredictionRecord):
    schema_version: Literal[1]
    proposal_id: UUID
    candidate_id: UUID
    proof_id: UUID
    scan_report_id: UUID
    legs: Annotated[tuple[ShadowLegPlan, ...], Field(min_length=2)]
    bottleneck_leg_index: int
    max_quantity: PositiveDecimal
    order_policy: Literal["taker_cross_only"]
    expires_at: datetime
    completion_path: NonEmptyString
    cancellation_path: NonEmptyString
    unwind_path: NonEmptyString
    max_incomplete_exposure_usd: NonNegativeDecimal
    max_incomplete_loss_usd: NonNegativeDecimal
    frozen_hashes: tuple[Sha256, ...]
    policy_id: str
    policy_version: str
    risk_policy_version: str
    minimum_basket_payout: PositiveDecimal
    kill_conditions: Annotated[tuple[str, ...], Field(min_length=1)]
    information_cutoff: datetime
    observed_at: datetime

    @field_validator("frozen_hashes")
    @classmethod
    def _require_sorted_unique_hashes(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("frozen_hashes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _require_consistent_plan(self) -> ShadowPlan:
        if len({leg.leg_index for leg in self.legs}) != len(self.legs):
            raise ValueError("leg_index values must be unique within a plan")
        if self.bottleneck_leg_index not in {leg.leg_index for leg in self.legs}:
            raise ValueError("bottleneck_leg_index must identify a leg in the plan")
        if {leg.sequence_position for leg in self.legs} != set(range(len(self.legs))):
            raise ValueError("leg sequence_positions must be a permutation of 0..N-1")
        if self.expires_at <= self.observed_at:
            raise ValueError("expires_at must be after observed_at")
        return self


class ShadowFill(PredictionRecord):
    leg_index: Annotated[int, Field(ge=0)]
    side: Literal["buy", "sell"]
    price_levels: tuple[tuple[Decimal, Decimal], ...]
    quantity: PositiveDecimal

    @model_validator(mode="after")
    def _require_consistent_levels(self) -> ShadowFill:
        if not self.price_levels or any(
            price <= 0 or level_quantity <= 0 for price, level_quantity in self.price_levels
        ):
            raise ValueError("price_levels must contain positive price and quantity pairs")
        if self.quantity != sum(level_quantity for _, level_quantity in self.price_levels):
            raise ValueError("quantity must equal the sum of price_levels quantities")
        return self


class ShadowEvent(PredictionRecord):
    schema_version: Literal[1]
    event_id: UUID
    proposal_id: UUID
    sequence: Annotated[int, Field(ge=0)]
    from_state: ShadowState | None
    to_state: ShadowState
    occurred_at: datetime
    detail: str
    quantity_filled: Decimal | None
    leg_index: int | None
    scenario_id: str | None
    fills: tuple[ShadowFill, ...] = ()
    evidence_hashes: tuple[Sha256, ...] = ()

    @field_validator("occurred_at")
    @classmethod
    def _require_utc_occurred_at(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("evidence_hashes")
    @classmethod
    def _require_sorted_unique_evidence_hashes(
        cls, value: tuple[Sha256, ...]
    ) -> tuple[Sha256, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("evidence_hashes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _require_valid_transition(self) -> ShadowEvent:
        if self.sequence == 0:
            if self.from_state is not None or self.to_state is not ShadowState.DISCOVERED:
                raise ValueError("sequence 0 must have no from_state and transition to discovered")
        elif self.from_state is None:
            raise ValueError("only sequence 0 may have no from_state")
        elif (self.from_state, self.to_state) not in ALLOWED_TRANSITIONS:
            raise ValueError("shadow event contains an illegal state transition")
        return self


def derive_current_state(events: Sequence[ShadowEvent]) -> ShadowState:
    """Return the terminal state of a contiguous, append-only shadow event stream."""
    if not events:
        raise ValueError("cannot derive a state from an empty event chain")

    previous_state: ShadowState | None = None
    for expected_sequence, event in enumerate(events):
        if event.sequence != expected_sequence:
            raise ValueError("shadow event sequences must be contiguous from zero")
        if expected_sequence == 0:
            if event.from_state is not None or event.to_state is not ShadowState.DISCOVERED:
                raise ValueError("shadow event sequence zero must begin at discovered")
        elif event.from_state is not previous_state:
            raise ValueError("shadow event chain is not contiguous")
        previous_state = event.to_state

    return previous_state


def deterministic_proposal_id(scan_report_id: UUID, plan_content: Mapping[str, object]) -> UUID:
    """Derive a stable proposal identity from a scan report and frozen plan content."""
    canonical = json.dumps(
        [str(scan_report_id), plan_content],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_SHADOW_PROPOSAL_IDENTITY_NAMESPACE, canonical)
