from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from polytrading.predictions.execution.ledger import (
    AuthoritativeTradeEconomics,
    LiveLedgerError,
    postings_for_confirmed_trades,
    verify_live_conservation,
)
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    LiveLedgerPosting,
    VenueTradeEvent,
    VenueTradeState,
)
from tests.predictions.execution_helpers import execution_intent_fields

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)
ACCOUNT = "a" * 64
ORDER_ID = "venue-order-1"
TRADE_ID = "venue-trade-1"
TRADE_HASH = "1" * 64
SETTLEMENT_HASH = "2" * 64
FEE_HASH = "3" * 64
SOURCE_HASH = "4" * 64
BALANCE_HASH = "5" * 64
COST_BASIS_HASH = "6" * 64


def intent(**overrides: object) -> ExecutionIntent:
    return ExecutionIntent(**execution_intent_fields(**overrides))


def trade_event(
    source_intent: ExecutionIntent,
    *,
    state: VenueTradeState = VenueTradeState.CONFIRMED,
    event_id: UUID | None = None,
    raw_event_hash: str = TRADE_HASH,
    received_at: datetime = NOW + timedelta(seconds=2),
) -> VenueTradeEvent:
    return VenueTradeEvent(
        schema_version=1,
        trade_event_id=event_id or uuid4(),
        venue="polymarket",
        raw_event_hash=raw_event_hash,
        source_channel="recovery_read",
        venue_trade_id=TRADE_ID,
        venue_order_id=ORDER_ID,
        intent_id=source_intent.intent_id,
        original_venue_state=state.value,
        normalized_state=state,
        terminal=state in {VenueTradeState.CONFIRMED, VenueTradeState.FAILED},
        venue_timestamp=NOW + timedelta(seconds=1),
        received_at=received_at,
        sequence_number=None,
        protocol_version=source_intent.protocol_version,
        lineage_hashes=(raw_event_hash,),
    )


def economics(
    source_intent: ExecutionIntent,
    **overrides: object,
) -> AuthoritativeTradeEconomics:
    fields: dict[str, object] = {
        "schema_version": 1,
        "account_fingerprint": source_intent.account_fingerprint,
        "intent_id": source_intent.intent_id,
        "venue_order_id": ORDER_ID,
        "venue_trade_id": TRADE_ID,
        "trade_event_hash": TRADE_HASH,
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
        "fee_hash": FEE_HASH,
        "settlement_hash": SETTLEMENT_HASH,
        "source_hash": SOURCE_HASH,
        "balance_evidence_hashes": (BALANCE_HASH,),
        "occurred_at": NOW + timedelta(seconds=1),
        "information_cutoff": NOW + timedelta(seconds=3),
        "protocol_version": source_intent.protocol_version,
        "realized_pnl": None,
        "cost_basis_evidence_hash": None,
    }
    fields.update(overrides)
    return AuthoritativeTradeEconomics(**fields)


def account_net(postings: tuple[LiveLedgerPosting, ...], account: str, asset: str) -> Decimal:
    return sum(
        (
            posting.debit_amount
            if posting.debit_account == account and posting.asset_id == asset
            else -posting.credit_amount
            if posting.credit_account == account and posting.asset_id == asset
            else Decimal("0")
        )
        for posting in postings
    )


def test_empty_live_ledger_is_conserved() -> None:
    assert verify_live_conservation(()) is None


def test_confirmed_buy_creates_exact_balanced_cash_position_and_fee_pairs() -> None:
    source_intent = intent()

    postings = postings_for_confirmed_trades(
        (source_intent,),
        (trade_event(source_intent),),
        (economics(source_intent),),
    )

    assert len(postings) == 6
    assert tuple(posting.posting_id for posting in postings) == tuple(
        sorted(posting.posting_id for posting in postings)
    )
    assert verify_live_conservation(tuple(reversed(postings))) is None
    assert account_net(postings, "venue_cash:USDC", "USDC") == Decimal("-5.11")
    assert account_net(postings, "venue_position:217426", "217426") == Decimal("10")
    assert account_net(postings, "fees_paid:USDC", "USDC") == Decimal("0.01")
    exact = economics(source_intent)
    assert all(
        {
            TRADE_HASH,
            SETTLEMENT_HASH,
            FEE_HASH,
            SOURCE_HASH,
            BALANCE_HASH,
            exact.economics_fingerprint,
        }
        <= set(posting.lineage_hashes)
        for posting in postings
    )


def test_confirmed_sell_reverses_cash_and_position_without_guessing_cost_basis() -> None:
    source_intent = intent(side="sell", maximum_spend=None)
    exact = economics(source_intent, side="sell")

    postings = postings_for_confirmed_trades(
        (source_intent,), (trade_event(source_intent),), (exact,)
    )

    assert account_net(postings, "venue_cash:USDC", "USDC") == Decimal("5.09")
    assert account_net(postings, "venue_position:217426", "217426") == Decimal("-10")
    assert not any("realized_pnl" in (row.debit_account, row.credit_account) for row in postings)


def test_spend_bounded_intent_without_requested_base_size_uses_only_exact_fill_size() -> None:
    source_intent = intent(base_size=None, maximum_spend=Decimal("5.10"))

    postings = postings_for_confirmed_trades(
        (source_intent,),
        (trade_event(source_intent),),
        (economics(source_intent),),
    )

    assert account_net(postings, "venue_position:217426", "217426") == Decimal("10")


def test_fill_after_intent_deadline_is_rejected() -> None:
    source_intent = intent(deadline=NOW + timedelta(milliseconds=500))

    with pytest.raises(LiveLedgerError, match="TRADE_ECONOMICS_MISMATCH"):
        postings_for_confirmed_trades(
            (source_intent,),
            (trade_event(source_intent),),
            (economics(source_intent),),
        )


@pytest.mark.parametrize(
    "state",
    [
        VenueTradeState.MATCHED_NOT_BROADCASTED,
        VenueTradeState.MATCHED,
        VenueTradeState.MINED,
        VenueTradeState.RETRYING,
        VenueTradeState.FAILED,
    ],
)
def test_nonconfirmed_or_failed_trade_creates_no_final_posting(state: VenueTradeState) -> None:
    source_intent = intent()

    assert (
        postings_for_confirmed_trades(
            (source_intent,), (trade_event(source_intent, state=state),), ()
        )
        == ()
    )


def test_identical_trade_and_economics_duplicates_are_idempotent_and_order_independent() -> None:
    source_intent = intent()
    event = trade_event(source_intent, event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"))
    exact = economics(source_intent)

    single = postings_for_confirmed_trades((source_intent,), (event,), (exact,))
    duplicate = postings_for_confirmed_trades(
        (source_intent, source_intent), (event, event), (exact, exact)
    )

    assert duplicate == single


def test_conflicting_duplicate_trade_economics_is_rejected() -> None:
    source_intent = intent()
    first = economics(source_intent)
    conflicting = economics(source_intent, source_hash="7" * 64)

    with pytest.raises(LiveLedgerError, match="TRADE_ECONOMICS_CONFLICT"):
        postings_for_confirmed_trades(
            (source_intent,), (trade_event(source_intent),), (first, conflicting)
        )


def test_failed_then_confirmed_history_for_same_trade_is_contradictory() -> None:
    source_intent = intent()
    failed = trade_event(
        source_intent,
        state=VenueTradeState.FAILED,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f2"),
        received_at=NOW + timedelta(milliseconds=1500),
    )
    confirmed = trade_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
    )

    with pytest.raises(LiveLedgerError, match="TRADE_EVENT_CONFLICT"):
        postings_for_confirmed_trades(
            (source_intent,), (failed, confirmed), (economics(source_intent),)
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", Decimal("1.01")),
        ("price", Decimal("0.5100001")),
        ("size", Decimal("10.001")),
        ("fee", Decimal("0.0000001")),
        ("cash_quantum", Decimal("0.0000001")),
        ("position_quantum", Decimal("0.001")),
    ],
)
def test_economics_rejects_excess_or_nondivisible_precision(field: str, value: Decimal) -> None:
    source_intent = intent()
    with pytest.raises(ValueError, match="TRADE_ECONOMICS_INVALID"):
        economics(source_intent, **{field: value})


def test_confirmed_economics_must_match_exact_trade_and_intent_lineage() -> None:
    source_intent = intent()
    with pytest.raises(LiveLedgerError, match="TRADE_ECONOMICS_MISMATCH"):
        postings_for_confirmed_trades(
            (source_intent,),
            (trade_event(source_intent),),
            (economics(source_intent, trade_event_hash="8" * 64),),
        )


@pytest.mark.parametrize("realized_pnl", [Decimal("1.25"), Decimal("-0.75")])
def test_explicit_cost_basis_bound_pnl_creates_balanced_signed_rows(
    realized_pnl: Decimal,
) -> None:
    source_intent = intent()
    exact = economics(
        source_intent,
        realized_pnl=realized_pnl,
        cost_basis_evidence_hash=COST_BASIS_HASH,
    )

    postings = postings_for_confirmed_trades(
        (source_intent,), (trade_event(source_intent),), (exact,)
    )

    assert len(postings) == 8
    assert account_net(postings, "realized_pnl:USDC", "USDC") == -realized_pnl
    assert all(
        COST_BASIS_HASH in row.lineage_hashes
        for row in postings
        if "realized_pnl:USDC" in (row.debit_account, row.credit_account)
    )
    assert verify_live_conservation(postings) is None


def test_pnl_without_cost_basis_is_unrepresentable() -> None:
    with pytest.raises(ValueError, match="TRADE_ECONOMICS_INVALID"):
        economics(intent(), realized_pnl=Decimal("1"))


def test_authoritative_economics_denies_reinitialization_and_subclass_forks() -> None:
    exact = economics(intent())
    alias = exact
    changed = exact.model_dump(mode="python")
    changed["source_hash"] = "7" * 64

    with pytest.raises(ValueError, match="TRADE_ECONOMICS_INVALID"):
        exact.__init__(**changed)
    assert alias.source_hash == SOURCE_HASH
    with pytest.raises(TypeError, match="TRADE_ECONOMICS_NOT_SUBCLASSABLE"):

        class ForkedEconomics(AuthoritativeTradeEconomics):
            pass

    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(ValueError, match="TRADE_ECONOMICS_INVALID"):
            operation(exact)
    with pytest.raises(ValueError, match="TRADE_ECONOMICS_INVALID"):
        exact.__getstate__()


def test_conservation_rejects_duplicate_ids_unpaired_rows_and_content_mutation() -> None:
    source_intent = intent()
    postings = postings_for_confirmed_trades(
        (source_intent,), (trade_event(source_intent),), (economics(source_intent),)
    )

    with pytest.raises(LiveLedgerError, match="POSTING_ID_DUPLICATE"):
        verify_live_conservation((postings[0], postings[0]))
    with pytest.raises(LiveLedgerError, match="POSTING_PAIR_INVALID"):
        verify_live_conservation(postings[:-1])
    changed = postings[0].model_copy(update={"asset_id": "other"})
    with pytest.raises(LiveLedgerError, match="POSTING_ID_MISMATCH"):
        verify_live_conservation((changed, *postings[1:]))


def test_conservation_rejects_unclosed_account_names() -> None:
    source_intent = intent()
    postings = postings_for_confirmed_trades(
        (source_intent,), (trade_event(source_intent),), (economics(source_intent),)
    )
    changed = postings[0].model_copy(update={"debit_account": "wallet_cash"})

    with pytest.raises(LiveLedgerError, match="POSTING_ACCOUNT_INVALID"):
        verify_live_conservation((changed, *postings[1:]))


@given(
    price_units=st.integers(min_value=1, max_value=99),
    size_units=st.integers(min_value=1, max_value=1000),
    fee_units=st.integers(min_value=0, max_value=1),
)
def test_generated_confirmed_postings_conserve_for_exact_quantized_economics(
    price_units: int,
    size_units: int,
    fee_units: int,
) -> None:
    price = Decimal(price_units) / Decimal("100")
    size = Decimal(size_units) / Decimal("100")
    fee = Decimal(fee_units) / Decimal("1000000")
    source_intent = intent(
        limit_price=price,
        base_size=size,
        maximum_spend=price * size,
    )
    exact = economics(source_intent, price=price, size=size, fee=fee)

    postings = postings_for_confirmed_trades(
        (source_intent,), (trade_event(source_intent),), (exact,)
    )

    assert verify_live_conservation(postings) is None
    for asset in {row.asset_id for row in postings}:
        assert sum(
            (row.debit_amount for row in postings if row.asset_id == asset), Decimal("0")
        ) == sum((row.credit_amount for row in postings if row.asset_id == asset), Decimal("0"))
