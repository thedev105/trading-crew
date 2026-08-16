from datetime import timedelta
from pathlib import Path

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.health import PredictionHealthAuditor, VenueEvidenceStatus
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.domain_helpers import NOW, market_record, prediction_book_snapshot
from tests.predictions.manifest_helpers import venue_manifest


def test_venue_with_no_evidence_is_not_collected(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    report = PredictionHealthAuditor(store).audit(NOW)

    assert len(report.venues) == 2
    assert all(venue.status is VenueEvidenceStatus.NOT_COLLECTED for venue in report.venues)
    assert all(venue.collection_gate.allowed is False for venue in report.venues)


def test_watchlisted_venue_reports_gate_reason_not_a_data_gap(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.KALSHI,
            implementation_state=AdapterImplementationState.WATCHLIST,
        )
    )
    report = PredictionHealthAuditor(store).audit(NOW)

    kalshi = next(v for v in report.venues if v.venue is PredictionVenue.KALSHI)
    assert kalshi.collection_gate.allowed is False
    assert kalshi.status is VenueEvidenceStatus.NOT_COLLECTED
    assert any(code.startswith("COLLECTION_GATE:") for code in kalshi.reason_codes)


def test_current_book_evidence_is_reported_as_current(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    market = market_record()
    store.append_market(market)
    store.append_book_snapshot(
        prediction_book_snapshot(
            market_id=market.market_id,
            outcome_token_id=market.outcome_token_ids[0],
            observed_at=NOW - timedelta(seconds=10),
            effective_at=NOW - timedelta(seconds=10),
        )
    )

    report = PredictionHealthAuditor(store).audit(NOW)
    polymarket = next(v for v in report.venues if v.venue is PredictionVenue.POLYMARKET)
    assert polymarket.status is VenueEvidenceStatus.CURRENT
    assert polymarket.market_count == 1
    assert polymarket.reason_codes == ()


def test_stale_book_evidence_degrades_status(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    market = market_record()
    store.append_market(market)
    store.append_book_snapshot(
        prediction_book_snapshot(
            market_id=market.market_id,
            outcome_token_id=market.outcome_token_ids[0],
            observed_at=NOW - timedelta(minutes=10),
            effective_at=NOW - timedelta(minutes=10),
        )
    )

    report = PredictionHealthAuditor(store).audit(NOW)
    polymarket = next(v for v in report.venues if v.venue is PredictionVenue.POLYMARKET)
    assert polymarket.status is VenueEvidenceStatus.STALE
    assert "BOOK_STALE" in polymarket.reason_codes


def test_very_stale_book_evidence_is_degraded(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    market = market_record()
    store.append_market(market)
    store.append_book_snapshot(
        prediction_book_snapshot(
            market_id=market.market_id,
            outcome_token_id=market.outcome_token_ids[0],
            observed_at=NOW - timedelta(hours=2),
            effective_at=NOW - timedelta(hours=2),
        )
    )

    report = PredictionHealthAuditor(store).audit(NOW)
    polymarket = next(v for v in report.venues if v.venue is PredictionVenue.POLYMARKET)
    assert polymarket.status is VenueEvidenceStatus.DEGRADED


def test_markets_with_no_book_evidence_are_not_collected(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.append_market(market_record())

    report = PredictionHealthAuditor(store).audit(NOW)
    polymarket = next(v for v in report.venues if v.venue is PredictionVenue.POLYMARKET)
    assert polymarket.status is VenueEvidenceStatus.NOT_COLLECTED
    assert polymarket.market_count == 1


def test_report_never_leaks_evidence_observed_after_the_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    market = market_record()
    store.append_market(market)
    store.append_book_snapshot(
        prediction_book_snapshot(
            market_id=market.market_id,
            outcome_token_id=market.outcome_token_ids[0],
            observed_at=NOW + timedelta(hours=1),
            effective_at=NOW + timedelta(hours=1),
        )
    )

    report = PredictionHealthAuditor(store).audit(NOW)
    polymarket = next(v for v in report.venues if v.venue is PredictionVenue.POLYMARKET)
    assert polymarket.latest_book_observed_at is None
    assert polymarket.status is VenueEvidenceStatus.NOT_COLLECTED
