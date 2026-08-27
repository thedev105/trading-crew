import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ImmediateOrderType,
    LiveExecutionPlan,
    LiveLedgerPosting,
    LiveReconciliation,
    SignedOrderEnvelope,
    VenueOrderEvent,
    VenueOrderState,
    VenueTradeEvent,
    VenueTradeState,
    deterministic_intent_id,
)
from tests.predictions.execution_helpers import execution_intent_fields, live_execution_plan_fields

PUBLIC_UNSIGNED_ORDER = {
    "builder": "0x" + "00" * 32,
    "expiration": "0",
    "maker": "0x" + "11" * 20,
    "makerAmount": "5100000",
    "metadata": "0x" + "00" * 32,
    "salt": 1,
    "side": "BUY",
    "signatureType": 0,
    "signer": "0x" + "11" * 20,
    "takerAmount": "10000000",
    "timestamp": "1787673600000",
    "tokenId": "217426",
}


def test_intent_accepts_only_immediate_order_types() -> None:
    intent = ExecutionIntent(**execution_intent_fields(order_type=ImmediateOrderType.FAK))
    assert intent.order_type is ImmediateOrderType.FAK
    with pytest.raises(ValidationError):
        ExecutionIntent(**execution_intent_fields(order_type="GTC"))


def test_intent_identity_is_stable_and_content_bound() -> None:
    fields = execution_intent_fields()
    first = ExecutionIntent(**fields)
    second = ExecutionIntent(**fields)
    changed = ExecutionIntent(**execution_intent_fields(limit_price=Decimal("0.52")))
    assert first.intent_id == second.intent_id == deterministic_intent_id(first)
    assert changed.intent_id != first.intent_id


def test_intent_tick_size_is_required_positive_and_content_bound() -> None:
    baseline = ExecutionIntent(**execution_intent_fields())
    changed = ExecutionIntent(**execution_intent_fields(tick_size=Decimal("0.001")))

    assert baseline.tick_size == Decimal("0.01")
    assert changed.intent_id != baseline.intent_id
    missing = execution_intent_fields()
    missing.pop("tick_size")
    with pytest.raises(ValidationError, match="Field required"):
        ExecutionIntent(**missing)
    with pytest.raises(ValidationError, match="greater than 0"):
        ExecutionIntent(**execution_intent_fields(tick_size=Decimal("0")))


def test_intent_exchange_kind_is_required_closed_and_content_bound() -> None:
    baseline = ExecutionIntent(**execution_intent_fields())
    changed = ExecutionIntent(**execution_intent_fields(exchange_kind="negative_risk"))

    assert baseline.exchange_kind == "standard"
    assert changed.intent_id != baseline.intent_id
    missing = execution_intent_fields()
    missing.pop("exchange_kind")
    with pytest.raises(ValidationError, match="Field required"):
        ExecutionIntent(**missing)
    with pytest.raises(ValidationError, match="standard"):
        ExecutionIntent(**execution_intent_fields(exchange_kind="future_exchange"))


def test_signed_envelope_accepts_exact_current_public_unsigned_order() -> None:
    intent = ExecutionIntent(**execution_intent_fields())

    envelope = SignedOrderEnvelope(
        schema_version=1,
        intent_id=intent.intent_id,
        intent_fingerprint=intent.intent_fingerprint,
        protocol_version="polymarket-clob-2026-08-25-v1",
        salt=1,
        signature_type=0,
        public_signature="0x" + "11" * 65,
        domain_fingerprint="1" * 64,
        exact_body_hash="2" * 64,
        order_fingerprint="3" * 64,
        signer_version="eth-account==0.13.7",
        canonical_order_json=json.dumps(
            PUBLIC_UNSIGNED_ORDER,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )

    assert json.loads(envelope.canonical_order_json) == PUBLIC_UNSIGNED_ORDER


def test_signed_envelope_rejects_unicode_decimal_order_integer_strings() -> None:
    intent = ExecutionIntent(**execution_intent_fields())
    fullwidth_token_id = "".join(chr(codepoint) for codepoint in (0xFF12, 0xFF11, 0xFF17))
    public_order = {**PUBLIC_UNSIGNED_ORDER, "tokenId": fullwidth_token_id}

    with pytest.raises(ValidationError, match="integer string"):
        SignedOrderEnvelope(
            schema_version=1,
            intent_id=intent.intent_id,
            intent_fingerprint=intent.intent_fingerprint,
            protocol_version="polymarket-clob-2026-08-25-v1",
            salt=1,
            signature_type=0,
            public_signature="0x" + "11" * 65,
            domain_fingerprint="1" * 64,
            exact_body_hash="2" * 64,
            order_fingerprint="3" * 64,
            signer_version="eth-account==0.13.7",
            canonical_order_json=json.dumps(
                public_order,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )


def test_signed_envelope_rejects_empty_public_order_object() -> None:
    intent = ExecutionIntent(**execution_intent_fields())

    with pytest.raises(ValidationError, match="public order fields"):
        SignedOrderEnvelope(
            schema_version=1,
            intent_id=intent.intent_id,
            intent_fingerprint=intent.intent_fingerprint,
            protocol_version="polymarket-clob-2026-08-25-v1",
            salt=1,
            signature_type=0,
            public_signature="0x" + "11" * 65,
            domain_fingerprint="1" * 64,
            exact_body_hash="2" * 64,
            order_fingerprint="3" * 64,
            signer_version="eth-account==0.13.7",
            canonical_order_json="{}",
        )


def test_signed_envelope_rejects_a_mismatched_intent_fingerprint() -> None:
    intent = ExecutionIntent(**execution_intent_fields())
    with pytest.raises(ValidationError, match="intent fingerprint"):
        SignedOrderEnvelope(
            schema_version=1,
            intent_id=intent.intent_id,
            intent_fingerprint="0" * 64,
            protocol_version="polymarket-clob-2026-08-25-v1",
            salt=1,
            signature_type=0,
            public_signature="0x" + "11" * 65,
            domain_fingerprint="1" * 64,
            exact_body_hash="2" * 64,
            order_fingerprint="3" * 64,
            signer_version="1",
            canonical_order_json=json.dumps(
                PUBLIC_UNSIGNED_ORDER,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )


@pytest.mark.parametrize(
    "secret_field",
    ("apiSecret", "headers", "authenticatedFrame", "capabilityBytes", "signature"),
)
def test_signed_envelope_rejects_nonpublic_order_json_fields(secret_field: str) -> None:
    intent = ExecutionIntent(**execution_intent_fields())
    public_order = {**PUBLIC_UNSIGNED_ORDER, secret_field: "secret-canary"}
    with pytest.raises(ValidationError, match="public order fields"):
        SignedOrderEnvelope(
            schema_version=1,
            intent_id=intent.intent_id,
            intent_fingerprint=intent.intent_fingerprint,
            protocol_version="polymarket-clob-2026-08-25-v1",
            salt=1,
            signature_type=0,
            public_signature="0x" + "11" * 65,
            domain_fingerprint="1" * 64,
            exact_body_hash="2" * 64,
            order_fingerprint="3" * 64,
            signer_version="1",
            canonical_order_json=json.dumps(public_order, separators=(",", ":"), sort_keys=True),
        )


def test_live_execution_plan_accepts_only_polymarket_immediate_legs() -> None:
    plan = LiveExecutionPlan(**live_execution_plan_fields())
    assert plan.venue is PredictionVenue.POLYMARKET
    assert plan.leg_order_types == (ImmediateOrderType.FAK, ImmediateOrderType.FOK)
    with pytest.raises(ValidationError):
        LiveExecutionPlan(**live_execution_plan_fields(venue=PredictionVenue.KALSHI))
    with pytest.raises(ValidationError):
        LiveExecutionPlan(**live_execution_plan_fields(leg_order_types=("GTC", "FOK")))


def test_live_execution_plan_rejects_misaligned_or_stale_leg_evidence() -> None:
    with pytest.raises(ValidationError, match="align"):
        LiveExecutionPlan(**live_execution_plan_fields(limit_prices=(Decimal("0.51"),)))
    with pytest.raises(ValidationError, match="timezone-aware"):
        LiveExecutionPlan(**live_execution_plan_fields(book_deadline=datetime(2026, 8, 25, 16)))
    with pytest.raises(ValidationError, match="freshness deadlines"):
        LiveExecutionPlan(
            **live_execution_plan_fields(book_deadline=datetime(2026, 8, 25, 16, tzinfo=UTC))
        )


def test_intent_rejects_nonpositive_limits_naive_deadlines_and_unsorted_lineage() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        ExecutionIntent(**execution_intent_fields(limit_price=Decimal("0")))
    with pytest.raises(ValidationError, match="timezone-aware"):
        ExecutionIntent(**execution_intent_fields(deadline=datetime(2026, 8, 25, 16, 0, 5)))
    with pytest.raises(ValidationError, match="sorted and unique"):
        ExecutionIntent(**execution_intent_fields(lineage_hashes=("f" * 64, "a" * 64)))


def test_order_and_trade_events_require_terminal_state_consistency() -> None:
    with pytest.raises(ValidationError, match="terminal"):
        VenueOrderEvent(
            schema_version=1,
            event_id=UUID("8fd1c06b-1eef-4673-a6e0-8e4f3a52a4d0"),
            venue=PredictionVenue.POLYMARKET,
            raw_event_hash="a" * 64,
            source_channel="user",
            venue_order_id="order-1",
            intent_id=None,
            original_venue_state="live",
            normalized_state=VenueOrderState.FILLED,
            terminal=False,
            venue_timestamp=None,
            received_at=datetime(2026, 8, 25, 16, tzinfo=UTC),
            sequence_number=None,
            protocol_version="polymarket-clob-2026-08-25-v1",
        )
    with pytest.raises(ValidationError, match="terminal"):
        VenueTradeEvent(
            schema_version=1,
            trade_event_id=UUID("9c99c069-43ea-4bc6-bdc1-f5392801814b"),
            venue=PredictionVenue.POLYMARKET,
            raw_event_hash="b" * 64,
            source_channel="user",
            venue_trade_id="trade-1",
            venue_order_id="order-1",
            intent_id=None,
            original_venue_state="matched",
            normalized_state=VenueTradeState.MATCHED,
            terminal=True,
            venue_timestamp=None,
            received_at=datetime(2026, 8, 25, 16, tzinfo=UTC),
            sequence_number=None,
            protocol_version="polymarket-clob-2026-08-25-v1",
        )


def test_ledger_requires_exactly_one_nonzero_side() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        LiveLedgerPosting(
            schema_version=1,
            posting_id=UUID("4c64d2bf-fd26-4a33-af35-867bc5a9a62e"),
            account_fingerprint="a" * 64,
            intent_id=None,
            venue_order_id=None,
            venue_trade_id=None,
            settlement_hash=None,
            fee_hash=None,
            balance_evidence_hashes=(),
            debit_account="cash",
            credit_account="position",
            asset_id="USDC",
            debit_amount=Decimal("1"),
            credit_amount=Decimal("1"),
            occurred_at=datetime(2026, 8, 25, 16, tzinfo=UTC),
        )


def test_complete_reconciliation_rejects_unexplained_differences() -> None:
    with pytest.raises(ValidationError, match="complete reconciliation"):
        LiveReconciliation(
            schema_version=1,
            reconciliation_id=UUID("98209e96-ae4b-4599-a18c-525a93ddd2c6"),
            account_fingerprint="a" * 64,
            observed_at=datetime(2026, 8, 25, 16, tzinfo=UTC),
            complete=True,
            differences=("unmatched trade",),
            evidence_hashes=(),
            next_action=None,
        )


@pytest.mark.parametrize("complete", [False, True])
def test_live_reconciliation_identity_is_derived_from_every_other_field(complete: bool) -> None:
    from polytrading.predictions.execution.models import canonical_live_reconciliation_id

    record = LiveReconciliation(
        schema_version=1,
        reconciliation_id=UUID(int=0),
        account_fingerprint="a" * 64,
        observed_at=datetime(2026, 8, 25, 16, tzinfo=UTC),
        complete=complete,
        differences=() if complete else ("ORDER_HISTORY_INVALID",),
        evidence_hashes=("1" * 64,),
        next_action=None if complete else "HALT_AND_RECONCILE",
        venue_order_hashes=("2" * 64,),
        venue_trade_hashes=("3" * 64,),
        balance_hashes=("4" * 64,),
        allowance_hashes=("5" * 64,),
        expected_posting_ids=(UUID(int=1),),
        lineage_hashes=("6" * 64,),
    )
    canonical = record.model_copy(
        update={"reconciliation_id": canonical_live_reconciliation_id(record)}
    )
    changed = canonical.model_copy(update={"evidence_hashes": ("7" * 64,)})

    assert canonical_live_reconciliation_id(canonical) == canonical.reconciliation_id
    assert canonical_live_reconciliation_id(changed) != canonical.reconciliation_id
