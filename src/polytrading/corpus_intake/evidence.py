from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

import httpx

from polytrading.corpus_intake.models import CorpusIntakeError
from polytrading.corpus_intake.source_policy import (
    IntendedUseScope,
    SourceEvidence,
    SourceUseAssessment,
    canonical_sha256,
)
from polytrading.domain.models import normalize_utc_timestamp

SOURCE_USE_SCHEMA_VERSION = "source-use-v1"


@dataclass(frozen=True)
class EvidenceTarget:
    source: Literal["polymarket"]
    url: str
    locator: str
    excerpt: str


POLYMARKET_EVIDENCE_TARGETS = (
    EvidenceTarget(
        source="polymarket",
        url="https://docs.polymarket.com/api-reference/predictions/overview",
        locator="Gamma API",
        excerpt=(
            "Discover events and markets, and retrieve the metadata needed to work with them."
        ),
    ),
    EvidenceTarget(
        source="polymarket",
        url="https://institutional.polymarket.com/",
        locator="in consultation with Polymarket and ICE",
        excerpt=(
            "All Capital Markets Entities looking to consume Polymarket data must do so in "
            "consultation with Polymarket and ICE."
        ),
    ),
)


@dataclass(frozen=True)
class VerifiedSourceUseRun:
    evidence_count: int
    evidence_sha256s: tuple[str, ...]
    body_sha256s: tuple[str, ...]
    assessment: SourceUseAssessment
    scope: IntendedUseScope
    manifest_sha256: str


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._excluded_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "template"}:
            self._excluded_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "template"} and self._excluded_depth:
            self._excluded_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._excluded_depth == 0:
            self.parts.append(data)


async def capture_evidence(
    client: httpx.AsyncClient,
    target: EvidenceTarget,
    *,
    retrieved_at: datetime,
    max_response_bytes: int,
) -> SourceEvidence:
    if target not in POLYMARKET_EVIDENCE_TARGETS:
        raise CorpusIntakeError("source evidence target is not on the immutable allowlist")
    retrieved_at = normalize_utc_timestamp(retrieved_at)
    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or not 1 <= max_response_bytes <= 64 * 1024 * 1024
    ):
        raise CorpusIntakeError("source evidence size limit must be within 1..67108864 bytes")

    async with client.stream(
        "GET",
        target.url,
        headers={"Accept": "text/html", "Accept-Encoding": "identity"},
    ) as response:
        if str(response.url) != target.url:
            raise CorpusIntakeError("source evidence response URL differs from allowlisted target")
        if response.status_code != 200:
            raise CorpusIntakeError(
                f"source evidence response has unexpected status {response.status_code}"
            )
        content_type = response.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().casefold() != "text/html":
            raise CorpusIntakeError("source evidence response has unexpected content type")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as error:
                raise CorpusIntakeError("source evidence content length is malformed") from error
            if declared_length > max_response_bytes:
                raise CorpusIntakeError("source evidence response exceeds size limit")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_response_bytes:
                raise CorpusIntakeError("source evidence response exceeds size limit")

        exact_body = bytes(body)
        if not exact_body:
            raise CorpusIntakeError("source evidence response body is empty")
        try:
            body_text = exact_body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CorpusIntakeError("source evidence response is not valid UTF-8") from error
        parser = _VisibleTextParser()
        try:
            parser.feed(body_text)
            parser.close()
        except Exception as error:
            raise CorpusIntakeError("source evidence HTML cannot be parsed") from error
        visible_text = " ".join(" ".join(parser.parts).split())
        if target.locator.casefold() not in visible_text.casefold():
            raise CorpusIntakeError("source evidence locator is absent from official page")
        if target.excerpt.casefold() not in visible_text.casefold():
            raise CorpusIntakeError("source evidence excerpt is absent from official page")

        return SourceEvidence(
            schema_version=1,
            source=target.source,
            url=target.url,
            retrieved_at=retrieved_at,
            status_code=200,
            content_type=content_type,
            body_byte_count=len(exact_body),
            body_sha256=hashlib.sha256(exact_body).hexdigest(),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            locator=target.locator,
            excerpt=target.excerpt,
            excerpt_sha256=canonical_sha256(target.excerpt),
            full_body_retained=False,
        )


class SourceUseRunWriter:
    def __init__(self, output: Path, *, project_root: Path, retrieved_at: datetime) -> None:
        self.output = _validated_output_path(output, project_root)
        if self.output.exists() and any(self.output.iterdir()):
            raise CorpusIntakeError("source-use output directory must not be non-empty")
        self.output.mkdir(parents=True, exist_ok=True)
        self.retrieved_at = normalize_utc_timestamp(retrieved_at)
        self._completed = False

    def complete(self, *, evidence: tuple[SourceEvidence, ...], scope: IntendedUseScope) -> None:
        if self._completed or (self.output / "manifest.json").exists():
            raise CorpusIntakeError("source-use evidence run is already complete")
        ordered = tuple(sorted(evidence, key=lambda record: record.url))
        expected_urls = {target.url for target in POLYMARKET_EVIDENCE_TARGETS}
        if {record.url for record in ordered} != expected_urls or len(ordered) != len(
            expected_urls
        ):
            raise CorpusIntakeError("source-use run requires the exact evidence target set")
        if any(record.source != scope.source for record in ordered):
            raise CorpusIntakeError("evidence source does not match intended-use scope")
        if any(record.retrieved_at != self.retrieved_at for record in ordered):
            raise CorpusIntakeError("evidence retrieval time does not match source-use run")

        evidence_hashes = tuple(sorted(canonical_sha256(record) for record in ordered))
        assessment = SourceUseAssessment(
            schema_version=1,
            source=scope.source,
            assessed_at=self.retrieved_at,
            status="requires_external_confirmation",
            reason_code="source_consultation_notice",
            scope=scope,
            scope_sha256=canonical_sha256(scope),
            evidence_sha256s=evidence_hashes,
        )
        evidence_path = self.output / "evidence.jsonl"
        assessment_path = self.output / "assessment.json"
        inquiry_path = self.output / "licensing-inquiry.md"
        _atomic_write_text(
            evidence_path,
            "".join(_canonical_json(record.model_dump(mode="json")) + "\n" for record in ordered),
        )
        _atomic_write_text(
            assessment_path, _canonical_json(assessment.model_dump(mode="json")) + "\n"
        )
        _atomic_write_text(inquiry_path, _render_inquiry(scope, ordered, evidence_hashes))
        artifact_names = ("evidence.jsonl", "assessment.json", "licensing-inquiry.md")
        manifest = {
            "schema_version": SOURCE_USE_SCHEMA_VERSION,
            "status": "complete",
            "source": scope.source,
            "retrieved_at": _timestamp(self.retrieved_at),
            "scope_sha256": canonical_sha256(scope),
            "evidence_sha256s": list(evidence_hashes),
            "approval_generated": False,
            "full_response_bodies_retained": False,
            "files": {name: _file_evidence(self.output / name) for name in artifact_names},
        }
        _atomic_write_text(self.output / "manifest.json", _canonical_json(manifest) + "\n")
        self._completed = True


def verify_source_use_run(output: Path) -> VerifiedSourceUseRun:
    manifest_path = output / "manifest.json"
    manifest = _read_json_object(manifest_path)
    if (
        manifest.get("schema_version") != SOURCE_USE_SCHEMA_VERSION
        or manifest.get("status") != "complete"
        or manifest.get("source") != "polymarket"
    ):
        raise CorpusIntakeError("source-use manifest is not a completed supported run")
    if manifest.get("approval_generated") is not False:
        raise CorpusIntakeError("source-use evidence run cannot claim generated approval")
    if manifest.get("full_response_bodies_retained") is not False:
        raise CorpusIntakeError("source-use evidence run cannot retain full response bodies")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise CorpusIntakeError("source-use manifest files must be an object")
    for name in ("evidence.jsonl", "assessment.json", "licensing-inquiry.md"):
        if files.get(name) != _file_evidence(output / name):
            raise CorpusIntakeError(f"source-use artifact hash or byte count mismatch for {name}")

    evidence = tuple(
        SourceEvidence.model_validate_json(_canonical_json(row))
        for row in _read_jsonl(output / "evidence.jsonl")
    )
    expected_urls = {target.url for target in POLYMARKET_EVIDENCE_TARGETS}
    if {record.url for record in evidence} != expected_urls or len(evidence) != len(expected_urls):
        raise CorpusIntakeError("source-use evidence does not match exact target set")
    evidence_hashes = tuple(sorted(canonical_sha256(record) for record in evidence))
    assessment = SourceUseAssessment.model_validate_json((output / "assessment.json").read_bytes())
    if assessment.status != "requires_external_confirmation":
        raise CorpusIntakeError("automated source-use run must require external confirmation")
    if assessment.evidence_sha256s != evidence_hashes:
        raise CorpusIntakeError("source-use assessment evidence binding does not match records")
    if assessment.scope_sha256 != canonical_sha256(assessment.scope):
        raise CorpusIntakeError("source-use assessment scope binding does not match exact scope")
    if manifest.get("scope_sha256") != assessment.scope_sha256:
        raise CorpusIntakeError("source-use manifest scope binding does not match assessment")
    if manifest.get("evidence_sha256s") != list(evidence_hashes):
        raise CorpusIntakeError("source-use manifest evidence binding does not match records")
    inquiry = (output / "licensing-inquiry.md").read_text(encoding="utf-8")
    if not inquiry.startswith("# DRAFT — NOT SENT"):
        raise CorpusIntakeError("licensing inquiry must remain explicitly marked unsent")
    return VerifiedSourceUseRun(
        evidence_count=len(evidence),
        evidence_sha256s=evidence_hashes,
        body_sha256s=tuple(sorted(record.body_sha256 for record in evidence)),
        assessment=assessment,
        scope=assessment.scope,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )


def _render_inquiry(
    scope: IntendedUseScope,
    evidence: tuple[SourceEvidence, ...],
    evidence_hashes: tuple[str, ...],
) -> str:
    body_hashes = "\n".join(f"- `{record.body_sha256}` — {record.url}" for record in evidence)
    record_hashes = "\n".join(f"- `{value}`" for value in evidence_hashes)
    return (
        "# DRAFT — NOT SENT\n\n"
        "To: Polymarket data licensing\n"
        "Subject: Data-use confirmation for local proprietary research\n\n"
        "We are considering retaining at most "
        f"{scope.maximum_records} public Polymarket market records locally. The intended "
        "work would create human semantic labels and derived aggregate statistics for offline "
        "model evaluation supporting proprietary automated-trading research. We would not "
        "redistribute source text or train a generative model on it in this phase.\n\n"
        "Please confirm whether this scope requires consultation, a license, or other conditions, "
        "and identify the permitted retention period and derived-data terms.\n\n"
        "Observed official-page body hashes:\n"
        f"{body_hashes}\n\n"
        "Canonical evidence-record hashes:\n"
        f"{record_hashes}\n\n"
        "No corpus is represented as approved by this draft.\n"
    )


def _validated_output_path(output: Path, project_root: Path) -> Path:
    quarantine = (project_root.resolve() / "var" / "source-use").resolve()
    resolved = output.resolve()
    if quarantine not in resolved.parents:
        raise CorpusIntakeError("output must be a run directory beneath var/source-use")
    return resolved


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as destination:
            destination.write(text)
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _file_evidence(path: Path) -> dict[str, int | str]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise CorpusIntakeError(f"cannot read required source-use artifact {path.name}") from error
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusIntakeError(f"cannot parse required JSON artifact {path.name}") from error
    if not isinstance(value, dict):
        raise CorpusIntakeError(f"required JSON artifact {path.name} must contain an object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise CorpusIntakeError(f"cannot read required JSONL artifact {path.name}") from error
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise CorpusIntakeError(
                f"cannot parse {path.name} line {line_number} as JSON"
            ) from error
        if not isinstance(value, dict):
            raise CorpusIntakeError(f"{path.name} line {line_number} must contain an object")
        rows.append(value)
    return rows
