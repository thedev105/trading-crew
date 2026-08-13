from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from polytrading.ai.model_registry import ModelRegistry
from polytrading.ai.models import GoldRelationship
from polytrading.ai.retrieval import (
    RetrievalDocument,
    TfidfCandidateRetriever,
    build_tfidf_model_card,
    known_relationship_candidate_recall,
)
from polytrading.storage.store import DuckDBStore

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
MANIFEST_HASH = "a" * 64


def document(contract_id: str, text: str, split: str = "validation", **overrides: object):
    values: dict[str, object] = {
        "contract_id": contract_id,
        "split": split,
        "text": text,
        "event_family": "btc-close",
        "settlement_family": "price-close",
        "asset_or_entity": "BTC",
        "window_start": NOW,
        "window_end": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return RetrievalDocument(**values)


def fitted_retriever(*documents: RetrievalDocument, top_k: int = 50):
    training = (
        document("train-a", "btc close threshold", "train"),
        document("train-b", "eth weather election unrelated", "train"),
    )
    retriever = TfidfCandidateRetriever(top_k=top_k)
    retriever.fit(training + documents)
    return retriever


def test_retrieval_filters_incompatible_metadata_before_ranking() -> None:
    query = document("query", "Will BTC close above 100?")
    compatible = document("compatible", "BTC closes higher than 100")
    wrong_asset = document("wrong-asset", "Will BTC close above 100?", asset_or_entity="ETH")
    wrong_family = document("wrong-family", "Will BTC close above 100?", event_family="btc-open")
    no_overlap = document(
        "no-overlap",
        "Will BTC close above 100?",
        window_start=NOW + timedelta(days=1),
        window_end=NOW + timedelta(days=1, hours=1),
    )

    results = fitted_retriever(query, compatible, wrong_asset, wrong_family, no_overlap).retrieve(
        (query, compatible, wrong_asset, wrong_family, no_overlap), "validation"
    )

    assert {(row.query_contract_id, row.candidate_contract_id) for row in results} == {
        ("compatible", "query"),
        ("query", "compatible"),
    }


def test_unknown_metadata_widens_retrieval_and_emits_stable_warnings() -> None:
    query = document("query", "BTC close above 100", settlement_family=None)
    candidate = document("candidate", "BTC close higher than 100", asset_or_entity=None)

    rows = fitted_retriever(query, candidate).retrieve((query, candidate), "validation")

    assert len(rows) == 2
    assert rows[0].warnings == (
        "missing_metadata:asset_or_entity",
        "missing_metadata:settlement_family",
    )


def test_retrieval_uses_train_vocabulary_without_refitting_requested_split() -> None:
    validation = (
        document("v1", "zzunique-one"),
        document("v2", "zzunique-two"),
    )
    retriever = fitted_retriever(*validation)
    vocabulary_before = dict(retriever.vocabulary)

    retriever.retrieve(validation, "validation")

    assert retriever.vocabulary == vocabulary_before
    assert not any(token.startswith("zzu") for token in retriever.vocabulary)


def test_ranking_is_deterministic_excludes_self_and_breaks_ties_by_contract_id() -> None:
    query = document("query", "BTC close above 100")
    candidate_b = document("candidate-b", "BTC close above 100")
    candidate_a = document("candidate-a", "BTC close above 100")
    unrelated = document("unrelated", "Rain in Stockholm")
    documents = (query, candidate_b, candidate_a, unrelated)
    retriever = fitted_retriever(*documents, top_k=3)

    first = retriever.retrieve(documents, "validation")
    second = retriever.retrieve(tuple(reversed(documents)), "validation")
    query_rows = [row for row in first if row.query_contract_id == "query"]

    assert first == second
    assert [row.candidate_contract_id for row in query_rows[:2]] == [
        "candidate-a",
        "candidate-b",
    ]
    assert all(row.query_contract_id != row.candidate_contract_id for row in first)
    assert all(row.similarity_decimal.as_tuple().exponent == -12 for row in first)


def test_exact_title_paraphrase_ranks_above_unrelated_text() -> None:
    query = document("query", "Will Bitcoin close above $100,000 on August 12?")
    paraphrase = document("paraphrase", "Bitcoin closing price higher than $100,000 on August 12")
    unrelated = document("unrelated", "Will rainfall exceed 10 millimeters in Stockholm?")
    documents = (query, paraphrase, unrelated)

    rows = fitted_retriever(*documents).retrieve(documents, "validation")
    query_rows = [row for row in rows if row.query_contract_id == "query"]

    assert [row.candidate_contract_id for row in query_rows] == ["paraphrase", "unrelated"]
    assert query_rows[0].similarity_decimal > query_rows[1].similarity_decimal


def test_retrieval_never_crosses_splits() -> None:
    validation = document("validation", "BTC close above 100", "validation")
    test = document("test", "BTC close above 100", "test")
    retriever = fitted_retriever(validation, test)

    assert retriever.retrieve((validation, test), "validation") == ()
    assert retriever.retrieve((validation, test), "test") == ()


def test_known_relationship_candidate_recall_is_exact_nine_of_ten() -> None:
    documents = tuple(document(f"c{index:02d}", f"BTC threshold {index}") for index in range(20))
    relationships = tuple(
        GoldRelationship(
            schema_version=1,
            relationship_id=f"r{index:02d}",
            member_contract_ids=(f"c{index * 2:02d}", f"c{index * 2 + 1:02d}"),
            split="validation",
        )
        for index in range(10)
    )
    retriever = fitted_retriever(*documents, top_k=50)
    rows = retriever.retrieve(documents, "validation")
    removed_pair = {"c18", "c19"}
    rows = tuple(
        row for row in rows if {row.query_contract_id, row.candidate_contract_id} != removed_pair
    )

    assert known_relationship_candidate_recall(relationships, rows) == Decimal("0.9")


def test_invalid_retrieval_configuration_and_windows_fail_closed() -> None:
    with pytest.raises(ValueError, match="top_k must be positive"):
        TfidfCandidateRetriever(top_k=0)
    with pytest.raises(ValueError, match="window end"):
        document(
            "bad-window",
            "text",
            window_start=NOW + timedelta(hours=1),
            window_end=NOW,
        )
    with pytest.raises(ValueError, match="train"):
        TfidfCandidateRetriever().fit((document("v", "text"),))


def test_baseline_model_card_is_pinned_draft_research_only_and_registrable(
    tmp_path: Path,
) -> None:
    card = build_tfidf_model_card(MANIFEST_HASH, "deadbeef")
    repeated = build_tfidf_model_card(MANIFEST_HASH, "deadbeef")
    changed = build_tfidf_model_card(MANIFEST_HASH, "cafebabe")

    assert card == repeated
    assert card.model_id == "semantic-tfidf-char35"
    assert card.version == "1.0.0"
    assert card.authority == "research_only"
    assert card.implementation_kind == "deterministic_baseline"
    assert card.status == "draft"
    assert card.validation_dataset_hash == MANIFEST_HASH
    assert card.feature_version != changed.feature_version
    assert set(card.prohibited_uses) >= {
        "trade_approval",
        "order_submission",
        "risk_limit_changes",
        "credential_access",
    }

    store = DuckDBStore(tmp_path / "retrieval-model.duckdb")
    try:
        registry = ModelRegistry(store)
        assert registry.register(card) is True
        assert registry.register(card) is False
    finally:
        store.close()
