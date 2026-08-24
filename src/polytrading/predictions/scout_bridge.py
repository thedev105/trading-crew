from __future__ import annotations

from datetime import datetime
from typing import Literal

from polytrading.ai.evaluate import SemanticEvaluation
from polytrading.ai.models import ModelCard
from polytrading.ai.retrieval import (
    RetrievalDocument,
    TfidfCandidateRetriever,
    build_tfidf_model_card,
)
from polytrading.predictions.candidates import _all_legs_or_none, _is_eligible_open_market
from polytrading.predictions.candidates_models import (
    AIProvenance,
    CandidateDisposition,
    CandidateLeg,
    CandidateRelationship,
    RelationshipType,
    deterministic_candidate_id,
)
from polytrading.predictions.domain import MarketRecord, PredictionRecord, PredictionVenue, Sha256
from polytrading.predictions.registry import PredictionRegistry

# Spec section 4.4: every Engine D cross-venue equivalence dimension, verbatim. A
# cross-venue nomination is never AI-resolved on any of these -- the equivalence
# compiler (increment 3) is the only thing permitted to narrow this list.
_ENGINE_D_UNRESOLVED_FIELDS: tuple[str, ...] = (
    "proposition_threshold_inclusivity",
    "observation_period_timezone",
    "resolution_sources",
    "void_dispute_behavior",
    "outcome_completeness",
    "denomination_collateral_rounding",
    "settlement_finality_timing",
    "venue_access_custody_rules",
)
_INVALIDATION_CONDITIONS: tuple[str, ...] = ("any participating rule_version change",)

# The bridge fits an index-time-only TF-IDF corpus per call, not against a versioned
# validation dataset, so these are fixed identity inputs, not tunable knobs.
_RETRIEVAL_CODE_REVISION = "unversioned"
_UNVALIDATED_DATASET_HASH = "0" * 64


class ScoutAbstention(PredictionRecord):
    """A typed, fail-closed refusal to nominate -- never a silent empty result."""

    reason: Literal["SCOUT_GATE_UNMET", "NO_EVALUATION_SUPPLIED", "NO_ELIGIBLE_MARKETS"]
    evaluation_request_hash: Sha256 | None
    as_of: datetime


def nominate_cross_venue_candidates(
    registry: PredictionRegistry,
    evaluation: SemanticEvaluation | None,
    venue_a: PredictionVenue,
    venue_b: PredictionVenue,
    as_of: datetime,
    *,
    trial_family_id: str,
    top_k: int = 3,
) -> tuple[CandidateRelationship, ...] | ScoutAbstention:
    """Nominate ``CROSS_VENUE_EQUIVALENCE`` candidates, gated on a passing evaluation.

    Pure nomination: no storage writes, no network access, no disposition mutation. Every
    emitted candidate is quarantined AI provenance carrying every Engine D equivalence
    dimension unresolved. Fails closed: a missing or non-passing evaluation, or an empty
    eligible-market set on either venue, produces a typed ``ScoutAbstention`` instead of an
    empty tuple.
    """
    if evaluation is None:
        return ScoutAbstention(
            reason="NO_EVALUATION_SUPPLIED", evaluation_request_hash=None, as_of=as_of
        )
    if evaluation.gate_status != "PASS":
        return ScoutAbstention(
            reason="SCOUT_GATE_UNMET",
            evaluation_request_hash=evaluation.request_hash,
            as_of=as_of,
        )

    markets_a = {
        market.market_id: market
        for market in registry.markets_by_venue_as_of(venue_a, as_of)
        if _is_eligible_open_market(market)
    }
    markets_b = {
        market.market_id: market
        for market in registry.markets_by_venue_as_of(venue_b, as_of)
        if _is_eligible_open_market(market)
    }
    if not markets_a or not markets_b:
        return ScoutAbstention(
            reason="NO_ELIGIBLE_MARKETS",
            evaluation_request_hash=evaluation.request_hash,
            as_of=as_of,
        )

    # Index venue B's open order-book markets, then query with venue A's -- one venue A
    # market at a time, so top_k is spent entirely on venue B candidates rather than being
    # diluted by other venue A markets sharing the retrieval batch.
    index_documents = tuple(
        sorted(
            (_retrieval_document(venue_b, market, "train") for market in markets_b.values()),
            key=lambda document: document.contract_id,
        )
    )
    retriever = TfidfCandidateRetriever(top_k=top_k)
    retriever.fit(index_documents, code_revision=_RETRIEVAL_CODE_REVISION)
    model_card = build_tfidf_model_card(_UNVALIDATED_DATASET_HASH, _RETRIEVAL_CODE_REVISION)

    candidate_documents = tuple(
        sorted(
            (_retrieval_document(venue_b, market, "test") for market in markets_b.values()),
            key=lambda document: document.contract_id,
        )
    )

    candidates: list[CandidateRelationship] = []
    for market_id in sorted(markets_a):
        market_a = markets_a[market_id]
        query_document = _retrieval_document(venue_a, market_a, "test")
        retrieved = retriever.retrieve((query_document, *candidate_documents), "test")
        for result in retrieved:
            if result.query_contract_id != query_document.contract_id:
                continue
            market_b = markets_b.get(_market_id_from_contract_id(result.candidate_contract_id))
            if market_b is None:
                continue
            legs = _all_legs_or_none(registry, [market_a, market_b], as_of)
            if legs is None:
                continue
            candidates.append(
                _build_candidate(legs, trial_family_id, as_of, evaluation, model_card)
            )

    if not candidates:
        return ScoutAbstention(
            reason="NO_ELIGIBLE_MARKETS",
            evaluation_request_hash=evaluation.request_hash,
            as_of=as_of,
        )
    return tuple(candidates)


def _retrieval_document(
    venue: PredictionVenue, market: MarketRecord, split: Literal["train", "test"]
) -> RetrievalDocument:
    return RetrievalDocument(
        contract_id=_contract_id(venue, market.market_id),
        split=split,
        text=market.question,
        event_family=None,
        settlement_family=None,
        asset_or_entity=None,
        window_start=None,
        window_end=None,
    )


def _contract_id(venue: PredictionVenue, market_id: str) -> str:
    return f"{venue.value}:{market_id}"


def _market_id_from_contract_id(contract_id: str) -> str:
    _, _, market_id = contract_id.partition(":")
    return market_id


def _build_candidate(
    legs: tuple[CandidateLeg, ...],
    trial_family_id: str,
    as_of: datetime,
    evaluation: SemanticEvaluation,
    model_card: ModelCard,
) -> CandidateRelationship:
    provenance = AIProvenance(
        kind="ai",
        model_id=model_card.model_id,
        model_version=model_card.version,
        feature_version=model_card.feature_version,
        prompt_version=model_card.prompt_version,
        evaluation_request_hash=evaluation.request_hash,
        gate_status="PASS",
    )
    return CandidateRelationship(
        schema_version=1,
        candidate_id=deterministic_candidate_id(RelationshipType.CROSS_VENUE_EQUIVALENCE, legs),
        trial_family_id=trial_family_id,
        relationship_type=RelationshipType.CROSS_VENUE_EQUIVALENCE,
        legs=legs,
        information_cutoff=as_of,
        observed_at=as_of,
        provenance=provenance,
        propositions=(),
        unresolved_fields=_ENGINE_D_UNRESOLVED_FIELDS,
        contradictions=(),
        invalidation_conditions=_INVALIDATION_CONDITIONS,
        review_status="unreviewed",
        disposition=CandidateDisposition.QUARANTINED,
        superseded_by_candidate_id=None,
    )
