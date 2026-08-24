from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.predictions.domain import (
    NonNegativeDecimal,
    PredictionRecord,
    PredictionVenue,
    Sha256,
)
from polytrading.predictions.propositions import PropositionSpan

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]

VoidBehavior = Literal["refund_at_cost", "resolve_to_rules_price", "unknown"]


class RuleAttestation(PredictionRecord):
    """A human-reviewed, hash-bound attestation of one rule version's typed payout facts.

    This is the only bridge from natural-language market rules to typed facts the proof
    compilers consume (spec §5): there is deliberately no code path that generates an
    attestation's content, only the ``predictions attest`` CLI that ingests an
    operator-authored file. Every ``supporting_spans`` entry must be indexed into the
    exact rule text this attestation itself is bound to -- see ``rule_source_hash``.
    """

    schema_version: Literal[1]
    attestation_id: UUID
    venue: PredictionVenue
    market_id: str
    rule_version_id: UUID
    rule_source_hash: Sha256
    payout_unit: Literal["usdc_1_per_share", "usd_1_per_contract"]
    winner_payout_per_share: PositiveDecimal
    loser_payout_per_share: NonNegativeDecimal
    outcome_set_exhaustive: bool
    void_or_invalid_possible: bool
    void_behavior: VoidBehavior
    tie_possible: bool
    tie_behavior: str | None
    resolution_source_attested: str
    deadline_utc: datetime | None
    threshold_text: str | None
    threshold_inclusive: bool | None
    supporting_spans: tuple[PropositionSpan, ...]
    review_identity: NonEmptyString
    reviewed_at: datetime

    @field_validator("supporting_spans")
    @classmethod
    def _require_at_least_one_span(
        cls, value: tuple[PropositionSpan, ...]
    ) -> tuple[PropositionSpan, ...]:
        if not value:
            raise ValueError("rule attestation requires at least one supporting span")
        return value

    @model_validator(mode="after")
    def _require_consistent_attestation(self) -> RuleAttestation:
        if self.tie_possible and self.tie_behavior is None:
            raise ValueError("tie_possible=True requires a non-None tie_behavior")
        if any(span.rule_source_hash != self.rule_source_hash for span in self.supporting_spans):
            raise ValueError(
                "every supporting span must be bound to this attestation's own rule_source_hash"
            )
        return self
