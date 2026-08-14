from __future__ import annotations

import json
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator

from polytrading.domain.models import (
    FeeSchedule,
    StrictRecord,
    Venue,
    normalize_utc_timestamp,
)
from polytrading.storage.store import DuckDBStore


class _DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError("reviewed fee document contains a duplicate JSON key")
        result[key] = value
    return result


def _require_official_fee_url(item: FeeSchedule) -> None:
    try:
        parsed = urlsplit(item.source_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("fee source URL is invalid") from error
    expected_host = {
        Venue.DYDX: "help.dydx.trade",
        Venue.LIGHTER: "docs.lighter.xyz",
    }[item.venue]
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise ValueError("fee source URL must be an official source for venue")


class ReviewedFeeDocument(StrictRecord):
    schema_version: Literal[1]
    reviewed_at: datetime
    fees: tuple[FeeSchedule, FeeSchedule]

    @field_validator("reviewed_at")
    @classmethod
    def require_review_timestamp(cls, value: datetime) -> datetime:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def require_reviewed_canonical_fees(self) -> ReviewedFeeDocument:
        if tuple(item.venue for item in self.fees) != (Venue.DYDX, Venue.LIGHTER):
            raise ValueError("fees must use canonical dYdX/Lighter order")
        identities = tuple(
            (item.venue, item.tier_name, item.effective_from, item.observed_at)
            for item in self.fees
        )
        if len(set(identities)) != len(identities):
            raise ValueError("reviewed fee identities must be unique")
        for item in self.fees:
            if not item.tier_name.strip():
                raise ValueError("reviewed fee tier name must not be blank")
            if not item.maker_rate.is_finite() or not item.taker_rate.is_finite():
                raise ValueError("reviewed fee rates must be finite")
            if item.taker_rate < 0:
                raise ValueError("reviewed taker fee must be nonnegative")
            if item.observed_at > self.reviewed_at:
                raise ValueError("fee observation must not follow review time")
            _require_official_fee_url(item)
        return self


def _prevalidate_raw_document(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("invalid reviewed fee document")
    fees = value.get("fees")
    if not isinstance(fees, list):
        raise ValueError("invalid reviewed fee document")
    if len(fees) != 2:
        raise ValueError("reviewed fee document requires exactly one dYdX and one Lighter fee")
    if not all(isinstance(item, dict) for item in fees):
        raise ValueError("invalid reviewed fee document")
    venues = tuple(item.get("venue") for item in fees)
    if any(venue not in (Venue.DYDX.value, Venue.LIGHTER.value) for venue in venues):
        raise ValueError("reviewed fees support only dYdX and Lighter")
    if venues != (Venue.DYDX.value, Venue.LIGHTER.value):
        raise ValueError("fees must use canonical dYdX/Lighter order")
    if any(
        not isinstance(item.get(field), str)
        for item in fees
        for field in ("maker_rate", "taker_rate")
    ):
        raise ValueError("invalid reviewed fee document")


def parse_reviewed_fee_document(payload: bytes) -> ReviewedFeeDocument:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("reviewed fee document must be UTF-8") from error
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object)
    except _DuplicateKeyError:
        raise
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("reviewed fee document must contain valid JSON") from error
    _prevalidate_raw_document(raw)
    try:
        return ReviewedFeeDocument.model_validate_json(text)
    except ValueError as error:
        safe_messages = (
            "canonical dYdX/Lighter order",
            "fee source URL must be an official source for venue",
            "reviewed taker fee must be nonnegative",
            "reviewed fee tier name must not be blank",
            "fee observation must not follow review time",
            "reviewed fee identities must be unique",
            "reviewed fee rates must be finite",
        )
        rendered = str(error)
        for message in safe_messages:
            if message in rendered:
                raise ValueError(message) from None
        raise ValueError("invalid reviewed fee document") from None


def record_reviewed_fees(store: DuckDBStore, document: ReviewedFeeDocument) -> int:
    inserted = 0
    with store.transaction():
        for item in document.fees:
            inserted += int(store.append_fee_schedule(item))
    return inserted
