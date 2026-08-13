from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

import numpy as np
from pydantic import field_validator, model_validator
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from polytrading.ai.models import GoldRelationship, ModelCard, NonEmptyString
from polytrading.domain.models import StrictRecord, normalize_utc_timestamp

Split = Literal["train", "validation", "test"]
_SIMILARITY_QUANTUM = Decimal("0.000000000001")
_PARAMETERS = {
    "analyzer": "char_wb",
    "dtype": "float64",
    "lowercase": True,
    "min_df": 1,
    "ngram_range": [3, 5],
    "norm": "l2",
}
_PROHIBITED_USES = (
    "credential_access",
    "order_submission",
    "risk_limit_changes",
    "trade_approval",
)


class RetrievalDocument(StrictRecord):
    contract_id: NonEmptyString
    split: Split
    text: str
    event_family: NonEmptyString | None
    settlement_family: NonEmptyString | None
    asset_or_entity: NonEmptyString | None
    window_start: datetime | None
    window_end: datetime | None

    @field_validator("window_start", "window_end")
    @classmethod
    def require_aware_window(cls, value: datetime | None) -> datetime | None:
        return normalize_utc_timestamp(value) if value is not None else None

    @model_validator(mode="after")
    def require_ordered_window(self) -> RetrievalDocument:
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_end < self.window_start
        ):
            raise ValueError("window end must not precede window start")
        return self


class RetrievalCandidate(StrictRecord):
    query_contract_id: NonEmptyString
    candidate_contract_id: NonEmptyString
    similarity_decimal: Decimal
    rank: int
    warnings: tuple[str, ...]
    feature_version: NonEmptyString


class TfidfCandidateRetriever:
    def __init__(self, top_k: int = 50) -> None:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        self._top_k = top_k
        self._vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(3, 5),
            lowercase=True,
            min_df=1,
            norm="l2",
            dtype=np.float64,
        )
        self._fitted = False
        self._feature_version = _feature_version("unversioned")

    @property
    def vocabulary(self) -> dict[str, int]:
        return dict(self._vectorizer.vocabulary_) if self._fitted else {}

    @property
    def feature_version(self) -> str:
        return self._feature_version

    def fit(
        self, documents: tuple[RetrievalDocument, ...], *, code_revision: str = "unversioned"
    ) -> TfidfCandidateRetriever:
        training = tuple(
            sorted(
                (document for document in documents if document.split == "train"),
                key=lambda item: item.contract_id,
            )
        )
        if not training:
            raise ValueError("at least one train document is required")
        if len({document.contract_id for document in documents}) != len(documents):
            raise ValueError("retrieval document IDs must be unique")
        self._vectorizer.fit([document.text for document in training])
        self._feature_version = _feature_version(code_revision)
        self._fitted = True
        return self

    def retrieve(
        self, documents: tuple[RetrievalDocument, ...], split: Split
    ) -> tuple[RetrievalCandidate, ...]:
        if not self._fitted:
            raise RuntimeError("retriever must be fitted before retrieval")
        selected = tuple(
            sorted(
                (document for document in documents if document.split == split),
                key=lambda item: item.contract_id,
            )
        )
        if len({document.contract_id for document in selected}) != len(selected):
            raise ValueError("retrieval document IDs must be unique")
        if len(selected) < 2:
            return ()
        matrix = self._vectorizer.transform([document.text for document in selected])
        scores = linear_kernel(matrix, matrix)
        output: list[RetrievalCandidate] = []
        for query_index, query in enumerate(selected):
            ranked: list[tuple[float, RetrievalDocument, tuple[str, ...]]] = []
            for candidate_index, candidate in enumerate(selected):
                if query_index == candidate_index:
                    continue
                compatible, warnings = _metadata_compatibility(query, candidate)
                if compatible:
                    ranked.append(
                        (
                            float(scores[query_index, candidate_index]),
                            candidate,
                            warnings,
                        )
                    )
            ranked.sort(key=lambda item: (-item[0], item[1].contract_id))
            for rank, (score, candidate, warnings) in enumerate(ranked[: self._top_k], start=1):
                stable_score = Decimal(str(max(score, 0.0))).quantize(
                    _SIMILARITY_QUANTUM, rounding=ROUND_HALF_EVEN
                )
                output.append(
                    RetrievalCandidate(
                        query_contract_id=query.contract_id,
                        candidate_contract_id=candidate.contract_id,
                        similarity_decimal=stable_score,
                        rank=rank,
                        warnings=warnings,
                        feature_version=self._feature_version,
                    )
                )
        return tuple(output)


def known_relationship_candidate_recall(
    relationships: tuple[GoldRelationship, ...],
    candidates: tuple[RetrievalCandidate, ...],
) -> Decimal:
    if not relationships:
        return Decimal(0)
    retrieved_pairs = {
        frozenset((candidate.query_contract_id, candidate.candidate_contract_id))
        for candidate in candidates
    }
    retrieved = 0
    for relationship in relationships:
        members = relationship.member_contract_ids
        related_pairs = (
            frozenset((left, right))
            for index, left in enumerate(members)
            for right in members[index + 1 :]
        )
        retrieved += any(pair in retrieved_pairs for pair in related_pairs)
    return Decimal(retrieved) / Decimal(len(relationships))


def build_tfidf_model_card(validation_dataset_hash: str, code_revision: str) -> ModelCard:
    return ModelCard(
        schema_version=1,
        model_id="semantic-tfidf-char35",
        version="1.0.0",
        owner="polytrading-research",
        intended_use="offline semantic relationship candidate retrieval",
        prohibited_uses=_PROHIBITED_USES,
        authority="research_only",
        implementation_kind="deterministic_baseline",
        training_cutoff=None,
        prompt_version=None,
        feature_version=_feature_version(code_revision),
        validation_dataset_hash=validation_dataset_hash,
        status="draft",
        approved_at=None,
        expires_at=None,
    )


def _feature_version(code_revision: str) -> str:
    payload = json.dumps(
        {"code_revision": code_revision, "parameters": _PARAMETERS},
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metadata_compatibility(
    query: RetrievalDocument, candidate: RetrievalDocument
) -> tuple[bool, tuple[str, ...]]:
    warnings: set[str] = set()
    for field in ("event_family", "settlement_family", "asset_or_entity"):
        left = getattr(query, field)
        right = getattr(candidate, field)
        if left is None or right is None:
            warnings.add(f"missing_metadata:{field}")
        elif left != right:
            return False, ()
    bounds = (query.window_start, query.window_end, candidate.window_start, candidate.window_end)
    if any(value is None for value in bounds):
        warnings.add("missing_metadata:date_window")
    else:
        assert query.window_start is not None
        assert query.window_end is not None
        assert candidate.window_start is not None
        assert candidate.window_end is not None
        if query.window_end < candidate.window_start or candidate.window_end < query.window_start:
            return False, ()
    return True, tuple(sorted(warnings))
