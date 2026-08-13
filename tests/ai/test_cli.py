from __future__ import annotations

import ast
import json
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel

from polytrading.ai.artifact_import import ArtifactEnvelope, ArtifactImportResult
from polytrading.ai.corpus import CorpusContract, freeze_manifest, item_input_hash
from polytrading.ai.evaluate import (
    BooleanCaseResult,
    EvaluationAttempt,
    EvaluationRequest,
    FailureExample,
    FieldEvaluationCase,
    ModelVersionRef,
    PayoffCompilerResults,
    SemanticEvaluation,
)
from polytrading.ai.extraction import RegexExtractionResult, build_regex_model_card
from polytrading.ai.metrics import MetricResult, MutationCaseResult, RelationshipMetricCase
from polytrading.ai.model_registry import ModelRegistry
from polytrading.ai.models import (
    ContractSpanEvidence,
    CriticalField,
    GoldContract,
    GoldContractLabel,
    GoldRelationship,
    GoldRelationshipLabel,
    ModelCard,
    RelationshipCandidateArtifact,
    RuleExtractionArtifact,
    RuleFieldSet,
    SourceSpan,
)
from polytrading.ai.prompt_packets import PromptPacket
from polytrading.ai.report import SemanticScoutReport
from polytrading.ai.retrieval import RetrievalCandidate, RetrievalDocument
from polytrading.ai.review import CorpusReviewAssignment, ReviewRecord
from polytrading.cli import main
from polytrading.storage.store import DuckDBStore

FIXTURE = Path("tests/fixtures/ai/corpus")
NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
EXPERIMENT_ID = "019b3b42-0000-7000-8000-000000000001"
CODE_REVISION = "offline-ai-cli-v1"


def frozen_fixture(tmp_path: Path) -> tuple[Path, object]:
    corpus = tmp_path / "corpus"
    shutil.copytree(FIXTURE, corpus)
    manifest = freeze_manifest(corpus, created_at=NOW, require_reviews=False)
    return corpus, manifest


def test_offline_ai_cli_pipeline_is_deterministic_and_research_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus, manifest = frozen_fixture(tmp_path)
    retrieval = tmp_path / "retrieval.jsonl"
    extractions = tmp_path / "extractions.jsonl"
    packets = tmp_path / "packets.jsonl"
    database = tmp_path / "artifacts.duckdb"
    report_dir = tmp_path / "report"

    retrieve_args = [
        "ai",
        "retrieve",
        "--corpus",
        str(corpus),
        "--split",
        "validation",
        "--top-k",
        "50",
        "--output",
        str(retrieval),
    ]
    assert main(retrieve_args) == 0
    first_retrieval = retrieval.read_bytes()
    assert main(retrieve_args) == 0
    assert retrieval.read_bytes() == first_retrieval
    retrieval_rows = [json.loads(line) for line in retrieval.read_text().splitlines()]
    assert len(retrieval_rows) == 2
    assert {row["query_contract_id"] for row in retrieval_rows} == {
        "contract-0003",
        "contract-0004",
    }

    assert (
        main(
            [
                "ai",
                "extract-baseline",
                "--corpus",
                str(corpus),
                "--split",
                "validation",
                "--output",
                str(extractions),
            ]
        )
        == 0
    )
    extraction_rows = [
        ArtifactEnvelope.model_validate_json(line) for line in extractions.read_text().splitlines()
    ]
    assert len(extraction_rows) == 2
    assert all(row.declared_inference_cost_usd == Decimal(0) for row in extraction_rows)

    assert (
        main(
            [
                "ai",
                "prompt-packets",
                "--corpus",
                str(corpus),
                "--split",
                "validation",
                "--output",
                str(packets),
            ]
        )
        == 0
    )
    packet_rows = [
        PromptPacket.model_validate_json(line) for line in packets.read_text().splitlines()
    ]
    assert len(packet_rows) == 2
    assert all(packet.tools_enabled is False for packet in packet_rows)

    draft = build_regex_model_card(manifest.dataset_id, CODE_REVISION)
    fixture_card = draft.model_copy(
        update={
            "status": "validated",
            "approved_at": NOW,
            "expires_at": NOW + timedelta(days=30),
        }
    )
    store = DuckDBStore(database)
    try:
        ModelRegistry(store).register(fixture_card)
    finally:
        store.close()
    assert (
        main(
            [
                "ai",
                "import-artifacts",
                "--input",
                str(extractions),
                "--corpus",
                str(corpus),
                "--db",
                str(database),
                "--equity-usd",
                "8000",
            ]
        )
        == 0
    )

    evaluate_args = [
        "ai",
        "evaluate",
        "--corpus",
        str(corpus),
        "--experiment-id",
        EXPERIMENT_ID,
        "--output",
        str(report_dir),
    ]
    assert main(evaluate_args) == 0
    report_json = report_dir / "semantic-scout-report.json"
    report_markdown = report_dir / "semantic-scout-report.md"
    first_json = report_json.read_bytes()
    first_markdown = report_markdown.read_bytes()
    assert main(evaluate_args) == 0
    assert report_json.read_bytes() == first_json
    assert report_markdown.read_bytes() == first_markdown
    report = json.loads(first_json)
    assert report["overall_status"] == "RESEARCH_ONLY_NOT_PROMOTABLE"
    assert report["evaluation_basis"] == "synthetic_fixture_self_consistency"
    assert report["class_g_false_eligibility"]["status"] == "BLOCKED_BY_DEPENDENCY"
    assert report["inference_cost_usd"] == "0"
    assert not list(report_dir.glob("*.tmp"))
    capsys.readouterr()


def test_production_corpus_deficits_fail_closed_with_exit_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "ai",
            "corpus",
            "validate",
            "--dir",
            "data/gold",
            "--require-contracts",
            "500",
            "--require-templates",
            "20",
            "--require-relationships",
            "250",
            "--require-adversarial",
            "200",
            "--require-two-reviews",
        ]
    )

    assert exit_code == 1
    message = capsys.readouterr().err
    assert "contracts: 0/500" in message
    assert "relationships: 0/250" in message
    assert "independent review" in message


def test_review_cli_writes_only_selected_mutable_corpus(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "mutable-corpus"
    shutil.copytree(FIXTURE, corpus)
    (corpus / "manifest.json").write_text('{"frozen":false}\n')
    first_contract_line = (corpus / "contracts.jsonl").read_text().splitlines()[0]
    item = CorpusContract.model_validate_json(first_contract_line)
    review = ReviewRecord(
        schema_version=1,
        review_id="review-cli-001",
        item_type="contract",
        item_id=item.contract_id,
        reviewer_id="reviewer-a",
        reviewer_role="reviewer",
        input_hash=item_input_hash(item),
        proposed_label_hash="a" * 64,
        decision="accept",
        corrections_json=None,
        reviewed_at=NOW,
    )
    assignment = CorpusReviewAssignment(
        schema_version=1,
        item_type="contract",
        item_id=item.contract_id,
        reviewer_id="reviewer-a",
        input_hash=item_input_hash(item),
    )
    review_path = tmp_path / "review.json"
    assignment_path = tmp_path / "assignment.json"
    review_path.write_text(review.model_dump_json())
    assignment_path.write_text(assignment.model_dump_json())
    production_reviews = Path("data/gold/reviews.jsonl")
    production_before = production_reviews.read_bytes()

    exit_code = main(
        [
            "ai",
            "corpus",
            "review",
            "--dir",
            str(corpus),
            "--item-type",
            "contract",
            "--item-id",
            item.contract_id,
            "--review-file",
            str(review_path),
            "--assignment-file",
            str(assignment_path),
        ]
    )

    assert exit_code == 0
    assert "review-cli-001" in (corpus / "reviews.jsonl").read_text()
    assert production_reviews.read_bytes() == production_before
    assert "recorded immutable reviewer" in capsys.readouterr().out


def test_review_cli_requires_explicit_corpus_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review_path = tmp_path / "review.json"
    review_path.write_text("{}")

    exit_code = main(
        [
            "ai",
            "corpus",
            "review",
            "--item-type",
            "contract",
            "--item-id",
            "contract-0001",
            "--review-file",
            str(review_path),
        ]
    )

    assert exit_code == 2
    assert "--dir" in capsys.readouterr().err


def test_ai_invalid_input_is_exit_one_and_usage_error_is_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus = tmp_path / "unfrozen"
    shutil.copytree(FIXTURE, corpus)
    (corpus / "manifest.json").unlink()

    assert (
        main(
            [
                "ai",
                "evaluate",
                "--corpus",
                str(corpus),
                "--experiment-id",
                EXPERIMENT_ID,
                "--output",
                str(tmp_path / "report"),
            ]
        )
        == 1
    )
    assert "frozen" in capsys.readouterr().err.lower()

    assert main(["ai", "retrieve", "--corpus", str(corpus)]) == 2
    assert "required" in capsys.readouterr().err.lower()


def test_ai_package_has_no_authenticated_execution_provider_or_shell_imports() -> None:
    prohibited_prefixes = (
        "anthropic",
        "aiohttp",
        "httpx",
        "openai",
        "polytrading.credentials",
        "polytrading.execution",
        "polytrading.venues",
        "requests",
        "socket",
        "subprocess",
        "urllib",
    )
    for path in sorted(Path("src/polytrading/ai").glob("*.py")):
        tree = ast.parse(path.read_text())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
        assert not any(
            module == prefix or module.startswith(prefix + ".")
            for module in imported
            for prefix in prohibited_prefixes
        ), path


def test_public_ai_artifact_schemas_have_no_authority_fields() -> None:
    prohibited = {
        "eligible",
        "leverage",
        "order",
        "risk_limit",
        "size",
        "trade_proposal",
    }
    models: tuple[type[BaseModel], ...] = (
        ArtifactEnvelope,
        ArtifactImportResult,
        BooleanCaseResult,
        ContractSpanEvidence,
        CriticalField,
        EvaluationAttempt,
        EvaluationRequest,
        FailureExample,
        FieldEvaluationCase,
        GoldContract,
        GoldContractLabel,
        GoldRelationship,
        GoldRelationshipLabel,
        MetricResult,
        ModelCard,
        ModelVersionRef,
        MutationCaseResult,
        PayoffCompilerResults,
        PromptPacket,
        RegexExtractionResult,
        RelationshipCandidateArtifact,
        RelationshipMetricCase,
        RetrievalCandidate,
        RetrievalDocument,
        RuleExtractionArtifact,
        RuleFieldSet,
        SemanticEvaluation,
        SemanticScoutReport,
        SourceSpan,
    )

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for child in value.values() for key in keys(child)}
        if isinstance(value, list):
            return {key for child in value for key in keys(child)}
        return set()

    assert get_args(RuleExtractionArtifact | RelationshipCandidateArtifact)
    for model in models:
        assert keys(model.model_json_schema()).isdisjoint(prohibited), model.__name__
