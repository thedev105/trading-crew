from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from polytrading.corpus_intake.artifacts import CorpusRunWriter, verify_run
from polytrading.corpus_intake.models import (
    AcquisitionDiagnostics,
    AcquisitionRequest,
    AcquisitionResult,
    CorpusIntakeError,
)
from polytrading.corpus_intake.polymarket import parse_page

FIXTURE = Path("tests/fixtures/polymarket/markets_keyset_page_1.json")
RETRIEVED_AT = datetime(2026, 8, 12, 16, tzinfo=UTC)
CUTOFF = datetime(2026, 8, 12, 15, tzinfo=UTC)


def _request() -> AcquisitionRequest:
    return AcquisitionRequest(
        retrieved_at=RETRIEVED_AT,
        information_cutoff=CUTOFF,
        max_candidates=500,
        page_size=100,
        max_pages=10,
    )


def _page():
    return parse_page(
        body=FIXTURE.read_bytes(),
        request_url="https://gamma-api.polymarket.com/markets/keyset?limit=100",
        requested_cursor=None,
        page_ordinal=1,
        retrieved_at=RETRIEVED_AT,
        information_cutoff=CUTOFF,
        status_code=200,
        headers={"content-type": "application/json"},
    )


def _result():
    page = _page()
    return AcquisitionResult(
        candidates=tuple(reversed(page.candidates)),
        diagnostics=AcquisitionDiagnostics(
            page_count=1,
            received_market_count=2,
            exact_duplicate_count=0,
            canonical_duplicate_count=0,
            truncated_at_candidate_limit=False,
            truncated_at_page_limit=False,
        ),
    )


def _output(project: Path, name: str = "run") -> Path:
    return project / "var" / "corpus-intake" / name


def test_writer_streams_raw_then_writes_canonical_manifest_last(tmp_path: Path) -> None:
    output = _output(tmp_path)
    writer = CorpusRunWriter(output, project_root=tmp_path, request=_request())

    writer.append_raw_page(_page().raw)
    assert (output / "raw_pages.jsonl").exists()
    assert not (output / "manifest.json").exists()
    writer.complete(_result())

    candidates_text = (output / "candidates.jsonl").read_text()
    candidate_rows = [json.loads(line) for line in candidates_text.splitlines()]
    assert candidates_text.endswith("\n")
    assert [row["source_market_id"] for row in candidate_rows] == ["101", "102"]
    assert {row["retention_status"] for row in candidate_rows} == {"review_required"}
    assert candidate_rows[0]["retrieved_at"] == "2026-08-12T16:00:00Z"

    coverage = json.loads((output / "coverage.json").read_text())
    assert coverage["candidate_count"] == 2
    assert coverage["event_family_count"] == 2
    assert coverage["categories"] == {"Crypto": 1, "Sports": 1}
    assert coverage["routing_tags"]["deadline_or_date"] == 2

    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["schema_version"] == "corpus-intake-v1"
    assert manifest["status"] == "complete"
    assert manifest["retention_status"] == "review_required"
    assert manifest["retention_basis"] is None
    for name in ("raw_pages.jsonl", "candidates.jsonl", "coverage.json"):
        data = (output / name).read_bytes()
        assert manifest["files"][name] == {
            "bytes": len(data),
            "sha256": sha256(data).hexdigest(),
        }

    summary = verify_run(output)
    assert summary.candidate_count == 2
    assert summary.event_family_count == 2
    assert summary.raw_page_count == 1
    assert summary.manifest_sha256 == sha256((output / "manifest.json").read_bytes()).hexdigest()


def test_failed_or_partial_run_has_no_completion_manifest(tmp_path: Path) -> None:
    output = _output(tmp_path)
    writer = CorpusRunWriter(output, project_root=tmp_path, request=_request())

    writer.append_raw_page(_page().raw)

    assert sorted(path.name for path in output.iterdir()) == ["raw_pages.jsonl"]
    with pytest.raises(CorpusIntakeError, match="page count"):
        writer.complete(
            replace(
                _result(),
                diagnostics=replace(_result().diagnostics, page_count=2),
            )
        )
    assert not (output / "manifest.json").exists()


def test_writer_rejects_non_quarantine_gold_nonempty_and_symlink_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    gold = project / "data" / "gold"
    gold.mkdir(parents=True)
    outside = project / "candidate-run"

    for path in (gold / "run", outside):
        with pytest.raises(CorpusIntakeError, match="var/corpus-intake"):
            CorpusRunWriter(path, project_root=project, request=_request())

    nonempty = _output(project, "nonempty")
    nonempty.mkdir(parents=True)
    (nonempty / "existing").write_text("owned")
    with pytest.raises(CorpusIntakeError, match="non-empty"):
        CorpusRunWriter(nonempty, project_root=project, request=_request())

    quarantine = project / "var" / "corpus-intake"
    quarantine.mkdir(parents=True, exist_ok=True)
    linked = quarantine / "linked"
    linked.symlink_to(gold, target_is_directory=True)
    with pytest.raises(CorpusIntakeError, match="var/corpus-intake"):
        CorpusRunWriter(linked / "run", project_root=project, request=_request())


def test_writer_rejects_lineage_mismatch_and_verifier_detects_tampering(tmp_path: Path) -> None:
    output = _output(tmp_path)
    writer = CorpusRunWriter(output, project_root=tmp_path, request=_request())
    page = _page()
    writer.append_raw_page(page.raw)
    bad_candidate = replace(page.candidates[0], raw_body_sha256="f" * 64)
    with pytest.raises(CorpusIntakeError, match="lineage"):
        writer.complete(replace(_result(), candidates=(bad_candidate,)))

    clean_output = _output(tmp_path, "clean")
    clean = CorpusRunWriter(clean_output, project_root=tmp_path, request=_request())
    clean.append_raw_page(page.raw)
    clean.complete(_result())
    with (clean_output / "candidates.jsonl").open("a") as destination:
        destination.write("{}\n")
    with pytest.raises(CorpusIntakeError, match="hash"):
        verify_run(clean_output)


def test_writer_rejects_second_completion(tmp_path: Path) -> None:
    output = _output(tmp_path)
    writer = CorpusRunWriter(output, project_root=tmp_path, request=_request())
    writer.append_raw_page(_page().raw)
    writer.complete(_result())

    with pytest.raises(CorpusIntakeError, match="already complete"):
        writer.complete(_result())
