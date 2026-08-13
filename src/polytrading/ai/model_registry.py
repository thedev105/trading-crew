from __future__ import annotations

from datetime import datetime
from typing import Protocol

from polytrading.ai.models import ModelCard, RelationshipCandidateArtifact, RuleExtractionArtifact
from polytrading.storage.store import DuckDBStore


class ArtifactForValidation(Protocol):
    model_id: str
    model_version: str
    information_cutoff: datetime
    created_at: datetime
    expires_at: datetime


class UnregisteredModelError(ValueError):
    """Raised when an artifact cannot be associated with an active registered model card."""


class ModelRegistry:
    def __init__(self, store: DuckDBStore) -> None:
        self._store = store

    def register(self, card: ModelCard) -> bool:
        return self._store.append_model_card(card)

    def validate_artifact(self, artifact: ArtifactForValidation) -> ModelCard:
        card = self._store.get_model_card(artifact.model_id, artifact.model_version)
        if card is None:
            raise UnregisteredModelError("model card is not registered")
        if card.status == "revoked":
            raise UnregisteredModelError("model card is revoked")
        if card.status == "expired" or (
            card.expires_at is not None and card.expires_at <= artifact.created_at
        ):
            raise UnregisteredModelError("model card is expired")
        if card.status != "validated":
            raise UnregisteredModelError("model card is not validated")
        if artifact.information_cutoff > artifact.created_at:
            raise ValueError("artifact information cutoff must not be after creation")
        if artifact.created_at >= artifact.expires_at:
            raise ValueError("artifact must expire after creation")
        return card

    def record_artifact(
        self, artifact: RuleExtractionArtifact | RelationshipCandidateArtifact
    ) -> bool:
        self.validate_artifact(artifact)
        return self._store.append_ai_artifact(artifact)
