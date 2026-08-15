from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from polytrading.corpus_intake.artifacts import CorpusRunWriter
from polytrading.corpus_intake.evidence import (
    POLYMARKET_EVIDENCE_TARGETS,
    SourceUseRunWriter,
    verify_source_use_run,
)
from polytrading.corpus_intake.models import (
    AcquisitionDiagnostics,
    AcquisitionRequest,
    AcquisitionResult,
    CorpusIntakeError,
)
from polytrading.corpus_intake.polymarket import parse_page
from polytrading.corpus_intake.review_queue import (
    prepare_review_queue,
    verify_review_queue_run,
)
from polytrading.corpus_intake.source_policy import (
    IntendedUseScope,
    SourceEvidence,
    SourceUseApproval,
    canonical_sha256,
)
from polytrading.predictions.domain import PredictionSource

NOW = datetime(2026, 8, 12, 16, tzinfo=UTC)
FIXTURE = Path("tests/fixtures/polymarket/markets_keyset_page_1.json")


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


def build_intake(project: Path, name: str = "intake") -> Path:
    body = FIXTURE.read_bytes()
    page = parse_page(
        body=body,
        request_url="https://gamma-api.polymarket.com/markets/keyset?limit=2&include_tag=true",
        requested_cursor=None,
        page_ordinal=1,
        retrieved_at=NOW,
        information_cutoff=NOW - timedelta(hours=1),
        status_code=200,
        headers={"content-type": "application/json"},
    )
    request = AcquisitionRequest(
        retrieved_at=NOW,
        information_cutoff=NOW - timedelta(hours=1),
        max_candidates=2,
        page_size=2,
        max_pages=1,
        request_delay_seconds=0,
    )
    result = AcquisitionResult(
        candidates=page.candidates,
        diagnostics=AcquisitionDiagnostics(
            page_count=1,
            received_market_count=2,
            exact_duplicate_count=0,
            canonical_duplicate_count=0,
            truncated_at_candidate_limit=True,
            truncated_at_page_limit=False,
        ),
    )
    output = project / "var/corpus-intake" / name
    writer = CorpusRunWriter(output, project_root=project, request=request)
    writer.append_raw_page(page.raw)
    writer.complete(result)
    return output


def evidence_record(index: int) -> SourceEvidence:
    target = POLYMARKET_EVIDENCE_TARGETS[index]
    return SourceEvidence(
        schema_version=1,
        source=PredictionSource.POLYMARKET,
        url=target.url,
        retrieved_at=NOW,
        status_code=200,
        content_type="text/html",
        body_byte_count=index + 1,
        body_sha256=str(index + 1) * 64,
        etag=None,
        last_modified=None,
        locator=target.locator,
        excerpt=target.excerpt,
        excerpt_sha256=canonical_sha256(target.excerpt),
        full_body_retained=False,
    )


def build_source_use(project: Path) -> Path:
    output = project / "var/source-use/evidence"
    writer = SourceUseRunWriter(output, project_root=project, retrieved_at=NOW)
    writer.complete(evidence=(evidence_record(0), evidence_record(1)), scope=scope())
    return output


def exact_approval(intake: Path, source_use: Path, **changes: object) -> SourceUseApproval:
    verified = verify_source_use_run(source_use)
    values: dict[str, object] = {
        "schema_version": 1,
        "source": PredictionSource.POLYMARKET,
        "approver_id": "human-legal-reviewer-001",
        "approver_role": "qualified_legal_review",
        "approval_reference": "synthetic-test-approval-only",
        "approved_at": NOW - timedelta(days=2),
        "effective_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=1),
        "scope_sha256": canonical_sha256(verified.scope),
        "evidence_sha256s": verified.evidence_sha256s,
        "intake_manifest_sha256s": (sha256((intake / "manifest.json").read_bytes()).hexdigest(),),
    }
    values.update(changes)
    return SourceUseApproval(**values)


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_unresolved_use_emits_metadata_only_inventory(tmp_path: Path) -> None:
    intake = build_intake(tmp_path)
    source_use = build_source_use(tmp_path)
    output = tmp_path / "var/review-queue/blocked"

    result = prepare_review_queue(
        intake_directories=(intake,),
        source_use_directory=source_use,
        output=output,
        project_root=tmp_path,
        as_of=NOW,
        approval=None,
        reviewer_ids=None,
        ontology_version="candidate-triage-v1",
    )
    verified = verify_review_queue_run(output)
    rows = read_jsonl(output / "blocked_inventory.jsonl")

    assert result.allowed is False
    assert result.reason_code == "external_confirmation_required"
    assert result.item_count == 2
    assert result.blocked_item_count == 2
    assert result.reviewer_packet_count == 0
    assert verified.manifest_sha256 == result.manifest_sha256
    assert verified.blocked_item_count == 2
    assert len(rows) == 2
    assert set(rows[0]) == {
        "candidate_id",
        "candidate_sha256",
        "event_family_id",
        "intake_manifest_sha256",
        "routing_tags",
        "schema_version",
        "source",
        "source_market_id",
    }
    assert not (output / "reviewer-a").exists()
    assert not (output / "reviewer-b").exists()
    assert not list(output.rglob("*.tmp"))


def test_exact_synthetic_approval_emits_two_blinded_packets(tmp_path: Path) -> None:
    intake = build_intake(tmp_path)
    source_use = build_source_use(tmp_path)
    output = tmp_path / "var/review-queue/allowed"
    approval = exact_approval(intake, source_use)

    result = prepare_review_queue(
        intake_directories=(intake,),
        source_use_directory=source_use,
        output=output,
        project_root=tmp_path,
        as_of=NOW,
        approval=approval,
        reviewer_ids=("reviewer-a", "reviewer-b"),
        ontology_version="candidate-triage-v1",
    )
    left_path = output / "reviewer-a/assignments.jsonl"
    right_path = output / "reviewer-b/assignments.jsonl"
    left = read_jsonl(left_path)
    right = read_jsonl(right_path)
    verified = verify_review_queue_run(output)

    assert result.allowed is True
    assert result.reason_code == "exact_human_approval"
    assert result.reviewer_packet_count == 4
    assert result.blocked_item_count == 0
    assert verified.reviewer_packet_count == 4
    assert len(left) == len(right) == 2
    assert {row["reviewer_id"] for row in left} == {"reviewer-a"}
    assert {row["reviewer_id"] for row in right} == {"reviewer-b"}
    assert "reviewer-b" not in left_path.read_text()
    assert "reviewer-a" not in right_path.read_text()
    assert [row["input_hash"] for row in left] == [row["input_hash"] for row in right]
    assert json.loads((output / "decision.json").read_text())["approval_sha256"] == (
        canonical_sha256(approval)
    )
    assert not (output / "blocked_inventory.jsonl").exists()


def test_verifier_rejects_a_packet_that_breaks_reviewer_blinding(tmp_path: Path) -> None:
    intake = build_intake(tmp_path)
    source_use = build_source_use(tmp_path)
    output = tmp_path / "var/review-queue/allowed"
    prepare_review_queue(
        intake_directories=(intake,),
        source_use_directory=source_use,
        output=output,
        project_root=tmp_path,
        as_of=NOW,
        approval=exact_approval(intake, source_use),
        reviewer_ids=("reviewer-a", "reviewer-b"),
        ontology_version="candidate-triage-v1",
    )
    left_path = output / "reviewer-a/assignments.jsonl"
    left = read_jsonl(left_path)
    left[0]["reviewer_id"] = "reviewer-b"
    left_path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            for row in left
        )
    )
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    data = left_path.read_bytes()
    manifest["files"]["reviewer-a/assignments.jsonl"] = {
        "bytes": len(data),
        "sha256": sha256(data).hexdigest(),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    )

    with pytest.raises(CorpusIntakeError, match="blinding"):
        verify_review_queue_run(output)


@pytest.mark.parametrize(
    "approval_changes",
    [
        {"scope_sha256": "a" * 64},
        {"evidence_sha256s": ("a" * 64,)},
        {"intake_manifest_sha256s": ("a" * 64,)},
        {
            "effective_at": NOW - timedelta(days=2),
            "expires_at": NOW - timedelta(days=1),
        },
        {
            "effective_at": NOW + timedelta(days=1),
            "expires_at": NOW + timedelta(days=2),
        },
    ],
)
def test_nonexact_or_inactive_approval_never_writes_source_packets(
    tmp_path: Path, approval_changes: dict[str, object]
) -> None:
    intake = build_intake(tmp_path)
    source_use = build_source_use(tmp_path)
    output = tmp_path / "var/review-queue/blocked"

    result = prepare_review_queue(
        intake_directories=(intake,),
        source_use_directory=source_use,
        output=output,
        project_root=tmp_path,
        as_of=NOW,
        approval=exact_approval(intake, source_use, **approval_changes),
        reviewer_ids=("reviewer-a", "reviewer-b"),
        ontology_version="candidate-triage-v1",
    )

    assert result.allowed is False
    assert result.reviewer_packet_count == 0
    assert not list(output.glob("reviewer-*"))
    assert len(read_jsonl(output / "blocked_inventory.jsonl")) == 2


def test_allowed_release_requires_two_distinct_reviewer_ids(tmp_path: Path) -> None:
    intake = build_intake(tmp_path)
    source_use = build_source_use(tmp_path)

    for number, reviewer_ids in enumerate((None, ("same", "same")), start=1):
        output = tmp_path / f"var/review-queue/invalid-{number}"
        with pytest.raises(CorpusIntakeError, match="two distinct reviewer"):
            prepare_review_queue(
                intake_directories=(intake,),
                source_use_directory=source_use,
                output=output,
                project_root=tmp_path,
                as_of=NOW,
                approval=exact_approval(intake, source_use),
                reviewer_ids=reviewer_ids,
                ontology_version="candidate-triage-v1",
            )
        assert not (output / "manifest.json").exists()


def test_queue_rejects_tampered_intake_and_duplicate_runs(tmp_path: Path) -> None:
    intake = build_intake(tmp_path)
    source_use = build_source_use(tmp_path)
    candidates = intake / "candidates.jsonl"
    original = candidates.read_bytes()
    candidates.write_bytes(original + b"{}\n")

    with pytest.raises(CorpusIntakeError, match="hash"):
        prepare_review_queue(
            intake_directories=(intake,),
            source_use_directory=source_use,
            output=tmp_path / "var/review-queue/tampered",
            project_root=tmp_path,
            as_of=NOW,
            approval=None,
            reviewer_ids=None,
            ontology_version="candidate-triage-v1",
        )

    candidates.write_bytes(original)
    with pytest.raises(CorpusIntakeError, match="duplicate candidate"):
        prepare_review_queue(
            intake_directories=(intake, intake),
            source_use_directory=source_use,
            output=tmp_path / "var/review-queue/duplicate",
            project_root=tmp_path,
            as_of=NOW,
            approval=None,
            reviewer_ids=None,
            ontology_version="candidate-triage-v1",
        )


def test_queue_rejects_nonempty_and_escaping_output(tmp_path: Path) -> None:
    intake = build_intake(tmp_path)
    source_use = build_source_use(tmp_path)
    nonempty = tmp_path / "var/review-queue/nonempty"
    nonempty.mkdir(parents=True)
    (nonempty / "owned").write_text("preserve")

    with pytest.raises(CorpusIntakeError, match="non-empty"):
        prepare_review_queue(
            intake_directories=(intake,),
            source_use_directory=source_use,
            output=nonempty,
            project_root=tmp_path,
            as_of=NOW,
            approval=None,
            reviewer_ids=None,
            ontology_version="candidate-triage-v1",
        )
    with pytest.raises(CorpusIntakeError, match="var/review-queue"):
        prepare_review_queue(
            intake_directories=(intake,),
            source_use_directory=source_use,
            output=tmp_path / "data/gold/reviews",
            project_root=tmp_path,
            as_of=NOW,
            approval=None,
            reviewer_ids=None,
            ontology_version="candidate-triage-v1",
        )
