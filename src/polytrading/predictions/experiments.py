from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import StringConstraints, field_validator, model_validator

from polytrading.predictions.domain import (
    PredictionRecord,
    PredictionVenue,
    normalize_utc_timestamp,
)
from polytrading.predictions.shadow_models import ShadowState

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
ExperimentTerminalState = Literal[
    ShadowState.COMPLETE,
    ShadowState.UNWOUND,
    ShadowState.EXPIRED,
    ShadowState.UNKNOWN,
    ShadowState.RECONCILED,
]


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


class TrialFamily(PredictionRecord):
    family_id: NonEmptyString
    hypothesis: NonEmptyString
    preregistered_at: datetime
    thresholds_json: NonEmptyString
    venues: tuple[PredictionVenue, ...]
    registered_by: NonEmptyString

    @field_validator("family_id", "hypothesis", "thresholds_json", "registered_by")
    @classmethod
    def _require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text fields must not be blank")
        return value

    @field_validator("preregistered_at")
    @classmethod
    def _require_utc_preregistered_at(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("thresholds_json")
    @classmethod
    def _require_json_object(cls, value: str) -> str:
        try:
            thresholds = json.loads(value, parse_constant=_reject_nonstandard_json_constant)
        except (TypeError, ValueError) as error:
            raise ValueError("thresholds_json must contain a JSON object") from error
        if not isinstance(thresholds, dict):
            raise ValueError("thresholds_json must contain a JSON object")
        return value

    @field_validator("venues")
    @classmethod
    def _require_sorted_unique_venues(
        cls, value: tuple[PredictionVenue, ...]
    ) -> tuple[PredictionVenue, ...]:
        if not value or value != tuple(sorted(set(value), key=lambda venue: venue.value)):
            raise ValueError("venues must be nonempty, sorted, and unique")
        return value


class ShadowExperiment(PredictionRecord):
    experiment_id: UUID
    family_id: NonEmptyString
    proposal_id: UUID
    scenario_id: NonEmptyString
    terminal_state: ExperimentTerminalState
    paper_pnl_usd: Decimal | None
    reconciled: bool
    as_of: datetime
    observed_at: datetime

    @field_validator("family_id", "scenario_id")
    @classmethod
    def _require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text fields must not be blank")
        return value

    @model_validator(mode="after")
    def _require_reconciliation_before_paper_pnl(self) -> ShadowExperiment:
        if self.paper_pnl_usd is not None and not self.reconciled:
            raise ValueError("paper_pnl_usd requires reconciled=True")
        return self
