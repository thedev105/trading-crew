from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.storage.store import ConflictingRecordError, PredictionMarketStore
from tests.predictions.domain_helpers import (
    NOW,
    fee_rate,
    market_record,
    prediction_book_snapshot,
    rule_version,
    trade_record,
)
from tests.predictions.manifest_helpers import venue_manifest
from tests.predictions.store_helpers import raw_envelope


def test_current_schema_contains_prediction_core_tables(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    tables = {row[0] for row in store._connection.execute("SHOW TABLES").fetchall()}
    store.close()
    assert {
        "prediction_raw_envelopes",
        "venue_manifests",
        "markets",
        "rule_versions",
        "trades",
        "prediction_books",
        "prediction_fee_rates",
        "schema_migrations",
    } <= tables
    perpetual_futures_tables = {
        "raw_envelopes",
        "instrument_specs",
        "funding_observations",
        "market_snapshots",
        "book_snapshots",
    }
    assert not (perpetual_futures_tables & tables)


def test_raw_envelope_round_trip_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    envelope = raw_envelope()

    assert store.append_raw(envelope) is True
    assert store.append_raw(envelope) is False
    with pytest.raises(ConflictingRecordError):
        store.append_raw(envelope.model_copy(update={"payload_json": "different"}))


def test_read_only_open_requires_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "predictions.duckdb"
    PredictionMarketStore(path).close()

    store = PredictionMarketStore(path, read_only=True)
    store.close()


def test_read_only_open_rejects_a_stale_schema(tmp_path: Path) -> None:
    path = tmp_path / "predictions.duckdb"
    PredictionMarketStore(path).close()

    import duckdb

    connection = duckdb.connect(str(path))
    connection.execute("DROP TABLE schema_migrations")
    connection.close()

    with pytest.raises(RuntimeError, match="current schema"):
        PredictionMarketStore(path, read_only=True)


def test_venue_manifest_round_trip_and_conflict(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    manifest = venue_manifest()

    assert store.append_venue_manifest(manifest) is True
    assert store.append_venue_manifest(manifest) is False
    with pytest.raises(ConflictingRecordError):
        store.append_venue_manifest(
            manifest.model_copy(
                update={"implementation_state": AdapterImplementationState.WATCHLIST}
            )
        )


def test_latest_venue_manifest_as_of_excludes_a_future_review(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    early = venue_manifest(reviewed_at=NOW - timedelta(hours=1))
    late = venue_manifest(reviewed_at=NOW + timedelta(hours=1))
    store.append_venue_manifest(early)
    store.append_venue_manifest(late)

    assert store.latest_venue_manifest_as_of(PredictionVenue.POLYMARKET, NOW) == early
    assert (
        store.latest_venue_manifest_as_of(PredictionVenue.POLYMARKET, NOW + timedelta(hours=2))
        == late
    )
    assert store.latest_venue_manifest_as_of(PredictionVenue.KALSHI, NOW) is None


def test_markets_as_of_never_leaks_a_later_rule_version(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    first_rule = UUID("00000000-0000-0000-0000-000000000f01")
    second_rule = UUID("00000000-0000-0000-0000-000000000f02")
    first_market = market_record(rule_version_id=first_rule, retrieved_at=NOW - timedelta(hours=1))
    second_market = market_record(rule_version_id=second_rule, retrieved_at=NOW)
    store.append_market(first_market)
    store.append_market(second_market)

    early = store.markets_as_of(PredictionVenue.POLYMARKET, NOW - timedelta(minutes=30))
    late = store.markets_as_of(PredictionVenue.POLYMARKET, NOW)

    assert len(early) == 1 and early[0].rule_version_id == first_rule
    assert len(late) == 1 and late[0].rule_version_id == second_rule


def test_market_conflict_and_idempotent_retry(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    market = market_record()

    assert store.append_market(market) is True
    assert store.append_market(market) is False
    with pytest.raises(ConflictingRecordError):
        store.append_market(market.model_copy(update={"question": "different question?"}))


def test_rule_history_is_ordered_and_cutoff_safe(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    first = rule_version(
        rule_version_id=UUID("00000000-0000-0000-0000-000000001001"),
        effective_at=NOW - timedelta(hours=2),
    )
    second = rule_version(
        rule_version_id=UUID("00000000-0000-0000-0000-000000001002"),
        effective_at=NOW - timedelta(hours=1),
        superseded_rule_version_id=first.rule_version_id,
    )
    store.append_rule_version(first)
    store.append_rule_version(second)

    history = store.rule_versions_for_market(first.market_id, NOW)
    assert [item.rule_version_id for item in history] == [
        first.rule_version_id,
        second.rule_version_id,
    ]
    partial_cutoff = NOW - timedelta(hours=1, minutes=30)
    assert store.rule_versions_for_market(first.market_id, partial_cutoff) == (first,)


def test_trades_between_excludes_future_known_as_of(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    trade = trade_record()
    store.append_trade(trade)

    assert store.trades_between(
        PredictionVenue.POLYMARKET,
        trade.market_id,
        NOW - timedelta(hours=1),
        NOW + timedelta(hours=1),
        NOW,
    ) == (trade,)
    assert (
        store.trades_between(
            PredictionVenue.POLYMARKET,
            trade.market_id,
            NOW - timedelta(hours=1),
            NOW + timedelta(hours=1),
            NOW - timedelta(microseconds=1),
        )
        == ()
    )


def test_latest_book_as_of_rejects_a_future_observation(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    snapshot = prediction_book_snapshot()
    store.append_book_snapshot(snapshot)

    assert (
        store.latest_book_as_of(
            PredictionVenue.POLYMARKET, snapshot.market_id, snapshot.outcome_token_id, NOW
        )
        == snapshot
    )
    assert (
        store.latest_book_as_of(
            PredictionVenue.POLYMARKET,
            snapshot.market_id,
            snapshot.outcome_token_id,
            NOW - timedelta(microseconds=1),
        )
        is None
    )


def test_latest_book_observed_at_for_venue_ignores_future_and_other_venues(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    older = prediction_book_snapshot(cycle_id=UUID(int=1), observed_at=NOW - timedelta(hours=1))
    newer = prediction_book_snapshot(cycle_id=UUID(int=2), observed_at=NOW)
    store.append_book_snapshot(older)
    store.append_book_snapshot(newer)

    assert store.latest_book_observed_at_for_venue(PredictionVenue.POLYMARKET, NOW) == NOW
    assert store.latest_book_observed_at_for_venue(
        PredictionVenue.POLYMARKET, NOW - timedelta(hours=1)
    ) == NOW - timedelta(hours=1)
    assert store.latest_book_observed_at_for_venue(PredictionVenue.KALSHI, NOW) is None


def test_latest_fee_rate_as_of_handles_a_venue_wide_null_market_id(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    rate = fee_rate(market_id=None)
    store.append_fee_rate(rate)

    assert store.latest_fee_rate_as_of(PredictionVenue.POLYMARKET, None, NOW) == rate
    assert store.latest_fee_rate_as_of(PredictionVenue.POLYMARKET, "some-market", NOW) is None


def test_evidence_counts_as_of_sums_every_table(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_raw(raw_envelope())
    store.append_venue_manifest(venue_manifest())
    store.append_market(market_record())
    store.append_rule_version(rule_version())
    store.append_trade(trade_record())
    store.append_book_snapshot(prediction_book_snapshot())
    store.append_fee_rate(fee_rate())

    counts = store.evidence_counts_as_of(NOW)
    assert counts == {
        "prediction_raw_envelopes": 1,
        "venue_manifests": 1,
        "markets": 1,
        "rule_versions": 1,
        "trades": 1,
        "prediction_books": 1,
        "prediction_fee_rates": 1,
    }
