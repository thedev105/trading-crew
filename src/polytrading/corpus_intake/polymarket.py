from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from hashlib import sha256
from typing import Any
from urllib.parse import quote

import httpx

from polytrading.corpus_intake.models import (
    AcquisitionDiagnostics,
    AcquisitionRequest,
    AcquisitionResult,
    CorpusCandidate,
    CorpusIntakeError,
    ParsedPage,
    RawPageCapture,
)

SOURCE = "polymarket"
ENDPOINT = "https://gamma-api.polymarket.com/markets/keyset"
DOCUMENTATION_URL = (
    "https://docs.polymarket.com/api-reference/markets/list-markets-keyset-pagination"
)
_CAPTURED_HEADERS = frozenset({"content-type", "date", "etag", "last-modified"})
_THRESHOLD = re.compile(
    r"\b(?:above|below|over|under|more than|less than|at least|at most|reach|hit)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?:[$€£]\s*)?\d[\d,.]*(?:\s*%|\s*[KMB])?", re.IGNORECASE)
_DATE_OR_DEADLINE = re.compile(
    r"\b(?:before|after|by|on|date|deadline|january|february|march|april|may|june|"
    r"july|august|september|october|november|december|20\d{2})\b",
    re.IGNORECASE,
)


def parse_page(
    *,
    body: bytes,
    request_url: str,
    requested_cursor: str | None,
    page_ordinal: int,
    retrieved_at,
    information_cutoff,
    status_code: int,
    headers: Mapping[str, str],
) -> ParsedPage:
    try:
        body_text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise CorpusIntakeError("response body must be valid UTF-8") from error
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as error:
        raise CorpusIntakeError("response body must contain valid JSON") from error
    if not isinstance(payload, dict):
        raise CorpusIntakeError("response JSON must be an object")
    markets = payload.get("markets")
    if not isinstance(markets, list):
        raise CorpusIntakeError("response markets must be a list")
    returned_cursor = payload.get("next_cursor")
    if returned_cursor is not None and not _is_nonempty_string(returned_cursor):
        raise CorpusIntakeError("response next_cursor must be a non-empty string")

    body_hash = sha256(body).hexdigest()
    selected_headers = tuple(
        sorted(
            (name.casefold(), value)
            for name, value in headers.items()
            if name.casefold() in _CAPTURED_HEADERS
        )
    )
    raw = RawPageCapture(
        source=SOURCE,
        endpoint=ENDPOINT,
        request_url=request_url,
        requested_cursor=requested_cursor,
        returned_cursor=returned_cursor,
        page_ordinal=page_ordinal,
        retrieved_at=retrieved_at,
        information_cutoff=information_cutoff,
        status_code=status_code,
        response_headers=selected_headers,
        body_text=body_text,
        body_sha256=body_hash,
    )
    candidates = tuple(
        _normalize_market(
            market,
            retrieved_at=retrieved_at,
            information_cutoff=information_cutoff,
            body_hash=body_hash,
            page_ordinal=page_ordinal,
        )
        for market in markets
    )
    return ParsedPage(raw=raw, candidates=candidates)


def _normalize_market(
    market: object,
    *,
    retrieved_at,
    information_cutoff,
    body_hash: str,
    page_ordinal: int,
) -> CorpusCandidate:
    if not isinstance(market, dict):
        raise CorpusIntakeError("each market must be an object")
    market_id = _required_identifier(market.get("id"), "market id")
    question = market.get("question")
    if not _is_nonempty_string(question):
        raise CorpusIntakeError(f"market {market_id} question must be a non-empty string")

    warnings: list[str] = []
    condition_id = _optional_string(market, "conditionId", warnings)
    slug = _optional_string(market, "slug", warnings)
    description = _optional_string(market, "description", warnings)
    resolution_source = _optional_string(market, "resolutionSource", warnings)
    category = _optional_string(market, "category", warnings)
    start_date = _optional_string(market, "startDate", warnings)
    end_date = _optional_string(market, "endDate", warnings)
    active = _optional_bool(market, "active", warnings)
    closed = _optional_bool(market, "closed", warnings)
    archived = _optional_bool(market, "archived", warnings)

    event = _first_event(market, warnings)
    event_id = _optional_identifier_value(event.get("id")) if event is not None else None
    event_slug = _optional_plain_string(event.get("slug")) if event is not None else None
    event_category = _optional_plain_string(event.get("category")) if event is not None else None
    if category is None:
        category = event_category
    family_value = event_id or condition_id or market_id
    family_kind = "event" if event_id is not None else "condition" if condition_id else "market"
    event_family_id = f"{SOURCE}:{family_kind}:{family_value}"
    public_event_url = (
        f"https://polymarket.com/event/{quote(event_slug, safe='')}" if event_slug else None
    )
    routing_tags = _routing_tags(
        market,
        event,
        question=question,
        description=description,
        resolution_source=resolution_source,
        category=category,
        start_date=start_date,
        end_date=end_date,
    )
    return CorpusCandidate(
        candidate_id=f"{SOURCE}:{market_id}",
        source=SOURCE,
        source_market_id=market_id,
        condition_id=condition_id,
        event_family_id=event_family_id,
        slug=slug,
        api_url=f"https://gamma-api.polymarket.com/markets/{quote(market_id, safe='')}",
        public_event_url=public_event_url,
        question=question,
        description=description,
        resolution_source=resolution_source,
        category=category,
        start_date=start_date,
        end_date=end_date,
        active=active,
        closed=closed,
        archived=archived,
        retrieved_at=retrieved_at,
        information_cutoff=information_cutoff,
        raw_body_sha256=body_hash,
        raw_page_ordinal=page_ordinal,
        retention_status="review_required",
        warnings=tuple(sorted(warnings)),
        routing_tags=routing_tags,
    )


def _first_event(market: dict[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    events = market.get("events")
    if events is None:
        warnings.append("missing_events")
        return None
    if not isinstance(events, list):
        warnings.append("invalid_events")
        return None
    if not events:
        warnings.append("empty_events")
        return None
    event = events[0]
    if not isinstance(event, dict):
        warnings.append("invalid_event")
        return None
    return event


def _optional_string(market: dict[str, Any], name: str, warnings: list[str]) -> str | None:
    if name not in market or market[name] is None or market[name] == "":
        warnings.append(f"missing_{_snake_case(name)}")
        return None
    value = market[name]
    if not isinstance(value, str):
        warnings.append(f"invalid_{_snake_case(name)}")
        return None
    return value


def _optional_bool(market: dict[str, Any], name: str, warnings: list[str]) -> bool | None:
    if name not in market or market[name] is None:
        warnings.append(f"missing_{name}")
        return None
    value = market[name]
    if not isinstance(value, bool):
        warnings.append(f"invalid_{name}")
        return None
    return value


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).casefold()


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _required_identifier(value: object, label: str) -> str:
    normalized = _optional_identifier_value(value)
    if normalized is None:
        raise CorpusIntakeError(f"{label} must be a non-empty string or integer")
    return normalized


def _optional_identifier_value(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if _is_nonempty_string(value):
        return value.strip()
    return None


def _optional_plain_string(value: object) -> str | None:
    return value if _is_nonempty_string(value) else None


def _routing_tags(
    market: dict[str, Any],
    event: dict[str, Any] | None,
    *,
    question: str,
    description: str | None,
    resolution_source: str | None,
    category: str | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, ...]:
    tags: set[str] = set()
    semantic_text = " ".join(part for part in (question, description) if part)
    category_text = (category or "").casefold()
    all_text = f"{category_text} {semantic_text.casefold()}"
    if "crypto" in all_text or any(
        token in all_text for token in ("bitcoin", "ethereum", "solana")
    ):
        tags.add("crypto")
    if any(token in all_text for token in ("politic", "election", "president", "parliament")):
        tags.add("politics")
    if "sport" in all_text or market.get("gameId") is not None:
        tags.add("sports")
    if _THRESHOLD.search(semantic_text) and _NUMBER.search(semantic_text):
        tags.add("numeric_threshold")
    if (
        re.search(r"\bbetween\b.+\band\b", semantic_text, re.IGNORECASE)
        or market.get("lowerBound") is not None
        or market.get("upperBound") is not None
    ):
        tags.add("bounded_range")
    if start_date or end_date or _DATE_OR_DEADLINE.search(semantic_text):
        tags.add("deadline_or_date")
    if resolution_source and (
        resolution_source.casefold().startswith(("http://", "https://"))
        or "according to" in semantic_text.casefold()
    ):
        tags.add("named_source")
    event_markets = event.get("markets") if event is not None else None
    if isinstance(event_markets, list) and len(event_markets) > 1:
        tags.add("multi_outcome_event")
    if resolution_source is None:
        tags.add("ambiguous_resolution_text")
    return tuple(sorted(tags))


async def acquire_polymarket(
    client: httpx.AsyncClient,
    request: AcquisitionRequest,
    on_raw_page: Callable[[RawPageCapture], None],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AcquisitionResult:
    candidates_by_id: dict[str, CorpusCandidate] = {}
    canonical_fingerprints: set[str] = set()
    exact_duplicate_count = 0
    canonical_duplicate_count = 0
    received_market_count = 0
    requested_cursor: str | None = None
    seen_cursors: set[str] = set()
    page_count = 0
    truncated_at_candidate_limit = False
    truncated_at_page_limit = False

    for page_ordinal in range(1, request.max_pages + 1):
        params = {
            "limit": str(request.page_size),
            "closed": "false",
            "include_tag": "true",
        }
        if requested_cursor is not None:
            params["after_cursor"] = requested_cursor
        response = await client.get(ENDPOINT, params=params, follow_redirects=False)
        if response.status_code != 200:
            raise CorpusIntakeError(f"public source returned HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().casefold()
        if content_type != "application/json":
            raise CorpusIntakeError("public source returned an unexpected content type")
        body = response.content
        if len(body) > request.max_response_bytes:
            raise CorpusIntakeError("public source response size exceeds the configured limit")
        parsed = parse_page(
            body=body,
            request_url=str(response.request.url),
            requested_cursor=requested_cursor,
            page_ordinal=page_ordinal,
            retrieved_at=request.retrieved_at,
            information_cutoff=request.information_cutoff,
            status_code=response.status_code,
            headers=response.headers,
        )
        page_count += 1
        received_market_count += len(parsed.candidates)
        on_raw_page(parsed.raw)
        for candidate in parsed.candidates:
            identity_fingerprint = _candidate_fingerprint(candidate, include_identity=True)
            prior = candidates_by_id.get(candidate.source_market_id)
            if prior is not None:
                if _candidate_fingerprint(prior, include_identity=True) != identity_fingerprint:
                    raise CorpusIntakeError(
                        f"conflicting market ID {candidate.source_market_id} across pages"
                    )
                exact_duplicate_count += 1
                continue
            canonical_fingerprint = _candidate_fingerprint(candidate, include_identity=False)
            if canonical_fingerprint in canonical_fingerprints:
                canonical_duplicate_count += 1
                continue
            candidates_by_id[candidate.source_market_id] = candidate
            canonical_fingerprints.add(canonical_fingerprint)

        if len(candidates_by_id) >= request.max_candidates:
            truncated_at_candidate_limit = len(candidates_by_id) > request.max_candidates or bool(
                parsed.raw.returned_cursor
            )
            break
        next_cursor = parsed.raw.returned_cursor
        if next_cursor is None:
            break
        if next_cursor == requested_cursor or next_cursor in seen_cursors:
            raise CorpusIntakeError("public source returned a cursor loop")
        seen_cursors.add(next_cursor)
        requested_cursor = next_cursor
        if page_ordinal == request.max_pages:
            truncated_at_page_limit = True
            break
        await sleep(float(request.request_delay_seconds))

    ordered = tuple(
        sorted(
            candidates_by_id.values(),
            key=lambda item: (item.source, item.event_family_id, item.source_market_id),
        )[: request.max_candidates]
    )
    if len(candidates_by_id) > request.max_candidates:
        truncated_at_candidate_limit = True
    return AcquisitionResult(
        candidates=ordered,
        diagnostics=AcquisitionDiagnostics(
            page_count=page_count,
            received_market_count=received_market_count,
            exact_duplicate_count=exact_duplicate_count,
            canonical_duplicate_count=canonical_duplicate_count,
            truncated_at_candidate_limit=truncated_at_candidate_limit,
            truncated_at_page_limit=truncated_at_page_limit,
        ),
    )


def _candidate_fingerprint(candidate: CorpusCandidate, *, include_identity: bool) -> str:
    values = {
        "question": candidate.question,
        "description": candidate.description,
        "resolution_source": candidate.resolution_source,
        "category": candidate.category,
        "start_date": candidate.start_date,
        "end_date": candidate.end_date,
        "active": candidate.active,
        "closed": candidate.closed,
        "archived": candidate.archived,
        "routing_tags": candidate.routing_tags,
        "warnings": candidate.warnings,
    }
    if include_identity:
        values.update(
            {
                "source_market_id": candidate.source_market_id,
                "condition_id": candidate.condition_id,
                "event_family_id": candidate.event_family_id,
                "slug": candidate.slug,
                "public_event_url": candidate.public_event_url,
            }
        )
    canonical = json.dumps(values, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()
