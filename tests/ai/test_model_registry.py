from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from polytrading.ai.model_registry import ModelRegistry, UnregisteredModelError
from polytrading.ai.models import CriticalField, ModelCard, RuleExtractionArtifact, RuleFieldSet
from polytrading.storage.store import ConflictingRecordError, DuckDBStore

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


def card(**overrides: object) -> ModelCard:
    values: dict[str, object] = {
        "schema_version": 1,
        "model_id": "rule-regex-baseline",
        "version": "1.0.0",
        "owner": "research",
        "intended_use": "offline rule extraction research",
        "prohibited_uses": (
            "trade_approval",
            "order_submission",
            "risk_limit_changes",
            "credential_access",
        ),
        "authority": "research_only",
        "implementation_kind": "deterministic_baseline",
        "training_cutoff": NOW,
        "prompt_version": "rules-v1",
        "feature_version": "features-v1",
        "validation_dataset_hash": "a" * 64,
        "status": "validated",
        "approved_at": NOW,
        "expires_at": NOW.replace(year=2027),
    }
    values.update(overrides)
    return ModelCard(**values)


def artifact(**overrides: object) -> RuleExtractionArtifact:
    unknown = CriticalField(status="unknown", value=None, supporting_spans=())
    values: dict[str, object] = {
        "schema_version": 1,
        "artifact_id": UUID("00000000-0000-0000-0000-000000000111"),
        "contract_id": "contract-001",
        "information_cutoff": NOW,
        "source_hashes": ("a" * 64,),
        "model_id": "rule-regex-baseline",
        "model_version": "1.0.0",
        "prompt_version": "rules-v1",
        "inference_parameters_hash": "a" * 64,
        "extracted_fields": RuleFieldSet(**{field: unknown for field in RuleFieldSet.model_fields}),
        "uncertainty": Decimal("0.1"),
        "abstention_reason": "no deterministic match",
        "inference_latency_ms": Decimal("2.5"),
        "inference_cost_usd": Decimal("0"),
        "created_at": NOW,
        "expires_at": NOW.replace(year=2027),
        "invalidation_conditions": ("source revision",),
    }
    values.update(overrides)
    return RuleExtractionArtifact(**values)


def test_registry_rejects_unregistered_model_versions(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "registry.duckdb")
    registry = ModelRegistry(store)

    with pytest.raises(UnregisteredModelError, match="not registered"):
        registry.validate_artifact(artifact())

    store.close()


def test_registry_is_exactly_idempotent_and_rejects_conflicting_card_retries(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "registry.duckdb")
    registry = ModelRegistry(store)

    assert registry.register(card()) is True
    assert registry.register(card()) is False
    with pytest.raises(ConflictingRecordError, match="conflicting model card"):
        registry.register(card(owner="other"))

    store.close()


def test_registry_rejects_expired_card_and_mismatched_artifact_version(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "registry.duckdb")
    registry = ModelRegistry(store)
    registry.register(
        card(
            approved_at=NOW.replace(day=10),
            expires_at=NOW.replace(day=11),
        )
    )

    with pytest.raises(UnregisteredModelError, match="expired"):
        registry.validate_artifact(artifact())

    store.close()


def test_registry_rejects_revoked_model_cards(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "registry.duckdb")
    registry = ModelRegistry(store)
    registry.register(card(status="revoked"))

    with pytest.raises(UnregisteredModelError, match="revoked"):
        registry.validate_artifact(artifact())

    store.close()


def test_registry_rejects_draft_model_cards(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "registry.duckdb")
    registry = ModelRegistry(store)
    registry.register(card(status="draft", approved_at=None))

    with pytest.raises(UnregisteredModelError, match="model card is not validated"):
        registry.validate_artifact(artifact())

    store.close()


def test_registry_rejects_an_artifact_model_version_that_is_not_registered(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "registry.duckdb")
    registry = ModelRegistry(store)
    registry.register(card())

    with pytest.raises(UnregisteredModelError, match="not registered"):
        registry.validate_artifact(artifact(model_version="2.0.0"))

    store.close()


def test_registry_records_artifacts_with_exact_retry_semantics(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "registry.duckdb")
    registry = ModelRegistry(store)
    candidate = artifact()
    registry.register(card())

    assert registry.record_artifact(candidate) is True
    assert registry.record_artifact(candidate) is False
    with pytest.raises(ConflictingRecordError, match="conflicting AI artifact"):
        registry.record_artifact(artifact(prompt_version="rules-v2"))

    store.close()
