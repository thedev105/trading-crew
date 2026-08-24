from datetime import timedelta
from pathlib import Path
from uuid import UUID

from polytrading.predictions.candidates_models import CandidateDisposition, RelationshipType
from polytrading.predictions.dashboard import PredictionDashboardBuilder
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.candidate_helpers import ai_provenance, candidate_relationship
from tests.predictions.domain_helpers import NOW, market_record, prediction_book_snapshot
from tests.predictions.manifest_helpers import venue_manifest


def test_snapshot_never_shows_a_market_retrieved_after_its_own_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_market(market_record(retrieved_at=NOW))
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert all(market.retrieved_at <= NOW for market in snapshot.markets)


def test_snapshot_recipes_are_copy_only_text(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert all(isinstance(recipe, str) for recipe in snapshot.recipes.recipes)
    assert len(snapshot.recipes.recipes) > 0


def test_snapshot_includes_health_for_all_venues(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert {venue.venue for venue in snapshot.health.venues} == {
        PredictionVenue.POLYMARKET,
        PredictionVenue.KALSHI,
        PredictionVenue.LIMITLESS,
    }


def test_snapshot_evidence_counts_match_the_store(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_market(market_record(retrieved_at=NOW))
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert snapshot.evidence_counts.counts["markets"] == 1


def test_snapshot_includes_the_latest_book_for_each_market_outcome(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_market(market_record(retrieved_at=NOW))
    store.append_book_snapshot(prediction_book_snapshot(outcome_token_id="111"))
    store.append_book_snapshot(prediction_book_snapshot(outcome_token_id="222"))
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert {book.outcome_token_id for book in snapshot.books} == {"111", "222"}
    assert all(book.observed_at <= NOW for book in snapshot.books)


def test_snapshot_omits_a_book_observed_after_its_own_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_market(market_record(retrieved_at=NOW - timedelta(hours=1)))
    store.append_book_snapshot(
        prediction_book_snapshot(outcome_token_id="111", effective_at=NOW, observed_at=NOW)
    )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(
        NOW - timedelta(hours=1)
    )
    assert snapshot.books == ()


def test_snapshot_candidates_summary_is_empty_when_no_candidates_exist(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert snapshot.candidates.total == 0
    assert snapshot.candidates.by_relationship_type == {}
    assert snapshot.candidates.by_disposition == {}
    assert snapshot.candidates.by_provenance_kind == {}
    assert snapshot.candidates.latest == ()


def test_snapshot_candidates_summary_counts_match_seeded_candidates(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_candidate_relationship(
        candidate_relationship(candidate_id=UUID(int=1), observed_at=NOW)
    )
    store.append_candidate_relationship(
        candidate_relationship(
            candidate_id=UUID(int=2),
            observed_at=NOW,
            relationship_type=RelationshipType.EXHAUSTIVE_OUTCOME_SET,
            disposition=CandidateDisposition.REJECTED,
            provenance=ai_provenance(),
            unresolved_fields=("resolution_source",),
        )
    )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)

    assert snapshot.candidates.total == 2
    assert snapshot.candidates.by_relationship_type == {
        "binary_complement": 1,
        "exhaustive_outcome_set": 1,
    }
    assert snapshot.candidates.by_disposition == {"quarantined": 1, "rejected": 1}
    assert snapshot.candidates.by_provenance_kind == {"deterministic": 1, "ai": 1}

    latest_by_id = {listing.candidate_id: listing for listing in snapshot.candidates.latest}
    ai_listing = latest_by_id[UUID(int=2)]
    assert ai_listing.relationship_type == RelationshipType.EXHAUSTIVE_OUTCOME_SET
    assert ai_listing.disposition == CandidateDisposition.REJECTED
    assert ai_listing.provenance_kind == "ai"
    assert ai_listing.unresolved_field_count == 1
    assert ai_listing.venues == (PredictionVenue.POLYMARKET,)
    assert ai_listing.observed_at == NOW


def test_snapshot_omits_a_candidate_observed_after_its_own_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_candidate_relationship(
        candidate_relationship(candidate_id=UUID(int=1), observed_at=NOW + timedelta(hours=1))
    )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert snapshot.candidates.total == 0
    assert snapshot.candidates.latest == ()


def test_snapshot_candidates_latest_is_newest_first_and_capped_at_twenty(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    for offset in range(25):
        store.append_candidate_relationship(
            candidate_relationship(
                candidate_id=UUID(int=offset + 1),
                observed_at=NOW - timedelta(minutes=25 - offset),
            )
        )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)

    assert snapshot.candidates.total == 25
    assert len(snapshot.candidates.latest) == 20
    observed_ats = [listing.observed_at for listing in snapshot.candidates.latest]
    assert observed_ats == sorted(observed_ats, reverse=True)
    assert snapshot.candidates.latest[0].candidate_id == UUID(int=25)
