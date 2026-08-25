import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.predictions.candidates_models import CandidateDisposition, RelationshipType
from polytrading.predictions.dashboard import (
    PredictionDashboardBuilder,
    render_prediction_dashboard_json,
)
from polytrading.predictions.dashboard_models import ShadowListing, ShadowSummary
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.experiments import ShadowExperiment
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.shadow_ledger import ShadowReconciliation
from polytrading.predictions.shadow_models import (
    ShadowEvent,
    ShadowLegPlan,
    ShadowPlan,
    ShadowState,
)
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.candidate_helpers import ai_provenance, candidate_relationship
from tests.predictions.domain_helpers import NOW, market_record, prediction_book_snapshot
from tests.predictions.manifest_helpers import venue_manifest
from tests.predictions.proof_helpers import proof_artifact
from tests.predictions.scan_helpers import scan_report


def shadow_listing(**overrides: object) -> ShadowListing:
    values: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": UUID(int=1),
        "candidate_id": UUID(int=101),
        "current_state": ShadowState.RECONCILED,
        "scenario_id": "baseline",
        "quantity": Decimal("10"),
        "paper_pnl": Decimal("-2.50"),
        "observed_at": NOW,
    }
    values.update(overrides)
    return ShadowListing(**values)


def shadow_summary(**overrides: object) -> ShadowSummary:
    values: dict[str, object] = {
        "schema_version": 1,
        "proposals_total": 1,
        "by_terminal_state": {"reconciled": 1},
        "reconciled_count": 1,
        "reconciled_paper_pnl_usd": Decimal("-2.50"),
        "unreconciled_count": 0,
        "latest": (shadow_listing(),),
        "experiments_by_family": {"cross-venue-equivalence-v1": 1},
    }
    values.update(overrides)
    return ShadowSummary(**values)


def shadow_plan(**overrides: object) -> ShadowPlan:
    proposal_id = overrides.get("proposal_id", UUID(int=1))
    observed_at = overrides.get("observed_at", NOW - timedelta(minutes=10))
    values: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": proposal_id,
        "candidate_id": UUID(int=1000 + cast(UUID, proposal_id).int),
        "proof_id": UUID(int=2001),
        "scan_report_id": UUID(int=2002),
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
                venue=PredictionVenue.KALSHI,
                market_id="market-b",
                outcome_token_id=None,
                sequence_position=1,
                limit_price_levels=((Decimal("0.50"), Decimal("10")),),
                max_quantity=Decimal("10"),
            ),
        ),
        "bottleneck_leg_index": 0,
        "max_quantity": Decimal("10"),
        "order_policy": "taker_cross_only",
        "expires_at": cast(datetime, observed_at) + timedelta(hours=1),
        "completion_path": "complete remaining legs",
        "cancellation_path": "cancel unfilled orders",
        "unwind_path": "sell acquired inventory",
        "max_incomplete_exposure_usd": Decimal("10"),
        "max_incomplete_loss_usd": Decimal("2"),
        "frozen_hashes": ("a" * 64,),
        "policy_id": "economics-v1",
        "policy_version": "1",
        "risk_policy_version": "1",
        "minimum_basket_payout": Decimal("1"),
        "kill_conditions": ("evidence unavailable",),
        "information_cutoff": observed_at,
        "observed_at": observed_at,
    }
    values.update(overrides)
    return ShadowPlan(**values)


_PATH_TO_FIRST_LEG = (
    ShadowState.DISCOVERED,
    ShadowState.PROOF_VALIDATED,
    ShadowState.ECONOMICS_VALIDATED,
    ShadowState.SHADOW_PLANNED,
    ShadowState.FIRST_LEG_SIMULATED,
)


def shadow_events(
    plan: ShadowPlan,
    terminal_state: ShadowState,
    *,
    terminal_at: datetime,
    scenario_id: str = "baseline",
    reconciled_at: datetime | None = None,
) -> tuple[ShadowEvent, ...]:
    states = (*_PATH_TO_FIRST_LEG, terminal_state)
    if reconciled_at is not None:
        states = (*states, ShadowState.RECONCILED)
    result: list[ShadowEvent] = []
    for sequence, state in enumerate(states):
        occurred_at = terminal_at - timedelta(seconds=len(states) - sequence - 1)
        if state is ShadowState.RECONCILED:
            occurred_at = reconciled_at
        result.append(
            ShadowEvent(
                schema_version=1,
                event_id=UUID(int=plan.proposal_id.int * 100 + sequence + 1),
                proposal_id=plan.proposal_id,
                sequence=sequence,
                from_state=None if sequence == 0 else states[sequence - 1],
                to_state=state,
                occurred_at=occurred_at,
                detail=f"transition to {state.value}",
                quantity_filled=None,
                leg_index=None,
                scenario_id=scenario_id if sequence >= 4 else None,
            )
        )
    return tuple(result)


def append_plan_and_events(
    store: PredictionMarketStore,
    plan: ShadowPlan,
    events: tuple[ShadowEvent, ...],
) -> None:
    store.append_shadow_plan(plan)
    for event in events:
        store.append_shadow_event(event)


def shadow_reconciliation(
    plan: ShadowPlan,
    terminal_event: ShadowEvent,
    *,
    observed_at: datetime,
    complete: bool = True,
) -> ShadowReconciliation:
    return ShadowReconciliation(
        reconciliation_id=UUID(int=plan.proposal_id.int * 1000 + 1),
        proposal_id=plan.proposal_id,
        terminal_event_id=terminal_event.event_id,
        terminal_state=terminal_event.to_state,
        venues_reconciled=(PredictionVenue.KALSHI, PredictionVenue.POLYMARKET) if complete else (),
        complete=complete,
        unexplained_difference_usd=Decimal("0"),
        observed_at=observed_at,
    )


def shadow_experiment(
    plan: ShadowPlan,
    *,
    observed_at: datetime,
    terminal_state: ShadowState,
    paper_pnl_usd: Decimal | None,
    reconciled: bool,
    family_id: str = "cross-venue-equivalence-v1",
    scenario_id: str = "baseline",
    experiment_id: UUID | None = None,
) -> ShadowExperiment:
    return ShadowExperiment(
        experiment_id=experiment_id or UUID(int=plan.proposal_id.int * 1000 + 2),
        family_id=family_id,
        proposal_id=plan.proposal_id,
        scenario_id=scenario_id,
        terminal_state=terminal_state,
        paper_pnl_usd=paper_pnl_usd,
        reconciled=reconciled,
        as_of=observed_at,
        observed_at=observed_at,
    )


def test_shadow_summary_accepts_a_strict_consistent_reconciled_snapshot() -> None:
    summary = shadow_summary()

    assert summary.latest[0].current_state == "reconciled"
    assert summary.latest[0].paper_pnl == Decimal("-2.50")
    with pytest.raises(ValidationError):
        summary.proposals_total = 2


def test_shadow_models_reject_coercible_python_values() -> None:
    with pytest.raises(ValidationError):
        shadow_listing(quantity="10")
    with pytest.raises(ValidationError):
        shadow_summary(proposals_total="1")
    with pytest.raises(ValidationError):
        shadow_summary(latest=[shadow_listing()])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("proposals_total", -1),
        ("by_terminal_state", {"reconciled": -1}),
        ("reconciled_count", -1),
        ("unreconciled_count", -1),
        ("experiments_by_family", {"family": -1}),
    ],
)
def test_shadow_summary_rejects_negative_counts(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        shadow_summary(**{field: value})


def test_shadow_summary_rejects_inconsistent_totals_and_reconciled_pnl_rows() -> None:
    unreconciled = shadow_listing(
        current_state=ShadowState.UNKNOWN, paper_pnl=None, proposal_id=UUID(int=2)
    )

    with pytest.raises(ValidationError):
        shadow_summary(proposals_total=2)
    with pytest.raises(ValidationError):
        shadow_summary(latest=(unreconciled,))
    with pytest.raises(ValidationError):
        shadow_summary(
            proposals_total=1,
            by_terminal_state={"unknown": 1},
            reconciled_count=0,
            unreconciled_count=1,
            latest=(unreconciled.model_copy(update={"paper_pnl": Decimal("0")}),),
        )
    with pytest.raises(ValidationError):
        shadow_summary(reconciled_paper_pnl_usd=Decimal("0"))
    with pytest.raises(ValidationError):
        shadow_summary(
            proposals_total=1,
            by_terminal_state={"unknown": 1},
            reconciled_count=0,
            reconciled_paper_pnl_usd=Decimal("1"),
            unreconciled_count=1,
            latest=(unreconciled,),
        )


def test_shadow_summary_rejects_unknown_current_state_count_keys() -> None:
    with pytest.raises(ValidationError):
        shadow_summary(
            by_terminal_state={"not-a-shadow-state": 1},
            reconciled_count=0,
            reconciled_paper_pnl_usd=Decimal("0"),
            unreconciled_count=1,
            latest=(),
        )


def test_shadow_summary_requires_sorted_mappings_and_newest_first_unique_latest_rows() -> None:
    older = shadow_listing(
        proposal_id=UUID(int=1),
        candidate_id=UUID(int=201),
        observed_at=NOW - timedelta(seconds=1),
    )
    newer = shadow_listing(proposal_id=UUID(int=2), candidate_id=UUID(int=202))

    with pytest.raises(ValidationError):
        shadow_summary(by_terminal_state={"unknown": 0, "reconciled": 1})
    with pytest.raises(ValidationError):
        shadow_summary(latest=(older, newer))
    with pytest.raises(ValidationError):
        shadow_summary(latest=(newer, newer))


def test_shadow_summary_rejects_more_than_twenty_latest_rows() -> None:
    listings = tuple(
        shadow_listing(
            proposal_id=UUID(int=index + 1),
            candidate_id=UUID(int=100 + index),
            observed_at=NOW - timedelta(seconds=index),
        )
        for index in range(21)
    )
    with pytest.raises(ValidationError):
        shadow_summary(latest=listings)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_shadow_models_reject_nonfinite_decimals(value: Decimal) -> None:
    with pytest.raises(ValidationError):
        shadow_listing(quantity=value)
    with pytest.raises(ValidationError):
        shadow_listing(paper_pnl=value)
    with pytest.raises(ValidationError):
        shadow_summary(reconciled_paper_pnl_usd=value)


def test_shadow_listing_normalizes_aware_observed_at_and_rejects_naive_time() -> None:
    eastern = NOW.astimezone(timezone(timedelta(hours=-4)))
    assert shadow_listing(observed_at=eastern).observed_at == NOW

    with pytest.raises(ValidationError):
        shadow_listing(observed_at=NOW.replace(tzinfo=None))


def test_snapshot_shadow_summary_is_empty_when_no_plans_or_experiments_exist(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")

    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    summary = snapshot.shadow

    assert summary.proposals_total == 0
    assert summary.by_terminal_state == {}
    assert summary.reconciled_count == 0
    assert summary.reconciled_paper_pnl_usd == Decimal("0")
    assert summary.unreconciled_count == 0
    assert summary.latest == ()
    assert summary.experiments_by_family == {}


def test_snapshot_shadow_summary_uses_each_cutoff_safe_event_derived_state(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    states = (
        ShadowState.COMPLETE,
        ShadowState.EXPIRED,
        ShadowState.UNKNOWN,
        ShadowState.UNWOUND,
    )
    for index, state in enumerate(states, start=1):
        plan = shadow_plan(
            proposal_id=UUID(int=index),
            observed_at=NOW - timedelta(minutes=20 + index),
        )
        append_plan_and_events(
            store,
            plan,
            shadow_events(
                plan,
                state,
                terminal_at=NOW - timedelta(minutes=index),
                scenario_id=f"scenario-{index}",
            ),
        )

    summary = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW).shadow

    assert summary.proposals_total == 4
    assert summary.by_terminal_state == {
        "complete": 1,
        "expired": 1,
        "unknown": 1,
        "unwound": 1,
    }
    assert summary.reconciled_count == 0
    assert summary.unreconciled_count == 4
    assert [item.current_state for item in summary.latest] == list(states)
    assert [item.scenario_id for item in summary.latest] == [
        "scenario-1",
        "scenario-2",
        "scenario-3",
        "scenario-4",
    ]
    assert all(item.paper_pnl is None for item in summary.latest)


def test_snapshot_shadow_state_stops_at_the_latest_event_at_or_before_cutoff(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan = shadow_plan(observed_at=NOW - timedelta(minutes=10))
    events = shadow_events(plan, ShadowState.COMPLETE, terminal_at=NOW + timedelta(seconds=1))
    append_plan_and_events(store, plan, events)

    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    listing = snapshot.shadow.latest[0]

    assert listing.current_state is ShadowState.FIRST_LEG_SIMULATED
    assert listing.observed_at == NOW
    assert listing.scenario_id == "baseline"


def test_snapshot_shadow_omits_plans_not_known_by_the_cutoff(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan = shadow_plan(observed_at=NOW + timedelta(seconds=1))
    append_plan_and_events(
        store,
        plan,
        shadow_events(plan, ShadowState.UNKNOWN, terminal_at=NOW + timedelta(seconds=7)),
    )

    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    assert snapshot.shadow.latest == ()


def test_snapshot_shadow_exposes_pnl_only_from_complete_reconciled_evidence(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    expected_pnls = (Decimal("-2.50"), Decimal("0"))
    for index, pnl in enumerate(expected_pnls, start=1):
        plan = shadow_plan(
            proposal_id=UUID(int=index),
            observed_at=NOW - timedelta(minutes=20 + index),
        )
        reconciled_at = NOW - timedelta(minutes=index)
        events = shadow_events(
            plan,
            ShadowState.COMPLETE if index == 1 else ShadowState.UNWOUND,
            terminal_at=reconciled_at,
            reconciled_at=reconciled_at,
        )
        append_plan_and_events(store, plan, events)
        store.append_reconciliation(
            shadow_reconciliation(plan, events[-2], observed_at=reconciled_at)
        )
        store.append_shadow_experiment(
            shadow_experiment(
                plan,
                observed_at=reconciled_at,
                terminal_state=ShadowState.RECONCILED,
                paper_pnl_usd=pnl,
                reconciled=True,
            )
        )

    unknown_plan = shadow_plan(proposal_id=UUID(int=3), observed_at=NOW - timedelta(minutes=23))
    unknown_events = shadow_events(
        unknown_plan, ShadowState.UNKNOWN, terminal_at=NOW - timedelta(minutes=3)
    )
    append_plan_and_events(store, unknown_plan, unknown_events)
    store.append_reconciliation(
        shadow_reconciliation(
            unknown_plan,
            unknown_events[-1],
            observed_at=unknown_events[-1].occurred_at,
            complete=False,
        )
    )
    store.append_shadow_experiment(
        shadow_experiment(
            unknown_plan,
            observed_at=unknown_events[-1].occurred_at,
            terminal_state=ShadowState.UNKNOWN,
            paper_pnl_usd=None,
            reconciled=False,
            family_id="unknown-outcomes-v1",
        )
    )

    snapshot = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)
    summary = snapshot.shadow

    assert summary.reconciled_count == 2
    assert summary.unreconciled_count == 1
    assert summary.reconciled_paper_pnl_usd == Decimal("-2.50")
    assert summary.experiments_by_family == {
        "cross-venue-equivalence-v1": 2,
        "unknown-outcomes-v1": 1,
    }
    listings = {item.proposal_id: item for item in summary.latest}
    assert listings[UUID(int=1)].paper_pnl == Decimal("-2.50")
    assert listings[UUID(int=2)].paper_pnl == Decimal("0")
    assert listings[UUID(int=3)].current_state is ShadowState.UNKNOWN
    assert listings[UUID(int=3)].paper_pnl is None
    document = json.loads(render_prediction_dashboard_json(snapshot))
    serialized = {item["proposal_id"]: item for item in document["shadow"]["latest"]}
    assert serialized[str(UUID(int=1))]["paper_pnl"] == "-2.50"
    assert serialized[str(UUID(int=2))]["paper_pnl"] == "0"
    assert serialized[str(UUID(int=3))]["paper_pnl"] is None


def test_snapshot_shadow_does_not_leak_future_reconciliation_or_experiment(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan = shadow_plan(observed_at=NOW - timedelta(minutes=10))
    reconciled_at = NOW + timedelta(seconds=1)
    events = shadow_events(
        plan,
        ShadowState.COMPLETE,
        terminal_at=NOW,
        reconciled_at=reconciled_at,
    )
    append_plan_and_events(store, plan, events)
    store.append_reconciliation(shadow_reconciliation(plan, events[-2], observed_at=reconciled_at))
    store.append_shadow_experiment(
        shadow_experiment(
            plan,
            observed_at=reconciled_at,
            terminal_state=ShadowState.RECONCILED,
            paper_pnl_usd=Decimal("4.00"),
            reconciled=True,
        )
    )

    summary = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW).shadow

    assert summary.by_terminal_state == {"complete": 1}
    assert summary.reconciled_count == 0
    assert summary.reconciled_paper_pnl_usd == Decimal("0")
    assert summary.latest[0].paper_pnl is None
    assert summary.experiments_by_family == {}


def test_snapshot_shadow_latest_is_newest_first_capped_at_twenty_with_uuid_ties(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    for index in range(1, 26):
        plan = shadow_plan(proposal_id=UUID(int=index), observed_at=NOW - timedelta(minutes=30))
        append_plan_and_events(
            store,
            plan,
            shadow_events(plan, ShadowState.UNKNOWN, terminal_at=NOW),
        )

    summary = PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW).shadow

    assert summary.proposals_total == 25
    assert len(summary.latest) == 20
    assert [item.proposal_id for item in summary.latest] == [
        UUID(int=index) for index in range(25, 5, -1)
    ]


def test_snapshot_shadow_fails_closed_on_missing_or_noncontiguous_event_chains(
    tmp_path: Path,
) -> None:
    missing_store = PredictionMarketStore(tmp_path / "missing.duckdb")
    missing_store.append_shadow_plan(shadow_plan())
    with pytest.raises(ValueError, match="missing its event chain"):
        PredictionDashboardBuilder(missing_store, tmp_path / "missing.duckdb").build(NOW)

    gap_store = PredictionMarketStore(tmp_path / "gap.duckdb")
    plan = shadow_plan()
    events = shadow_events(plan, ShadowState.UNKNOWN, terminal_at=NOW)
    gap_store.append_shadow_plan(plan)
    gap_store.append_shadow_event(events[0])
    gap_store.append_shadow_event(events[2])
    with pytest.raises(ValueError, match="contiguous"):
        PredictionDashboardBuilder(gap_store, tmp_path / "gap.duckdb").build(NOW)

    chronology_store = PredictionMarketStore(tmp_path / "chronology.duckdb")
    plan = shadow_plan()
    events = shadow_events(plan, ShadowState.UNKNOWN, terminal_at=NOW)
    chronology_store.append_shadow_plan(plan)
    for event in events:
        chronology_store.append_shadow_event(
            event.model_copy(update={"occurred_at": NOW - timedelta(hours=1)})
            if event.sequence == 2
            else event
        )
    with pytest.raises(ValueError, match="chronological"):
        PredictionDashboardBuilder(chronology_store, tmp_path / "chronology.duckdb").build(NOW)


def test_snapshot_shadow_requires_scenario_identity_on_execution_events(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan = shadow_plan()
    events = shadow_events(plan, ShadowState.UNKNOWN, terminal_at=NOW)
    append_plan_and_events(
        store,
        plan,
        tuple(
            event.model_copy(update={"scenario_id": None}) if event.sequence >= 4 else event
            for event in events
        ),
    )

    with pytest.raises(ValueError, match="execution events require one scenario"):
        PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)


def test_snapshot_shadow_fails_closed_on_multiple_reconciliations_or_experiments(
    tmp_path: Path,
) -> None:
    reconciliation_store = PredictionMarketStore(tmp_path / "reconciliations.duckdb")
    plan = shadow_plan()
    events = shadow_events(plan, ShadowState.COMPLETE, terminal_at=NOW, reconciled_at=NOW)
    append_plan_and_events(reconciliation_store, plan, events)
    reconciliation = shadow_reconciliation(plan, events[-2], observed_at=NOW)
    reconciliation_store.append_reconciliation(reconciliation)
    reconciliation_store.append_reconciliation(
        reconciliation.model_copy(update={"reconciliation_id": UUID(int=999)})
    )
    with pytest.raises(ValueError, match="multiple shadow reconciliations"):
        PredictionDashboardBuilder(reconciliation_store, tmp_path / "reconciliations.duckdb").build(
            NOW
        )

    experiment_store = PredictionMarketStore(tmp_path / "experiments.duckdb")
    plan = shadow_plan()
    events = shadow_events(plan, ShadowState.UNKNOWN, terminal_at=NOW)
    append_plan_and_events(experiment_store, plan, events)
    reconciliation = shadow_reconciliation(plan, events[-1], observed_at=NOW, complete=False)
    experiment_store.append_reconciliation(reconciliation)
    experiment = shadow_experiment(
        plan,
        observed_at=NOW,
        terminal_state=ShadowState.UNKNOWN,
        paper_pnl_usd=None,
        reconciled=False,
    )
    experiment_store.append_shadow_experiment(experiment)
    experiment_store.append_shadow_experiment(
        experiment.model_copy(update={"experiment_id": UUID(int=998)})
    )
    with pytest.raises(ValueError, match="multiple shadow experiments"):
        PredictionDashboardBuilder(experiment_store, tmp_path / "experiments.duckdb").build(NOW)


def test_snapshot_shadow_fails_closed_on_inconsistent_reconciled_result_evidence(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan = shadow_plan()
    events = shadow_events(plan, ShadowState.COMPLETE, terminal_at=NOW, reconciled_at=NOW)
    append_plan_and_events(store, plan, events)
    store.append_reconciliation(
        shadow_reconciliation(plan, events[-2], observed_at=NOW).model_copy(
            update={"terminal_event_id": UUID(int=987)}
        )
    )
    store.append_shadow_experiment(
        shadow_experiment(
            plan,
            observed_at=NOW,
            terminal_state=ShadowState.RECONCILED,
            paper_pnl_usd=Decimal("12.00"),
            reconciled=True,
        )
    )

    with pytest.raises(ValueError, match="result evidence is inconsistent"):
        PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)


def test_snapshot_shadow_uses_verified_plan_and_experiment_records(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan = shadow_plan()
    append_plan_and_events(store, plan, shadow_events(plan, ShadowState.UNKNOWN, terminal_at=NOW))
    store._connection.execute(
        "UPDATE shadow_plans SET record_hash = ? WHERE proposal_id = ?",
        ["0" * 64, plan.proposal_id],
    )

    with pytest.raises(ValueError, match="immutable record hash"):
        PredictionDashboardBuilder(store, tmp_path / "predictions.duckdb").build(NOW)


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


def test_snapshot_recipes_include_shadow_run_and_replay_examples(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)

    recipes = PredictionDashboardBuilder(store, database).build(NOW).recipes.recipes

    assert (
        f"polytrading predictions shadow run --db {database} "
        "--trial-family <trial-family> --format json"
    ) in recipes
    assert (
        f"polytrading predictions shadow replay --db {database} "
        "--proposal-id <proposal-id> --format json"
    ) in recipes


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
