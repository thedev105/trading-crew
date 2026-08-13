from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import StringConstraints

from polytrading.corpus_intake.artifacts import verify_run
from polytrading.corpus_intake.evidence import verify_source_use_run
from polytrading.corpus_intake.models import CorpusIntakeError
from polytrading.corpus_intake.source_policy import (
    GateDecision,
    Sha256,
    SourceUseApproval,
    canonical_sha256,
    evaluate_source_gate,
)
from polytrading.domain.models import StrictRecord

REVIEW_QUEUE_SCHEMA_VERSION = "review-queue-v1"
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]


class BlockedInventoryRow(StrictRecord):
    schema_version: Literal[1]
    candidate_id: NonEmptyString
    source: Literal["polymarket"]
    source_market_id: NonEmptyString
    event_family_id: NonEmptyString
    routing_tags: tuple[str, ...]
    candidate_sha256: Sha256
    intake_manifest_sha256: Sha256


class ReviewAssignment(StrictRecord):
    schema_version: Literal[1]
    candidate_id: NonEmptyString
    reviewer_id: NonEmptyString
    ontology_version: NonEmptyString
    input_hash: Sha256
    source: Literal["polymarket"]
    source_market_id: NonEmptyString
    event_family_id: NonEmptyString
    question: NonEmptyString
    description: str | None
    resolution_source: str | None
    category: str | None
    source_tags: tuple[str, ...]
    routing_tags: tuple[str, ...]
    retrieved_at: NonEmptyString
    information_cutoff: NonEmptyString


@dataclass(frozen=True)
class ReviewQueueResult:
    output: Path
    allowed: bool
    reason_code: str
    item_count: int
    blocked_item_count: int
    reviewer_packet_count: int
    manifest_sha256: str


@dataclass(frozen=True)
class VerifiedReviewQueueRun:
    allowed: bool
    reason_code: str
    item_count: int
    blocked_item_count: int
    reviewer_packet_count: int
    manifest_sha256: str


@dataclass(frozen=True)
class _CandidateWithLineage:
    row: dict[str, Any]
    candidate_sha256: str
    intake_manifest_sha256: str


def prepare_review_queue(
    *,
    intake_directories: tuple[Path, ...],
    source_use_directory: Path,
    output: Path,
    project_root: Path,
    as_of,
    approval: SourceUseApproval | None,
    reviewer_ids: tuple[str, str] | None,
    ontology_version: str,
) -> ReviewQueueResult:
    resolved_output = _validated_output_path(output, project_root)
    if resolved_output.exists() and any(resolved_output.iterdir()):
        raise CorpusIntakeError("review-queue output directory must not be non-empty")
    if not intake_directories:
        raise CorpusIntakeError("review queue requires at least one intake directory")
    if not isinstance(ontology_version, str) or not ontology_version.strip():
        raise CorpusIntakeError("review queue ontology version must not be empty")
    resolved_output.mkdir(parents=True, exist_ok=True)

    source_use = verify_source_use_run(source_use_directory)
    candidates: list[_CandidateWithLineage] = []
    candidate_ids: set[str] = set()
    market_ids: set[tuple[str, str]] = set()
    manifest_hashes: list[str] = []
    for intake_directory in intake_directories:
        verified_intake = verify_run(intake_directory)
        manifest_hash = hashlib.sha256(
            (intake_directory / "manifest.json").read_bytes()
        ).hexdigest()
        if manifest_hash != verified_intake.manifest_sha256:
            raise CorpusIntakeError(
                "verified intake manifest hash changed during queue preparation"
            )
        manifest_hashes.append(manifest_hash)
        for row in _read_jsonl(intake_directory / "candidates.jsonl"):
            candidate_id = _required_string(row, "candidate_id")
            source = _required_string(row, "source")
            source_market_id = _required_string(row, "source_market_id")
            if candidate_id in candidate_ids:
                raise CorpusIntakeError(
                    f"duplicate candidate ID across intake runs: {candidate_id}"
                )
            market_identity = (source, source_market_id)
            if market_identity in market_ids:
                raise CorpusIntakeError(
                    "duplicate candidate source-market identity across intake runs"
                )
            candidate_ids.add(candidate_id)
            market_ids.add(market_identity)
            candidates.append(
                _CandidateWithLineage(
                    row=row,
                    candidate_sha256=canonical_sha256(row),
                    intake_manifest_sha256=manifest_hash,
                )
            )
    if len(candidates) > source_use.scope.maximum_records:
        raise CorpusIntakeError("review queue exceeds exact intended-use record limit")
    if not candidates:
        raise CorpusIntakeError("review queue requires at least one verified candidate")
    ordered = tuple(
        sorted(
            candidates,
            key=lambda item: (
                _required_string(item.row, "source"),
                _required_string(item.row, "event_family_id"),
                _required_string(item.row, "source_market_id"),
                _required_string(item.row, "candidate_id"),
            ),
        )
    )
    decision = evaluate_source_gate(
        assessment=source_use.assessment,
        approval=approval,
        scope=source_use.scope,
        evidence_sha256s=source_use.evidence_sha256s,
        intake_manifest_sha256s=tuple(sorted(set(manifest_hashes))),
        as_of=as_of,
    )

    decision_path = resolved_output / "decision.json"
    decision_payload = {
        "schema_version": REVIEW_QUEUE_SCHEMA_VERSION,
        "gate": decision.model_dump(mode="json"),
        "item_count": len(ordered),
        "source_use_manifest_sha256": source_use.manifest_sha256,
        "ontology_version": ontology_version,
        "approval_sha256": canonical_sha256(approval) if approval is not None else None,
    }
    _atomic_write_text(decision_path, _canonical_json(decision_payload) + "\n")
    artifact_paths: list[Path] = [decision_path]
    blocked_count = 0
    packet_count = 0
    if decision.allowed:
        reviewers = _validate_reviewers(reviewer_ids)
        for directory_name, reviewer_id in zip(
            ("reviewer-a", "reviewer-b"), reviewers, strict=True
        ):
            assignments = tuple(
                _assignment(item, reviewer_id, ontology_version) for item in ordered
            )
            assignment_path = resolved_output / directory_name / "assignments.jsonl"
            _atomic_write_text(
                assignment_path,
                "".join(
                    _canonical_json(assignment.model_dump(mode="json")) + "\n"
                    for assignment in assignments
                ),
            )
            artifact_paths.append(assignment_path)
            packet_count += len(assignments)
    else:
        inventory = tuple(_blocked_inventory_row(item) for item in ordered)
        inventory_path = resolved_output / "blocked_inventory.jsonl"
        _atomic_write_text(
            inventory_path,
            "".join(_canonical_json(row.model_dump(mode="json")) + "\n" for row in inventory),
        )
        artifact_paths.append(inventory_path)
        blocked_count = len(inventory)

    manifest = {
        "schema_version": REVIEW_QUEUE_SCHEMA_VERSION,
        "status": "complete",
        "allowed": decision.allowed,
        "reason_code": decision.reason_code,
        "counts": {
            "items": len(ordered),
            "blocked_items": blocked_count,
            "reviewer_packets": packet_count,
        },
        "files": {
            str(path.relative_to(resolved_output)): _file_evidence(path) for path in artifact_paths
        },
    }
    manifest_path = resolved_output / "manifest.json"
    _atomic_write_text(manifest_path, _canonical_json(manifest) + "\n")
    return ReviewQueueResult(
        output=resolved_output,
        allowed=decision.allowed,
        reason_code=decision.reason_code,
        item_count=len(ordered),
        blocked_item_count=blocked_count,
        reviewer_packet_count=packet_count,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )


def verify_review_queue_run(output: Path) -> VerifiedReviewQueueRun:
    manifest_path = output / "manifest.json"
    manifest = _read_json_object(manifest_path)
    if (
        manifest.get("schema_version") != REVIEW_QUEUE_SCHEMA_VERSION
        or manifest.get("status") != "complete"
        or not isinstance(manifest.get("allowed"), bool)
        or not isinstance(manifest.get("reason_code"), str)
    ):
        raise CorpusIntakeError("review-queue manifest is not a completed supported run")
    files = manifest.get("files")
    if not isinstance(files, dict) or "decision.json" not in files:
        raise CorpusIntakeError("review-queue manifest files are malformed")
    for relative_name, evidence in files.items():
        if not isinstance(relative_name, str) or evidence != _file_evidence(output / relative_name):
            raise CorpusIntakeError(
                f"review-queue artifact hash or byte count mismatch for {relative_name}"
            )
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise CorpusIntakeError("review-queue manifest counts are malformed")
    item_count = _nonnegative_count(counts, "items")
    blocked_count = _nonnegative_count(counts, "blocked_items")
    packet_count = _nonnegative_count(counts, "reviewer_packets")
    decision_payload = _read_json_object(output / "decision.json")
    try:
        decision = GateDecision.model_validate_json(_canonical_json(decision_payload.get("gate")))
    except Exception as error:
        raise CorpusIntakeError("review-queue decision gate is malformed") from error
    if (
        decision.allowed != manifest["allowed"]
        or decision.reason_code != manifest["reason_code"]
        or decision_payload.get("item_count") != item_count
    ):
        raise CorpusIntakeError("review-queue decision does not match manifest")
    if decision.allowed:
        expected_files = {
            "decision.json",
            "reviewer-a/assignments.jsonl",
            "reviewer-b/assignments.jsonl",
        }
        if set(files) != expected_files or blocked_count != 0 or packet_count != item_count * 2:
            raise CorpusIntakeError("allowed review queue has inconsistent packet artifacts")
        left = _read_jsonl(output / "reviewer-a/assignments.jsonl")
        right = _read_jsonl(output / "reviewer-b/assignments.jsonl")
        if len(left) + len(right) != packet_count:
            raise CorpusIntakeError("review-queue packet rows do not match manifest")
        try:
            left_records = tuple(
                ReviewAssignment.model_validate_json(_canonical_json(row)) for row in left
            )
            right_records = tuple(
                ReviewAssignment.model_validate_json(_canonical_json(row)) for row in right
            )
        except Exception as error:
            raise CorpusIntakeError("review-queue packet assignment is malformed") from error
        left_reviewers = {record.reviewer_id for record in left_records}
        right_reviewers = {record.reviewer_id for record in right_records}
        if (
            len(left_reviewers) != 1
            or len(right_reviewers) != 1
            or left_reviewers == right_reviewers
            or [(record.candidate_id, record.input_hash) for record in left_records]
            != [(record.candidate_id, record.input_hash) for record in right_records]
        ):
            raise CorpusIntakeError("review-queue packet blinding or item binding is invalid")
        approval_hash = decision_payload.get("approval_sha256")
        if not isinstance(approval_hash, str) or len(approval_hash) != 64:
            raise CorpusIntakeError("allowed review queue is missing approval hash")
    else:
        if set(files) != {"decision.json", "blocked_inventory.jsonl"}:
            raise CorpusIntakeError("blocked review queue has unexpected source-text artifacts")
        inventory = _read_jsonl(output / "blocked_inventory.jsonl")
        if len(inventory) != blocked_count or blocked_count != item_count or packet_count != 0:
            raise CorpusIntakeError("blocked review-queue rows do not match manifest")
        for row in inventory:
            try:
                BlockedInventoryRow.model_validate_json(_canonical_json(row))
            except Exception as error:
                raise CorpusIntakeError("blocked review-queue inventory is malformed") from error
    return VerifiedReviewQueueRun(
        allowed=decision.allowed,
        reason_code=decision.reason_code,
        item_count=item_count,
        blocked_item_count=blocked_count,
        reviewer_packet_count=packet_count,
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )


def _blocked_inventory_row(item: _CandidateWithLineage) -> BlockedInventoryRow:
    return BlockedInventoryRow(
        schema_version=1,
        candidate_id=_required_string(item.row, "candidate_id"),
        source=_required_string(item.row, "source"),
        source_market_id=_required_string(item.row, "source_market_id"),
        event_family_id=_required_string(item.row, "event_family_id"),
        routing_tags=_string_tuple(item.row, "routing_tags"),
        candidate_sha256=item.candidate_sha256,
        intake_manifest_sha256=item.intake_manifest_sha256,
    )


def _assignment(
    item: _CandidateWithLineage, reviewer_id: str, ontology_version: str
) -> ReviewAssignment:
    row = item.row
    return ReviewAssignment(
        schema_version=1,
        candidate_id=_required_string(row, "candidate_id"),
        reviewer_id=reviewer_id,
        ontology_version=ontology_version,
        input_hash=item.candidate_sha256,
        source=_required_string(row, "source"),
        source_market_id=_required_string(row, "source_market_id"),
        event_family_id=_required_string(row, "event_family_id"),
        question=_required_string(row, "question"),
        description=_optional_string(row, "description"),
        resolution_source=_optional_string(row, "resolution_source"),
        category=_optional_string(row, "category"),
        source_tags=_string_tuple(row, "source_tags"),
        routing_tags=_string_tuple(row, "routing_tags"),
        retrieved_at=_required_string(row, "retrieved_at"),
        information_cutoff=_required_string(row, "information_cutoff"),
    )


def _validate_reviewers(reviewer_ids: tuple[str, str] | None) -> tuple[str, str]:
    if (
        reviewer_ids is None
        or len(reviewer_ids) != 2
        or any(not isinstance(value, str) or not value.strip() for value in reviewer_ids)
        or reviewer_ids[0] == reviewer_ids[1]
    ):
        raise CorpusIntakeError("allowed review release requires two distinct reviewer IDs")
    return reviewer_ids


def _required_string(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise CorpusIntakeError(f"candidate field {key} must be a non-empty string")
    return value


def _optional_string(row: dict[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise CorpusIntakeError(f"candidate field {key} must be a string or null")
    return value


def _string_tuple(row: dict[str, Any], key: str) -> tuple[str, ...]:
    value = row.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CorpusIntakeError(f"candidate field {key} must be a string list")
    return tuple(value)


def _nonnegative_count(counts: dict[str, Any], key: str) -> int:
    value = counts.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CorpusIntakeError(f"review-queue count {key} is malformed")
    return value


def _validated_output_path(output: Path, project_root: Path) -> Path:
    quarantine = (project_root.resolve() / "var" / "review-queue").resolve()
    resolved = output.resolve()
    if quarantine not in resolved.parents:
        raise CorpusIntakeError("output must be a run directory beneath var/review-queue")
    return resolved


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        raise CorpusIntakeError(f"cannot read review-queue artifact {path}") from error
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
