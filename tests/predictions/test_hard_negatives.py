"""Gold hard-negative pairs: title-similar, rule-divergent market pairs (spec section 15.2).

Loads ``tests/fixtures/predictions/hard_negatives.json`` -- a checked-in set of at least six
pairs whose titles are similar enough that TF-IDF retrieval would plausibly nominate them, but
whose rule text diverges on exactly one critical dimension (threshold inclusivity, deadline
timezone, resolution source, observation window, same-underlying-exchange frontend, or
subset/superset scope). This fixture is also reused by increment 3's equivalence-compiler
mutation tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from polytrading.ai.evaluate import SemanticEvaluation
from polytrading.predictions.candidates import (
    propose_binary_complements,
    propose_venue_native_outcome_sets,
)
from polytrading.predictions.candidates_models import (
    CandidateDisposition,
    CandidateRelationship,
    RelationshipType,
)
from polytrading.predictions.domain import MarketRecord, PredictionVenue, RuleVersion
from polytrading.predictions.registry import PredictionRegistry
from polytrading.predictions.scout_bridge import ScoutAbstention, nominate_cross_venue_candidates
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.domain_helpers import NOW, market_record, rule_version

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "predictions" / "hard_negatives.json"
)

_REQUIRED_DIVERGENT_FIELDS = {
    "threshold_inclusivity",
    "deadline_timezone",
    "resolution_source",
    "observation_window",
    "underlying_exchange",
    "scope_subset_superset",
}

_ENGINE_D_UNRESOLVED_FIELDS = {
    "proposition_threshold_inclusivity",
    "observation_period_timezone",
    "resolution_sources",
    "void_dispute_behavior",
    "outcome_completeness",
    "denomination_collateral_rounding",
    "settlement_finality_timing",
    "venue_access_custody_rules",
}


def _load_pairs() -> list[dict[str, Any]]:
    payload = json.loads(FIXTURE_PATH.read_text())
    return payload["pairs"]


def _rule_version_id(market_id: str):
    # Deterministic per market_id so repeated builds of the same fixture entry are stable;
    # the fixture itself stores no UUIDs since it's meant to be venue-agnostic gold data.
    return uuid5(NAMESPACE_URL, f"hard-negative:{market_id}")


def _build(entry: dict[str, Any]) -> tuple[MarketRecord, RuleVersion]:
    venue = PredictionVenue(entry["venue"])
    rule_version_id = _rule_version_id(entry["market_id"])
    market = market_record(
        market_id=entry["market_id"],
        venue=venue,
        question=entry["question"],
        underlying_exchange=entry.get("underlying_exchange"),
        event_id=entry["event_id"],
        resolution_source=entry["resolution_source"],
        outcomes=tuple(entry["outcomes"]),
        negative_risk=entry.get("negative_risk"),
        rule_version_id=rule_version_id,
    )
    rule = rule_version(
        rule_version_id=rule_version_id,
        market_id=entry["market_id"],
        venue=venue,
        question=entry["question"],
        description=entry["rule_text"],
        resolution_source=entry["resolution_source"],
        outcomes=tuple(entry["outcomes"]),
    )
    return market, rule


def _seed(store: PredictionMarketStore, market: MarketRecord, rule: RuleVersion) -> None:
    store.append_market(market)
    store.append_rule_version(rule)


def _evaluation(**overrides: Any) -> SemanticEvaluation:
    values: dict[str, Any] = {
        "request_hash": "c" * 64,
        "split": "test",
        "metrics": (),
        "gate_status": "PASS",
        "failure_examples": (),
        "malformed_case_results": (),
        "hostile_case_results": (),
        "mutation_case_results": (),
        "payoff_compiler_results": None,
    }
    values.update(overrides)
    return SemanticEvaluation(**values)


def test_fixture_has_at_least_six_axes_with_required_divergences() -> None:
    pairs = _load_pairs()
    assert len(pairs) >= 6

    divergent_fields = {pair["divergent_field"] for pair in pairs}
    assert divergent_fields >= _REQUIRED_DIVERGENT_FIELDS

    for pair in pairs:
        market_a, rule_a = _build(pair["market_a"])
        market_b, rule_b = _build(pair["market_b"])
        # Titles must be similar enough to be plausible TF-IDF nominations, and the two
        # members must be genuinely separate events (never the same market_id/event_id).
        assert market_a.market_id != market_b.market_id
        assert market_a.event_id != market_b.event_id
        assert rule_a.description != rule_b.description


def test_deterministic_generators_never_pair_across_markets(tmp_path: Path) -> None:
    """Complements only ever have two legs of ONE market; outcome sets only group by exact
    ``event_id``. Feed every hard-negative pair in as two separate events and confirm no
    deterministic generator output relates a pair's two members to each other.
    """
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    pairs = _load_pairs()

    venues_present: set[PredictionVenue] = set()
    pair_market_ids: list[tuple[str, str]] = []
    for pair in pairs:
        market_a, rule_a = _build(pair["market_a"])
        market_b, rule_b = _build(pair["market_b"])
        _seed(store, market_a, rule_a)
        _seed(store, market_b, rule_b)
        venues_present.add(market_a.venue)
        venues_present.add(market_b.venue)
        pair_market_ids.append((market_a.market_id, market_b.market_id))

    registry = PredictionRegistry(store)

    complements: list[CandidateRelationship] = []
    outcome_sets: list[CandidateRelationship] = []
    for venue in venues_present:
        complements.extend(
            propose_binary_complements(
                registry, venue, NOW, trial_family_id="hard-neg", code_revision="test"
            )
        )
        outcome_sets.extend(
            propose_venue_native_outcome_sets(
                registry, venue, NOW, trial_family_id="hard-neg", code_revision="test"
            )
        )

    # Every hard-negative market has a unique event_id, so no exhaustive-outcome-set group
    # ever reaches the required two members -- the generator must emit nothing.
    assert outcome_sets == []

    # A binary complement candidate's two legs always come from the SAME market (its own
    # two outcomes); it structurally cannot span two different markets.
    for candidate in complements:
        leg_market_ids = {leg.market_id for leg in candidate.legs}
        assert len(leg_market_ids) == 1

    # Belt-and-suspenders: no candidate of any kind relates a pair's two members.
    all_candidates = complements + outcome_sets
    for candidate in all_candidates:
        leg_market_ids = {leg.market_id for leg in candidate.legs}
        for market_id_a, market_id_b in pair_market_ids:
            assert not ({market_id_a, market_id_b} <= leg_market_ids)


def test_scout_nominated_hard_negatives_remain_fully_unresolved_and_quarantined(
    tmp_path: Path,
) -> None:
    """Index the hard-negative pairs, run the bridge with a passing evaluation, and confirm
    every nomination is QUARANTINED with all 8 equivalence dimensions unresolved -- title
    similarity produces a nomination but never reduces the unresolved surface.
    """
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    pairs = _load_pairs()

    venue_pairs: set[tuple[PredictionVenue, PredictionVenue]] = set()
    for pair in pairs:
        market_a, rule_a = _build(pair["market_a"])
        market_b, rule_b = _build(pair["market_b"])
        _seed(store, market_a, rule_a)
        _seed(store, market_b, rule_b)
        venue_pairs.add((market_a.venue, market_b.venue))

    registry = PredictionRegistry(store)

    all_candidates: list[CandidateRelationship] = []
    for venue_a, venue_b in venue_pairs:
        result = nominate_cross_venue_candidates(
            registry,
            _evaluation(),
            venue_a,
            venue_b,
            NOW,
            trial_family_id="hard-neg",
        )
        assert not isinstance(result, ScoutAbstention), (
            f"expected nominations for {venue_a}/{venue_b}, got abstention: {result}"
        )
        all_candidates.extend(result)

    assert len(all_candidates) >= len(pairs)
    for candidate in all_candidates:
        assert candidate.relationship_type is RelationshipType.CROSS_VENUE_EQUIVALENCE
        assert candidate.disposition is CandidateDisposition.QUARANTINED
        assert candidate.provenance.kind == "ai"
        assert candidate.provenance.gate_status == "PASS"
        assert len(candidate.unresolved_fields) == 8
        assert set(candidate.unresolved_fields) == _ENGINE_D_UNRESOLVED_FIELDS


def test_same_underlying_exchange_frontend_pair_is_flagged_in_fixture() -> None:
    """The frontend-pair fixture entry parses and both members declare the same underlying
    exchange, so increment 3's equivalence compiler can reject it as non-independent.
    """
    pairs = _load_pairs()
    matches = [pair for pair in pairs if pair["divergent_field"] == "underlying_exchange"]
    assert len(matches) == 1
    pair = matches[0]

    market_a, _ = _build(pair["market_a"])
    market_b, _ = _build(pair["market_b"])

    assert market_a.underlying_exchange is not None
    assert market_b.underlying_exchange is not None
    assert market_a.underlying_exchange == market_b.underlying_exchange
    # And this really is a hard negative, not a duplicate: still two distinct markets/events.
    assert market_a.market_id != market_b.market_id
    assert market_a.event_id != market_b.event_id
