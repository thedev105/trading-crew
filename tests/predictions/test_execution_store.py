from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb
import pytest
from pydantic import ValidationError

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.models import (
    ActivationEvidence,
    ExecutionIntent,
    KillSwitchEvent,
    LiveExecutionPlan,
    LiveLedgerPosting,
    LiveReconciliation,
    ProtocolConformanceResult,
    SignedOrderEnvelope,
    VenueOrderEvent,
    VenueOrderState,
    VenueTradeEvent,
    VenueTradeState,
)
from polytrading.predictions.storage.store import ConflictingRecordError, PredictionMarketStore
from tests.predictions.execution_helpers import (
    execution_intent_fields,
    live_execution_plan_fields,
    public_unsigned_order_json,
)
from tests.predictions.store_helpers import raw_envelope

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)


def execution_records() -> tuple[
    LiveExecutionPlan,
    ExecutionIntent,
    SignedOrderEnvelope,
    VenueOrderEvent,
    VenueTradeEvent,
    LiveLedgerPosting,
    LiveReconciliation,
    KillSwitchEvent,
    ActivationEvidence,
    ProtocolConformanceResult,
]:
    plan = LiveExecutionPlan.model_validate(live_execution_plan_fields())
    intent = ExecutionIntent.model_validate(execution_intent_fields(plan_id=plan.plan_id))
    envelope = SignedOrderEnvelope.model_validate(
        {
            "schema_version": 1,
            "intent_id": intent.intent_id,
            "intent_fingerprint": intent.intent_fingerprint,
            "protocol_version": intent.protocol_version,
            "salt": 1,
            "signature_type": 0,
            "public_signature": "0xdeadbeef",
            "domain_fingerprint": "1" * 64,
            "exact_body_hash": "2" * 64,
            "order_fingerprint": "3" * 64,
            "signer_version": "v1",
            "canonical_order_json": public_unsigned_order_json(),
        }
    )
    order_event = VenueOrderEvent.model_validate(
        {
            "schema_version": 1,
            "event_id": UUID("11111111-1111-1111-1111-111111111111"),
            "venue": PredictionVenue.POLYMARKET,
            "raw_event_hash": "4" * 64,
            "source_channel": "orders",
            "venue_order_id": "order-1",
            "intent_id": intent.intent_id,
            "original_venue_state": "matched",
            "normalized_state": VenueOrderState.ACK_MATCHED,
            "terminal": False,
            "venue_timestamp": NOW,
            "received_at": NOW,
            "sequence_number": 1,
            "protocol_version": intent.protocol_version,
        }
    )
    trade_event = VenueTradeEvent.model_validate(
        {
            "schema_version": 1,
            "trade_event_id": UUID("22222222-2222-2222-2222-222222222222"),
            "venue": PredictionVenue.POLYMARKET,
            "raw_event_hash": "5" * 64,
            "source_channel": "trades",
            "venue_trade_id": "trade-1",
            "venue_order_id": order_event.venue_order_id,
            "intent_id": intent.intent_id,
            "original_venue_state": "matched",
            "normalized_state": VenueTradeState.MATCHED,
            "terminal": False,
            "venue_timestamp": NOW,
            "received_at": NOW,
            "sequence_number": 1,
            "protocol_version": intent.protocol_version,
        }
    )
    posting = LiveLedgerPosting.model_validate(
        {
            "schema_version": 1,
            "posting_id": UUID("33333333-3333-3333-3333-333333333333"),
            "account_fingerprint": plan.account_fingerprint,
            "intent_id": intent.intent_id,
            "venue_order_id": order_event.venue_order_id,
            "venue_trade_id": trade_event.venue_trade_id,
            "settlement_hash": None,
            "fee_hash": None,
            "balance_evidence_hashes": ("6" * 64,),
            "debit_account": "cash",
            "credit_account": "clearing",
            "asset_id": "USDC",
            "debit_amount": Decimal("1"),
            "credit_amount": Decimal("0"),
            "occurred_at": NOW,
        }
    )
    reconciliation = LiveReconciliation.model_validate(
        {
            "schema_version": 1,
            "reconciliation_id": UUID("44444444-4444-4444-4444-444444444444"),
            "account_fingerprint": plan.account_fingerprint,
            "observed_at": NOW,
            "complete": True,
            "differences": (),
            "evidence_hashes": ("7" * 64,),
            "next_action": None,
        }
    )
    kill_event = KillSwitchEvent.model_validate(
        {
            "schema_version": 1,
            "kill_event_id": UUID("55555555-5555-5555-5555-555555555555"),
            "trigger": "manual",
            "scope": "account",
            "source_intent_id": intent.intent_id,
            "source_order_id": order_event.venue_order_id,
            "prior_state": False,
            "occurred_at": NOW,
        }
    )
    activation = ActivationEvidence.model_validate(
        {
            "schema_version": 1,
            "activation_evidence_id": UUID("66666666-6666-6666-6666-666666666666"),
            "capability_digest": "8" * 64,
            "manifest_digest": "9" * 64,
            "verifier_result": True,
            "verified_at": NOW,
            "expires_at": NOW + timedelta(minutes=1),
        }
    )
    conformance = ProtocolConformanceResult.model_validate(
        {
            "schema_version": 1,
            "conformance_result_id": UUID("77777777-7777-7777-7777-777777777777"),
            "fixture_hashes": ("a" * 64,),
            "source_hashes": ("b" * 64,),
            "implementation_revision": "abc123",
            "executed_checks": ("encoding",),
            "result": "passed",
            "observed_at": NOW,
        }
    )
    return (
        plan,
        intent,
        envelope,
        order_event,
        trade_event,
        posting,
        reconciliation,
        kill_event,
        activation,
        conformance,
    )


def test_migration_008_creates_all_execution_tables(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    names = {
        row[0]
        for row in store._connection.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert {
        "live_execution_plans",
        "execution_intents",
        "signed_order_envelopes",
        "venue_order_events",
        "venue_trade_events",
        "live_ledger_postings",
        "live_reconciliations",
        "execution_kill_events",
        "activation_evidence",
        "protocol_conformance_results",
    } <= names


def test_intent_retry_is_idempotent_but_conflicting_content_fails(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    intent = ExecutionIntent.model_validate(execution_intent_fields())

    assert store.append_execution_intent(intent)
    assert not store.append_execution_intent(intent)
    with pytest.raises(ConflictingRecordError):
        store.append_execution_intent(intent.model_copy(update={"limit_price": Decimal("0.52")}))


def test_verified_execution_records_round_trip_through_named_queries(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    (
        plan,
        intent,
        envelope,
        order_event,
        trade_event,
        posting,
        reconciliation,
        kill_event,
        activation,
        conformance,
    ) = execution_records()

    assert store.append_live_execution_plan(plan)
    assert store.append_execution_intent(intent)
    assert store.append_signed_order_envelope(envelope)
    assert store.append_venue_order_event(order_event)
    assert store.append_venue_trade_event(trade_event)
    assert store.append_live_ledger_posting(posting)
    assert store.append_live_reconciliation(reconciliation)
    assert store.append_kill_switch_event(kill_event)
    assert store.append_activation_evidence(activation)
    assert store.append_protocol_conformance_result(conformance)

    assert store.verified_live_execution_plan(plan.plan_id, NOW) == plan
    assert store.verified_execution_intent(intent.intent_id, NOW) == intent
    assert store.verified_execution_intents_for_plan(plan.plan_id, NOW) == (intent,)
    assert store.verified_signed_order_envelope(intent.intent_id) == envelope
    assert store.verified_venue_order_events_for_intent(intent.intent_id, NOW) == (order_event,)
    assert store.verified_venue_trade_events_for_intent(intent.intent_id, NOW) == (trade_event,)
    assert store.latest_order_state(intent.intent_id, NOW) == order_event
    assert store.verified_live_ledger_postings_for_account(plan.account_fingerprint, NOW) == (
        posting,
    )
    assert store.verified_live_reconciliations_for_account(plan.account_fingerprint, NOW) == (
        reconciliation,
    )
    assert store.verified_kill_switch_events(kill_event.scope, NOW) == (kill_event,)
    assert store.verified_activation_evidence(activation.capability_digest, NOW) == activation
    assert store.verified_protocol_conformance_results(NOW) == (conformance,)


def test_execution_transaction_rolls_back_plan_intent_and_event_together(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan, intent, _, order_event, *_ = execution_records()

    with pytest.raises(RuntimeError, match="abort"), store.transaction():
        store.append_live_execution_plan(plan)
        store.append_execution_intent(intent)
        store.append_venue_order_event(order_event)
        raise RuntimeError("abort")

    assert store.verified_live_execution_plan(plan.plan_id) is None
    assert store.verified_execution_intent(intent.intent_id) is None
    assert store.verified_venue_order_events_for_intent(intent.intent_id, NOW) == ()


def test_verified_execution_reads_exclude_later_events_and_expired_intents(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    _, intent, _, order_event, *_ = execution_records()
    later = order_event.model_copy(
        update={
            "event_id": UUID("88888888-8888-8888-8888-888888888888"),
            "received_at": NOW + timedelta(seconds=1),
            "sequence_number": 2,
        }
    )
    store.append_execution_intent(intent)
    store.append_venue_order_event(order_event)
    store.append_venue_order_event(later)

    assert store.verified_venue_order_events_for_intent(intent.intent_id, NOW) == (order_event,)
    assert (
        store.verified_execution_intent(
            intent.intent_id, intent.deadline + timedelta(microseconds=1)
        )
        is None
    )
    store._connection.execute(
        "UPDATE execution_intents SET created_at = ? WHERE intent_id = ?",
        [NOW - timedelta(seconds=1), intent.intent_id],
    )
    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_execution_intent(intent.intent_id, NOW - timedelta(microseconds=1))


def test_existing_migration_007_database_upgrades_without_changing_prior_hashes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "predictions.duckdb"
    with duckdb.connect(str(path)) as connection:
        for version, migration in PredictionMarketStore._migration_entries()[:7]:
            connection.execute(migration.read_text(encoding="utf-8"))
            connection.execute("INSERT INTO schema_migrations VALUES (?, ?)", [version, NOW])
        envelope = raw_envelope()
        connection.execute(
            "INSERT INTO prediction_raw_envelopes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                envelope.event_id,
                envelope.venue.value,
                envelope.endpoint,
                envelope.venue_timestamp,
                envelope.observed_at,
                envelope.received_monotonic_ns,
                envelope.request_latency_ms,
                envelope.source_version,
                envelope.payload_json,
                envelope.source_hash,
                envelope.schema_version,
            ],
        )
        prior_hash = envelope.source_hash

    store = PredictionMarketStore(path)
    assert store._connection.execute(
        "SELECT source_hash FROM prediction_raw_envelopes WHERE event_id = ?", [envelope.event_id]
    ).fetchone() == (prior_hash,)
    versions = store._connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    assert versions[-1] == (8,)


def test_reopened_read_only_store_verifies_execution_readiness(tmp_path: Path) -> None:
    path = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(path)
    plan, *_ = execution_records()
    store.append_live_execution_plan(plan)
    store.close()

    read_only = PredictionMarketStore(path, read_only=True)
    assert read_only.verified_live_execution_plan(plan.plan_id, NOW) == plan
    read_only.close()


def test_execution_record_json_never_contains_unmodeled_secret_canaries(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    _, intent, *_ = execution_records()
    canary = "private-key-canary-clob-api-secret-passphrase-auth-header"
    unsafe = ExecutionIntent.model_construct(**intent.model_dump(), raw_secret=canary)

    assert store.append_execution_intent(unsafe)
    stored_json = store._connection.execute(
        "SELECT record_json FROM execution_intents WHERE intent_id = ?", [intent.intent_id]
    ).fetchone()[0]
    assert canary not in stored_json


@pytest.mark.parametrize(
    ("append_method", "record_index", "field", "invalid_value", "table"),
    [
        (
            "append_live_execution_plan",
            0,
            "observed_at",
            datetime(2026, 8, 25, 16),
            "live_execution_plans",
        ),
        (
            "append_execution_intent",
            1,
            "created_at",
            datetime(2026, 8, 25, 16),
            "execution_intents",
        ),
        (
            "append_signed_order_envelope",
            2,
            "canonical_order_json",
            '{"private_key":"private-key-canary"}',
            "signed_order_envelopes",
        ),
        (
            "append_venue_order_event",
            3,
            "received_at",
            datetime(2026, 8, 25, 16),
            "venue_order_events",
        ),
        (
            "append_venue_trade_event",
            4,
            "received_at",
            datetime(2026, 8, 25, 16),
            "venue_trade_events",
        ),
        ("append_live_ledger_posting", 5, "debit_amount", Decimal("-1"), "live_ledger_postings"),
        (
            "append_live_reconciliation",
            6,
            "observed_at",
            datetime(2026, 8, 25, 16),
            "live_reconciliations",
        ),
        (
            "append_kill_switch_event",
            7,
            "occurred_at",
            datetime(2026, 8, 25, 16),
            "execution_kill_events",
        ),
        (
            "append_activation_evidence",
            8,
            "verified_at",
            datetime(2026, 8, 25, 16),
            "activation_evidence",
        ),
        (
            "append_protocol_conformance_result",
            9,
            "observed_at",
            datetime(2026, 8, 25, 16),
            "protocol_conformance_results",
        ),
    ],
)
def test_execution_appends_revalidate_constructed_models(
    append_method: str,
    record_index: int,
    field: str,
    invalid_value: object,
    table: str,
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    record = execution_records()[record_index]
    fields = record.model_dump()
    fields[field] = invalid_value
    constructed = type(record).model_construct(**fields)

    with pytest.raises(ValidationError):
        getattr(store, append_method)(constructed)
    assert store._connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)


def test_verified_execution_intent_rejects_a_corrupted_indexed_identity(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    _, intent, *_ = execution_records()
    store.append_execution_intent(intent)
    store._connection.execute(
        "UPDATE execution_intents SET intent_id = ? WHERE intent_id = ?",
        [UUID("99999999-9999-9999-9999-999999999999"), intent.intent_id],
    )

    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_execution_intent(intent.intent_id, NOW)


def test_order_history_uses_sequence_number_before_uuid_for_equal_timestamps(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    _, intent, _, first, *_ = execution_records()
    first = first.model_copy(update={"event_id": UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")})
    second = first.model_copy(
        update={
            "event_id": UUID("00000000-0000-0000-0000-000000000001"),
            "sequence_number": 2,
            "normalized_state": VenueOrderState.FILLED,
            "terminal": True,
        }
    )
    store.append_venue_order_event(first)
    store.append_venue_order_event(second)

    assert store.verified_venue_order_events_for_intent(intent.intent_id, NOW) == (first, second)
    assert store.latest_order_state(intent.intent_id, NOW) == second


def test_verified_execution_plans_for_account_isolated_cutoff_ordered_and_verified(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    first, *_ = execution_records()
    second = first.model_copy(
        update={
            "plan_id": UUID("00000000-0000-4000-8000-000000000002"),
            "observed_at": NOW,
        }
    )
    other = first.model_copy(
        update={
            "plan_id": UUID("00000000-0000-4000-8000-000000000003"),
            "account_fingerprint": "9" * 64,
        }
    )
    future = first.model_copy(
        update={
            "plan_id": UUID("00000000-0000-4000-8000-000000000004"),
            "observed_at": NOW + timedelta(seconds=1),
            "information_cutoff": NOW + timedelta(seconds=1),
            "book_deadline": NOW + timedelta(seconds=2),
            "proof_deadline": NOW + timedelta(seconds=2),
            "economics_deadline": NOW + timedelta(seconds=2),
            "account_deadline": NOW + timedelta(seconds=2),
            "geoblock_deadline": NOW + timedelta(seconds=2),
        }
    )
    for plan in (first, second, other, future):
        store.append_live_execution_plan(plan)

    expected = tuple(sorted((first, second), key=lambda plan: (plan.observed_at, plan.plan_id)))
    assert (
        store.verified_live_execution_plans_for_account(first.account_fingerprint, NOW) == expected
    )
    assert store.verified_live_execution_plans_for_account(other.account_fingerprint, NOW) == (
        other,
    )

    store._connection.execute(
        "UPDATE live_execution_plans SET account_fingerprint = ? WHERE plan_id = ?",
        ["8" * 64, first.plan_id],
    )
    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_live_execution_plans_for_account(first.account_fingerprint, NOW)


def test_execution_intent_history_includes_expired_but_excludes_future_and_other_plans(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan, intent, *_ = execution_records()
    expired_cutoff = intent.deadline + timedelta(seconds=1)
    future = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan.plan_id,
            created_at=expired_cutoff + timedelta(seconds=1),
            deadline=expired_cutoff + timedelta(seconds=2),
        )
    )
    other_plan = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=UUID("99999999-9999-4999-8999-999999999999"),
        )
    )
    for candidate in (intent, future, other_plan):
        store.append_execution_intent(candidate)

    assert store.verified_execution_intent_history_for_plan(
        plan.plan_id,
        expired_cutoff,
    ) == (intent,)


def test_execution_intent_history_orders_equal_times_by_intent_id_and_detects_corruption(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan, first, *_ = execution_records()
    second = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan.plan_id,
            leg_sequence=1,
            token_id=plan.token_ids[1],
            order_type=plan.leg_order_types[1],
            limit_price=plan.limit_prices[1],
            fee_rate_bps_cap=plan.fee_rate_bps_caps[1],
            created_at=first.created_at,
            deadline=first.deadline,
        )
    )
    store.append_execution_intent(first)
    store.append_execution_intent(second)
    expected = tuple(sorted((first, second), key=lambda item: (item.created_at, item.intent_id)))
    assert store.verified_execution_intent_history_for_plan(plan.plan_id, NOW) == expected

    store._connection.execute(
        "UPDATE execution_intents SET created_at = ? WHERE intent_id = ?",
        [NOW - timedelta(seconds=1), first.intent_id],
    )
    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_execution_intent_history_for_plan(plan.plan_id, NOW)


def test_account_plan_history_detects_indexed_time_corruption(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan, *_ = execution_records()
    store.append_live_execution_plan(plan)
    store._connection.execute(
        "UPDATE live_execution_plans SET observed_at = ? WHERE plan_id = ?",
        [plan.observed_at - timedelta(seconds=1), plan.plan_id],
    )

    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_live_execution_plans_for_account(
            plan.account_fingerprint,
            NOW,
        )


def test_execution_intent_history_detects_indexed_identity_corruption(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan, intent, *_ = execution_records()
    store.append_execution_intent(intent)
    store._connection.execute(
        "UPDATE execution_intents SET plan_id = ? WHERE intent_id = ?",
        [UUID("99999999-9999-4999-8999-999999999999"), intent.intent_id],
    )

    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_execution_intent_history_for_plan(plan.plan_id, NOW)
