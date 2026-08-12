from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.domain.models import StrictRecord, normalize_utc_timestamp

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]


class ParameterValue(StrictRecord):
    kind: Literal["string", "boolean", "integer", "decimal"]
    string_value: str | None = None
    boolean_value: bool | None = None
    integer_value: int | None = None
    decimal_value: Decimal | None = None

    @model_validator(mode="after")
    def require_value_matching_kind(self) -> "ParameterValue":
        fields = {
            "string": self.string_value,
            "boolean": self.boolean_value,
            "integer": self.integer_value,
            "decimal": self.decimal_value,
        }
        if fields[self.kind] is None or sum(value is not None for value in fields.values()) != 1:
            raise ValueError("parameter value must contain exactly the field selected by kind")
        return self


class ExperimentParameter(StrictRecord):
    name: NonEmptyString
    value: ParameterValue


class EvaluationWindow(StrictRecord):
    starts_at: datetime
    ends_at: datetime

    @field_validator("starts_at", "ends_at")
    @classmethod
    def require_utc_window_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_increasing_window(self) -> "EvaluationWindow":
        if self.ends_at <= self.starts_at:
            raise ValueError("evaluation window must end after it starts")
        return self


class SuccessCriterion(StrictRecord):
    metric: NonEmptyString
    operator: Literal["gt", "gte", "lt", "lte", "eq"]
    threshold: Decimal


class ExperimentRecord(StrictRecord):
    schema_version: Literal[1]
    experiment_id: UUID
    hypothesis: NonEmptyString
    feature_allowlist: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    parameters: tuple[ExperimentParameter, ...]
    evaluation_window: EvaluationWindow
    benchmark: NonEmptyString
    success_criteria: Annotated[tuple[SuccessCriterion, ...], Field(min_length=1)]
    code_revision: NonEmptyString
    data_cutoff: datetime
    fee_version: NonEmptyString
    trial_family_id: NonEmptyString

    @field_validator("data_cutoff")
    @classmethod
    def require_utc_data_cutoff(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("feature_allowlist")
    @classmethod
    def canonicalize_features(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("feature allowlist must not contain duplicates")
        return tuple(sorted(value))

    @field_validator("parameters")
    @classmethod
    def canonicalize_parameters(
        cls, value: tuple[ExperimentParameter, ...]
    ) -> tuple[ExperimentParameter, ...]:
        if len({parameter.name for parameter in value}) != len(value):
            raise ValueError("parameter names must be unique")
        return tuple(sorted(value, key=lambda parameter: parameter.name))

    @field_validator("success_criteria")
    @classmethod
    def canonicalize_success_criteria(
        cls, value: tuple[SuccessCriterion, ...]
    ) -> tuple[SuccessCriterion, ...]:
        return tuple(
            sorted(
                value,
                key=lambda criterion: (
                    criterion.metric,
                    criterion.operator,
                    criterion.threshold,
                ),
            )
        )
