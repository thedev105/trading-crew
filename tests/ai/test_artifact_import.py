import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.ai.artifact_import import (
    ArtifactEnvelope,
    ArtifactImporter,
    ArtifactImportError,
    ProhibitedArtifactFieldError,
    monthly_inference_budget_usd,
)
from polytrading.ai.corpus import CorpusContract, CorpusManifest, hash_raw_text
from polytrading.ai.extraction import RegexRuleExtractor
from polytrading.ai.model_registry import ModelRegistry
from polytrading.ai.models import (
    ContractSpanEvidence,
    ModelCard,
    RelationshipCandidateArtifact,
    RuleExtractionArtifact,
    SourceSpan,
)
from polytrading.storage.store import ConflictingRecordError, DuckDBStore

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
IMPORTED_AT = NOW + timedelta(hours=2)
DATASET_ID = "a" * 64
ARTIFACT_ID = UUID("00000000-0000-7000-8000-000000000701")


def contract(
    text: str = "BTC resolves above $100.",
    *,
    contract_id: str = "contract-001",
) -> CorpusContract:
    text_hash = hash_raw_text(text)
    return CorpusContract(
        schema_version=1,
        contract_id=contract_id,
        source_url=f"https://example.test/rules/{contract_id}",
        source_retrieved_at=NOW,
        information_cutoff=NOW,
        raw_text=text,
        raw_text_hash=text_hash,
        canonical_text=text,
        canonical_text_hash=text_hash,
        event_family="btc-close",
        sampling_stratum="synthetic",
        split="validation",
        rule_template="binary_threshold",
        provenance=("synthetic fixture",),
        revision_of=None,
        derivative_of=None,
    )


def manifest(**overrides: object) -> CorpusManifest:
    values: dict[str, object] = {
        "schema_version": 1,
        "dataset_id": DATASET_ID,
        "created_at": NOW,
        "information_cutoff": NOW,
        "file_hashes": {
            "contracts": "b" * 64,
            "relationships": "c" * 64,
            "labels": "d" * 64,
            "reviews": "e" * 64,
        },
        "split_family_hashes": {
            "train": "1" * 64,
            "validation": "2" * 64,
            "test": "3" * 64,
        },
        "counts": {"contracts": 1, "relationships": 0, "labels": 0, "reviews": 0},
        "rule_template_counts": {"binary_threshold": 1},
        "adversarial_tag_counts": {},
        "review_completion": {"complete": 0, "unresolved": 1},
        "frozen": True,
    }
    values.update(overrides)
    return CorpusManifest(**values)


def model_card(**overrides: object) -> ModelCard:
    values: dict[str, object] = {
        "schema_version": 1,
        "model_id": "external-rule-artifact",
        "version": "1.0.0",
        "owner": "research",
        "intended_use": "offline imported rule extraction",
        "prohibited_uses": (
            "trade_approval",
            "order_submission",
            "risk_limit_changes",
            "credential_access",
        ),
        "authority": "research_only",
        "implementation_kind": "external_artifact_import",
        "training_cutoff": NOW - timedelta(days=1),
        "prompt_version": "rule-extraction-v1",
        "feature_version": "artifact-schema-v1",
        "validation_dataset_hash": DATASET_ID,
        "status": "validated",
        "approved_at": NOW,
        "expires_at": NOW + timedelta(days=30),
    }
    values.update(overrides)
    return ModelCard(**values)


def artifact(source: CorpusContract, **overrides: object) -> RuleExtractionArtifact:
    values: dict[str, object] = {
        "schema_version": 1,
        "artifact_id": ARTIFACT_ID,
        "contract_id": source.contract_id,
        "information_cutoff": NOW,
        "source_hashes": (source.canonical_text_hash,),
        "model_id": "external-rule-artifact",
        "model_version": "1.0.0",
        "prompt_version": "rule-extraction-v1",
        "inference_parameters_hash": "f" * 64,
        "extracted_fields": RegexRuleExtractor().extract(source.canonical_text).fields,
        "uncertainty": Decimal("0.10"),
        "abstention_reason": None,
        "inference_latency_ms": Decimal("125"),
        "inference_cost_usd": Decimal("1.25"),
        "created_at": NOW + timedelta(hours=1),
        "expires_at": NOW + timedelta(days=7),
        "invalidation_conditions": ("source revision",),
    }
    values.update(overrides)
    return RuleExtractionArtifact(**values)


def envelope_json(source: CorpusContract, **artifact_overrides: object) -> str:
    envelope = ArtifactEnvelope(
        schema_version=1,
        artifact=artifact(source, **artifact_overrides),
        declared_inference_cost_usd=Decimal("1.25"),
        opaque_reasoning='{"trade_proposal":"this remains an opaque string"}',
    )
    return envelope.model_dump_json()


@pytest.fixture
def importer(tmp_path: Path):
    store = DuckDBStore(tmp_path / "artifact-import.duckdb")
    registry = ModelRegistry(store)
    registry.register(model_card())
    source = contract()
    service = ArtifactImporter(registry, manifest(), (source,))
    try:
        yield service, source
    finally:
        store.close()


def test_monthly_budget_uses_exact_decimal_arithmetic() -> None:
    assert monthly_inference_budget_usd(Decimal("8000")) == Decimal("25")
    assert monthly_inference_budget_usd(Decimal("1000")) == Decimal("3.125000")
    assert monthly_inference_budget_usd(Decimal("0")) == Decimal("0.000000")
    with pytest.raises(ValueError, match="negative"):
        monthly_inference_budget_usd(Decimal("-0.01"))


def test_import_accepts_one_strict_source_bound_artifact_and_keeps_reasoning_opaque(
    importer,
) -> None:
    service, source = importer

    result = service.import_json(
        envelope_json(source),
        imported_at=IMPORTED_AT,
        equity_usd=Decimal("8000"),
        spent_usd=Decimal("2"),
    )

    assert result.disposition == "accepted"
    assert result.charged_cost_usd == Decimal("1.25")
    assert result.cumulative_cost_usd == Decimal("3.25")
    assert result.remaining_budget_usd == Decimal("21.75")
    assert result.opaque_reasoning == '{"trade_proposal":"this remains an opaque string"}'


@pytest.mark.parametrize(
    "payload",
    [
        "preface {}",
        "{} trailing",
        "{} {}",
        "[]",
    ],
)
def test_import_rejects_prose_multiple_values_and_non_object_json(importer, payload: str) -> None:
    service, _ = importer

    with pytest.raises(ArtifactImportError, match="exactly one JSON object"):
        service.import_json(
            payload,
            imported_at=IMPORTED_AT,
            equity_usd=Decimal("8000"),
            spent_usd=Decimal(0),
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "action",
        "tool_call",
        "trade_proposal",
        "eligible",
        "order",
        "size",
        "leverage",
        "risk_limit",
    ],
)
def test_import_rejects_prohibited_fields_at_any_nesting_depth(importer, field_name: str) -> None:
    service, source = importer
    payload = json.loads(envelope_json(source))
    payload["artifact"]["extracted_fields"]["subject"]["nested"] = {field_name: "bad"}

    with pytest.raises(ProhibitedArtifactFieldError, match=field_name):
        service.import_json(
            json.dumps(payload),
            imported_at=IMPORTED_AT,
            equity_usd=Decimal("8000"),
            spent_usd=Decimal(0),
        )


def test_import_rejects_unknown_fields_and_invalid_enums(importer) -> None:
    service, source = importer
    unknown = json.loads(envelope_json(source))
    unknown["unexpected"] = True
    invalid_enum = json.loads(envelope_json(source))
    invalid_enum["artifact"]["extracted_fields"]["operator"]["status"] = "maybe"

    for payload in (unknown, invalid_enum):
        with pytest.raises(ValidationError):
            service.import_json(
                json.dumps(payload),
                imported_at=IMPORTED_AT,
                equity_usd=Decimal("8000"),
                spent_usd=Decimal(0),
            )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"model_id": "unknown-model"}, "not registered"),
        ({"model_version": "latest"}, "semantic version"),
        ({"source_hashes": ("9" * 64,)}, "source hashes"),
        ({"information_cutoff": NOW + timedelta(minutes=1)}, "information cutoff"),
        ({"expires_at": NOW + timedelta(minutes=90)}, "expired"),
    ],
)
def test_import_rejects_unregistered_alias_changed_future_or_expired_artifacts(
    importer, overrides: dict[str, object], message: str
) -> None:
    service, source = importer

    with pytest.raises((ArtifactImportError, ValueError), match=message):
        service.import_json(
            envelope_json(source, **overrides),
            imported_at=IMPORTED_AT,
            equity_usd=Decimal("8000"),
            spent_usd=Decimal(0),
        )


def test_import_rejects_invalid_source_span(importer) -> None:
    service, source = importer
    payload = json.loads(envelope_json(source))
    payload["artifact"]["extracted_fields"]["threshold"]["supporting_spans"][0]["exact_text"] = (
        "$999"
    )

    with pytest.raises(ValueError, match="span text mismatch"):
        service.import_json(
            json.dumps(payload),
            imported_at=IMPORTED_AT,
            equity_usd=Decimal("8000"),
            spent_usd=Decimal(0),
        )


def test_import_rejects_cost_over_exact_budget(importer) -> None:
    service, source = importer

    with pytest.raises(ArtifactImportError, match="monthly inference budget"):
        service.import_json(
            envelope_json(source),
            imported_at=IMPORTED_AT,
            equity_usd=Decimal("1000"),
            spent_usd=Decimal("2"),
        )


def test_duplicate_id_is_idempotent_only_for_identical_content(importer) -> None:
    service, source = importer
    payload = envelope_json(source)
    first = service.import_json(
        payload,
        imported_at=IMPORTED_AT,
        equity_usd=Decimal("8000"),
        spent_usd=Decimal(0),
    )
    repeated = service.import_json(
        payload,
        imported_at=IMPORTED_AT,
        equity_usd=Decimal("8000"),
        spent_usd=first.cumulative_cost_usd,
    )

    assert repeated.disposition == "duplicate_exact"
    assert repeated.charged_cost_usd == Decimal(0)

    changed = json.loads(payload)
    changed["artifact"]["uncertainty"] = "0.20"
    with pytest.raises(ConflictingRecordError, match="conflicting AI artifact"):
        service.import_json(
            json.dumps(changed),
            imported_at=IMPORTED_AT,
            equity_usd=Decimal("8000"),
            spent_usd=first.cumulative_cost_usd,
        )


def test_import_rejects_unfrozen_or_model_mismatched_manifest(tmp_path: Path) -> None:
    source = contract()
    for corpus_manifest, card in (
        (manifest(frozen=False), model_card()),
        (manifest(), model_card(validation_dataset_hash="9" * 64)),
    ):
        store = DuckDBStore(tmp_path / f"{card.validation_dataset_hash}.duckdb")
        registry = ModelRegistry(store)
        registry.register(card)
        try:
            service = ArtifactImporter(registry, corpus_manifest, (source,))
            with pytest.raises(ArtifactImportError):
                service.import_json(
                    envelope_json(source),
                    imported_at=IMPORTED_AT,
                    equity_usd=Decimal("8000"),
                    spent_usd=Decimal(0),
                )
        finally:
            store.close()


def test_relationship_artifact_validates_exact_spans_for_every_member(tmp_path: Path) -> None:
    left = contract("BTC resolves above $100.", contract_id="left")
    right = contract("BTC resolves at or below $100.", contract_id="right")
    relationship_card = model_card(
        model_id="external-relationship-artifact",
        prompt_version="relationship-v1",
    )
    store = DuckDBStore(tmp_path / "relationship-import.duckdb")
    registry = ModelRegistry(store)
    registry.register(relationship_card)
    service = ArtifactImporter(registry, manifest(), (left, right))

    def evidence(source: CorpusContract) -> ContractSpanEvidence:
        exact = "$100"
        start = source.canonical_text.index(exact)
        return ContractSpanEvidence(
            contract_id=source.contract_id,
            supporting_spans=(
                SourceSpan(
                    start_char=start,
                    end_char=start + len(exact),
                    exact_text=exact,
                    canonical_text_hash=sha256(source.canonical_text.encode("utf-8")).hexdigest(),
                ),
            ),
        )

    candidate = RelationshipCandidateArtifact(
        schema_version=1,
        artifact_id=UUID("00000000-0000-7000-8000-000000000702"),
        member_contract_ids=(left.contract_id, right.contract_id),
        proposed_relationship="complement",
        supporting_evidence=(evidence(left), evidence(right)),
        model_id=relationship_card.model_id,
        model_version=relationship_card.version,
        information_cutoff=NOW,
        uncertainty=Decimal("0.05"),
        abstention_reason=None,
        created_at=NOW + timedelta(hours=1),
        expires_at=NOW + timedelta(days=7),
    )
    payload = ArtifactEnvelope(
        schema_version=1,
        artifact=candidate,
        declared_inference_cost_usd=Decimal("0.50"),
        opaque_reasoning=None,
    ).model_dump_json()
    try:
        result = service.import_json(
            payload,
            imported_at=IMPORTED_AT,
            equity_usd=Decimal("8000"),
            spent_usd=Decimal(0),
        )
        assert result.disposition == "accepted"
        assert result.charged_cost_usd == Decimal("0.50")
    finally:
        store.close()
