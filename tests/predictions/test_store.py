import json
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import duckdb
import pytest
from pydantic import ValidationError

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.economics_models import EconomicsResult
from polytrading.predictions.experiments import ShadowExperiment, TrialFamily
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.shadow_ledger import LedgerPosting, ShadowReconciliation
from polytrading.predictions.shadow_models import (
    ShadowEvent,
    ShadowLegPlan,
    ShadowPlan,
    ShadowState,
)
from polytrading.predictions.storage.store import ConflictingRecordError, PredictionMarketStore
from polytrading.storage.store import DuckDBStore
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
    versions = store._connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
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
        "shadow_plans",
        "shadow_events",
        "shadow_ledger_postings",
        "shadow_reconciliations",
        "trial_families",
        "shadow_experiments",
        "schema_migrations",
    } <= tables
    assert versions == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]
    perpetual_futures_tables = {
        "raw_envelopes",
        "instrument_specs",
        "funding_observations",
        "market_snapshots",
        "book_snapshots",
    }
    assert not (perpetual_futures_tables & tables)


def test_experiment_registry_schema_declares_both_primary_keys(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")

    primary_keys = {
        row[0]: tuple(row[1])
        for row in store._connection.execute(
            """
            SELECT table_name, constraint_column_names
            FROM duckdb_constraints()
            WHERE table_name IN ('trial_families', 'shadow_experiments')
              AND constraint_type = 'PRIMARY KEY'
            """
        ).fetchall()
    }

    assert primary_keys == {
        "trial_families": ("family_id", "preregistered_at"),
        "shadow_experiments": ("experiment_id",),
    }


def test_trial_family_table_rejects_a_direct_duplicate_key(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    family = trial_family()
    store.append_trial_family(family)

    with pytest.raises(duckdb.ConstraintException):
        store._connection.execute(
            """
            INSERT INTO trial_families
            SELECT * FROM trial_families
            WHERE family_id = ? AND preregistered_at = ?
            """,
            [family.family_id, family.preregistered_at],
        )


def test_shadow_experiment_table_rejects_a_direct_duplicate_key(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    experiment = shadow_experiment()
    store.append_shadow_experiment(experiment)

    with pytest.raises(duckdb.ConstraintException):
        store._connection.execute(
            """
            INSERT INTO shadow_experiments
            SELECT * FROM shadow_experiments
            WHERE experiment_id = ?
            """,
            [experiment.experiment_id],
        )


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


def test_read_write_open_rejects_a_core_store_database_without_mutating_it(tmp_path: Path) -> None:
    path = tmp_path / "forward.duckdb"
    DuckDBStore(path).close()

    with pytest.raises(RuntimeError, match="prediction-market database"):
        PredictionMarketStore(path)

    with duckdb.connect(str(path), read_only=True) as connection:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        versions = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert "raw_envelopes" in tables
    assert "prediction_raw_envelopes" not in tables
    assert versions == [(1,), (2,), (3,), (4,), (5,), (6,)]


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


@pytest.mark.parametrize(
    ("column", "tampered"),
    [
        ("venue", PredictionVenue.KALSHI.value),
        ("market_id", "tampered-market"),
        ("observed_at", NOW + timedelta(seconds=1)),
    ],
)
def test_verified_fee_rate_rejects_indexed_column_tamper(
    column: str, tampered: object, tmp_path: Path
) -> None:
    store = PredictionMarketStore(tmp_path / f"fee-{column}.duckdb")
    rate = fee_rate()
    store.append_fee_rate(rate)
    store._connection.execute(
        f"UPDATE prediction_fee_rates SET {column} = ? WHERE record_hash IS NOT NULL",
        [tampered],
    )

    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_fee_rate_by_source_hash(rate.venue, rate.market_id, rate.source_hash, NOW)


@pytest.mark.parametrize("conflicting", [False, True])
def test_verified_fee_rate_rejects_repeated_logical_identity(
    conflicting: bool, tmp_path: Path
) -> None:
    store = PredictionMarketStore(tmp_path / f"fee-duplicate-{conflicting}.duckdb")
    rate = fee_rate()
    store.append_fee_rate(rate)
    repeated = rate.model_copy(update={"taker_rate": Decimal("0.25")}) if conflicting else rate
    canonical = json.dumps(
        repeated.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    store._connection.execute(
        "INSERT INTO prediction_fee_rates VALUES (?, ?, ?, ?, ?)",
        [
            repeated.venue.value,
            repeated.market_id,
            repeated.observed_at,
            repeated.model_dump_json(),
            sha256(canonical.encode()).hexdigest(),
        ],
    )

    with pytest.raises(ConflictingRecordError, match="logical identity"):
        store.verified_fee_rate_by_source_hash(rate.venue, rate.market_id, rate.source_hash, NOW)


def test_verified_fee_rate_allows_history_and_selects_latest_cutoff_safe_version(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "fee-history.duckdb")
    early = fee_rate(observed_at=NOW - timedelta(hours=1))
    late = early.model_copy(update={"observed_at": NOW, "taker_rate": Decimal("0.25")})
    store.append_fee_rate(late)
    store.append_fee_rate(early)

    assert (
        store.verified_fee_rate_by_source_hash(
            early.venue,
            early.market_id,
            early.source_hash,
            NOW - timedelta(minutes=30),
        )
        == early
    )
    assert (
        store.verified_fee_rate_by_source_hash(early.venue, early.market_id, early.source_hash, NOW)
        == late
    )


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


def test_shadow_plan_round_trip_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan = shadow_plan()

    assert store.append_shadow_plan(plan) is True
    assert store.append_shadow_plan(plan) is False
    assert store.shadow_plan_by_proposal(plan.proposal_id) == plan
    assert store.shadow_plan_by_proposal(UUID("00000000-0000-0000-0000-000000008099")) is None
    with pytest.raises(ConflictingRecordError):
        store.append_shadow_plan(plan.model_copy(update={"policy_version": "different"}))


def test_shadow_plans_as_of_is_ordered_and_cutoff_safe(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    early = shadow_plan(
        proposal_id=UUID("00000000-0000-0000-0000-000000008101"),
        candidate_id=UUID("00000000-0000-0000-0000-000000008201"),
        observed_at=NOW - timedelta(hours=1),
        information_cutoff=NOW - timedelta(hours=1),
    )
    late = shadow_plan(
        proposal_id=UUID("00000000-0000-0000-0000-000000008102"),
        candidate_id=UUID("00000000-0000-0000-0000-000000008202"),
    )
    store.append_shadow_plan(late)
    store.append_shadow_plan(early)

    assert store.shadow_plans_as_of(NOW - timedelta(minutes=30)) == (early,)
    assert store.shadow_plans_as_of(NOW) == (early, late)
    assert store.shadow_plans_as_of(NOW - timedelta(hours=2)) == ()


def test_shadow_events_enforce_proposal_sequence_integrity_and_cutoff_safe_ordering(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    first = shadow_event(occurred_at=NOW - timedelta(hours=1))
    second = shadow_event(
        event_id=UUID("00000000-0000-0000-0000-000000009002"),
        sequence=1,
        from_state=ShadowState.DISCOVERED,
        to_state=ShadowState.PROOF_VALIDATED,
    )

    assert store.append_shadow_event(second) is True
    assert store.append_shadow_event(first) is True
    assert store.append_shadow_event(first) is False
    assert store.shadow_events_for_proposal(first.proposal_id, NOW - timedelta(minutes=30)) == (
        first,
    )
    assert store.shadow_events_for_proposal(first.proposal_id, NOW) == (first, second)
    with pytest.raises(ConflictingRecordError):
        store.append_shadow_event(
            shadow_event(event_id=UUID("00000000-0000-0000-0000-000000009003"))
        )
    with pytest.raises(ConflictingRecordError):
        store.append_shadow_event(first.model_copy(update={"detail": "different detail"}))


def test_verified_shadow_events_reject_stale_record_json_hash(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    event = shadow_event()
    store.append_shadow_event(event)

    assert store.verified_shadow_events_for_proposal(event.proposal_id, NOW) == (event,)
    store._connection.execute(
        "UPDATE shadow_events SET record_json = ? WHERE event_id = ?",
        [event.model_copy(update={"detail": "tampered"}).model_dump_json(), event.event_id],
    )

    with pytest.raises(ConflictingRecordError, match="immutable record hash"):
        store.verified_shadow_events_for_proposal(event.proposal_id, NOW)


@pytest.mark.parametrize(
    ("column", "tampered"),
    [
        ("event_id", UUID("00000000-0000-0000-0000-000000009999")),
        ("proposal_id", UUID("00000000-0000-0000-0000-000000008999")),
        ("sequence", 7),
        ("occurred_at", NOW - timedelta(seconds=1)),
    ],
)
def test_verified_shadow_events_reject_indexed_column_tamper(
    column: str, tampered: object, tmp_path: Path
) -> None:
    store = PredictionMarketStore(tmp_path / f"event-{column}.duckdb")
    event = shadow_event()
    store.append_shadow_event(event)
    store._connection.execute(
        f"UPDATE shadow_events SET {column} = ? WHERE event_id = ?",
        [tampered, event.event_id],
    )

    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_shadow_events_for_proposal(event.proposal_id, NOW)


def test_verified_ledger_postings_reject_stale_record_json_hash(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    posting = ledger_posting()
    store.append_ledger_posting(posting)

    assert store.verified_ledger_postings_for_proposal(posting.proposal_id, NOW) == (posting,)
    store._connection.execute(
        "UPDATE shadow_ledger_postings SET record_json = ? WHERE posting_id = ?",
        [posting.model_copy(update={"detail": "tampered"}).model_dump_json(), posting.posting_id],
    )

    with pytest.raises(ConflictingRecordError, match="immutable record hash"):
        store.verified_ledger_postings_for_proposal(posting.proposal_id, NOW)


@pytest.mark.parametrize(
    ("column", "tampered"),
    [
        ("posting_id", UUID("00000000-0000-0000-0000-00000000b999")),
        ("proposal_id", UUID("00000000-0000-0000-0000-000000008999")),
        ("event_id", UUID("00000000-0000-0000-0000-000000009999")),
        ("occurred_at", NOW - timedelta(seconds=1)),
    ],
)
def test_verified_ledger_postings_reject_indexed_column_tamper(
    column: str, tampered: object, tmp_path: Path
) -> None:
    store = PredictionMarketStore(tmp_path / f"posting-{column}.duckdb")
    posting = ledger_posting()
    store.append_ledger_posting(posting)
    store._connection.execute(
        f"UPDATE shadow_ledger_postings SET {column} = ? WHERE posting_id = ?",
        [tampered, posting.posting_id],
    )

    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_ledger_postings_for_proposal(posting.proposal_id, NOW)


def test_trial_family_round_trip_supports_append_only_preregistration_versions(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    early = trial_family(preregistered_at=NOW - timedelta(hours=2))
    late = trial_family(
        hypothesis="The preregistered hypothesis was revised before another trial.",
        preregistered_at=NOW,
    )

    assert store.append_trial_family(early) is True
    assert store.append_trial_family(early) is False
    assert store.append_trial_family(late) is True
    assert store.trial_family_by_id(early.family_id, NOW - timedelta(hours=1)) == early
    assert store.trial_family_by_id(early.family_id, NOW) == late
    assert store.trial_family_by_id("missing-family", NOW) is None
    with pytest.raises(ConflictingRecordError):
        store.append_trial_family(early.model_copy(update={"hypothesis": "conflicting retry"}))


def test_trial_families_as_of_returns_every_known_version_in_deterministic_order(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    second_id = trial_family(
        family_id="z-family",
        preregistered_at=NOW - timedelta(hours=1),
    )
    first_id = trial_family(
        family_id="a-family",
        preregistered_at=NOW - timedelta(hours=1),
    )
    future = trial_family(
        family_id="future-family",
        preregistered_at=NOW + timedelta(hours=1),
    )
    store.append_trial_family(second_id)
    store.append_trial_family(future)
    store.append_trial_family(first_id)

    assert store.trial_families_as_of(NOW) == (first_id, second_id)
    assert store.trial_families_as_of(NOW - timedelta(hours=2)) == ()


def test_verified_family_market_and_experiment_reads_reject_record_json_tamper(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    family = trial_family()
    market = market_record()
    experiment = shadow_experiment()
    store.append_trial_family(family)
    store.append_market(market)
    store.append_shadow_experiment(experiment)

    assert store.verified_trial_family_by_id(family.family_id, NOW) == family
    assert store.verified_markets_as_of(market.venue, NOW) == (market,)
    assert store.verified_shadow_experiments_as_of(NOW) == (experiment,)

    store._connection.execute(
        "UPDATE trial_families SET record_json = ? WHERE family_id = ?",
        [
            family.model_copy(update={"thresholds_json": '{"tampered":true}'}).model_dump_json(),
            family.family_id,
        ],
    )
    with pytest.raises(ConflictingRecordError, match="immutable record hash"):
        store.verified_trial_family_by_id(family.family_id, NOW)

    store._connection.execute(
        "UPDATE markets SET record_json = ? WHERE venue = ? AND market_id = ?",
        [
            market.model_copy(update={"event_id": "tampered-event"}).model_dump_json(),
            market.venue.value,
            market.market_id,
        ],
    )
    with pytest.raises(ConflictingRecordError, match="immutable record hash"):
        store.verified_markets_as_of(market.venue, NOW)

    store._connection.execute(
        "UPDATE shadow_experiments SET record_json = ? WHERE experiment_id = ?",
        [
            experiment.model_copy(update={"paper_pnl_usd": Decimal("99")}).model_dump_json(),
            experiment.experiment_id,
        ],
    )
    with pytest.raises(ConflictingRecordError, match="immutable record hash"):
        store.verified_shadow_experiments_as_of(NOW)


def test_verified_reconciliations_return_every_row_in_order_and_reject_tamper(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    earliest = shadow_reconciliation(
        reconciliation_id=UUID("00000000-0000-0000-0000-00000000a003"),
        observed_at=NOW - timedelta(hours=1),
    )
    same_time_first = shadow_reconciliation(
        reconciliation_id=UUID("00000000-0000-0000-0000-00000000a001")
    )
    same_time_second = shadow_reconciliation(
        reconciliation_id=UUID("00000000-0000-0000-0000-00000000a002")
    )
    for reconciliation in (same_time_second, earliest, same_time_first):
        store.append_reconciliation(reconciliation)

    assert store.verified_shadow_reconciliations_for_proposal(
        earliest.proposal_id, NOW - timedelta(minutes=30)
    ) == (earliest,)
    assert store.verified_shadow_reconciliations_for_proposal(earliest.proposal_id, NOW) == (
        earliest,
        same_time_first,
        same_time_second,
    )

    store._connection.execute(
        "UPDATE shadow_reconciliations SET record_json = ? WHERE reconciliation_id = ?",
        [
            same_time_second.model_copy(
                update={"terminal_state": ShadowState.UNWOUND}
            ).model_dump_json(),
            same_time_second.reconciliation_id,
        ],
    )
    with pytest.raises(ConflictingRecordError, match="immutable record hash"):
        store.verified_shadow_reconciliations_for_proposal(earliest.proposal_id, NOW)


@pytest.mark.parametrize(
    ("column", "tampered"),
    [
        ("reconciliation_id", UUID("00000000-0000-0000-0000-00000000afff")),
        ("proposal_id", UUID("00000000-0000-0000-0000-000000008fff")),
        ("observed_at", NOW + timedelta(seconds=1)),
    ],
)
def test_verified_reconciliations_reject_indexed_column_tamper(
    column: str, tampered: object, tmp_path: Path
) -> None:
    store = PredictionMarketStore(tmp_path / f"reconciliation-{column}.duckdb")
    reconciliation = shadow_reconciliation()
    store.append_reconciliation(reconciliation)
    store._connection.execute(
        f"UPDATE shadow_reconciliations SET {column} = ? WHERE record_hash IS NOT NULL",
        [tampered],
    )

    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_shadow_reconciliations_for_proposal(reconciliation.proposal_id, NOW)


def test_shadow_experiment_round_trip_is_idempotent_conflict_safe_and_family_agnostic(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    orphan = shadow_experiment(family_id="not-yet-registered")

    assert store.append_shadow_experiment(orphan) is True
    assert store.append_shadow_experiment(orphan) is False
    assert store.shadow_experiments_for_family("not-yet-registered", NOW) == (orphan,)
    with pytest.raises(ConflictingRecordError):
        store.append_shadow_experiment(
            orphan.model_copy(update={"scenario_id": "different-scenario"})
        )


def test_shadow_experiment_reads_keep_unknown_unreconciled_and_negative_pnl_rows(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    unknown = shadow_experiment(
        experiment_id=UUID("00000000-0000-0000-0000-00000000e011"),
        terminal_state=ShadowState.UNKNOWN,
        paper_pnl_usd=None,
        reconciled=False,
        observed_at=NOW - timedelta(hours=2),
        as_of=NOW - timedelta(hours=2),
    )
    losing = shadow_experiment(
        experiment_id=UUID("00000000-0000-0000-0000-00000000e012"),
        terminal_state=ShadowState.UNWOUND,
        paper_pnl_usd=Decimal("-4.25"),
        reconciled=True,
        observed_at=NOW - timedelta(hours=1),
        as_of=NOW - timedelta(hours=1),
    )
    future = shadow_experiment(
        experiment_id=UUID("00000000-0000-0000-0000-00000000e013"),
        terminal_state=ShadowState.EXPIRED,
        paper_pnl_usd=Decimal("0"),
        reconciled=True,
        observed_at=NOW + timedelta(hours=1),
        as_of=NOW + timedelta(hours=1),
    )
    future_knowledge = shadow_experiment(
        experiment_id=UUID("00000000-0000-0000-0000-00000000e014"),
        observed_at=NOW - timedelta(minutes=30),
        as_of=NOW + timedelta(hours=1),
    )
    for experiment in (future, future_knowledge, losing, unknown):
        store.append_shadow_experiment(experiment)

    assert store.shadow_experiments_as_of(NOW) == (unknown, losing)
    assert store.shadow_experiments_for_family(unknown.family_id, NOW) == (unknown, losing)
    assert store.shadow_experiments_as_of(NOW - timedelta(hours=3)) == ()


def test_verified_shadow_plan_and_experiment_reads_reject_record_json_tamper(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan = shadow_plan()
    experiment = shadow_experiment(proposal_id=plan.proposal_id)
    store.append_shadow_plan(plan)
    store.append_shadow_experiment(experiment)

    store._connection.execute(
        "UPDATE shadow_plans SET record_json = ? WHERE proposal_id = ?",
        [
            plan.model_copy(update={"completion_path": "tampered"}).model_dump_json(),
            plan.proposal_id,
        ],
    )
    with pytest.raises(ConflictingRecordError, match="immutable record hash"):
        store.verified_shadow_plan_by_proposal(plan.proposal_id)

    store._connection.execute(
        "UPDATE shadow_experiments SET record_json = ? WHERE experiment_id = ?",
        [
            experiment.model_copy(update={"scenario_id": "tampered"}).model_dump_json(),
            experiment.experiment_id,
        ],
    )
    with pytest.raises(ConflictingRecordError, match="immutable record hash"):
        store.verified_shadow_experiments_for_proposal(plan.proposal_id, NOW)


@pytest.mark.parametrize(
    ("column", "tampered"),
    [
        ("experiment_id", UUID("00000000-0000-0000-0000-00000000efff")),
        ("family_id", "tampered-family"),
        ("proposal_id", UUID("00000000-0000-0000-0000-000000008fff")),
        ("scenario_id", "tampered-scenario"),
        ("terminal_state", ShadowState.EXPIRED.value),
        ("reconciled", False),
        ("as_of", NOW + timedelta(seconds=1)),
        ("observed_at", NOW + timedelta(seconds=1)),
    ],
)
def test_verified_experiments_reject_indexed_column_tamper(
    column: str, tampered: object, tmp_path: Path
) -> None:
    store = PredictionMarketStore(tmp_path / f"experiment-{column}.duckdb")
    experiment = shadow_experiment()
    store.append_shadow_experiment(experiment)
    store._connection.execute(
        f"UPDATE shadow_experiments SET {column} = ? WHERE experiment_id = ?",
        [tampered, experiment.experiment_id],
    )

    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_shadow_experiments_as_of(NOW)


@pytest.mark.parametrize(
    ("column", "tampered"),
    [
        ("proposal_id", UUID("00000000-0000-0000-0000-000000008999")),
        ("candidate_id", UUID("00000000-0000-0000-0000-000000008998")),
        ("observed_at", NOW - timedelta(seconds=1)),
        ("information_cutoff", NOW - timedelta(seconds=1)),
    ],
)
def test_verified_shadow_plans_reject_indexed_column_tamper(
    column: str, tampered: object, tmp_path: Path
) -> None:
    store = PredictionMarketStore(tmp_path / f"plan-{column}.duckdb")
    plan = shadow_plan()
    store.append_shadow_plan(plan)
    store._connection.execute(
        f"UPDATE shadow_plans SET {column} = ? WHERE proposal_id = ?",
        [tampered, plan.proposal_id],
    )

    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_shadow_plans_as_of(NOW)


def test_experiment_registry_revalidates_unchecked_models_before_persistence(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    invalid_family = trial_family().model_copy(update={"venues": ()})
    invalid_experiment = shadow_experiment().model_copy(
        update={"paper_pnl_usd": Decimal("1"), "reconciled": False}
    )

    with pytest.raises(ValidationError):
        store.append_trial_family(invalid_family)
    with pytest.raises(ValidationError):
        store.append_shadow_experiment(invalid_experiment)
    assert store.trial_families_as_of(NOW) == ()
    assert store.shadow_experiments_as_of(NOW) == ()


@pytest.mark.parametrize(
    "state",
    [
        ShadowState.DISCOVERED,
        ShadowState.PROOF_VALIDATED,
        ShadowState.ECONOMICS_VALIDATED,
        ShadowState.SHADOW_PLANNED,
        ShadowState.FIRST_LEG_SIMULATED,
    ],
)
def test_experiment_store_revalidates_unchecked_intermediate_states(
    tmp_path: Path, state: ShadowState
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    unchecked = shadow_experiment().model_copy(update={"terminal_state": state})

    with pytest.raises(ValidationError):
        store.append_shadow_experiment(unchecked)
    assert store.shadow_experiments_as_of(NOW) == ()


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


def test_exact_shadow_lineage_getters_ignore_later_evidence(tmp_path: Path) -> None:
    """Replay must retrieve cited records/hashes, never an unrelated newest row."""
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    candidate = candidate_relationship()
    proof = proof_artifact(candidate_id=candidate.candidate_id)
    report = scan_report(candidate_id=candidate.candidate_id, proof_id=proof.proof_id)
    original_book = prediction_book_snapshot(source_hash="b" * 64)
    later_book = prediction_book_snapshot(
        cycle_id=UUID("00000000-0000-0000-0000-000000000b02"),
        observed_at=NOW + timedelta(seconds=1),
        effective_at=NOW + timedelta(seconds=1),
        source_hash="c" * 64,
    )
    original_fee = fee_rate(market_id="0xcondition", source_hash="d" * 64)
    later_fee = fee_rate(
        market_id="0xcondition",
        observed_at=NOW + timedelta(seconds=1),
        source_hash="e" * 64,
    )
    store.append_candidate_relationship(candidate)
    store.append_proof_artifact(proof)
    store.append_scan_report(report)
    store.append_book_snapshot(original_book)
    store.append_book_snapshot(later_book)
    store.append_fee_rate(original_fee)
    store.append_fee_rate(later_fee)

    cutoff = NOW + timedelta(minutes=1)
    assert store.candidate_relationship_by_id(candidate.candidate_id, cutoff) == candidate
    assert store.proof_artifact_by_id(proof.proof_id, cutoff) == proof
    assert store.scan_report_by_id(report.report_id, cutoff) == report
    assert (
        store.book_snapshot_by_source_hash(
            original_book.venue,
            original_book.market_id,
            original_book.outcome_token_id,
            original_book.source_hash,
            cutoff,
        )
        == original_book
    )
    assert (
        store.fee_rate_by_source_hash(
            original_fee.venue,
            original_fee.market_id,
            original_fee.source_hash,
            cutoff,
        )
        == original_fee
    )
    store.close()


def shadow_plan(**overrides: object) -> ShadowPlan:
    values: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": UUID("00000000-0000-0000-0000-000000008001"),
        "candidate_id": UUID("00000000-0000-0000-0000-000000008002"),
        "proof_id": UUID("00000000-0000-0000-0000-000000008003"),
        "scan_report_id": UUID("00000000-0000-0000-0000-000000008004"),
        "legs": (
            ShadowLegPlan(
                leg_index=0,
                venue=PredictionVenue.POLYMARKET,
                market_id="market-a",
                outcome_token_id="token-a",
                sequence_position=0,
                limit_price_levels=((Decimal("0.40"), Decimal("10")),),
                max_quantity=Decimal("10"),
            ),
            ShadowLegPlan(
                leg_index=1,
                venue=PredictionVenue.POLYMARKET,
                market_id="market-b",
                outcome_token_id="token-b",
                sequence_position=1,
                limit_price_levels=((Decimal("0.60"), Decimal("10")),),
                max_quantity=Decimal("10"),
            ),
        ),
        "bottleneck_leg_index": 1,
        "max_quantity": Decimal("10"),
        "order_policy": "taker_cross_only",
        "expires_at": NOW + timedelta(minutes=5),
        "completion_path": "Buy remaining legs after the first fill.",
        "cancellation_path": "Cancel unfilled orders before expiry.",
        "unwind_path": "Sell filled inventory at the best available bids.",
        "max_incomplete_exposure_usd": Decimal("15"),
        "max_incomplete_loss_usd": Decimal("5"),
        "frozen_hashes": ("a" * 64, "b" * 64),
        "policy_id": "research-v1",
        "policy_version": "1",
        "risk_policy_version": "1",
        "minimum_basket_payout": Decimal("1.00"),
        "kill_conditions": ("book becomes stale",),
        "information_cutoff": NOW,
        "observed_at": NOW,
    }
    values.update(overrides)
    return ShadowPlan(**values)


def shadow_event(**overrides: object) -> ShadowEvent:
    values: dict[str, object] = {
        "schema_version": 1,
        "event_id": UUID("00000000-0000-0000-0000-000000009001"),
        "proposal_id": UUID("00000000-0000-0000-0000-000000008001"),
        "sequence": 0,
        "from_state": None,
        "to_state": ShadowState.DISCOVERED,
        "occurred_at": NOW,
        "detail": "candidate admitted to shadow tracking",
        "quantity_filled": None,
        "leg_index": None,
        "scenario_id": None,
    }
    values.update(overrides)
    return ShadowEvent(**values)


def shadow_reconciliation(**overrides: object) -> ShadowReconciliation:
    values: dict[str, object] = {
        "reconciliation_id": UUID("00000000-0000-0000-0000-00000000a001"),
        "proposal_id": UUID("00000000-0000-0000-0000-000000008001"),
        "terminal_event_id": UUID("00000000-0000-0000-0000-000000009099"),
        "terminal_state": ShadowState.COMPLETE,
        "venues_reconciled": (PredictionVenue.POLYMARKET,),
        "complete": True,
        "unexplained_difference_usd": Decimal("0"),
        "observed_at": NOW,
    }
    values.update(overrides)
    return ShadowReconciliation(**values)


def ledger_posting(**overrides: object) -> LedgerPosting:
    values: dict[str, object] = {
        "posting_id": UUID("00000000-0000-0000-0000-00000000b001"),
        "proposal_id": UUID("00000000-0000-0000-0000-000000008001"),
        "event_id": UUID("00000000-0000-0000-0000-000000009001"),
        "venue": PredictionVenue.POLYMARKET,
        "account": "reserve",
        "debit_usd": Decimal("1"),
        "credit_usd": Decimal("0"),
        "occurred_at": NOW,
        "detail": "verified posting fixture",
    }
    values.update(overrides)
    return LedgerPosting(**values)


def trial_family(**overrides: object) -> TrialFamily:
    values: dict[str, object] = {
        "family_id": "cross-venue-equivalence-v1",
        "hypothesis": "Equivalent contracts retain positive surplus after doubled costs.",
        "preregistered_at": NOW,
        "thresholds_json": '{"minimum_surplus_usd":"5.00","version":1}',
        "venues": (PredictionVenue.KALSHI, PredictionVenue.POLYMARKET),
        "registered_by": "research-operator@example.com",
    }
    values.update(overrides)
    return TrialFamily(**values)


def shadow_experiment(**overrides: object) -> ShadowExperiment:
    values: dict[str, object] = {
        "experiment_id": UUID("00000000-0000-0000-0000-00000000e001"),
        "family_id": "cross-venue-equivalence-v1",
        "proposal_id": UUID("00000000-0000-0000-0000-00000000e002"),
        "scenario_id": "baseline",
        "terminal_state": ShadowState.RECONCILED,
        "paper_pnl_usd": Decimal("-2.50"),
        "reconciled": True,
        "as_of": NOW,
        "observed_at": NOW,
    }
    values.update(overrides)
    return ShadowExperiment(**values)
