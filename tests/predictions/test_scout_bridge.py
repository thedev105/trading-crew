from pathlib import Path
from typing import Any
from uuid import UUID

from polytrading.ai.evaluate import SemanticEvaluation
from polytrading.predictions.candidates_models import CandidateDisposition, RelationshipType
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.registry import PredictionRegistry
from polytrading.predictions.scout_bridge import ScoutAbstention, nominate_cross_venue_candidates
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.domain_helpers import NOW, market_record, rule_version

AS_OF = NOW
RULE_A = UUID("00000000-0000-0000-0000-000000006001")
RULE_B = UUID("00000000-0000-0000-0000-000000006002")


def _store(tmp_path: Path) -> PredictionMarketStore:
    return PredictionMarketStore(tmp_path / "predictions.duckdb")


def _seed(store: PredictionMarketStore, market, rule) -> None:
    store.append_market(market)
    store.append_rule_version(rule)


def evaluation(**overrides: Any) -> SemanticEvaluation:
    values: dict[str, Any] = {
        "request_hash": "b" * 64,
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


def test_same_venue_request_abstains_typed_and_never_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        evaluation(),
        PredictionVenue.POLYMARKET,
        PredictionVenue.POLYMARKET,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert isinstance(result, ScoutAbstention)
    assert result.reason == "SAME_VENUE"
    assert result.evaluation_request_hash == "b" * 64
    assert result.as_of == AS_OF


def test_same_venue_request_abstains_even_with_no_evaluation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        None,
        PredictionVenue.KALSHI,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert isinstance(result, ScoutAbstention)
    assert result.reason == "SAME_VENUE"
    assert result.evaluation_request_hash is None


def test_missing_evaluation_abstains_typed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        None,
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert isinstance(result, ScoutAbstention)
    assert result.reason == "NO_EVALUATION_SUPPLIED"
    assert result.evaluation_request_hash is None
    assert result.as_of == AS_OF


def test_failed_gate_abstains_typed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        evaluation(gate_status="FAIL"),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert isinstance(result, ScoutAbstention)
    assert result.reason == "SCOUT_GATE_UNMET"
    assert result.evaluation_request_hash == "b" * 64


def test_not_evaluated_gate_status_also_abstains(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        evaluation(gate_status="NOT_EVALUATED"),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert isinstance(result, ScoutAbstention)
    assert result.reason == "SCOUT_GATE_UNMET"


def test_no_eligible_markets_on_either_venue_abstains_typed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Only venue A has an eligible market; venue B has none at all.
    market = market_record(rule_version_id=RULE_A)
    _seed(store, market, rule_version(rule_version_id=RULE_A))
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        evaluation(),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert isinstance(result, ScoutAbstention)
    assert result.reason == "NO_ELIGIBLE_MARKETS"
    assert result.evaluation_request_hash == "b" * 64


def _seed_cross_venue_pair(store: PredictionMarketStore) -> None:
    market_a = market_record(
        market_id="poly-btc-100k",
        venue=PredictionVenue.POLYMARKET,
        question="Will BTC close above $100k by end of year?",
        rule_version_id=RULE_A,
    )
    market_b = market_record(
        market_id="kalshi-btc-100k",
        venue=PredictionVenue.KALSHI,
        question="Will BTC close above $100k by end of year?",
        negative_risk=None,
        rule_version_id=RULE_B,
    )
    _seed(
        store,
        market_a,
        rule_version(
            rule_version_id=RULE_A,
            market_id=market_a.market_id,
            venue=PredictionVenue.POLYMARKET,
        ),
    )
    _seed(
        store,
        market_b,
        rule_version(
            rule_version_id=RULE_B, market_id=market_b.market_id, venue=PredictionVenue.KALSHI
        ),
    )


def test_passing_gate_nominates_quarantined_candidates_with_all_equivalence_dimensions(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed_cross_venue_pair(store)
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        evaluation(),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert not isinstance(result, ScoutAbstention)
    assert len(result) >= 1
    for candidate in result:
        assert candidate.relationship_type is RelationshipType.CROSS_VENUE_EQUIVALENCE
        assert candidate.disposition is CandidateDisposition.QUARANTINED
        assert candidate.provenance.kind == "ai"
        assert candidate.provenance.gate_status == "PASS"
        assert candidate.provenance.evaluation_request_hash == "b" * 64
        assert len(candidate.unresolved_fields) == 8
        assert candidate.unresolved_fields == (
            "proposition_threshold_inclusivity",
            "observation_period_timezone",
            "resolution_sources",
            "void_dispute_behavior",
            "outcome_completeness",
            "denomination_collateral_rounding",
            "settlement_finality_timing",
            "venue_access_custody_rules",
        )
        assert "any participating rule_version change" in candidate.invalidation_conditions
        assert candidate.review_status == "unreviewed"


def test_similar_titles_across_venues_are_retrieved_and_legs_span_both_venues(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed_cross_venue_pair(store)
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        evaluation(),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert not isinstance(result, ScoutAbstention)
    (candidate,) = result
    venues = {leg.venue for leg in candidate.legs}
    assert venues == {PredictionVenue.POLYMARKET, PredictionVenue.KALSHI}
    market_ids = {leg.market_id for leg in candidate.legs}
    assert market_ids == {"poly-btc-100k", "kalshi-btc-100k"}


def test_dissimilar_titles_still_nominate_within_top_k(tmp_path: Path) -> None:
    store = _store(tmp_path)
    market_a = market_record(
        market_id="poly-btc-100k",
        venue=PredictionVenue.POLYMARKET,
        question="Will BTC close above $100k by end of year?",
        rule_version_id=RULE_A,
    )
    market_b = market_record(
        market_id="kalshi-super-bowl",
        venue=PredictionVenue.KALSHI,
        question="Who will win the Super Bowl this season?",
        negative_risk=None,
        rule_version_id=RULE_B,
    )
    _seed(
        store,
        market_a,
        rule_version(
            rule_version_id=RULE_A,
            market_id=market_a.market_id,
            venue=PredictionVenue.POLYMARKET,
        ),
    )
    _seed(
        store,
        market_b,
        rule_version(
            rule_version_id=RULE_B, market_id=market_b.market_id, venue=PredictionVenue.KALSHI
        ),
    )
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        evaluation(),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
        top_k=1,
    )

    # The retriever ranks by similarity but applies no similarity threshold -- with exactly
    # one eligible market on each side, that sole pair is always the top-1 nomination
    # regardless of how dissimilar the titles are. Precision is a downstream review
    # concern, not something the bridge gates on.
    assert not isinstance(result, ScoutAbstention)
    (candidate,) = result
    assert candidate.disposition is CandidateDisposition.QUARANTINED


def test_cross_venue_candidate_uses_each_markets_own_current_rule_version(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _seed_cross_venue_pair(store)
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        evaluation(),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert not isinstance(result, ScoutAbstention)
    (candidate,) = result
    for leg in candidate.legs:
        if leg.venue is PredictionVenue.POLYMARKET:
            assert leg.rule_version_id == RULE_A
        else:
            assert leg.rule_version_id == RULE_B


def test_market_with_unresolvable_rule_version_is_excluded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    market_a = market_record(
        market_id="poly-btc-100k",
        venue=PredictionVenue.POLYMARKET,
        question="Will BTC close above $100k by end of year?",
        rule_version_id=RULE_A,
    )
    market_b = market_record(
        market_id="kalshi-btc-100k",
        venue=PredictionVenue.KALSHI,
        question="Will BTC close above $100k by end of year?",
        negative_risk=None,
        rule_version_id=RULE_B,
    )
    store.append_market(market_a)  # no rule version appended -- unresolvable
    _seed(
        store,
        market_b,
        rule_version(
            rule_version_id=RULE_B, market_id=market_b.market_id, venue=PredictionVenue.KALSHI
        ),
    )
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        evaluation(),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert isinstance(result, ScoutAbstention)
    assert result.reason == "NO_ELIGIBLE_MARKETS"


def test_closed_market_is_not_eligible(tmp_path: Path) -> None:
    store = _store(tmp_path)
    market_a = market_record(
        market_id="poly-btc-100k",
        venue=PredictionVenue.POLYMARKET,
        question="Will BTC close above $100k by end of year?",
        closed=True,
        rule_version_id=RULE_A,
    )
    market_b = market_record(
        market_id="kalshi-btc-100k",
        venue=PredictionVenue.KALSHI,
        question="Will BTC close above $100k by end of year?",
        negative_risk=None,
        rule_version_id=RULE_B,
    )
    _seed(
        store,
        market_a,
        rule_version(
            rule_version_id=RULE_A,
            market_id=market_a.market_id,
            venue=PredictionVenue.POLYMARKET,
        ),
    )
    _seed(
        store,
        market_b,
        rule_version(
            rule_version_id=RULE_B, market_id=market_b.market_id, venue=PredictionVenue.KALSHI
        ),
    )
    registry = PredictionRegistry(store)

    result = nominate_cross_venue_candidates(
        registry,
        evaluation(),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert isinstance(result, ScoutAbstention)
    assert result.reason == "NO_ELIGIBLE_MARKETS"


def test_regeneration_is_identity_stable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_cross_venue_pair(store)
    registry = PredictionRegistry(store)

    a = nominate_cross_venue_candidates(
        registry,
        evaluation(),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )
    b = nominate_cross_venue_candidates(
        registry,
        evaluation(),
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        AS_OF,
        trial_family_id="tf-1",
    )

    assert not isinstance(a, ScoutAbstention)
    assert not isinstance(b, ScoutAbstention)
    assert [c.candidate_id for c in a] == [c.candidate_id for c in b]
