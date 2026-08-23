from __future__ import annotations

from typing import Annotated, Literal

from pydantic import StringConstraints, model_validator

from polytrading.predictions.domain import PredictionRecord, Sha256

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]

PropositionKind = Literal[
    "binary_condition",
    "threshold",
    "deadline",
    "scope",
    "outcome_membership",
]


class PropositionSpan(PredictionRecord):
    """An exact supporting source span, indexed into one immutable rule version.

    ``rule_source_hash`` is the SHA-256 of the exact rule text this span's character
    offsets index into (a ``RuleVersion.source_hash``), so a span is only meaningful
    against that one immutable rule version.
    """

    start_char: int
    end_char: int
    exact_text: NonEmptyString
    rule_source_hash: Sha256

    @model_validator(mode="after")
    def _require_valid_bounds(self) -> PropositionSpan:
        if self.start_char < 0 or self.end_char <= self.start_char:
            raise ValueError("proposition span must have nonempty nonnegative bounds")
        return self


class TypedProposition(PredictionRecord):
    """A typed, evidence-backed proposition extracted from a rule version.

    No proposition field is ever inferred: an extractor that cannot support a field
    with an exact source span emits ``status="unknown"`` rather than guessing.
    """

    schema_version: Literal[1]
    kind: PropositionKind
    subject: NonEmptyString
    predicate: NonEmptyString
    value: str | None
    status: Literal["extracted", "unknown"]
    supporting_spans: tuple[PropositionSpan, ...]

    @model_validator(mode="after")
    def _require_evidence_consistent_with_status(self) -> TypedProposition:
        if self.status == "extracted":
            if not self.supporting_spans:
                raise ValueError("extracted proposition requires supporting spans")
        else:
            if self.value is not None:
                raise ValueError("unknown proposition cannot have a value")
            if self.supporting_spans:
                raise ValueError("unknown proposition cannot have supporting spans")
        return self
