from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import field_validator, model_validator

from polytrading.ai.corpus import CorpusContract, CorpusManifest
from polytrading.ai.model_registry import ModelRegistry
from polytrading.ai.models import (
    FiniteDecimal,
    RelationshipCandidateArtifact,
    RuleExtractionArtifact,
)
from polytrading.ai.spans import validate_rule_fields, validate_span
from polytrading.domain.models import StrictRecord, normalize_utc_timestamp
from polytrading.storage.store import ConflictingRecordError

Artifact = RuleExtractionArtifact | RelationshipCandidateArtifact
_EXACT_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_PROHIBITED_KEYS = frozenset(
    {
        "action",
        "actions",
        "cancel_order",
        "credential",
        "credentials",
        "eligible",
        "leverage",
        "order",
        "orders",
        "place_order",
        "risk_limit",
        "size",
        "tool",
        "tool_call",
        "tool_calls",
        "tools",
        "trade_proposal",
        "url_fetch",
        "wallet",
    }
)


class ArtifactImportError(ValueError):
    """Raised when an untrusted artifact fails the one-way import boundary."""


class ProhibitedArtifactFieldError(ArtifactImportError):
    """Raised before schema parsing when an authority-bearing key is present."""


class ArtifactEnvelope(StrictRecord):
    schema_version: Literal[1]
    artifact: Artifact
    declared_inference_cost_usd: FiniteDecimal
    opaque_reasoning: str | None

    @model_validator(mode="after")
    def require_declared_cost_consistency(self) -> ArtifactEnvelope:
        if self.declared_inference_cost_usd < 0:
            raise ValueError("declared inference cost must be nonnegative")
        if (
            isinstance(self.artifact, RuleExtractionArtifact)
            and self.declared_inference_cost_usd != self.artifact.inference_cost_usd
        ):
            raise ValueError("declared inference cost must equal artifact inference cost")
        return self


class ArtifactImportResult(StrictRecord):
    artifact_id: UUID
    disposition: Literal["accepted", "duplicate_exact"]
    artifact_hash: str
    charged_cost_usd: FiniteDecimal
    cumulative_cost_usd: FiniteDecimal
    remaining_budget_usd: FiniteDecimal
    opaque_reasoning: str | None

    @field_validator("artifact_hash")
    @classmethod
    def require_artifact_hash(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("artifact hash must be lowercase SHA-256")
        return value


def monthly_inference_budget_usd(equity_usd: Decimal) -> Decimal:
    if not equity_usd.is_finite():
        raise ValueError("equity_usd must be finite")
    if equity_usd < 0:
        raise ValueError("equity_usd cannot be negative")
    return min(Decimal("25"), equity_usd * Decimal("0.003125"))


class ArtifactImporter:
    def __init__(
        self,
        registry: ModelRegistry,
        manifest: CorpusManifest,
        contracts: tuple[CorpusContract, ...],
    ) -> None:
        if len({contract.contract_id for contract in contracts}) != len(contracts):
            raise ValueError("corpus contract IDs must be unique")
        self._registry = registry
        self._manifest = manifest
        self._contracts = {contract.contract_id: contract for contract in contracts}
        self._accepted_hashes: dict[UUID, str] = {}

    def import_json(
        self,
        payload: str | bytes,
        *,
        imported_at: datetime,
        equity_usd: Decimal,
        spent_usd: Decimal,
    ) -> ArtifactImportResult:
        parsed = _parse_one_object(payload)
        _reject_prohibited_keys(parsed)
        envelope = ArtifactEnvelope.model_validate_json(payload)
        artifact = envelope.artifact
        imported_at = normalize_utc_timestamp(imported_at)
        _validate_spend(spent_usd)

        if not self._manifest.frozen:
            raise ArtifactImportError("artifact import requires a frozen corpus manifest")
        if _EXACT_VERSION.fullmatch(artifact.model_version) is None:
            raise ArtifactImportError("model version must be an exact semantic version")
        if artifact.created_at > imported_at:
            raise ArtifactImportError("artifact creation timestamp is in the future")
        if artifact.expires_at <= imported_at:
            raise ArtifactImportError("artifact is expired at the explicit import timestamp")
        if artifact.information_cutoff > self._manifest.information_cutoff:
            raise ArtifactImportError("artifact information cutoff exceeds frozen corpus cutoff")

        card = self._registry.validate_artifact(artifact)
        if card.validation_dataset_hash != self._manifest.dataset_id:
            raise ArtifactImportError("model card does not match the frozen corpus manifest")
        if card.approved_at is None or card.approved_at > artifact.created_at:
            raise ArtifactImportError("model card was not approved before artifact creation")

        if isinstance(artifact, RuleExtractionArtifact):
            self._validate_rule_artifact(artifact, card.prompt_version)
        else:
            self._validate_relationship_artifact(artifact)

        artifact_hash = _artifact_hash(artifact)
        prior_hash = self._accepted_hashes.get(artifact.artifact_id)
        if prior_hash is not None:
            if prior_hash != artifact_hash:
                raise ConflictingRecordError("conflicting AI artifact for immutable identity")
            return _result(
                envelope,
                artifact_hash,
                "duplicate_exact",
                charged_cost=Decimal(0),
                spent_usd=spent_usd,
                budget=monthly_inference_budget_usd(equity_usd),
            )

        budget = monthly_inference_budget_usd(equity_usd)
        cost = envelope.declared_inference_cost_usd
        if spent_usd + cost > budget:
            raise ArtifactImportError("artifact exceeds the exact monthly inference budget")
        appended = self._registry.record_artifact(artifact)
        self._accepted_hashes[artifact.artifact_id] = artifact_hash
        if not appended:
            return _result(
                envelope,
                artifact_hash,
                "duplicate_exact",
                charged_cost=Decimal(0),
                spent_usd=spent_usd,
                budget=budget,
            )
        return _result(
            envelope,
            artifact_hash,
            "accepted",
            charged_cost=cost,
            spent_usd=spent_usd,
            budget=budget,
        )

    def _validate_rule_artifact(
        self, artifact: RuleExtractionArtifact, card_prompt_version: str | None
    ) -> None:
        contract = self._contracts.get(artifact.contract_id)
        if contract is None:
            raise ArtifactImportError("artifact references an unknown corpus contract")
        if artifact.information_cutoff != contract.information_cutoff:
            raise ArtifactImportError("artifact information cutoff does not match source contract")
        if artifact.source_hashes != (contract.canonical_text_hash,):
            raise ArtifactImportError("artifact source hashes do not exactly match source contract")
        if card_prompt_version != artifact.prompt_version:
            raise ArtifactImportError("artifact prompt version does not match model card")
        validate_rule_fields(artifact.extracted_fields, contract.canonical_text)

    def _validate_relationship_artifact(self, artifact: RelationshipCandidateArtifact) -> None:
        contracts: list[CorpusContract] = []
        for contract_id in artifact.member_contract_ids:
            contract = self._contracts.get(contract_id)
            if contract is None:
                raise ArtifactImportError("relationship artifact references an unknown contract")
            contracts.append(contract)
        expected_cutoff = max(contract.information_cutoff for contract in contracts)
        if artifact.information_cutoff != expected_cutoff:
            raise ArtifactImportError("relationship information cutoff does not match source set")
        canonical_by_id = {contract.contract_id: contract.canonical_text for contract in contracts}
        for evidence in artifact.supporting_evidence:
            canonical_text = canonical_by_id[evidence.contract_id]
            for span in evidence.supporting_spans:
                validate_span(span, canonical_text)


def _parse_one_object(payload: str | bytes) -> dict[str, object]:
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ArtifactImportError("input must contain exactly one JSON object") from error
    if not isinstance(parsed, dict):
        raise ArtifactImportError("input must contain exactly one JSON object")
    return parsed


def _reject_prohibited_keys(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.casefold().replace("-", "_")
            if normalized in _PROHIBITED_KEYS:
                location = ".".join((*path, key))
                raise ProhibitedArtifactFieldError(
                    f"prohibited artifact field {key!r} at {location}"
                )
            _reject_prohibited_keys(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_prohibited_keys(child, (*path, str(index)))


def _artifact_hash(artifact: Artifact) -> str:
    canonical = json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_spend(spent_usd: Decimal) -> None:
    if not spent_usd.is_finite() or spent_usd < 0:
        raise ValueError("spent_usd must be finite and nonnegative")


def _result(
    envelope: ArtifactEnvelope,
    artifact_hash: str,
    disposition: Literal["accepted", "duplicate_exact"],
    *,
    charged_cost: Decimal,
    spent_usd: Decimal,
    budget: Decimal,
) -> ArtifactImportResult:
    cumulative = spent_usd + charged_cost
    return ArtifactImportResult(
        artifact_id=envelope.artifact.artifact_id,
        disposition=disposition,
        artifact_hash=artifact_hash,
        charged_cost_usd=charged_cost,
        cumulative_cost_usd=cumulative,
        remaining_budget_usd=budget - cumulative,
        opaque_reasoning=envelope.opaque_reasoning,
    )
