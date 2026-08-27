from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Lock, Thread
from uuid import UUID

import duckdb
import pytest
from pydantic import ValidationError

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.ledger import (
    AuthoritativeTradeEconomics,
    postings_for_confirmed_trades,
)
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


def authoritative_economics(
    source_intent: ExecutionIntent,
    **overrides: object,
) -> AuthoritativeTradeEconomics:
    fields: dict[str, object] = {
        "schema_version": 1,
        "account_fingerprint": source_intent.account_fingerprint,
        "intent_id": source_intent.intent_id,
        "venue_order_id": "order-1",
        "venue_trade_id": "trade-1",
        "trade_event_hash": "5" * 64,
        "cash_asset_id": "USDC",
        "position_asset_id": source_intent.token_id,
        "side": source_intent.side,
        "price": Decimal("0.51"),
        "size": Decimal("10"),
        "fee": Decimal("0.01"),
        "cash_quantum": Decimal("0.000001"),
        "position_quantum": Decimal("0.01"),
        "trade_state": VenueTradeState.CONFIRMED,
        "settlement_state": VenueTradeState.CONFIRMED,
        "fee_hash": "3" * 64,
        "settlement_hash": "2" * 64,
        "source_hash": "4" * 64,
        "balance_evidence_hashes": ("6" * 64,),
        "occurred_at": NOW + timedelta(seconds=1),
        "information_cutoff": NOW + timedelta(seconds=3),
        "protocol_version": source_intent.protocol_version,
        "realized_pnl": None,
        "cost_basis_evidence_hash": None,
    }
    fields.update(overrides)
    return AuthoritativeTradeEconomics(**fields)


def test_durable_execution_claim_is_atomic_across_store_instances_and_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "claims.duckdb"
    first_store = PredictionMarketStore(path)
    second_store = PredictionMarketStore(path)
    _, intent, _, order_event, *_ = execution_records()
    order_event = order_event.model_copy(
        update={"normalized_state": VenueOrderState.PARTIALLY_FILLED}
    )
    start = Barrier(3)
    results: list[bool] = []
    result_lock = Lock()

    def claim(store: PredictionMarketStore) -> None:
        start.wait()
        acquired = store.claim_execution_intent_submission(intent, NOW)
        with result_lock:
            results.append(acquired)

    threads = (
        Thread(target=claim, args=(first_store,)),
        Thread(target=claim, args=(second_store,)),
    )
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
    assert first_store.claim_execution_intent_submission(intent, NOW) is False
    assert first_store.claim_execution_first_fill(intent, order_event, NOW) is True
    assert second_store.claim_execution_first_fill(intent, order_event, NOW) is False
    first_store.close()
    second_store.close()

    reopened = PredictionMarketStore(path)
    assert reopened.claim_execution_intent_submission(intent, NOW) is False
    assert reopened.claim_execution_first_fill(intent, order_event, NOW) is False
    reopened.close()


def test_execution_claim_rejects_same_key_with_different_occurrence(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "claims.duckdb")
    _, intent, _, order_event, *_ = execution_records()
    order_event = order_event.model_copy(
        update={"normalized_state": VenueOrderState.PARTIALLY_FILLED}
    )
    conflicting_event = order_event.model_copy(update={"raw_event_hash": "f" * 64})

    assert store.claim_execution_first_fill(intent, order_event, NOW)
    with pytest.raises(ConflictingRecordError, match="execution operation claim"):
        store.claim_execution_first_fill(intent, conflicting_event, NOW)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("record_hash", "0" * 64),
        ("account_fingerprint", "9" * 64),
        ("record_json", "{"),
    ],
)
def test_execution_claim_duplicate_detects_index_json_or_hash_corruption(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    store = PredictionMarketStore(tmp_path / "claims.duckdb")
    _, intent, *_ = execution_records()
    assert store.claim_execution_intent_submission(intent, NOW)
    store._connection.execute(
        f"UPDATE execution_operation_claims SET {column} = ?",
        [value],
    )

    with pytest.raises(ConflictingRecordError, match="execution operation claim"):
        store.claim_execution_intent_submission(intent, NOW)


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


def test_migration_010_creates_all_execution_tables(tmp_path: Path) -> None:
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
        "execution_operation_claims",
        "authoritative_trade_economics",
    } <= names


def test_authoritative_economics_round_trips_by_account_intent_and_cutoff(
    tmp_path: Path,
) -> None:
    path = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(path)
    _, source_intent, *_ = execution_records()
    exact = authoritative_economics(source_intent)

    assert store.append_authoritative_trade_economics(exact)
    assert not store.append_authoritative_trade_economics(exact)
    assert store.verified_authoritative_trade_economics_for_account(
        source_intent.account_fingerprint, exact.information_cutoff
    ) == (exact,)
    assert store.verified_authoritative_trade_economics_for_intent(
        source_intent.intent_id, exact.information_cutoff
    ) == (exact,)
    assert (
        store.verified_authoritative_trade_economics_for_account(
            source_intent.account_fingerprint,
            exact.information_cutoff - timedelta(microseconds=1),
        )
        == ()
    )
    store.close()

    reopened = PredictionMarketStore(path, read_only=True)
    assert reopened.verified_authoritative_trade_economics_for_account(
        source_intent.account_fingerprint, exact.information_cutoff
    ) == (exact,)
    reopened.close()


def test_authoritative_economics_conflict_for_same_trade_identity_fails_closed(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    _, source_intent, *_ = execution_records()
    exact = authoritative_economics(source_intent)
    conflicting = authoritative_economics(source_intent, source_hash="7" * 64)

    assert store.append_authoritative_trade_economics(exact)
    with pytest.raises(ConflictingRecordError, match="authoritative_trade_economics"):
        store.append_authoritative_trade_economics(conflicting)


def test_authoritative_economics_history_order_is_deterministic(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    _, source_intent, *_ = execution_records()
    later = authoritative_economics(
        source_intent,
        venue_trade_id="trade-2",
        trade_event_hash="7" * 64,
        source_hash="8" * 64,
        occurred_at=NOW + timedelta(seconds=2),
    )
    earlier = authoritative_economics(source_intent)

    store.append_authoritative_trade_economics(later)
    store.append_authoritative_trade_economics(earlier)

    assert store.verified_authoritative_trade_economics_for_intent(
        source_intent.intent_id, later.information_cutoff
    ) == (earlier, later)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("record_hash", "0" * 64),
        ("account_fingerprint", "9" * 64),
        ("record_json", "{"),
    ],
)
def test_authoritative_economics_verified_query_detects_corruption(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    _, source_intent, *_ = execution_records()
    exact = authoritative_economics(source_intent)
    store.append_authoritative_trade_economics(exact)
    store._connection.execute(
        f"UPDATE authoritative_trade_economics SET {column} = ?",
        [value],
    )

    with pytest.raises(ConflictingRecordError, match="authoritative_trade_economics"):
        store.verified_authoritative_trade_economics_for_account(
            source_intent.account_fingerprint, exact.information_cutoff
        )


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
    assert store.verified_execution_intent_history_for_account(intent.account_fingerprint, NOW) == (
        intent,
    )
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


def test_account_intent_history_finds_orphan_and_detects_index_corruption(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    intent = ExecutionIntent.model_validate(execution_intent_fields(created_at=NOW))
    assert store.append_execution_intent(intent)

    assert store.verified_execution_intent_history_for_account(
        intent.account_fingerprint,
        NOW,
    ) == (intent,)
    store._connection.execute(
        "UPDATE execution_intents SET account_fingerprint = ? WHERE intent_id = ?",
        ["9" * 64, intent.intent_id],
    )
    with pytest.raises(ConflictingRecordError, match="indexed columns"):
        store.verified_execution_intent_history_for_account(intent.account_fingerprint, NOW)


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
    assert versions[-1] == (10,)


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


def _relational_execution_records() -> tuple[
    LiveExecutionPlan,
    ExecutionIntent,
    AuthoritativeTradeEconomics,
    LiveLedgerPosting,
    LiveReconciliation,
]:
    plan = LiveExecutionPlan.model_validate(live_execution_plan_fields(observed_at=NOW))
    intent = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan.plan_id,
            account_fingerprint=plan.account_fingerprint,
            created_at=NOW,
        )
    )
    economics = authoritative_economics(intent)
    posting = execution_records()[5].model_copy(
        update={
            "account_fingerprint": plan.account_fingerprint,
            "intent_id": intent.intent_id,
            "venue_order_id": economics.venue_order_id,
            "venue_trade_id": economics.venue_trade_id,
            "occurred_at": NOW,
        }
    )
    reconciliation = execution_records()[6].model_copy(
        update={"account_fingerprint": plan.account_fingerprint, "observed_at": NOW}
    )
    return plan, intent, economics, posting, reconciliation


def test_verified_live_account_discovery_revalidates_every_account_bearing_family(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan, intent, order, trade, economics, postings, reconciliation = (
        _canonical_reconciliation_bundle()
    )
    store.append_live_execution_plan(plan)
    store.append_execution_intent(intent)
    store.append_venue_order_event(order)
    store.append_venue_trade_event(trade)
    store.append_authoritative_trade_economics(economics)
    for posting in postings:
        store.append_live_ledger_posting(posting)
    store.append_live_reconciliation(reconciliation)

    assert store.verified_live_execution_account_fingerprints(economics.information_cutoff) == (
        plan.account_fingerprint,
    )


def test_verified_live_account_discovery_excludes_a_complete_future_bundle(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    plan, _intent, _economics, posting, reconciliation = _relational_execution_records()
    future = NOW + timedelta(minutes=1)
    future_plan = plan.model_copy(
        update={
            "observed_at": future,
            "information_cutoff": future,
            "book_deadline": future + timedelta(seconds=5),
            "proof_deadline": future + timedelta(seconds=5),
            "economics_deadline": future + timedelta(seconds=5),
            "account_deadline": future + timedelta(seconds=5),
            "geoblock_deadline": future + timedelta(seconds=5),
        }
    )
    future_intent = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=future_plan.plan_id,
            account_fingerprint=future_plan.account_fingerprint,
            created_at=future,
            deadline=future + timedelta(seconds=5),
        )
    )
    future_economics = authoritative_economics(
        future_intent,
        occurred_at=future + timedelta(seconds=1),
        information_cutoff=future + timedelta(seconds=2),
    )
    store.append_live_execution_plan(future_plan)
    store.append_execution_intent(future_intent)
    store.append_authoritative_trade_economics(future_economics)
    store.append_live_ledger_posting(
        posting.model_copy(update={"intent_id": future_intent.intent_id, "occurred_at": future})
    )
    store.append_live_reconciliation(reconciliation.model_copy(update={"observed_at": future}))

    assert store.verified_live_execution_account_fingerprints(NOW) == ()


@pytest.mark.parametrize("orphan_kind", ["intent", "economics", "posting", "reconciliation"])
def test_verified_live_account_discovery_rejects_orphan_relational_evidence(
    orphan_kind: str,
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / f"orphan-{orphan_kind}.duckdb")
    _plan, intent, economics, posting, reconciliation = _relational_execution_records()
    append = {
        "intent": lambda: store.append_execution_intent(intent),
        "economics": lambda: store.append_authoritative_trade_economics(economics),
        "posting": lambda: store.append_live_ledger_posting(posting),
        "reconciliation": lambda: store.append_live_reconciliation(reconciliation),
    }
    append[orphan_kind]()

    with pytest.raises(ConflictingRecordError, match="orphan"):
        store.verified_live_execution_account_fingerprints(economics.information_cutoff)


def test_verified_live_account_discovery_rejects_cross_account_relations(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "cross-account.duckdb")
    plan, _intent, *_ = _relational_execution_records()
    mismatched = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan.plan_id,
            account_fingerprint="9" * 64,
            created_at=NOW,
        )
    )
    store.append_live_execution_plan(plan)
    store.append_execution_intent(mismatched)

    with pytest.raises(ConflictingRecordError, match="account"):
        store.verified_live_execution_account_fingerprints(NOW)


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("live_execution_plans", "account_fingerprint", "9" * 64),
        ("execution_intents", "record_hash", "0" * 64),
        ("authoritative_trade_economics", "intent_id", UUID(int=999)),
        ("live_ledger_postings", "occurred_at", NOW - timedelta(seconds=1)),
        ("live_reconciliations", "record_json", "{}"),
    ],
)
def test_verified_live_account_discovery_rejects_tampered_index_hash_or_json(
    table: str,
    column: str,
    value: object,
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / f"tampered-{table}.duckdb")
    plan, intent, economics, posting, reconciliation = _relational_execution_records()
    store.append_live_execution_plan(plan)
    store.append_execution_intent(intent)
    store.append_authoritative_trade_economics(economics)
    store.append_live_ledger_posting(posting)
    store.append_live_reconciliation(reconciliation)
    store._connection.execute(f"UPDATE {table} SET {column} = ?", [value])

    with pytest.raises(ConflictingRecordError):
        store.verified_live_execution_account_fingerprints(economics.information_cutoff)


def test_verified_dashboard_bulk_execution_cut_includes_every_record_family(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "dashboard-cut.duckdb")
    plan, intent, _envelope, order, trade, posting, reconciliation, kill, *_ = execution_records()
    economics = authoritative_economics(intent)
    kill = kill.model_copy(
        update={
            "scope": plan.account_fingerprint,
            "source_intent_id": intent.intent_id,
            "source_order_id": order.venue_order_id,
        }
    )
    for append, record in (
        (store.append_live_execution_plan, plan),
        (store.append_execution_intent, intent),
        (store.append_venue_order_event, order),
        (store.append_venue_trade_event, trade),
        (store.append_kill_switch_event, kill),
        (store.append_authoritative_trade_economics, economics),
        (store.append_live_ledger_posting, posting),
        (store.append_live_reconciliation, reconciliation),
    ):
        append(record)
    cutoff = economics.information_cutoff

    assert store.verified_live_execution_plans_as_of(cutoff) == (plan,)
    assert store.verified_execution_intent_history_as_of(cutoff) == (intent,)
    assert store.verified_venue_order_events_as_of(cutoff) == (order,)
    assert store.verified_venue_trade_events_as_of(cutoff) == (trade,)
    assert store.verified_kill_switch_events_as_of(cutoff) == (kill,)
    assert store.verified_authoritative_trade_economics_as_of(cutoff) == (economics,)
    assert store.verified_live_ledger_postings_as_of(cutoff) == (posting,)
    assert store.verified_live_reconciliations_as_of(cutoff) == (reconciliation,)


def test_verified_account_discovery_includes_account_kill_without_plan_but_not_global_kill(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "kill-discovery.duckdb")
    account = "a" * 64
    source = execution_records()[7]
    account_kill = source.model_copy(
        update={
            "scope": account,
            "source_intent_id": None,
            "source_order_id": None,
        }
    )
    global_kill = source.model_copy(
        update={
            "kill_event_id": UUID(int=999),
            "scope": "GLOBAL",
            "source_intent_id": None,
            "source_order_id": None,
        }
    )
    store.append_kill_switch_event(account_kill)
    store.append_kill_switch_event(global_kill)

    assert store.verified_live_execution_account_fingerprints(NOW) == (account,)
    assert store.verified_kill_switch_events_as_of(NOW) == tuple(
        sorted((account_kill, global_kill), key=lambda item: (item.occurred_at, item.kill_event_id))
    )


@pytest.mark.parametrize("kind", ["order", "trade"])
def test_verified_account_discovery_rejects_intentless_venue_events(
    kind: str,
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / f"intentless-{kind}.duckdb")
    order = execution_records()[3].model_copy(update={"intent_id": None})
    trade = execution_records()[4].model_copy(update={"intent_id": None})
    if kind == "order":
        store.append_venue_order_event(order)
    else:
        store.append_venue_trade_event(trade)

    with pytest.raises(ConflictingRecordError, match="intent"):
        store.verified_live_execution_account_fingerprints(NOW)


@pytest.mark.parametrize("bad_source", ["intent", "order"])
def test_verified_account_discovery_rejects_unresolved_kill_source_references(
    bad_source: str,
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / f"bad-kill-{bad_source}.duckdb")
    plan, intent, _envelope, order, *_records, kill, _activation, _conformance = execution_records()
    intent = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan.plan_id,
            account_fingerprint=plan.account_fingerprint,
        )
    )
    order = order.model_copy(update={"intent_id": intent.intent_id})
    store.append_live_execution_plan(plan)
    store.append_execution_intent(intent)
    store.append_venue_order_event(order)
    updates: dict[str, object] = {
        "scope": plan.account_fingerprint,
        "source_intent_id": intent.intent_id,
        "source_order_id": order.venue_order_id,
    }
    if bad_source == "intent":
        updates["source_intent_id"] = UUID(int=999_991)
        updates["source_order_id"] = None
    else:
        updates["source_order_id"] = "missing-order"
    store.append_kill_switch_event(kill.model_copy(update=updates))

    with pytest.raises(ConflictingRecordError, match=bad_source):
        store.verified_live_execution_account_fingerprints(NOW)


def test_verified_trade_requires_its_visible_source_order(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "orphan-trade-order.duckdb")
    plan, intent, _envelope, _order, trade, *_ = execution_records()
    store.append_live_execution_plan(plan)
    store.append_execution_intent(intent)
    store.append_venue_trade_event(trade)

    with pytest.raises(ConflictingRecordError, match="order"):
        store.verified_live_execution_account_fingerprints(NOW)


@pytest.mark.parametrize("family", ["order", "trade"])
def test_verified_account_discovery_rejects_cross_account_venue_identity_reuse(
    family: str,
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / f"cross-account-{family}-identity.duckdb")
    plan_a = LiveExecutionPlan.model_validate(
        live_execution_plan_fields(
            plan_id=UUID(int=81_001),
            proposal_id=UUID(int=81_002),
            candidate_id=UUID(int=81_003),
            account_fingerprint="a" * 64,
        )
    )
    plan_b = LiveExecutionPlan.model_validate(
        live_execution_plan_fields(
            plan_id=UUID(int=82_001),
            proposal_id=UUID(int=82_002),
            candidate_id=UUID(int=82_003),
            account_fingerprint="b" * 64,
        )
    )
    intent_a = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan_a.plan_id,
            account_fingerprint=plan_a.account_fingerprint,
        )
    )
    intent_b = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan_b.plan_id,
            account_fingerprint=plan_b.account_fingerprint,
        )
    )
    source_order = execution_records()[3]
    order_a = source_order.model_copy(
        update={"event_id": UUID(int=83_001), "intent_id": intent_a.intent_id}
    )
    order_b = source_order.model_copy(
        update={
            "event_id": UUID(int=83_002),
            "raw_event_hash": "8" * 64,
            "intent_id": intent_b.intent_id,
            "venue_order_id": "order-2" if family == "trade" else order_a.venue_order_id,
        }
    )
    for record in (plan_a, plan_b):
        store.append_live_execution_plan(record)
    for record in (intent_a, intent_b):
        store.append_execution_intent(record)
    for record in (order_a, order_b):
        store.append_venue_order_event(record)
    if family == "trade":
        source_trade = execution_records()[4]
        store.append_venue_trade_event(
            source_trade.model_copy(
                update={
                    "trade_event_id": UUID(int=84_001),
                    "intent_id": intent_a.intent_id,
                    "venue_order_id": order_a.venue_order_id,
                }
            )
        )
        store.append_venue_trade_event(
            source_trade.model_copy(
                update={
                    "trade_event_id": UUID(int=84_002),
                    "raw_event_hash": "9" * 64,
                    "intent_id": intent_b.intent_id,
                    "venue_order_id": order_b.venue_order_id,
                }
            )
        )

    with pytest.raises(ConflictingRecordError, match="account"):
        store.verified_live_execution_account_fingerprints(NOW)


@pytest.mark.parametrize("family", ["order", "trade"])
def test_verified_account_discovery_rejects_same_account_venue_identity_reuse(
    family: str,
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / f"same-account-{family}-identity.duckdb")
    plan = LiveExecutionPlan.model_validate(live_execution_plan_fields())
    intent_a = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan.plan_id,
            account_fingerprint=plan.account_fingerprint,
        )
    )
    intent_b = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan.plan_id,
            account_fingerprint=plan.account_fingerprint,
            leg_sequence=1,
            token_id="217427",
        )
    )
    source_order = execution_records()[3]
    order_a = source_order.model_copy(
        update={"event_id": UUID(int=86_003), "intent_id": intent_a.intent_id}
    )
    order_b = source_order.model_copy(
        update={
            "event_id": UUID(int=86_004),
            "raw_event_hash": "8" * 64,
            "intent_id": intent_b.intent_id,
            "venue_order_id": "order-2" if family == "trade" else order_a.venue_order_id,
        }
    )
    store.append_live_execution_plan(plan)
    store.append_execution_intent(intent_a)
    store.append_execution_intent(intent_b)
    store.append_venue_order_event(order_a)
    store.append_venue_order_event(order_b)
    if family == "trade":
        source_trade = execution_records()[4]
        for event_id, raw_hash, intent, order in (
            (UUID(int=86_005), "9" * 64, intent_a, order_a),
            (UUID(int=86_006), "a" * 64, intent_b, order_b),
        ):
            store.append_venue_trade_event(
                source_trade.model_copy(
                    update={
                        "trade_event_id": event_id,
                        "raw_event_hash": raw_hash,
                        "intent_id": intent.intent_id,
                        "venue_order_id": order.venue_order_id,
                    }
                )
            )

    with pytest.raises(ConflictingRecordError, match="intent"):
        store.verified_live_execution_account_fingerprints(NOW)


def _canonical_reconciliation_bundle() -> tuple[
    LiveExecutionPlan,
    ExecutionIntent,
    VenueOrderEvent,
    VenueTradeEvent,
    AuthoritativeTradeEconomics,
    tuple[LiveLedgerPosting, ...],
    LiveReconciliation,
]:
    plan, _source_intent, _envelope, order, trade, *_ = execution_records()
    intent = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan.plan_id,
            account_fingerprint=plan.account_fingerprint,
        )
    )
    order = order.model_copy(update={"intent_id": intent.intent_id})
    trade = trade.model_copy(update={"intent_id": intent.intent_id})
    economics = authoritative_economics(
        intent,
        source_hash="8" * 64,
        occurred_at=NOW + timedelta(seconds=1),
        information_cutoff=NOW + timedelta(seconds=3),
    )
    trade = trade.model_copy(
        update={
            "normalized_state": VenueTradeState.CONFIRMED,
            "original_venue_state": VenueTradeState.CONFIRMED.value,
            "terminal": True,
            "raw_event_hash": economics.trade_event_hash,
            "venue_timestamp": economics.occurred_at,
            "received_at": NOW + timedelta(seconds=2),
        }
    )
    postings = postings_for_confirmed_trades((intent,), (trade,), (economics,))
    economics_hashes = tuple(
        sorted(
            {
                economics.economics_fingerprint,
                economics.fee_hash,
                economics.settlement_hash,
                economics.source_hash,
            }
        )
    )
    reconciliation = LiveReconciliation(
        schema_version=1,
        reconciliation_id=UUID(int=86_100),
        account_fingerprint=plan.account_fingerprint,
        observed_at=economics.information_cutoff,
        complete=True,
        differences=(),
        evidence_hashes=economics_hashes,
        next_action=None,
        venue_order_hashes=(order.raw_event_hash,),
        venue_trade_hashes=(trade.raw_event_hash,),
        balance_hashes=economics.balance_evidence_hashes,
        allowance_hashes=("a" * 64,),
        expected_posting_ids=tuple(sorted(posting.posting_id for posting in postings)),
        lineage_hashes=(economics.economics_fingerprint,),
    )
    return plan, intent, order, trade, economics, postings, reconciliation


def _append_canonical_reconciliation_bundle(
    store: PredictionMarketStore,
    *,
    postings: tuple[LiveLedgerPosting, ...],
    reconciliation: LiveReconciliation,
) -> None:
    plan, intent, order, trade, economics, _canonical, _closed = _canonical_reconciliation_bundle()
    store.append_live_execution_plan(plan)
    store.append_execution_intent(intent)
    store.append_venue_order_event(order)
    store.append_venue_trade_event(trade)
    store.append_authoritative_trade_economics(economics)
    for posting in postings:
        store.append_live_ledger_posting(posting)
    store.append_live_reconciliation(reconciliation)


@pytest.mark.parametrize(
    "defect",
    [
        "missing_posting",
        "extra_posting",
        "future_posting",
        "missing_trade_hash",
        "unsupported_trade_hash",
        "missing_economics_hash",
        "overlapping_families",
        "noncanonical_posting",
        "lineage_outside_families",
    ],
)
def test_verified_reconciliation_requires_own_cut_canonical_typed_closure(
    defect: str,
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / f"reconciliation-{defect}.duckdb")
    _plan, _intent, _order, _trade, economics, postings, reconciliation = (
        _canonical_reconciliation_bundle()
    )
    if defect == "missing_posting":
        reconciliation = reconciliation.model_copy(
            update={"expected_posting_ids": reconciliation.expected_posting_ids[1:]}
        )
    elif defect == "extra_posting":
        reconciliation = reconciliation.model_copy(
            update={"expected_posting_ids": (*reconciliation.expected_posting_ids, UUID(int=9))}
        )
    elif defect == "future_posting":
        reconciliation = reconciliation.model_copy(
            update={"observed_at": economics.occurred_at - timedelta(microseconds=1)}
        )
    elif defect == "missing_trade_hash":
        reconciliation = reconciliation.model_copy(update={"venue_trade_hashes": ()})
    elif defect == "unsupported_trade_hash":
        reconciliation = reconciliation.model_copy(update={"venue_trade_hashes": ("f" * 64,)})
    elif defect == "missing_economics_hash":
        reconciliation = reconciliation.model_copy(
            update={"evidence_hashes": reconciliation.evidence_hashes[1:]}
        )
    elif defect == "overlapping_families":
        reconciliation = reconciliation.model_copy(
            update={"allowance_hashes": (reconciliation.balance_hashes[0],)}
        )
    elif defect == "noncanonical_posting":
        postings = (
            postings[0].model_copy(update={"debit_account": "cash:USDC"}),
            *postings[1:],
        )
    elif defect == "lineage_outside_families":
        postings = (
            postings[0].model_copy(
                update={"lineage_hashes": (*postings[0].lineage_hashes, "f" * 64)}
            ),
            *postings[1:],
        )
    _append_canonical_reconciliation_bundle(
        store,
        postings=postings,
        reconciliation=reconciliation,
    )

    with pytest.raises(ConflictingRecordError, match="reconciliation"):
        store.verified_live_execution_account_fingerprints(NOW + timedelta(seconds=4))


def test_verified_reconciliation_accepts_canonical_own_cut_closure(tmp_path: Path) -> None:
    store = PredictionMarketStore(tmp_path / "canonical-reconciliation.duckdb")
    plan, _intent, _order, _trade, _economics, postings, reconciliation = (
        _canonical_reconciliation_bundle()
    )
    _append_canonical_reconciliation_bundle(
        store,
        postings=postings,
        reconciliation=reconciliation,
    )

    assert store.verified_live_execution_account_fingerprints(NOW + timedelta(seconds=4)) == (
        plan.account_fingerprint,
    )


def test_standalone_economics_remains_visible_but_cannot_be_borrowed_by_complete_closure(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "standalone-economics-closure.duckdb")
    plan, _intent, _order, _trade, _economics, postings, reconciliation = (
        _canonical_reconciliation_bundle()
    )
    _append_canonical_reconciliation_bundle(
        store,
        postings=postings,
        reconciliation=reconciliation,
    )
    standalone_intent = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan.plan_id,
            account_fingerprint=plan.account_fingerprint,
            leg_sequence=1,
            token_id="217427",
        )
    )
    store.append_execution_intent(standalone_intent)
    store.append_authoritative_trade_economics(
        authoritative_economics(
            standalone_intent,
            venue_order_id="order-standalone",
            venue_trade_id="trade-standalone",
            source_hash="9" * 64,
        )
    )

    with pytest.raises(ConflictingRecordError, match="reconciliation"):
        store.verified_live_execution_account_fingerprints(NOW + timedelta(seconds=4))


def test_verified_reconciliation_cannot_borrow_another_accounts_posting(
    tmp_path: Path,
) -> None:
    store = PredictionMarketStore(tmp_path / "cross-account-reconciliation-posting.duckdb")
    plan_a, intent_a, economics_a, posting_a, reconciliation_a = _relational_execution_records()
    plan_b = LiveExecutionPlan.model_validate(
        live_execution_plan_fields(
            plan_id=UUID(int=85_001),
            proposal_id=UUID(int=85_002),
            candidate_id=UUID(int=85_003),
            account_fingerprint="b" * 64,
        )
    )
    intent_b = ExecutionIntent.model_validate(
        execution_intent_fields(
            plan_id=plan_b.plan_id,
            account_fingerprint=plan_b.account_fingerprint,
        )
    )
    economics_b = authoritative_economics(
        intent_b,
        venue_order_id="order-2",
        venue_trade_id="trade-2",
    )
    posting_b = posting_a.model_copy(
        update={
            "posting_id": UUID(int=85_004),
            "account_fingerprint": plan_b.account_fingerprint,
            "intent_id": intent_b.intent_id,
            "venue_order_id": economics_b.venue_order_id,
            "venue_trade_id": economics_b.venue_trade_id,
        }
    )
    reconciliation_a = reconciliation_a.model_copy(
        update={"expected_posting_ids": (posting_b.posting_id,)}
    )
    for append, records in (
        (store.append_live_execution_plan, (plan_a, plan_b)),
        (store.append_execution_intent, (intent_a, intent_b)),
        (store.append_authoritative_trade_economics, (economics_a, economics_b)),
        (store.append_live_ledger_posting, (posting_a, posting_b)),
        (store.append_live_reconciliation, (reconciliation_a,)),
    ):
        for record in records:
            append(record)

    with pytest.raises(ConflictingRecordError, match="account"):
        store.verified_live_execution_account_fingerprints(economics_a.information_cutoff)


def test_verified_bulk_queries_fail_before_silently_truncating_complete_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import polytrading.predictions.storage.store as store_module
    from tests.predictions.candidate_helpers import candidate_relationship

    store = PredictionMarketStore(tmp_path / "bounded-dashboard.duckdb")
    store.append_candidate_relationship(candidate_relationship(candidate_id=UUID(int=81001)))
    store.append_candidate_relationship(candidate_relationship(candidate_id=UUID(int=81002)))
    monkeypatch.setattr(store_module, "_MAX_VERIFIED_LIVE_ACCOUNT_RECORDS", 1)

    with pytest.raises(ConflictingRecordError, match="limit"):
        store.verified_candidate_relationships_as_of(NOW)
