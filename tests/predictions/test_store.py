from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.economics_models import EconomicsResult
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.storage.store import ConflictingRecordError, PredictionMarketStore
from tests.predictions.attestation_helpers import rule_attestation
from tests.predictions.candidate_helpers import candidate_relationship
from tests.predictions.domain_helpers import (
    NOW,
    fee_rate,
    market_record,
    prediction_book_snapshot,
    rule_version,
    trade_record,
)
from tests.predictions.manifest_helpers import venue_manifest
from tests.predictions.proof_helpers import proof_artifact
from tests.predictions.scan_helpers import scan_report
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
        "candidate_relationships",
        "rule_attestations",
        "proof_artifacts",
        "scan_reports",
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


def test_candidate_relationship_round_trip_is_idempotent_and_conflict_safe(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    candidate = candidate_relationship()

    assert store.append_candidate_relationship(candidate) is True
    assert store.append_candidate_relationship(candidate) is False
    with pytest.raises(ConflictingRecordError):
        store.append_candidate_relationship(
            candidate.model_copy(update={"trial_family_id": "different-family"})
        )


def test_candidate_relationships_as_of_respects_the_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    early = candidate_relationship(
        candidate_id=UUID("00000000-0000-0000-0000-000000004001"),
        observed_at=NOW - timedelta(hours=1),
        information_cutoff=NOW - timedelta(hours=1),
    )
    late = candidate_relationship(
        candidate_id=UUID("00000000-0000-0000-0000-000000004002"),
        observed_at=NOW,
        information_cutoff=NOW,
    )
    store.append_candidate_relationship(early)
    store.append_candidate_relationship(late)

    assert store.candidate_relationships_as_of(NOW - timedelta(minutes=30)) == (early,)
    assert store.candidate_relationships_as_of(NOW) == (early, late)
    assert store.candidate_relationships_as_of(NOW - timedelta(hours=2)) == ()


def test_rule_version_by_id_returns_none_for_an_unknown_id(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    store.append_rule_version(rule_version())

    assert store.rule_version_by_id(rule_version().rule_version_id) == rule_version()
    assert store.rule_version_by_id(UUID("00000000-0000-0000-0000-000000009999")) is None


def test_rule_attestation_round_trip_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    attestation = rule_attestation()

    assert store.append_rule_attestation(attestation) is True
    assert store.append_rule_attestation(attestation) is False
    with pytest.raises(ConflictingRecordError):
        store.append_rule_attestation(
            attestation.model_copy(update={"review_identity": "someone-else@example.test"})
        )


def test_latest_attestation_for_rule_version_respects_the_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    early = rule_attestation(
        attestation_id=UUID("00000000-0000-0000-0000-000000005201"),
        reviewed_at=NOW - timedelta(hours=1),
    )
    late = rule_attestation(
        attestation_id=UUID("00000000-0000-0000-0000-000000005202"),
        reviewed_at=NOW,
    )
    store.append_rule_attestation(early)
    store.append_rule_attestation(late)

    assert (
        store.latest_attestation_for_rule_version(
            early.rule_version_id, NOW - timedelta(minutes=30)
        )
        == early
    )
    assert store.latest_attestation_for_rule_version(early.rule_version_id, NOW) == late
    assert (
        store.latest_attestation_for_rule_version(early.rule_version_id, NOW - timedelta(hours=2))
        is None
    )


def test_proof_artifact_round_trip_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    proof = proof_artifact()

    assert store.append_proof_artifact(proof) is True
    assert store.append_proof_artifact(proof) is False
    with pytest.raises(ConflictingRecordError):
        store.append_proof_artifact(proof.model_copy(update={"compiler_version": "different"}))


def test_proof_artifacts_for_candidate_is_ordered_and_cutoff_safe(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    early = proof_artifact(
        proof_id=UUID("00000000-0000-0000-0000-000000006101"),
        observed_at=NOW - timedelta(hours=1),
        information_cutoff=NOW - timedelta(hours=1),
    )
    late = proof_artifact(
        proof_id=UUID("00000000-0000-0000-0000-000000006102"),
        observed_at=NOW,
        information_cutoff=NOW,
    )
    store.append_proof_artifact(early)
    store.append_proof_artifact(late)

    assert store.proof_artifacts_for_candidate(early.candidate_id, NOW - timedelta(minutes=30)) == (
        early,
    )
    assert store.proof_artifacts_for_candidate(early.candidate_id, NOW) == (early, late)
    assert store.proof_artifacts_for_candidate(early.candidate_id, NOW - timedelta(hours=2)) == ()


def test_proof_artifacts_for_candidate_excludes_other_candidates(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    mine = proof_artifact(proof_id=UUID("00000000-0000-0000-0000-000000006201"))
    other = proof_artifact(
        proof_id=UUID("00000000-0000-0000-0000-000000006202"),
        candidate_id=UUID("00000000-0000-0000-0000-000000003999"),
    )
    store.append_proof_artifact(mine)
    store.append_proof_artifact(other)

    assert store.proof_artifacts_for_candidate(mine.candidate_id, NOW) == (mine,)


def test_latest_proof_for_candidate_returns_the_most_recently_observed(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    early = proof_artifact(
        proof_id=UUID("00000000-0000-0000-0000-000000006301"),
        observed_at=NOW - timedelta(hours=1),
        information_cutoff=NOW - timedelta(hours=1),
    )
    late = proof_artifact(
        proof_id=UUID("00000000-0000-0000-0000-000000006302"),
        observed_at=NOW,
        information_cutoff=NOW,
    )
    store.append_proof_artifact(early)
    store.append_proof_artifact(late)

    assert store.latest_proof_for_candidate(early.candidate_id, NOW - timedelta(minutes=30)) == (
        early
    )
    assert store.latest_proof_for_candidate(early.candidate_id, NOW) == late
    assert store.latest_proof_for_candidate(early.candidate_id, NOW - timedelta(hours=2)) is None


def _economics_result(
    *,
    status: str = "evaluated",
    surplus: Decimal = Decimal("10"),
    insufficiency_reason: str | None = None,
) -> EconomicsResult:
    if status == "insufficient_evidence":
        return EconomicsResult(
            status="insufficient_evidence",
            insufficiency_reason=insufficiency_reason or "MISSING_BOOK",
            quantity=Decimal("0"),
            leg_plans=(),
            proven_floor_usd=Decimal("0"),
            all_in_cost_usd=Decimal("0"),
            failure_reserve_usd=Decimal("0"),
            conservative_surplus_usd=Decimal("0"),
            return_on_assigned_capital=Decimal("0"),
            capacity_usd_at_current_depth=Decimal("0"),
            stranded_collateral_by_venue={},
            max_capital_lock_days=Decimal("3"),
            doubled_cost_surplus_usd=Decimal("0"),
        )
    return EconomicsResult(
        status="evaluated",
        insufficiency_reason=None,
        quantity=Decimal("10"),
        leg_plans=(),
        proven_floor_usd=Decimal("10"),
        all_in_cost_usd=Decimal("1"),
        failure_reserve_usd=Decimal("0"),
        conservative_surplus_usd=surplus,
        return_on_assigned_capital=Decimal("0"),
        capacity_usd_at_current_depth=Decimal("5"),
        stranded_collateral_by_venue={},
        max_capital_lock_days=Decimal("3"),
        doubled_cost_surplus_usd=Decimal("0"),
    )


def test_proof_artifacts_as_of_respects_the_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    early = proof_artifact(
        proof_id=UUID("00000000-0000-0000-0000-000000006401"),
        candidate_id=UUID("00000000-0000-0000-0000-000000003401"),
        observed_at=NOW - timedelta(hours=1),
        information_cutoff=NOW - timedelta(hours=1),
    )
    late = proof_artifact(
        proof_id=UUID("00000000-0000-0000-0000-000000006402"),
        candidate_id=UUID("00000000-0000-0000-0000-000000003402"),
        observed_at=NOW,
        information_cutoff=NOW,
    )
    store.append_proof_artifact(early)
    store.append_proof_artifact(late)

    assert store.proof_artifacts_as_of(NOW - timedelta(minutes=30)) == (early,)
    assert store.proof_artifacts_as_of(NOW) == (early, late)
    assert store.proof_artifacts_as_of(NOW - timedelta(hours=2)) == ()


def test_scan_report_round_trip_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    report = scan_report()

    assert store.append_scan_report(report) is True
    assert store.append_scan_report(report) is False
    assert store.existing_scan_report_ids() == frozenset({report.report_id})
    with pytest.raises(ConflictingRecordError):
        store.append_scan_report(report.model_copy(update={"reason": "a different reason"}))


def test_scan_reports_as_of_respects_the_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    early = scan_report(
        candidate_id=UUID("00000000-0000-0000-0000-000000007001"),
        as_of=NOW - timedelta(hours=1),
        observed_at=NOW - timedelta(hours=1),
    )
    late = scan_report(
        candidate_id=UUID("00000000-0000-0000-0000-000000007002"),
        as_of=NOW,
        observed_at=NOW,
    )
    store.append_scan_report(early)
    store.append_scan_report(late)

    assert store.scan_reports_as_of(NOW - timedelta(minutes=30)) == (early,)
    assert store.scan_reports_as_of(NOW) == (early, late)
    assert store.scan_reports_as_of(NOW - timedelta(hours=2)) == ()


def test_scan_report_shadow_candidate_requires_a_proof_id() -> None:
    with pytest.raises(ValidationError):
        scan_report(
            decision="SHADOW_CANDIDATE",
            reason="conservative surplus positive at current depth",
        )


def test_scan_report_shadow_candidate_requires_evaluated_economics() -> None:
    with pytest.raises(ValidationError):
        scan_report(
            decision="SHADOW_CANDIDATE",
            reason="conservative surplus positive at current depth",
            proof_id=UUID("00000000-0000-0000-0000-000000006001"),
            economics=_economics_result(status="insufficient_evidence"),
        )


def test_scan_report_shadow_candidate_requires_a_positive_surplus() -> None:
    with pytest.raises(ValidationError):
        scan_report(
            decision="SHADOW_CANDIDATE",
            reason="conservative surplus not positive",
            proof_id=UUID("00000000-0000-0000-0000-000000006001"),
            economics=_economics_result(surplus=Decimal("-1")),
        )


def test_scan_report_shadow_candidate_accepts_a_fully_consistent_report() -> None:
    report = scan_report(
        decision="SHADOW_CANDIDATE",
        reason="conservative surplus positive at current depth",
        proof_id=UUID("00000000-0000-0000-0000-000000006001"),
        economics=_economics_result(surplus=Decimal("10")),
    )
    assert report.decision == "SHADOW_CANDIDATE"
