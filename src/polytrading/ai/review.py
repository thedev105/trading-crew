from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import StringConstraints, field_validator, model_validator

from polytrading.domain.models import StrictRecord, normalize_utc_timestamp

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
_SHA256 = re.compile(r"[0-9a-f]{64}")


class CorpusReviewAssignment(StrictRecord):
    schema_version: Literal[1]
    item_type: Literal["contract", "relationship"]
    item_id: NonEmptyString
    reviewer_id: NonEmptyString
    input_hash: str

    @field_validator("input_hash")
    @classmethod
    def require_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("assignment hash must contain 64 lowercase hexadecimal characters")
        return value


class ReviewRecord(StrictRecord):
    schema_version: Literal[1]
    review_id: NonEmptyString
    item_type: Literal["contract", "relationship"]
    item_id: NonEmptyString
    reviewer_id: NonEmptyString
    reviewer_role: Literal["reviewer", "adjudicator"]
    input_hash: str
    proposed_label_hash: str
    decision: Literal["accept", "correct", "reject"]
    corrections_json: str | None
    reviewed_at: datetime

    @field_validator("input_hash", "proposed_label_hash")
    @classmethod
    def require_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("review hashes must contain 64 lowercase hexadecimal characters")
        return value

    @field_validator("reviewed_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_correction_payload(self) -> ReviewRecord:
        if self.decision == "correct":
            if self.corrections_json is None:
                raise ValueError("correct decision requires corrections JSON")
            try:
                correction = json.loads(self.corrections_json)
            except json.JSONDecodeError as error:
                raise ValueError("corrections JSON must be valid JSON") from error
            if not isinstance(correction, dict):
                raise ValueError("corrections JSON must contain an object")
        elif self.corrections_json is not None:
            raise ValueError("corrections JSON is only allowed for a correct decision")
        return self


class ReviewResolution(StrictRecord):
    complete: bool
    proposed_label_hash: str | None
    review_ids: tuple[str, ...]
    adjudication_id: str | None


def validate_review_append(existing: tuple[ReviewRecord, ...], candidate: ReviewRecord) -> None:
    for prior in existing:
        if prior.review_id == candidate.review_id:
            if prior != candidate:
                raise ValueError(
                    f"review ID {candidate.review_id!r} already identifies another record"
                )
            return
        if (
            prior.item_type == candidate.item_type
            and prior.item_id == candidate.item_id
            and prior.reviewer_id == candidate.reviewer_id
        ):
            raise ValueError(
                f"reviewer {candidate.reviewer_id!r} already reviewed "
                f"{candidate.item_type} {candidate.item_id!r}"
            )


def resolve_reviews(records: tuple[ReviewRecord, ...]) -> ReviewResolution:
    if not records:
        return ReviewResolution(
            complete=False, proposed_label_hash=None, review_ids=(), adjudication_id=None
        )
    identity = {(record.item_type, record.item_id, record.input_hash) for record in records}
    if len(identity) != 1:
        raise ValueError("review records must address the same item and immutable input hash")
    review_ids = tuple(record.review_id for record in records)
    if len(review_ids) != len(set(review_ids)):
        raise ValueError("review IDs must be unique")
    reviewer_records = tuple(record for record in records if record.reviewer_role == "reviewer")
    adjudicators = tuple(record for record in records if record.reviewer_role == "adjudicator")
    reviewer_people = tuple(record.reviewer_id for record in reviewer_records)
    if len(reviewer_people) != len(set(reviewer_people)):
        raise ValueError("two reviews require distinct reviewer IDs")
    if len(reviewer_records) > 2:
        raise ValueError("an item may have exactly two independent reviews")
    if len(reviewer_records) < 2:
        if adjudicators:
            raise ValueError("adjudication cannot precede two independent reviews")
        return ReviewResolution(
            complete=False,
            proposed_label_hash=None,
            review_ids=review_ids,
            adjudication_id=None,
        )
    hashes = {record.proposed_label_hash for record in reviewer_records}
    if len(hashes) == 1:
        if adjudicators:
            raise ValueError("equal independent reviews do not require adjudication")
        return ReviewResolution(
            complete=True,
            proposed_label_hash=reviewer_records[0].proposed_label_hash,
            review_ids=review_ids,
            adjudication_id=None,
        )
    if not adjudicators:
        return ReviewResolution(
            complete=False,
            proposed_label_hash=None,
            review_ids=review_ids,
            adjudication_id=None,
        )
    if len(adjudicators) != 1:
        raise ValueError("a disagreement requires exactly one adjudicator")
    adjudicator = adjudicators[0]
    if adjudicator.reviewer_id in reviewer_people:
        raise ValueError("adjudicator must be distinct from both reviewers")
    return ReviewResolution(
        complete=True,
        proposed_label_hash=adjudicator.proposed_label_hash,
        review_ids=review_ids,
        adjudication_id=adjudicator.review_id,
    )
