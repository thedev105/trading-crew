from pathlib import Path

from polytrading.predictions.dashboard import PredictionDashboardBuilder
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.domain_helpers import NOW, market_record
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


def test_snapshot_includes_health_for_both_venues(tmp_path: Path) -> None:
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
    }


def test_snapshot_evidence_counts_match_the_store(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_market(market_record(retrieved_at=NOW))
    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert snapshot.evidence_counts.counts["markets"] == 1
