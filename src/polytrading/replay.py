from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from polytrading.domain.models import (
    FundingObservation,
    InstrumentSpec,
    Level2BookSnapshot,
    MarketSnapshot,
    RawEnvelope,
)
from polytrading.venues.public import AdapterBatch, AdapterWarning
from polytrading.venues.recorder import PublicRecordStore, append_normalized


class ReplayValidationError(ValueError):
    """Raised when a JSONL replay row violates the public adapter boundary."""


def replay_file(path: Path, store: PublicRecordStore) -> int:
    """Atomically replay every adapter batch in *path* using raw-first persistence."""
    batches = _read_batches(path)
    with store.transaction() as transaction:
        for batch in batches:
            for raw in batch.raw:
                transaction.append_raw(raw)
            for normalized in batch.normalized:
                append_normalized(transaction, normalized)
    return len(batches)


def _read_batches(path: Path) -> tuple[AdapterBatch, ...]:
    batches: list[AdapterBatch] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ReplayValidationError(f"cannot read replay input: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            batches.append(_parse_batch(value))
        except (TypeError, ValueError) as error:
            raise ReplayValidationError(f"replay line {line_number}: {error}") from error
    return tuple(batches)


def _parse_batch(value: object) -> AdapterBatch:
    if not isinstance(value, dict) or set(value) != {"raw", "normalized", "warnings"}:
        raise ValueError("batch must contain exactly raw, normalized, and warnings")
    raw_values = _require_list(value["raw"], "raw")
    normalized_values = _require_list(value["normalized"], "normalized")
    warning_values = _require_list(value["warnings"], "warnings")
    raw = tuple(_validate_json(RawEnvelope, item) for item in raw_values)
    normalized = tuple(_parse_normalized(item) for item in normalized_values)
    warnings = tuple(_parse_warning(item) for item in warning_values)
    for envelope in raw:
        actual_hash = sha256(envelope.payload_json.encode("utf-8")).hexdigest()
        if envelope.source_hash != actual_hash:
            raise ValueError("raw source hash does not match exact UTF-8 payload")
    raw_lineage = {(item.venue, item.source_hash) for item in raw}
    if any((item.venue, item.source_hash) not in raw_lineage for item in normalized):
        raise ValueError(
            "normalized lineage must reference a same-venue raw source hash in its batch"
        )
    return AdapterBatch(raw=raw, normalized=normalized, warnings=warnings)


def _parse_normalized(value: object):
    if not isinstance(value, dict):
        raise ValueError("normalized record must be an object")
    if "instrument_id" in value:
        model = InstrumentSpec
    elif "rate" in value:
        model = FundingObservation
    elif "bids" in value or "asks" in value:
        model = Level2BookSnapshot
    elif "bid" in value or "ask" in value:
        model = MarketSnapshot
    else:
        raise ValueError("unrecognized normalized record shape")
    return _validate_json(model, value)


def _parse_warning(value: object) -> AdapterWarning:
    if not isinstance(value, dict) or set(value) != {
        "code",
        "venue",
        "endpoint",
        "symbol",
        "message",
    }:
        raise ValueError("warning has invalid fields")
    from polytrading.domain.models import Venue

    return AdapterWarning(
        code=_require_string(value["code"], "warning code"),
        venue=Venue(_require_string(value["venue"], "warning venue")),
        endpoint=_require_string(value["endpoint"], "warning endpoint"),
        symbol=_require_string(value["symbol"], "warning symbol"),
        message=_require_string(value["message"], "warning message"),
    )


def _validate_json(model: Any, value: object):
    return model.model_validate_json(json.dumps(value, separators=(",", ":")))


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value
