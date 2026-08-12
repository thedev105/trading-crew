from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from polytrading.domain.models import Decimal38x18, StrictRecord, normalize_utc_timestamp

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]


class JournalPosting(StrictRecord):
    account: NonEmptyString
    asset: NonEmptyString
    debit: Decimal38x18 = Decimal(0)
    credit: Decimal38x18 = Decimal(0)

    @model_validator(mode="after")
    def require_one_positive_side(self) -> "JournalPosting":
        if self.debit < 0 or self.credit < 0 or (self.debit > 0) == (self.credit > 0):
            raise ValueError("exactly one of debit or credit must be positive")
        return self


class JournalTransaction(StrictRecord):
    schema_version: Literal[1]
    transaction_id: UUID
    occurred_at: datetime
    observed_at: datetime
    description: NonEmptyString
    postings: Annotated[tuple[JournalPosting, ...], Field(min_length=2)]
    evidence_ids: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]

    @field_validator("occurred_at", "observed_at")
    @classmethod
    def require_utc_transaction_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @field_validator("evidence_ids")
    @classmethod
    def canonicalize_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("evidence IDs must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def require_balanced_assets(self) -> "JournalTransaction":
        totals: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
        for posting in self.postings:
            totals[posting.asset][0] += posting.debit
            totals[posting.asset][1] += posting.credit
        if any(debit != credit for debit, credit in totals.values()):
            raise ValueError("debits must equal credits for each asset")
        return self


class TrialBalanceRow(StrictRecord):
    asset: NonEmptyString
    account: NonEmptyString
    debit: Decimal
    credit: Decimal
    difference: Decimal

    @model_validator(mode="after")
    def require_exact_difference(self) -> "TrialBalanceRow":
        if self.debit < 0 or self.credit < 0:
            raise ValueError("trial balance totals must be non-negative")
        if self.difference != self.debit - self.credit:
            raise ValueError("trial balance difference must equal debit minus credit")
        return self
