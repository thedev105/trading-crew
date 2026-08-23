from pathlib import Path
from uuid import UUID

from polytrading.predictions.candidates import (
    propose_binary_complements,
    propose_venue_native_outcome_sets,
)
from polytrading.predictions.candidates_models import CandidateDisposition, RelationshipType
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.registry import PredictionRegistry
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.domain_helpers import NOW, market_record, rule_version

AS_OF = NOW
RULE_A = UUID("00000000-0000-0000-0000-000000005001")
RULE_B = UUID("00000000-0000-0000-0000-000000005002")


def _store(tmp_path: Path) -> PredictionMarketStore:
    return PredictionMarketStore(tmp_path / "predictions.duckdb")


def _seed(store: PredictionMarketStore, market, rule) -> None:
    store.append_market(market)
    store.append_rule_version(rule)


def test_two_outcome_open_market_yields_quarantined_complement_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    market = market_record(rule_version_id=RULE_A)
    rule = rule_version(rule_version_id=RULE_A)
    _seed(store, market, rule)
    registry = PredictionRegistry(store)

    candidates = propose_binary_complements(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    (candidate,) = candidates
    assert candidate.relationship_type is RelationshipType.BINARY_COMPLEMENT
    assert candidate.disposition is CandidateDisposition.QUARANTINED
    assert candidate.review_status == "unreviewed"
    assert candidate.unresolved_fields == ("terminal_partition_unproven",)
    assert candidate.provenance.kind == "deterministic"
    assert candidate.provenance.generator == "binary_complement"
    assert candidate.observed_at == AS_OF
    assert len(candidate.legs) == 2
    assert {leg.outcome_index for leg in candidate.legs} == {0, 1}
    for leg in candidate.legs:
        assert leg.rule_version_id == RULE_A
        assert leg.rule_source_hash == rule.source_hash
    assert len(candidate.propositions) == 2
    for proposition in candidate.propositions:
        assert proposition.kind == "outcome_membership"
        assert proposition.status == "unknown"
        assert proposition.value is None
        assert proposition.supporting_spans == ()


def test_closed_or_bookless_or_three_outcome_markets_are_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    closed_market = market_record(market_id="closed-market", closed=True, rule_version_id=RULE_A)
    bookless_market = market_record(
        market_id="bookless-market", order_book_enabled=False, rule_version_id=RULE_A
    )
    three_outcome_market = market_record(
        market_id="three-outcome-market",
        outcomes=("A", "B", "C"),
        outcome_token_ids=("1", "2", "3"),
        rule_version_id=RULE_A,
    )
    rule = rule_version(rule_version_id=RULE_A)
    for market in (closed_market, bookless_market, three_outcome_market):
        _seed(store, market, rule)
    registry = PredictionRegistry(store)

    candidates = propose_binary_complements(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    assert candidates == ()


def test_market_with_unresolvable_rule_version_is_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Market references a rule version that never got appended to the registry.
    market = market_record(rule_version_id=RULE_A)
    store.append_market(market)
    registry = PredictionRegistry(store)

    candidates = propose_binary_complements(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    assert candidates == ()


def test_polymarket_group_without_negative_risk_is_not_an_outcome_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    market_1 = market_record(
        market_id="event-market-1",
        event_id="event-1",
        negative_risk=False,
        rule_version_id=RULE_A,
    )
    market_2 = market_record(
        market_id="event-market-2",
        event_id="event-1",
        negative_risk=False,
        rule_version_id=RULE_B,
    )
    for market, rule_id in ((market_1, RULE_A), (market_2, RULE_B)):
        _seed(store, market, rule_version(rule_version_id=rule_id, market_id=market.market_id))
    registry = PredictionRegistry(store)

    candidates = propose_venue_native_outcome_sets(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    assert candidates == ()


def test_polymarket_group_with_negative_risk_yields_outcome_set_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    market_1 = market_record(
        market_id="event-market-1",
        event_id="event-1",
        negative_risk=True,
        rule_version_id=RULE_A,
    )
    market_2 = market_record(
        market_id="event-market-2",
        event_id="event-1",
        negative_risk=True,
        rule_version_id=RULE_B,
    )
    for market, rule_id in ((market_1, RULE_A), (market_2, RULE_B)):
        _seed(store, market, rule_version(rule_version_id=rule_id, market_id=market.market_id))
    registry = PredictionRegistry(store)

    candidates = propose_venue_native_outcome_sets(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    (candidate,) = candidates
    assert candidate.relationship_type is RelationshipType.EXHAUSTIVE_OUTCOME_SET
    assert candidate.unresolved_fields == ("outcome_set_exhaustiveness_unproven",)
    assert len(candidate.legs) == 2
    assert {leg.market_id for leg in candidate.legs} == {"event-market-1", "event-market-2"}
    assert all(leg.outcome_index is None for leg in candidate.legs)


def test_kalshi_groups_by_event_id_without_consulting_negative_risk(tmp_path: Path) -> None:
    store = _store(tmp_path)
    market_1 = market_record(
        market_id="kalshi-market-1",
        venue=PredictionVenue.KALSHI,
        event_id="kalshi-event-1",
        negative_risk=None,
        rule_version_id=RULE_A,
    )
    market_2 = market_record(
        market_id="kalshi-market-2",
        venue=PredictionVenue.KALSHI,
        event_id="kalshi-event-1",
        negative_risk=None,
        rule_version_id=RULE_B,
    )
    for market, rule_id in ((market_1, RULE_A), (market_2, RULE_B)):
        _seed(
            store,
            market,
            rule_version(
                rule_version_id=rule_id, market_id=market.market_id, venue=PredictionVenue.KALSHI
            ),
        )
    registry = PredictionRegistry(store)

    candidates = propose_venue_native_outcome_sets(
        registry, PredictionVenue.KALSHI, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    (candidate,) = candidates
    assert candidate.relationship_type is RelationshipType.EXHAUSTIVE_OUTCOME_SET
    assert len(candidate.legs) == 2


def test_single_market_event_groups_are_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    market = market_record(
        market_id="lonely-market",
        event_id="event-lonely",
        negative_risk=True,
        rule_version_id=RULE_A,
    )
    _seed(store, market, rule_version(rule_version_id=RULE_A, market_id=market.market_id))
    registry = PredictionRegistry(store)

    candidates = propose_venue_native_outcome_sets(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    assert candidates == ()


def test_market_without_event_id_is_never_grouped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    market_1 = market_record(
        market_id="no-event-1", event_id=None, negative_risk=True, rule_version_id=RULE_A
    )
    market_2 = market_record(
        market_id="no-event-2", event_id=None, negative_risk=True, rule_version_id=RULE_B
    )
    for market, rule_id in ((market_1, RULE_A), (market_2, RULE_B)):
        _seed(store, market, rule_version(rule_version_id=rule_id, market_id=market.market_id))
    registry = PredictionRegistry(store)

    candidates = propose_venue_native_outcome_sets(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    assert candidates == ()


def test_regeneration_is_identity_stable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    market = market_record(rule_version_id=RULE_A)
    _seed(store, market, rule_version(rule_version_id=RULE_A))
    registry = PredictionRegistry(store)

    a = propose_binary_complements(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )
    b = propose_binary_complements(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    assert [c.candidate_id for c in a] == [c.candidate_id for c in b]


def test_outcome_set_regeneration_is_identity_stable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    market_1 = market_record(
        market_id="event-market-1",
        event_id="event-1",
        negative_risk=True,
        rule_version_id=RULE_A,
    )
    market_2 = market_record(
        market_id="event-market-2",
        event_id="event-1",
        negative_risk=True,
        rule_version_id=RULE_B,
    )
    for market, rule_id in ((market_1, RULE_A), (market_2, RULE_B)):
        _seed(store, market, rule_version(rule_version_id=rule_id, market_id=market.market_id))
    registry = PredictionRegistry(store)

    a = propose_venue_native_outcome_sets(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )
    b = propose_venue_native_outcome_sets(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    assert [c.candidate_id for c in a] == [c.candidate_id for c in b]


def test_venue_native_outcome_set_provenance_and_review_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    market_1 = market_record(
        market_id="event-market-1",
        event_id="event-1",
        negative_risk=True,
        rule_version_id=RULE_A,
    )
    market_2 = market_record(
        market_id="event-market-2",
        event_id="event-1",
        negative_risk=True,
        rule_version_id=RULE_B,
    )
    for market, rule_id in ((market_1, RULE_A), (market_2, RULE_B)):
        _seed(store, market, rule_version(rule_version_id=rule_id, market_id=market.market_id))
    registry = PredictionRegistry(store)

    (candidate,) = propose_venue_native_outcome_sets(
        registry, PredictionVenue.POLYMARKET, AS_OF, trial_family_id="tf-1", code_revision="abc"
    )

    assert candidate.disposition is CandidateDisposition.QUARANTINED
    assert candidate.review_status == "unreviewed"
    assert candidate.provenance.kind == "deterministic"
    assert candidate.provenance.generator == "venue_native_outcome_set"
    assert candidate.observed_at == AS_OF
    assert len(candidate.propositions) == 2
