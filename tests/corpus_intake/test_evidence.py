from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from polytrading.corpus_intake.evidence import (
    POLYMARKET_EVIDENCE_TARGETS,
    SourceUseRunWriter,
    capture_evidence,
    verify_source_use_run,
)
from polytrading.corpus_intake.models import CorpusIntakeError
from polytrading.corpus_intake.source_policy import (
    IntendedUseScope,
    SourceEvidence,
    canonical_sha256,
)
from polytrading.predictions.domain import PredictionSource

NOW = datetime(2026, 8, 12, 16, tzinfo=UTC)
DOCS_HTML = b"""<!doctype html><html><body>
<h2>Gamma API</h2>
<code>https://gamma-api.polymarket.com</code>
<p>Discover events and markets, and retrieve the metadata needed to work with them.</p>
</body></html>"""
INSTITUTIONAL_HTML = b"""<!doctype html><html><body>
<p>All Capital Markets Entities looking to consume Polymarket data must do so
in consultation with Polymarket and ICE.</p>
</body></html>"""


def scope() -> IntendedUseScope:
    return IntendedUseScope(
        schema_version=1,
        source=PredictionSource.POLYMARKET,
        maximum_records=1_000,
        local_retention=True,
        derived_semantic_labels=True,
        offline_model_evaluation=True,
        proprietary_trading_research=True,
        redistribution=False,
        generative_model_training=False,
    )


class SequenceTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[tuple[int, bytes, str]]) -> None:
        self._responses = iter(responses)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        status, body, content_type = next(self._responses)
        return httpx.Response(
            status,
            content=body,
            headers={
                "content-type": content_type,
                "etag": '"evidence-v1"',
                "last-modified": "Wed, 12 Aug 2026 15:00:00 GMT",
                "x-secret": "not-recorded",
            },
            request=request,
        )


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.consumed = 0

    async def __aiter__(self):
        for chunk in (b"x" * 60, b"y" * 60, b"z" * 60):
            self.consumed += 1
            yield chunk


class StreamingTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: ChunkStream) -> None:
        self.stream = stream

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=self.stream,
            request=request,
        )


def capture(target_index: int, body: bytes) -> SourceEvidence:
    async def exercise() -> SourceEvidence:
        transport = SequenceTransport([(200, body, "text/html; charset=utf-8")])
        async with httpx.AsyncClient(transport=transport) as client:
            return await capture_evidence(
                client,
                POLYMARKET_EVIDENCE_TARGETS[target_index],
                retrieved_at=NOW,
                max_response_bytes=4096,
            )

    return asyncio.run(exercise())


def records() -> tuple[SourceEvidence, SourceEvidence]:
    return (capture(0, DOCS_HTML), capture(1, INSTITUTIONAL_HTML))


def output_path(project: Path, name: str = "run") -> Path:
    return project / "var" / "source-use" / name


def test_capture_hashes_page_without_retaining_body() -> None:
    record = capture(1, INSTITUTIONAL_HTML)

    assert record.body_sha256 == sha256(INSTITUTIONAL_HTML).hexdigest()
    assert record.body_byte_count == len(INSTITUTIONAL_HTML)
    assert record.content_type == "text/html; charset=utf-8"
    assert record.etag == '"evidence-v1"'
    assert record.last_modified == "Wed, 12 Aug 2026 15:00:00 GMT"
    assert record.full_body_retained is False
    assert "body_text" not in SourceEvidence.model_fields
    assert record.excerpt_sha256 == canonical_sha256(record.excerpt)


def test_first_target_matches_current_canonical_gamma_api_overview() -> None:
    record = capture(0, DOCS_HTML)

    assert record.url == "https://docs.polymarket.com/api-reference/predictions/overview"
    assert record.excerpt == (
        "Discover events and markets, and retrieve the metadata needed to work with them."
    )


def test_capture_stops_streaming_as_soon_as_bound_is_crossed() -> None:
    stream = ChunkStream()

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=StreamingTransport(stream)) as client:
            with pytest.raises(CorpusIntakeError, match="size limit"):
                await capture_evidence(
                    client,
                    POLYMARKET_EVIDENCE_TARGETS[0],
                    retrieved_at=NOW,
                    max_response_bytes=100,
                )

    asyncio.run(exercise())
    assert stream.consumed == 2


@pytest.mark.parametrize(
    ("status", "body", "content_type", "message"),
    [
        (302, DOCS_HTML, "text/html", "status"),
        (200, DOCS_HTML, "application/json", "content type"),
        (200, b"\xff", "text/html", "UTF-8"),
        (200, b"<html><body>unrelated</body></html>", "text/html", "locator"),
    ],
)
def test_capture_rejects_unexpected_response(
    status: int, body: bytes, content_type: str, message: str
) -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=SequenceTransport([(status, body, content_type)])
        ) as client:
            with pytest.raises(CorpusIntakeError, match=message):
                await capture_evidence(
                    client,
                    POLYMARKET_EVIDENCE_TARGETS[0],
                    retrieved_at=NOW,
                    max_response_bytes=4096,
                )

    asyncio.run(exercise())


def test_capture_rejects_a_target_outside_the_immutable_allowlist() -> None:
    target = replace(POLYMARKET_EVIDENCE_TARGETS[0], url="https://example.test/terms")

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=SequenceTransport([(200, DOCS_HTML, "text/html")])
        ) as client:
            with pytest.raises(CorpusIntakeError, match="allowlist"):
                await capture_evidence(
                    client,
                    target,
                    retrieved_at=NOW,
                    max_response_bytes=4096,
                )

    asyncio.run(exercise())


def test_source_use_writer_is_deterministic_and_manifest_is_last(tmp_path: Path) -> None:
    output = output_path(tmp_path)
    writer = SourceUseRunWriter(output, project_root=tmp_path, retrieved_at=NOW)
    writer.complete(evidence=records(), scope=scope())

    verified = verify_source_use_run(output)
    first_bytes = {path.name: path.read_bytes() for path in output.iterdir()}

    assert verified.evidence_count == 2
    assert verified.assessment.status == "requires_external_confirmation"
    assert verified.assessment.scope == scope()
    assert verified.assessment.evidence_sha256s == tuple(
        sorted(canonical_sha256(record) for record in records())
    )
    assert verified.body_sha256s == tuple(sorted(record.body_sha256 for record in records()))
    assert len(verified.manifest_sha256) == 64
    assert (output / "licensing-inquiry.md").read_text().startswith("# DRAFT — NOT SENT")
    assert not list(output.glob("*.tmp"))
    assert set(first_bytes) == {
        "assessment.json",
        "evidence.jsonl",
        "licensing-inquiry.md",
        "manifest.json",
    }


def test_writer_rejects_nonempty_output_and_escape_paths(tmp_path: Path) -> None:
    nonempty = output_path(tmp_path, "nonempty")
    nonempty.mkdir(parents=True)
    (nonempty / "owned").write_text("preserve")
    with pytest.raises(CorpusIntakeError, match="non-empty"):
        SourceUseRunWriter(nonempty, project_root=tmp_path, retrieved_at=NOW)

    with pytest.raises(CorpusIntakeError, match="var/source-use"):
        SourceUseRunWriter(tmp_path / "data/gold/run", project_root=tmp_path, retrieved_at=NOW)

    quarantine = tmp_path / "var/source-use"
    outside = tmp_path / "outside"
    outside.mkdir()
    (quarantine / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CorpusIntakeError, match="var/source-use"):
        SourceUseRunWriter(quarantine / "linked/run", project_root=tmp_path, retrieved_at=NOW)


def test_writer_failure_never_claims_completion(tmp_path: Path) -> None:
    output = output_path(tmp_path)
    writer = SourceUseRunWriter(output, project_root=tmp_path, retrieved_at=NOW)

    with pytest.raises(CorpusIntakeError, match="exact evidence target set"):
        writer.complete(evidence=(records()[0],), scope=scope())

    assert not (output / "manifest.json").exists()


def test_verifier_detects_artifact_tampering(tmp_path: Path) -> None:
    output = output_path(tmp_path)
    writer = SourceUseRunWriter(output, project_root=tmp_path, retrieved_at=NOW)
    writer.complete(evidence=records(), scope=scope())
    assessment_path = output / "assessment.json"
    assessment = json.loads(assessment_path.read_text())
    assessment["status"] = "rejected"
    assessment["reason_code"] = "human_rejected"
    assessment_path.write_text(json.dumps(assessment) + "\n")

    with pytest.raises(CorpusIntakeError, match="hash or byte count"):
        verify_source_use_run(output)
