from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from polytrading.corpus_intake.models import (
    AcquisitionRequest,
    AcquisitionResult,
    CorpusCandidate,
    CorpusIntakeError,
    RawPageCapture,
)
from polytrading.corpus_intake.polymarket import DOCUMENTATION_URL, ENDPOINT, SOURCE, parse_page

SCHEMA_VERSION = "corpus-intake-v2"


@dataclass(frozen=True)
class VerifiedRun:
    candidate_count: int
    event_family_count: int
    raw_page_count: int
    manifest_sha256: str


class CorpusRunWriter:
    """Write one immutable, quarantined corpus-candidate acquisition run."""

    def __init__(
        self,
        output: Path,
        *,
        project_root: Path,
        request: AcquisitionRequest,
    ) -> None:
        self.output = _validated_output_path(output, project_root)
        self.output.mkdir(parents=True, exist_ok=True)
        self._raw_path = self.output / "raw_pages.jsonl"
        # Exclusively creating raw_pages.jsonl here (rather than lazily on the first
        # append) closes the check-then-act race between the emptiness check below and a
        # concurrent writer targeting the same output directory: only one process can win
        # this atomic create, so a second concurrent run fails here instead of interleaving
        # appends into the same file.
        try:
            claim_fd = os.open(str(self._raw_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise CorpusIntakeError(
                "corpus intake output directory is already claimed by a concurrent or "
                "unfinished run"
            ) from error
        os.close(claim_fd)
        if any(path != self._raw_path for path in self.output.iterdir()):
            self._raw_path.unlink()
            raise CorpusIntakeError("corpus intake output directory must not be non-empty")
        self.request = request
        self._raw_hashes: set[str] = set()
        self._raw_page_count = 0
        self._completed = False

    def append_raw_page(self, page: RawPageCapture) -> None:
        if self._completed or (self.output / "manifest.json").exists():
            raise CorpusIntakeError("corpus intake run is already complete")
        actual_hash = sha256(page.body_text.encode("utf-8")).hexdigest()
        if actual_hash != page.body_sha256:
            raise CorpusIntakeError("raw page body hash does not match exact UTF-8 text")
        if page.page_ordinal != self._raw_page_count + 1:
            raise CorpusIntakeError("raw page ordinals must be contiguous and one-based")
        with self._raw_path.open("a", encoding="utf-8", newline="\n") as destination:
            destination.write(_canonical_json(page) + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        self._raw_hashes.add(page.body_sha256)
        self._raw_page_count += 1

    def complete(self, result: AcquisitionResult) -> None:
        if self._completed or (self.output / "manifest.json").exists():
            raise CorpusIntakeError("corpus intake run is already complete")
        if result.diagnostics.page_count != self._raw_page_count:
            raise CorpusIntakeError("diagnostic page count does not match captured raw pages")
        ordered = tuple(
            sorted(
                result.candidates,
                key=lambda item: (item.source, item.event_family_id, item.source_market_id),
            )
        )
        _validate_candidates(ordered, self._raw_hashes)
        _atomic_write_jsonl(self.output / "candidates.jsonl", ordered)
        coverage = _coverage(ordered, result)
        _atomic_write_json(self.output / "coverage.json", coverage)
        artifact_names = ("raw_pages.jsonl", "candidates.jsonl", "coverage.json")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "complete",
            "source": SOURCE,
            "endpoint": ENDPOINT,
            "documentation_url": DOCUMENTATION_URL,
            "retention_status": "review_required",
            "retention_basis": None,
            "request": _json_value(self.request),
            "counts": {
                "candidates": len(ordered),
                "event_families": len({item.event_family_id for item in ordered}),
                "raw_pages": self._raw_page_count,
            },
            "diagnostics": _json_value(result.diagnostics),
            "files": {name: _file_evidence(self.output / name) for name in artifact_names},
        }
        _atomic_write_json(self.output / "manifest.json", manifest)
        self._completed = True


def verify_run(output: Path) -> VerifiedRun:
    manifest_path = output / "manifest.json"
    manifest = _read_json_object(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "complete":
        raise CorpusIntakeError("run manifest is not a completed supported intake run")
    if manifest.get("retention_status") != "review_required":
        raise CorpusIntakeError("run manifest has an unexpected retention status")
    if manifest.get("retention_basis") is not None:
        raise CorpusIntakeError("unapproved intake schema cannot contain a retention basis")
    if (
        manifest.get("source") != SOURCE
        or manifest.get("endpoint") != ENDPOINT
        or manifest.get("documentation_url") != DOCUMENTATION_URL
    ):
        raise CorpusIntakeError("run manifest source identity is not supported")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise CorpusIntakeError("run manifest files must be an object")
    for name in ("raw_pages.jsonl", "candidates.jsonl", "coverage.json"):
        evidence = files.get(name)
        if not isinstance(evidence, dict):
            raise CorpusIntakeError(f"run manifest is missing file evidence for {name}")
        actual = _file_evidence(output / name)
        if actual != evidence:
            raise CorpusIntakeError(f"file hash or byte count mismatch for {name}")

    raw_rows = _read_jsonl(output / "raw_pages.jsonl")
    raw_hashes: set[str] = set()
    expected_candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for ordinal, row in enumerate(raw_rows, start=1):
        if row.get("page_ordinal") != ordinal:
            raise CorpusIntakeError("raw page ordinals are not contiguous")
        body_text = row.get("body_text")
        body_hash = row.get("body_sha256")
        if not isinstance(body_text, str) or not isinstance(body_hash, str):
            raise CorpusIntakeError("raw page body evidence is malformed")
        if sha256(body_text.encode("utf-8")).hexdigest() != body_hash:
            raise CorpusIntakeError("raw page body hash mismatch")
        for candidate in _rederive_page_candidates(row, body_text):
            key = (candidate["candidate_id"], candidate["raw_body_sha256"])
            prior = expected_candidates.get(key)
            if prior is not None and prior != candidate:
                raise CorpusIntakeError("raw pages derive conflicting candidates")
            expected_candidates[key] = candidate
        raw_hashes.add(body_hash)

    candidate_rows = _read_jsonl(output / "candidates.jsonl")
    candidate_ids: set[str] = set()
    market_ids: set[tuple[str, str]] = set()
    event_families: set[str] = set()
    for row in candidate_rows:
        candidate_id = row.get("candidate_id")
        event_family_id = row.get("event_family_id")
        if not isinstance(candidate_id, str) or candidate_id in candidate_ids:
            raise CorpusIntakeError("candidate IDs must be present and unique")
        if not isinstance(event_family_id, str):
            raise CorpusIntakeError("candidate event-family ID is malformed")
        if row.get("retention_status") != "review_required":
            raise CorpusIntakeError("candidate has an unexpected retention status")
        if row.get("raw_body_sha256") not in raw_hashes:
            raise CorpusIntakeError("candidate lineage does not reference a captured raw page")
        source = row.get("source")
        market_id = row.get("source_market_id")
        if not isinstance(source, str) or not isinstance(market_id, str):
            raise CorpusIntakeError("candidate source-market identity is malformed")
        market_identity = (source, market_id)
        if market_identity in market_ids:
            raise CorpusIntakeError("candidate source-market identities must be unique")
        expected = expected_candidates.get((candidate_id, row["raw_body_sha256"]))
        if expected != row:
            raise CorpusIntakeError("candidate does not match its exact raw page")
        candidate_ids.add(candidate_id)
        market_ids.add(market_identity)
        event_families.add(event_family_id)

    ordered_rows = sorted(
        candidate_rows,
        key=lambda row: (row["source"], row["event_family_id"], row["source_market_id"]),
    )
    if candidate_rows != ordered_rows:
        raise CorpusIntakeError("candidate rows are not in deterministic order")

    counts = manifest.get("counts")
    expected_counts = {
        "candidates": len(candidate_rows),
        "event_families": len(event_families),
        "raw_pages": len(raw_rows),
    }
    if counts != expected_counts:
        raise CorpusIntakeError("manifest counts do not match run artifacts")
    return VerifiedRun(
        candidate_count=len(candidate_rows),
        event_family_count=len(event_families),
        raw_page_count=len(raw_rows),
        manifest_sha256=sha256(manifest_path.read_bytes()).hexdigest(),
    )


def _rederive_page_candidates(row: dict[str, Any], body_text: str) -> tuple[dict[str, Any], ...]:
    if row.get("source") != SOURCE or row.get("endpoint") != ENDPOINT:
        raise CorpusIntakeError("raw page source identity is not supported")
    request_url = row.get("request_url")
    requested_cursor = row.get("requested_cursor")
    page_ordinal = row.get("page_ordinal")
    status_code = row.get("status_code")
    response_headers = row.get("response_headers")
    if (
        not isinstance(request_url, str)
        or (requested_cursor is not None and not isinstance(requested_cursor, str))
        or isinstance(page_ordinal, bool)
        or not isinstance(page_ordinal, int)
        or isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not isinstance(response_headers, list)
    ):
        raise CorpusIntakeError("raw page acquisition metadata is malformed")
    header_map: dict[str, str] = {}
    for pair in response_headers:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(value, str) for value in pair)
        ):
            raise CorpusIntakeError("raw page response headers are malformed")
        header_map[pair[0]] = pair[1]
    parsed = parse_page(
        body=body_text.encode("utf-8"),
        request_url=request_url,
        requested_cursor=requested_cursor,
        page_ordinal=page_ordinal,
        retrieved_at=_parse_json_timestamp(row.get("retrieved_at"), "retrieved_at"),
        information_cutoff=_parse_json_timestamp(
            row.get("information_cutoff"), "information_cutoff"
        ),
        status_code=status_code,
        headers=header_map,
    )
    if parsed.raw.returned_cursor != row.get("returned_cursor"):
        raise CorpusIntakeError("raw page returned cursor does not match its exact body")
    return tuple(_json_value(candidate) for candidate in parsed.candidates)


def _parse_json_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise CorpusIntakeError(f"raw page {label} is malformed")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError
        return timestamp.astimezone(UTC)
    except ValueError as error:
        raise CorpusIntakeError(f"raw page {label} is malformed") from error


def _validated_output_path(output: Path, project_root: Path) -> Path:
    root = project_root.resolve()
    quarantine = (root / "var" / "corpus-intake").resolve()
    resolved = output.resolve()
    if quarantine not in resolved.parents:
        raise CorpusIntakeError("output must be a run directory beneath var/corpus-intake")
    return resolved


def _validate_candidates(candidates: tuple[CorpusCandidate, ...], raw_hashes: set[str]) -> None:
    candidate_ids: set[str] = set()
    market_ids: set[tuple[str, str]] = set()
    for candidate in candidates:
        if candidate.retention_status != "review_required":
            raise CorpusIntakeError("all candidates must require retention review")
        if candidate.raw_body_sha256 not in raw_hashes:
            raise CorpusIntakeError("candidate lineage does not reference a captured raw page")
        market_identity = (candidate.source, candidate.source_market_id)
        if candidate.candidate_id in candidate_ids or market_identity in market_ids:
            raise CorpusIntakeError("candidate identities must be unique")
        candidate_ids.add(candidate.candidate_id)
        market_ids.add(market_identity)


def _coverage(candidates: tuple[CorpusCandidate, ...], result: AcquisitionResult) -> dict[str, Any]:
    categories = Counter(item.category or "<missing>" for item in candidates)
    families = Counter(item.event_family_id for item in candidates)
    routing_tags = Counter(tag for item in candidates for tag in item.routing_tags)
    source_tags = Counter(tag for item in candidates for tag in item.source_tags)
    warnings = Counter(warning for item in candidates for warning in item.warnings)
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_count": len(candidates),
        "event_family_count": len(families),
        "categories": dict(sorted(categories.items())),
        "event_families": dict(sorted(families.items())),
        "routing_tags": dict(sorted(routing_tags.items())),
        "source_tags": dict(sorted(source_tags.items())),
        "warnings": dict(sorted(warnings.items())),
        "diagnostics": _json_value(result.diagnostics),
        "routing_tags_are_gold_labels": False,
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_value(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, datetime):
        utc_value = value.astimezone(UTC)
        return utc_value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(child) for child in value]
    return value


def _atomic_write_jsonl(path: Path, rows: tuple[object, ...]) -> None:
    text = "".join(_canonical_json(row) + "\n" for row in rows)
    _atomic_write_text(path, text)


def _atomic_write_json(path: Path, value: object) -> None:
    _atomic_write_text(path, _canonical_json(value) + "\n")


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
        raise CorpusIntakeError(f"cannot read required run artifact {path.name}") from error
    return {"bytes": len(data), "sha256": sha256(data).hexdigest()}


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
