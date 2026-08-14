import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

import polytrading.web.dashboard as web_dashboard
from polytrading.carry.audit import AuditStatus
from polytrading.carry.dossier import (
    evaluate_dossier,
    load_bundled_dossier,
)
from polytrading.carry.dossier_models import DossierStatus
from polytrading.carry.economics_models import EconomicsDecision
from polytrading.domain.models import Asset, BookLevel, FundingObservation, Venue
from polytrading.storage.store import DuckDBStore
from polytrading.trial.health_models import TrialCollectionStatus
from polytrading.web.dashboard import DashboardBuilder, render_dashboard_json
from tests.carry.test_economics_models import KNOWN_AS_OF, legacy_report_json
from tests.carry.test_economics_models import report as economics_report
from tests.domain.factories import (
    book_collection_cycle,
    book_snapshot,
    funding_observation,
    instrument_spec,
)
from tests.trial.funding_helpers import trial_funding_cycle
from tests.trial.test_book_evidence import append_pair
from tests.trial.test_health_models import report as ready_trial_health_report

AS_OF = datetime(2026, 8, 13, 16, 6, tzinfo=UTC)
SOURCE_HASH = "a" * 64
FUTURE_HASH = "f" * 64
BOOK_CYCLE_ID = UUID("00000000-0000-0000-0000-000000000a01")
DOSSIER_AT = datetime(2026, 8, 13, 15, 58, 12, tzinfo=UTC)
DISCOVERY_AT = datetime(2026, 8, 13, 16, 23, 8, tzinfo=UTC)


def _append_trial_cycle(store: DuckDBStore, *, boundary: datetime, identity: int) -> None:
    cycle = trial_funding_cycle(cycle_id=UUID(int=identity), cycle_end=boundary)
    for item in cycle.items:
        store.append_funding(
            FundingObservation(
                schema_version=1,
                venue=item.venue,
                symbol=item.symbol,
                asset=item.asset,
                rate=Decimal("0.0001"),
                interval_hours=Decimal("1"),
                effective_at=boundary,
                observed_at=item.funding_observed_at,
                source_hash=item.funding_source_hashes[0],
            )
        )
    store.append_lighter_dydx_funding_cycle(cycle)


def _ready_trial_health_at(as_of: datetime):
    boundary = as_of.replace(minute=0, second=0, microsecond=0)
    base = ready_trial_health_report()
    recent = base.recent_boundaries[0].model_copy(update={"cycle_end": boundary})
    assets = tuple(
        item.model_copy(
            update={
                "latest_funding_boundary": boundary,
                "latest_book_completed_at": as_of - timedelta(seconds=30),
                "latest_book_age_seconds": Decimal("30"),
                "projected_earliest_evaluation_end": boundary,
            }
        )
        for item in base.assets
    )
    return base.model_copy(
        update={
            "as_of": as_of,
            "latest_auditable_boundary": boundary,
            "trial_started_at": boundary,
            "recent_boundaries": (recent,),
            "assets": assets,
            "reviewed_fees": (),
            "source_hashes": (),
        }
    )


def test_empty_store_snapshot_fails_closed_without_invented_values(tmp_path: Path) -> None:
    path = tmp_path / "empty.duckdb"
    store = DuckDBStore(path)

    snapshot = DashboardBuilder(store, path).build(AS_OF)

    assert snapshot.as_of == AS_OF
    assert snapshot.database_name == "empty.duckdb"
    assert snapshot.funding_health.requested_hours == 24
    assert snapshot.funding_health.missing_boundary_count == 24
    assert snapshot.trial_health.status is TrialCollectionStatus.NOT_STARTED
    assert snapshot.trial_health.trial_started_at is None
    assert snapshot.trial_health.elapsed_auditable_hours == 0
    assert snapshot.trial_health.recent_boundaries == ()
    assert all(item.paired_total_funding_hours == 0 for item in snapshot.trial_health.assets)
    assert all(item.paired_book_hours == 0 for item in snapshot.trial_health.assets)
    assert all(
        item.latest_funding_boundary is None
        and item.latest_book_completed_at is None
        and item.projected_earliest_evaluation_end is None
        for item in snapshot.trial_health.assets
    )
    assert snapshot.latest_funding_cycle is None
    assert snapshot.latest_book_cycle is None
    assert len(snapshot.markets) == 12
    assert all(
        row.instrument_observed_at is None and row.funding_rate is None and row.best_bid is None
        for row in snapshot.markets
    )
    assert tuple(row.status for row in snapshot.carry_rows) == (
        AuditStatus.INSUFFICIENT_DATA,
        AuditStatus.INSUFFICIENT_DATA,
        AuditStatus.INSUFFICIENT_DATA,
    )
    assert tuple(row.asset for row in snapshot.economics_rows) == (
        Asset.BTC,
        Asset.ETH,
        Asset.SOL,
    )
    assert all(not row.report_available for row in snapshot.economics_rows)
    assert set(snapshot.evidence_counts.model_dump().values()) == {0}
    assert snapshot.evidence_counts.lighter_dydx_funding_cycles == 0
    assert snapshot.compatibility_dossier is not None
    assert snapshot.compatibility_dossier.status is DossierStatus.INELIGIBLE
    assert snapshot.venue_discovery is not None
    assert snapshot.venue_discovery.selected_dossier_id is None
    assert len(snapshot.venue_discovery.candidates) == 1
    store.close()


def test_builder_trial_health_and_counts_use_only_the_dashboard_cutoff(tmp_path: Path) -> None:
    path = tmp_path / "trial-cutoff.duckdb"
    store = DuckDBStore(path)
    current_boundary = AS_OF.replace(minute=0, second=0, microsecond=0)
    future_boundary = current_boundary + timedelta(hours=1)
    with store.transaction():
        _append_trial_cycle(store, boundary=current_boundary, identity=20_001)
        _append_trial_cycle(store, boundary=future_boundary, identity=20_002)
        for offset, asset in enumerate(Asset, start=1):
            append_pair(store, 21_000 + offset, current_boundary, asset=asset)
            append_pair(store, 22_000 + offset, AS_OF + timedelta(minutes=1), asset=asset)

    snapshot = DashboardBuilder(store, path).build(AS_OF)
    document = json.loads(render_dashboard_json(snapshot))

    assert snapshot.trial_health.as_of == AS_OF
    assert snapshot.trial_health.status is TrialCollectionStatus.COLLECTING
    assert snapshot.trial_health.trial_started_at == current_boundary
    assert len(snapshot.trial_health.recent_boundaries) == 1
    assert snapshot.trial_health.recent_boundaries[0].attempt_count == 1
    assert all(item.paired_total_funding_hours == 1 for item in snapshot.trial_health.assets)
    assert all(item.paired_book_hours == 1 for item in snapshot.trial_health.assets)
    assert snapshot.evidence_counts.lighter_dydx_funding_cycles == 1
    assert snapshot.evidence_counts.book_collection_cycles == 3
    assert document["trial_health"]["as_of"] == "2026-08-13T16:06:00Z"
    assert document["trial_health"]["recent_boundaries"][0]["attempt_count"] == 1
    assert document["evidence_counts"]["lighter_dydx_funding_cycles"] == 1
    store.close()


def test_collection_readiness_does_not_promote_an_absent_economics_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ready-without-economics.duckdb"
    store = DuckDBStore(path)
    ready = _ready_trial_health_at(AS_OF)

    class ReadyAuditor:
        def __init__(self, selected_store: DuckDBStore) -> None:
            assert selected_store is store

        def audit(self, as_of: datetime, recent_hours: int):
            assert as_of == AS_OF
            assert recent_hours == 24
            return ready

    monkeypatch.setattr(web_dashboard, "LighterDydxTrialHealthAuditor", ReadyAuditor)

    snapshot = DashboardBuilder(store, path).build(AS_OF)

    assert snapshot.trial_health.status is TrialCollectionStatus.READY_FOR_ECONOMICS_EVALUATION
    assert all(not row.report_available for row in snapshot.economics_rows)
    assert all(row.decision is None for row in snapshot.economics_rows)
    store.close()


def test_builder_selects_only_point_in_time_economics_reports(tmp_path: Path) -> None:
    path = tmp_path / "economics.duckdb"
    store = DuckDBStore(path)
    insufficient = economics_report(
        evaluation_id=UUID("00000000-0000-0000-0000-000000000b01"),
        evaluated_at=KNOWN_AS_OF + timedelta(seconds=1),
        decision=EconomicsDecision.INSUFFICIENT_EVIDENCE,
        reason_codes=("BOOK_COVERAGE_INSUFFICIENT",),
        direction=None,
        short_venue=None,
        long_venue=None,
        economics=None,
    )
    rejected = economics_report(
        evaluation_id=UUID("00000000-0000-0000-0000-000000000b02"),
        evaluated_at=KNOWN_AS_OF + timedelta(seconds=5),
        decision=EconomicsDecision.REJECTED,
        reason_codes=("COMPATIBILITY_BLOCKING",),
    )
    future = economics_report(
        evaluation_id=UUID("00000000-0000-0000-0000-000000000b03"),
        evaluated_at=KNOWN_AS_OF + timedelta(seconds=9),
    )
    for item in (insufficient, rejected, future):
        store.append_economic_evaluation(item)

    before = DashboardBuilder(store, path).build(KNOWN_AS_OF)
    at_insufficient = DashboardBuilder(store, path).build(insufficient.evaluated_at)
    at_rejected = DashboardBuilder(store, path).build(rejected.evaluated_at)
    before_future = DashboardBuilder(store, path).build(future.evaluated_at - timedelta(seconds=1))

    assert not before.economics_rows[0].report_available
    insufficient_row = at_insufficient.economics_rows[0]
    assert insufficient_row.decision is EconomicsDecision.INSUFFICIENT_EVIDENCE
    assert insufficient_row.primary_reason_code == "BOOK_COVERAGE_INSUFFICIENT"
    assert insufficient_row.assigned_capital_usd is None
    rejected_row = at_rejected.economics_rows[0]
    assert rejected_row.decision is EconomicsDecision.REJECTED
    assert rejected_row.assigned_capital_usd == Decimal("500")
    assert rejected_row.conservative_7d_net_usd == Decimal("8.80")
    assert rejected_row.conservative_14d_net_usd == Decimal("13.80")
    assert rejected_row.conservative_28d_net_usd == Decimal("18.80")
    assert rejected_row.stress_pass is True
    assert before_future.economics_rows[0] == rejected_row
    trial_economics = at_rejected.trial_health.economics[0]
    assert trial_economics.evaluation_id == rejected.evaluation_id
    assert trial_economics.policy_hash == rejected.policy_hash
    assert trial_economics.reason_codes == rejected.reason_codes
    assert before_future.trial_health.economics[0] == trial_economics
    assert all(not row.report_available for row in at_rejected.economics_rows[1:])

    document = json.loads(render_dashboard_json(at_rejected))
    assert len(document["economics_rows"]) == 3
    assert document["economics_rows"][0]["assigned_capital_usd"] == "500"
    assert len(document["markets"]) == 12
    assert len(document["carry_rows"]) == 3
    assert len(document["funding_health"]["boundaries"]) == 24
    store.close()


def test_builder_keeps_legacy_economics_visible_but_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "legacy-economics.duckdb"
    store = DuckDBStore(path)
    current_shape = economics_report()
    store._connection.execute(
        """
        INSERT INTO economic_evaluations VALUES (?, ?, ?, ?, ?, ?, ?, ?::JSON, ?, ?)
        """,
        [
            current_shape.evaluation_id,
            current_shape.asset.value,
            current_shape.known_as_of,
            current_shape.evaluated_at,
            current_shape.decision.value,
            current_shape.direction.value,
            current_shape.policy_hash,
            legacy_report_json(current_shape),
            1,
            "9" * 64,
        ],
    )

    row = DashboardBuilder(store, path).build(current_shape.evaluated_at).economics_rows[0]

    assert row.report_available is True
    assert row.decision is EconomicsDecision.INSUFFICIENT_EVIDENCE
    assert row.direction is None
    assert row.primary_reason_code == "LEGACY_ECONOMICS_SCHEMA_UNSUPPORTED"
    assert row.assigned_capital_usd is None
    assert row.known_as_of == current_shape.known_as_of
    assert row.evaluated_at == current_shape.evaluated_at
    trial_summary = (
        DashboardBuilder(store, path).build(current_shape.evaluated_at).trial_health.economics[0]
    )
    assert trial_summary.evaluation_schema_version == 1
    assert trial_summary.policy_hash is None
    assert trial_summary.decision is current_shape.decision
    assert "LEGACY_ECONOMICS_SCHEMA_UNSUPPORTED" in trial_summary.reason_codes
    store.close()


def test_builder_excludes_dossier_until_its_point_in_time_cutoff(tmp_path: Path) -> None:
    path = tmp_path / "cutoff.duckdb"
    store = DuckDBStore(path)

    before = DashboardBuilder(store, path).build(DOSSIER_AT - timedelta(microseconds=1))
    at_cutoff = DashboardBuilder(store, path).build(DOSSIER_AT)
    after_discovery = DashboardBuilder(store, path).build(DISCOVERY_AT)

    assert before.compatibility_dossier is None
    assert before.venue_discovery is None
    assert at_cutoff.compatibility_dossier == evaluate_dossier(load_bundled_dossier())
    assert at_cutoff.compatibility_dossier.primary_reason_code == "quanto_structure_excluded"
    assert len(at_cutoff.compatibility_dossier.checks) == 14
    assert at_cutoff.venue_discovery is not None
    assert at_cutoff.venue_discovery.selected_dossier_id is None
    assert tuple(item.dossier_id for item in at_cutoff.venue_discovery.candidates) == (
        "hyperliquid-dydx-core-v1",
    )
    assert after_discovery.venue_discovery is not None
    assert after_discovery.venue_discovery.selected_dossier_id == "lighter-dydx-core-v1"
    assert tuple(item.dossier_id for item in after_discovery.venue_discovery.candidates) == (
        "lighter-dydx-core-v1",
        "hyperliquid-dydx-core-v1",
    )
    assert json.loads(render_dashboard_json(before))["compatibility_dossier"] is None
    at_document = json.loads(render_dashboard_json(at_cutoff))
    assert at_document["compatibility_dossier"]["status"] == "ineligible"
    assert at_document["compatibility_dossier"]["observed_at"] == "2026-08-13T15:58:12Z"
    assert at_document["venue_discovery"]["selected_dossier_id"] is None
    after_document = json.loads(render_dashboard_json(after_discovery))
    assert after_document["venue_discovery"]["selected_dossier_id"] == "lighter-dydx-core-v1"
    store.close()


def test_builder_selects_latest_pre_cutoff_dydx_evidence_and_preserves_native_rate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.duckdb"
    store = DuckDBStore(path)
    instrument = instrument_spec(
        instrument_id="dydx:BTC-USD:linear_perpetual",
        venue=Venue.DYDX,
        symbol="BTC-USD",
        asset=Asset.BTC,
        collateral_asset="USDC",
        pnl_asset="USDC",
        funding_interval_hours=Decimal("1"),
        observed_at=AS_OF - timedelta(minutes=10),
    )
    future_instrument = instrument.model_copy(
        update={"observed_at": AS_OF + timedelta(minutes=1), "source_hash": FUTURE_HASH}
    )
    funding = funding_observation(
        venue=Venue.DYDX,
        symbol="BTC-USD",
        asset=Asset.BTC,
        rate=Decimal("0.0002"),
        interval_hours=Decimal("1"),
        effective_at=AS_OF - timedelta(hours=1),
        observed_at=AS_OF - timedelta(minutes=5),
    )
    future_funding = funding.model_copy(
        update={
            "rate": Decimal("0.9"),
            "effective_at": AS_OF - timedelta(minutes=30),
            "observed_at": AS_OF + timedelta(minutes=1),
            "source_hash": FUTURE_HASH,
        }
    )
    book = book_snapshot(
        cycle_id=BOOK_CYCLE_ID,
        venue=Venue.DYDX,
        symbol="BTC-USD",
        asset=Asset.BTC,
        bids=(BookLevel(price=Decimal("100"), quantity=Decimal("2"), order_count=None),),
        asks=(BookLevel(price=Decimal("101"), quantity=Decimal("3"), order_count=None),),
        effective_at=AS_OF - timedelta(seconds=2),
        observed_at=AS_OF - timedelta(seconds=1),
    )
    cycle = book_collection_cycle(
        cycle_id=BOOK_CYCLE_ID,
        assets=(Asset.BTC,),
        venues=(Venue.DYDX,),
        request_started_at=AS_OF - timedelta(seconds=3),
        request_completed_at=AS_OF - timedelta(seconds=1),
        effective_timestamps=(book.effective_at,),
    )
    for record in (instrument, future_instrument):
        store.append_instrument(record)
    for record in (funding, future_funding):
        store.append_funding(record)
    store.append_book_snapshot(book)
    store.append_book_collection_cycle(cycle)

    snapshot = DashboardBuilder(store, path).build(AS_OF)
    row = next(
        item for item in snapshot.markets if item.venue is Venue.DYDX and item.asset is Asset.BTC
    )

    assert row.symbol == "BTC-USD"
    assert row.instrument_observed_at == instrument.observed_at
    assert row.funding_rate == Decimal("0.0002")
    assert row.funding_interval_hours == Decimal("1")
    assert row.best_bid == Decimal("100")
    assert row.best_ask == Decimal("101")
    assert row.spread_bps == Decimal("99.50248756218905472636815920")
    assert snapshot.latest_book_cycle is not None
    assert snapshot.latest_book_cycle.cycle_id == BOOK_CYCLE_ID
    assert snapshot.evidence_counts.instrument_specs == 1
    assert snapshot.evidence_counts.funding_observations == 1
    store.close()


def test_builder_selects_latest_pre_cutoff_lighter_evidence_and_preserves_signed_rate(
    tmp_path: Path,
) -> None:
    # Catches symbol mismapping, future leakage, or loss of Lighter's signed hourly rate.
    path = tmp_path / "lighter-market.duckdb"
    store = DuckDBStore(path)
    instrument = instrument_spec(
        instrument_id="lighter:BTC",
        venue=Venue.LIGHTER,
        symbol="BTC",
        asset=Asset.BTC,
        collateral_asset="USDC",
        pnl_asset="USDC",
        funding_interval_hours=Decimal("1"),
        observed_at=AS_OF - timedelta(minutes=10),
    )
    future_instrument = instrument.model_copy(
        update={"observed_at": AS_OF + timedelta(minutes=1), "source_hash": FUTURE_HASH}
    )
    funding = funding_observation(
        venue=Venue.LIGHTER,
        symbol="BTC",
        asset=Asset.BTC,
        rate=Decimal("-0.0003"),
        interval_hours=Decimal("1"),
        effective_at=AS_OF - timedelta(hours=1),
        observed_at=AS_OF - timedelta(minutes=5),
    )
    future_funding = funding.model_copy(
        update={
            "rate": Decimal("0.9"),
            "effective_at": AS_OF - timedelta(minutes=30),
            "observed_at": AS_OF + timedelta(minutes=1),
            "source_hash": FUTURE_HASH,
        }
    )
    cycle_id = UUID("00000000-0000-0000-0000-000000000a02")
    book = book_snapshot(
        cycle_id=cycle_id,
        venue=Venue.LIGHTER,
        symbol="BTC",
        asset=Asset.BTC,
        bids=(BookLevel(price=Decimal("100"), quantity=Decimal("2"), order_count=2),),
        asks=(BookLevel(price=Decimal("101"), quantity=Decimal("3"), order_count=3),),
        effective_at=AS_OF - timedelta(seconds=2),
        observed_at=AS_OF - timedelta(seconds=1),
    )
    cycle = book_collection_cycle(
        cycle_id=cycle_id,
        assets=(Asset.BTC,),
        venues=(Venue.LIGHTER,),
        request_started_at=AS_OF - timedelta(seconds=3),
        request_completed_at=AS_OF - timedelta(seconds=1),
        effective_timestamps=(book.effective_at,),
    )
    for record in (instrument, future_instrument):
        store.append_instrument(record)
    for record in (funding, future_funding):
        store.append_funding(record)
    store.append_book_snapshot(book)
    store.append_book_collection_cycle(cycle)

    snapshot = DashboardBuilder(store, path).build(AS_OF)
    row = next(
        item for item in snapshot.markets if item.venue is Venue.LIGHTER and item.asset is Asset.BTC
    )
    document = json.loads(render_dashboard_json(snapshot))
    rendered = next(
        item
        for item in document["markets"]
        if item["venue"] == "lighter" and item["asset"] == "BTC"
    )

    assert row.symbol == "BTC"
    assert row.instrument_observed_at == instrument.observed_at
    assert row.funding_rate == Decimal("-0.0003")
    assert row.funding_interval_hours == Decimal("1")
    assert row.best_bid == Decimal("100")
    assert row.best_ask == Decimal("101")
    assert row.spread_bps == Decimal("99.50248756218905472636815920")
    assert rendered["funding_rate"] == "-0.000300000000000000"
    assert len(snapshot.markets) == 12
    assert len(snapshot.carry_rows) == 3
    assert snapshot.funding_health.requested_hours == 24
    assert len(snapshot.funding_health.boundaries) == 24
    store.close()


def test_recipes_shell_quote_database_path_and_json_uses_public_scalars(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "actual.duckdb")
    display_path = Path("/tmp/research data/owner's.duckdb")
    snapshot = DashboardBuilder(store, display_path).build(AS_OF)

    quoted = "'/tmp/research data/owner'\"'\"'s.duckdb'"
    assert quoted in snapshot.operation_recipes.collect_public
    assert quoted in snapshot.operation_recipes.collect_books_once
    for recipe in snapshot.operation_recipes.model_dump().values():
        assert quoted in recipe
    assert "REPLACE_WITH_EVALUATED_AT" in snapshot.operation_recipes.evaluate_trial_btc
    assert "REPLACE_WITH_EVALUATION_UUID" in snapshot.operation_recipes.evaluate_trial_btc
    assert "<" not in snapshot.operation_recipes.evaluate_trial_btc
    assert ">" not in snapshot.operation_recipes.evaluate_trial_btc
    assert "$(" not in snapshot.operation_recipes.evaluate_trial_btc
    assert "`" not in snapshot.operation_recipes.evaluate_trial_btc
    document = json.loads(render_dashboard_json(snapshot))
    assert document["as_of"] == "2026-08-13T16:06:00Z"
    assert document["funding_health"]["complete_coverage"] == "0"
    assert document["markets"][0]["venue"] == "bybit"
    assert document["markets"][0]["asset"] == "BTC"
    store.close()
