from datetime import timedelta
from decimal import Decimal
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
from tests.predictions.proof_helpers import proof_artifact
from tests.predictions.scan_helpers import scan_report


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


def test_snapshot_proofs_summary_is_empty_when_no_proofs_exist(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert snapshot.proofs.total == 0
    assert snapshot.proofs.by_status == {}
    assert snapshot.proofs.by_template == {}
    assert snapshot.proofs.latest == ()


def test_snapshot_proofs_summary_counts_match_seeded_proofs(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_proof_artifact(
        proof_artifact(
            proof_id=UUID(int=1),
            candidate_id=UUID(int=101),
            observed_at=NOW,
            information_cutoff=NOW,
        )
    )
    store.append_proof_artifact(
        proof_artifact(
            proof_id=UUID(int=2),
            candidate_id=UUID(int=102),
            template="exhaustive_outcome_set@1",
            status="rejected",
            rejection_reason="TEMPLATE_NOT_APPROVED",
            terminal_states=(),
            minimum_basket_payout=None,
            maximum_basket_payout=None,
            observed_at=NOW,
            information_cutoff=NOW,
        )
    )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)

    assert snapshot.proofs.total == 2
    assert snapshot.proofs.by_status == {"proof_ready": 1, "rejected": 1}
    assert snapshot.proofs.by_template == {
        "binary_complement@1": 1,
        "exhaustive_outcome_set@1": 1,
    }

    latest_by_id = {listing.proof_id: listing for listing in snapshot.proofs.latest}
    rejected_listing = latest_by_id[UUID(int=2)]
    assert rejected_listing.candidate_id == UUID(int=102)
    assert rejected_listing.status == "rejected"
    assert rejected_listing.rejection_reason == "TEMPLATE_NOT_APPROVED"
    assert rejected_listing.minimum_basket_payout is None
    assert rejected_listing.observed_at == NOW

    ready_listing = latest_by_id[UUID(int=1)]
    assert ready_listing.status == "proof_ready"
    assert ready_listing.rejection_reason is None
    assert ready_listing.minimum_basket_payout == Decimal("1")


def test_snapshot_omits_a_proof_observed_after_its_own_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_proof_artifact(
        proof_artifact(
            proof_id=UUID(int=1),
            candidate_id=UUID(int=101),
            observed_at=NOW + timedelta(hours=1),
            information_cutoff=NOW + timedelta(hours=1),
        )
    )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert snapshot.proofs.total == 0
    assert snapshot.proofs.latest == ()


def test_snapshot_proofs_latest_is_newest_first_and_capped_at_twenty(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    for offset in range(25):
        store.append_proof_artifact(
            proof_artifact(
                proof_id=UUID(int=offset + 1),
                candidate_id=UUID(int=200 + offset),
                observed_at=NOW - timedelta(minutes=25 - offset),
                information_cutoff=NOW - timedelta(minutes=25 - offset),
            )
        )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)

    assert snapshot.proofs.total == 25
    assert len(snapshot.proofs.latest) == 20
    observed_ats = [listing.observed_at for listing in snapshot.proofs.latest]
    assert observed_ats == sorted(observed_ats, reverse=True)
    assert snapshot.proofs.latest[0].proof_id == UUID(int=25)


def test_snapshot_scans_summary_is_empty_when_no_scans_exist(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert snapshot.scans.total == 0
    assert snapshot.scans.by_decision == {}
    assert snapshot.scans.latest == ()


def test_snapshot_scans_summary_counts_match_seeded_scans(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_scan_report(
        scan_report(
            candidate_id=UUID(int=301),
            decision="REJECTED",
            reason="economics unfavorable",
            economics=None,
            proof_id=None,
            as_of=NOW,
            observed_at=NOW,
        )
    )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)

    assert snapshot.scans.total == 1
    assert snapshot.scans.by_decision == {"REJECTED": 1}
    listing = snapshot.scans.latest[0]
    assert listing.candidate_id == UUID(int=301)
    assert listing.decision == "REJECTED"
    assert listing.reason == "economics unfavorable"
    assert listing.surplus is None
    assert listing.capacity is None
    assert listing.as_of == NOW


def test_snapshot_omits_a_scan_observed_after_its_own_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_scan_report(
        scan_report(
            candidate_id=UUID(int=301),
            decision="REJECTED",
            reason="economics unfavorable",
            economics=None,
            proof_id=None,
            as_of=NOW + timedelta(hours=1),
            observed_at=NOW + timedelta(hours=1),
        )
    )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert snapshot.scans.total == 0
    assert snapshot.scans.latest == ()


def test_snapshot_scans_latest_is_newest_first_and_capped_at_twenty(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    for offset in range(25):
        store.append_scan_report(
            scan_report(
                candidate_id=UUID(int=400 + offset),
                decision="REJECTED",
                reason="economics unfavorable",
                economics=None,
                proof_id=None,
                as_of=NOW - timedelta(minutes=25 - offset),
                observed_at=NOW - timedelta(minutes=25 - offset),
            )
        )
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)

    assert snapshot.scans.total == 25
    assert len(snapshot.scans.latest) == 20
    as_ofs = [listing.as_of for listing in snapshot.scans.latest]
    assert as_ofs == sorted(as_ofs, reverse=True)
    assert snapshot.scans.latest[0].candidate_id == UUID(int=424)
