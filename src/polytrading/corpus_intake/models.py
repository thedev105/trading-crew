from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from polytrading.domain.models import normalize_utc_timestamp


class CorpusIntakeError(ValueError):
    """A public-source response or intake request failed closed."""


@dataclass(frozen=True)
class AcquisitionRequest:
    retrieved_at: datetime
    information_cutoff: datetime
    max_candidates: int
    page_size: int = 100
    max_pages: int = 10
    max_response_bytes: int = 16 * 1024 * 1024
    request_delay_seconds: float = 0.05

    def __post_init__(self) -> None:
        retrieved_at = normalize_utc_timestamp(self.retrieved_at)
        information_cutoff = normalize_utc_timestamp(self.information_cutoff)
        object.__setattr__(self, "retrieved_at", retrieved_at)
        object.__setattr__(self, "information_cutoff", information_cutoff)
        if information_cutoff > retrieved_at:
            raise CorpusIntakeError("information cutoff must not follow retrieval time")
        _require_bounded_integer("max candidates", self.max_candidates, 1, 5_000)
        _require_bounded_integer("page size", self.page_size, 1, 100)
        _require_bounded_integer("max pages", self.max_pages, 1, 100)
        _require_bounded_integer("max response bytes", self.max_response_bytes, 1, 64 * 1024 * 1024)
        if (
            isinstance(self.request_delay_seconds, bool)
            or not isinstance(self.request_delay_seconds, (int, float))
            or not math.isfinite(self.request_delay_seconds)
            or not 0 <= self.request_delay_seconds <= 10
        ):
            raise CorpusIntakeError("request delay seconds must be finite and within 0..10")


def _require_bounded_integer(label: str, value: int, lower: int, upper: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise CorpusIntakeError(f"{label} must be an integer within {lower}..{upper}")


@dataclass(frozen=True)
class RawPageCapture:
    source: str
    endpoint: str
    request_url: str
    requested_cursor: str | None
    returned_cursor: str | None
    page_ordinal: int
    retrieved_at: datetime
    information_cutoff: datetime
    status_code: int
    response_headers: tuple[tuple[str, str], ...]
    body_text: str
    body_sha256: str


@dataclass(frozen=True)
class CorpusCandidate:
    candidate_id: str
    source: str
    source_market_id: str
    condition_id: str | None
    event_family_id: str
    slug: str | None
    api_url: str
    public_event_url: str | None
    question: str
    description: str | None
    resolution_source: str | None
    category: str | None
    start_date: str | None
    end_date: str | None
    active: bool | None
    closed: bool | None
    archived: bool | None
    retrieved_at: datetime
    information_cutoff: datetime
    raw_body_sha256: str
    raw_page_ordinal: int
    retention_status: Literal["review_required"]
    warnings: tuple[str, ...]
    routing_tags: tuple[str, ...]


@dataclass(frozen=True)
class ParsedPage:
    raw: RawPageCapture
    candidates: tuple[CorpusCandidate, ...]


@dataclass(frozen=True)
class AcquisitionDiagnostics:
    page_count: int
    received_market_count: int
    exact_duplicate_count: int
    canonical_duplicate_count: int
    truncated_at_candidate_limit: bool
    truncated_at_page_limit: bool


@dataclass(frozen=True)
class AcquisitionResult:
    candidates: tuple[CorpusCandidate, ...]
    diagnostics: AcquisitionDiagnostics
