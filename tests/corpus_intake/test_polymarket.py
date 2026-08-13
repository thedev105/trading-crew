from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from polytrading.corpus_intake.models import AcquisitionRequest, CorpusIntakeError
from polytrading.corpus_intake.polymarket import acquire_polymarket, parse_page

FIXTURES = Path("tests/fixtures/polymarket")
RETRIEVED_AT = datetime(2026, 8, 12, 16, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 12, 15, tzinfo=UTC)


def _parse_fixture(name: str = "markets_keyset_page_1.json"):
    body = (FIXTURES / name).read_bytes()
    return parse_page(
        body=body,
        request_url=("https://gamma-api.polymarket.com/markets/keyset?limit=2&include_tag=true"),
        requested_cursor=None,
        page_ordinal=1,
        retrieved_at=RETRIEVED_AT,
        information_cutoff=CUTOFF,
        status_code=200,
        headers={"Content-Type": "application/json", "ETag": '"abc"', "X-Secret": "drop"},
    )


def test_parse_page_preserves_exact_body_lineage_and_allowlists_headers() -> None:
    body = (FIXTURES / "markets_keyset_page_1.json").read_bytes()

    page = _parse_fixture()

    assert page.raw.body_text.encode("utf-8") == body
    assert page.raw.body_sha256 == sha256(body).hexdigest()
    assert page.raw.response_headers == (("content-type", "application/json"), ("etag", '"abc"'))
    assert page.raw.returned_cursor == "cursor-2"
    assert [candidate.source_market_id for candidate in page.candidates] == ["101", "102"]
    assert {candidate.retention_status for candidate in page.candidates} == {"review_required"}


def test_parse_page_extracts_event_family_and_only_review_routing_tags() -> None:
    first, second = _parse_fixture().candidates

    assert first.candidate_id == "polymarket:101"
    assert first.event_family_id == "polymarket:event:event-10"
    assert first.public_event_url == "https://polymarket.com/event/bitcoin-price-2026"
    assert first.category == "Crypto"
    assert first.routing_tags == (
        "crypto",
        "deadline_or_date",
        "multi_outcome_event",
        "named_source",
        "numeric_threshold",
    )
    assert first.warnings == ()
    assert second.routing_tags == ("deadline_or_date", "sports")
    assert second.warnings == ("missing_description",)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda payload: payload.update(markets={}), "markets"),
        (lambda payload: payload["markets"][0].update(id=""), "market id"),
        (lambda payload: payload["markets"][0].update(question=3), "question"),
        (lambda payload: payload.update(next_cursor=3), "next_cursor"),
    ],
)
def test_parse_page_rejects_schema_drift(mutation, match: str) -> None:
    payload = json.loads((FIXTURES / "markets_keyset_page_1.json").read_text())
    mutation(payload)

    with pytest.raises(CorpusIntakeError, match=match):
        parse_page(
            body=json.dumps(payload).encode(),
            request_url="https://gamma-api.polymarket.com/markets/keyset?limit=2",
            requested_cursor=None,
            page_ordinal=1,
            retrieved_at=RETRIEVED_AT,
            information_cutoff=CUTOFF,
            status_code=200,
            headers={"content-type": "application/json"},
        )


def test_parse_page_rejects_non_utf8_and_non_object_json() -> None:
    common = {
        "request_url": "https://gamma-api.polymarket.com/markets/keyset?limit=2",
        "requested_cursor": None,
        "page_ordinal": 1,
        "retrieved_at": RETRIEVED_AT,
        "information_cutoff": CUTOFF,
        "status_code": 200,
        "headers": {"content-type": "application/json"},
    }
    with pytest.raises(CorpusIntakeError, match="UTF-8"):
        parse_page(body=b"\xff", **common)
    with pytest.raises(CorpusIntakeError, match="object"):
        parse_page(body=b"[]", **common)


class _SequenceTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[tuple[int, bytes, str]]) -> None:
        self._responses = iter(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body, content_type = next(self._responses)
        return httpx.Response(
            status,
            content=body,
            headers={"content-type": content_type},
            request=request,
        )


async def _no_sleep(delay: float) -> None:
    assert delay == pytest.approx(0.05)


def _request(**changes: object) -> AcquisitionRequest:
    values = {
        "retrieved_at": RETRIEVED_AT,
        "information_cutoff": CUTOFF,
        "max_candidates": 500,
        "page_size": 2,
        "max_pages": 10,
        "max_response_bytes": 1_000_000,
        "request_delay_seconds": 0.05,
    }
    values.update(changes)
    return AcquisitionRequest(**values)


def test_acquire_follows_keyset_cursor_and_sorts_candidates() -> None:
    first = (FIXTURES / "markets_keyset_page_1.json").read_bytes()
    second = (FIXTURES / "markets_keyset_page_2.json").read_bytes()
    transport = _SequenceTransport(
        [(200, first, "application/json"), (200, second, "application/json")]
    )
    captured = []

    async def exercise():
        async with httpx.AsyncClient(transport=transport) as client:
            return await acquire_polymarket(client, _request(), captured.append, sleep=_no_sleep)

    result = asyncio.run(exercise())

    assert len(captured) == 2
    assert [item.source_market_id for item in result.candidates] == ["101", "102", "103"]
    assert transport.requests[0].method == "GET"
    assert transport.requests[0].url.params["closed"] == "false"
    assert transport.requests[0].url.params["include_tag"] == "true"
    assert "after_cursor" not in transport.requests[0].url.params
    assert transport.requests[1].url.params["after_cursor"] == "cursor-2"


def test_acquire_stops_exactly_at_candidate_limit() -> None:
    body = (FIXTURES / "markets_keyset_page_1.json").read_bytes()
    transport = _SequenceTransport([(200, body, "application/json")])

    async def exercise():
        async with httpx.AsyncClient(transport=transport) as client:
            return await acquire_polymarket(client, _request(max_candidates=1), lambda page: None)

    result = asyncio.run(exercise())
    assert [item.source_market_id for item in result.candidates] == ["101"]
    assert result.diagnostics.truncated_at_candidate_limit is True


@pytest.mark.parametrize(
    ("status", "body", "content_type", "match"),
    [
        (404, b'{"markets":[]}', "application/json", "HTTP 404"),
        (200, b'{"markets":[]}', "text/html", "content type"),
        (200, b"x" * 101, "application/json", "response size"),
    ],
)
def test_acquire_rejects_status_content_type_and_oversized_body(
    status: int, body: bytes, content_type: str, match: str
) -> None:
    transport = _SequenceTransport([(status, body, content_type)])

    async def exercise():
        async with httpx.AsyncClient(transport=transport) as client:
            return await acquire_polymarket(
                client, _request(max_response_bytes=100), lambda page: None
            )

    with pytest.raises(CorpusIntakeError, match=match):
        asyncio.run(exercise())


def test_acquire_rejects_cursor_loop() -> None:
    body = (FIXTURES / "markets_keyset_page_1.json").read_bytes()
    transport = _SequenceTransport(
        [(200, body, "application/json"), (200, body, "application/json")]
    )

    async def exercise():
        async with httpx.AsyncClient(transport=transport) as client:
            return await acquire_polymarket(client, _request(), lambda page: None, sleep=_no_sleep)

    with pytest.raises(CorpusIntakeError, match="cursor loop"):
        asyncio.run(exercise())


def test_acquire_reports_exact_duplicate_and_rejects_same_id_conflict() -> None:
    base = json.loads((FIXTURES / "markets_keyset_page_1.json").read_text())
    duplicate = dict(base["markets"][0])
    terminal_duplicate = json.dumps({"markets": [duplicate]}).encode()
    transport = _SequenceTransport(
        [
            (200, json.dumps(base).encode(), "application/json"),
            (200, terminal_duplicate, "application/json"),
        ]
    )

    async def exact_duplicate():
        async with httpx.AsyncClient(transport=transport) as client:
            return await acquire_polymarket(client, _request(), lambda page: None, sleep=_no_sleep)

    result = asyncio.run(exact_duplicate())
    assert result.diagnostics.exact_duplicate_count == 1

    duplicate["question"] = "Conflicting question"
    terminal_conflict = json.dumps({"markets": [duplicate]}).encode()
    conflicting = _SequenceTransport(
        [
            (200, json.dumps(base).encode(), "application/json"),
            (200, terminal_conflict, "application/json"),
        ]
    )

    async def conflict():
        async with httpx.AsyncClient(transport=conflicting) as client:
            return await acquire_polymarket(client, _request(), lambda page: None, sleep=_no_sleep)

    with pytest.raises(CorpusIntakeError, match="conflicting market ID"):
        asyncio.run(conflict())
