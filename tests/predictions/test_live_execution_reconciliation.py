from __future__ import annotations

import copy
import pickle
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from decimal import (
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Underflow,
    localcontext,
)
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.predictions.execution.ledger import (
    AuthoritativeTradeEconomics,
    postings_for_confirmed_trades,
)
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    LiveReconciliation,
    VenueOrderEvent,
    VenueOrderState,
    VenueTradeEvent,
    VenueTradeState,
)
from polytrading.predictions.execution.reconciliation import (
    AllowanceObservation,
    AssetAmountObservation,
    LiveReconciliationError,
    OpenOrderObservation,
    RecentTradeObservation,
    SettlementObservation,
    VenueAccountSnapshot,
    reconcile_live_account,
    reconciled_live_pnl,
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
ORDER_HASH = "7" * 64


class ScriptedSequence(Sequence[object]):
    """Returns a bounded caller-controlled view on each traversal."""

    def __init__(self, views: tuple[tuple[object, ...], ...]) -> None:
        self._views = views
        self.iterations = 0

    def __len__(self) -> int:
        return len(self._views[min(self.iterations, len(self._views) - 1)])

    def __getitem__(self, index: int) -> object:
        del index
        raise IndexError

    def __iter__(self) -> Iterator[object]:
        view = self._views[min(self.iterations, len(self._views) - 1)]
        self.iterations += 1
        yield from view


class MutatingAfterYieldSequence(Sequence[object]):
    def __init__(self, value: AuthoritativeTradeEconomics) -> None:
        self.value = value
        self.iterations = 0

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index: int) -> object:
        del index
        raise IndexError

    def __iter__(self) -> Iterator[object]:
        self.iterations += 1
        yield self.value
        object.__setattr__(self.value, "information_cutoff", NOW + timedelta(seconds=5))


def hostile_decimal(coefficient: int, exponent: int) -> Decimal:
    return Decimal((int(coefficient < 0), tuple(map(int, str(abs(coefficient)))), exponent))


def bounded_call[ResultT](operation: object) -> ResultT:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(operation)  # type: ignore[arg-type]
        return future.result(timeout=1)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def evidence_hash(index: int) -> str:
    return f"{index:064x}"


def intent(**overrides: object) -> ExecutionIntent:
    return ExecutionIntent(**execution_intent_fields(**overrides))


def trade_event(
    source_intent: ExecutionIntent,
    *,
    state: VenueTradeState = VenueTradeState.CONFIRMED,
    event_id: UUID | None = None,
    venue_order_id: str = ORDER_ID,
    venue_trade_id: str = TRADE_ID,
    raw_event_hash: str = TRADE_HASH,
    venue_timestamp: datetime | None = NOW + timedelta(seconds=1),
    received_at: datetime = NOW + timedelta(seconds=2),
    sequence_number: int | None = None,
) -> VenueTradeEvent:
    return VenueTradeEvent(
        schema_version=1,
        trade_event_id=event_id or UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
        venue="polymarket",
        raw_event_hash=raw_event_hash,
        source_channel="recovery_read",
        venue_trade_id=venue_trade_id,
        venue_order_id=venue_order_id,
        intent_id=source_intent.intent_id,
        original_venue_state=state.value,
        normalized_state=state,
        terminal=state in {VenueTradeState.CONFIRMED, VenueTradeState.FAILED},
        venue_timestamp=venue_timestamp,
        received_at=received_at,
        sequence_number=sequence_number,
        protocol_version=source_intent.protocol_version,
        lineage_hashes=(raw_event_hash,),
    )


def order_event(
    source_intent: ExecutionIntent,
    *,
    event_id: UUID | None = None,
    venue_order_id: str = ORDER_ID,
    raw_event_hash: str = ORDER_HASH,
    received_at: datetime = NOW + timedelta(milliseconds=500),
    protocol_version: str | None = None,
) -> VenueOrderEvent:
    return VenueOrderEvent(
        schema_version=1,
        event_id=event_id or UUID("42b33848-ff46-4c45-b9ab-0c74510687e0"),
        venue="polymarket",
        raw_event_hash=raw_event_hash,
        source_channel="recovery_read",
        venue_order_id=venue_order_id,
        intent_id=source_intent.intent_id,
        original_venue_state=VenueOrderState.RECONCILED.value,
        normalized_state=VenueOrderState.RECONCILED,
        terminal=True,
        venue_timestamp=received_at,
        received_at=received_at,
        sequence_number=None,
        protocol_version=protocol_version or source_intent.protocol_version,
        lineage_hashes=(raw_event_hash,),
    )


def exact_economics(
    source_intent: ExecutionIntent,
    *,
    realized_pnl: Decimal | None = None,
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
        "realized_pnl": realized_pnl,
        "cost_basis_evidence_hash": None if realized_pnl is None else COST_BASIS_HASH,
    }
    fields.update(overrides)
    return AuthoritativeTradeEconomics(**fields)


def exact_postings(*, realized_pnl: Decimal | None = None):
    source_intent = intent()
    exact = exact_economics(source_intent, realized_pnl=realized_pnl)
    return (
        source_intent,
        exact,
        postings_for_confirmed_trades((source_intent,), (trade_event(source_intent),), (exact,)),
    )


def reconcile_with_evidence(
    postings: object,
    snapshot: VenueAccountSnapshot,
    source_intent: ExecutionIntent,
    exact: AuthoritativeTradeEconomics,
):
    return reconcile_live_account(
        postings,  # type: ignore[arg-type]
        snapshot,
        (source_intent,),
        (trade_event(source_intent),),
        (exact,),
    )


def pnl_with_evidence(
    postings: object,
    reconciliation: LiveReconciliation,
    snapshot: VenueAccountSnapshot,
    source_intent: ExecutionIntent,
    exact: AuthoritativeTradeEconomics,
) -> Decimal | None:
    return reconciled_live_pnl(
        postings,  # type: ignore[arg-type]
        reconciliation,
        snapshot,
        (source_intent,),
        (trade_event(source_intent),),
        (exact,),
    )


def asset(
    asset_id: str,
    amount: str,
    quantum: str,
    fact_hash: str,
) -> AssetAmountObservation:
    return AssetAmountObservation(
        schema_version=1,
        asset_id=asset_id,
        amount=Decimal(amount),
        quantum=Decimal(quantum),
        evidence_hash=fact_hash,
    )


def snapshot_for(
    source_intent: ExecutionIntent,
    exact: AuthoritativeTradeEconomics,
    *,
    account_fingerprint: str = ACCOUNT,
    cash_current: str = "94.89",
    position_current: str = "10",
    fee_current: str = "0.01",
    allowance_current: str = "100",
    open_orders: tuple[OpenOrderObservation, ...] = (),
    include_trade: bool = True,
    include_settlement: bool = True,
    settlement_state: VenueTradeState = VenueTradeState.CONFIRMED,
    balance_hash: str = BALANCE_HASH,
    cutoff_at: datetime = NOW,
    observed_at: datetime = NOW + timedelta(seconds=4),
) -> VenueAccountSnapshot:
    recent_trades = (
        (
            RecentTradeObservation(
                schema_version=1,
                venue_trade_id=TRADE_ID,
                venue_order_id=ORDER_ID,
                intent_id=source_intent.intent_id,
                cash_asset_id="USDC",
                position_asset_id=source_intent.token_id,
                side="buy",
                state=VenueTradeState.CONFIRMED,
                trade_event_hash=TRADE_HASH,
                settlement_hash=SETTLEMENT_HASH,
                fee_hash=FEE_HASH,
                source_hash=SOURCE_HASH,
                balance_evidence_hashes=(balance_hash,),
                economics_fingerprint=exact.economics_fingerprint,
                realized_pnl=exact.realized_pnl,
                cost_basis_evidence_hash=exact.cost_basis_evidence_hash,
                occurred_at=NOW + timedelta(seconds=1),
            ),
        )
        if include_trade
        else ()
    )
    settlements = (
        (
            SettlementObservation(
                schema_version=1,
                venue_trade_id=TRADE_ID,
                venue_order_id=ORDER_ID,
                intent_id=source_intent.intent_id,
                position_asset_id=source_intent.token_id,
                state=settlement_state,
                settlement_hash=SETTLEMENT_HASH,
                evidence_hash=SETTLEMENT_HASH,
                occurred_at=NOW + timedelta(seconds=1),
            ),
        )
        if include_settlement
        else ()
    )
    return VenueAccountSnapshot(
        schema_version=1,
        account_fingerprint=account_fingerprint,
        cutoff_at=cutoff_at,
        observed_at=observed_at,
        opening_cash_balances=(asset("USDC", "100", "0.000001", evidence_hash(10)),),
        current_cash_balances=(asset("USDC", cash_current, "0.000001", balance_hash),),
        opening_token_positions=(asset(source_intent.token_id, "0", "0.01", evidence_hash(11)),),
        current_token_positions=(
            asset(source_intent.token_id, position_current, "0.01", balance_hash),
        ),
        opening_allowances=(
            AllowanceObservation(
                schema_version=1,
                asset_id="USDC",
                spender_address="0x" + "1" * 40,
                amount=Decimal("100"),
                quantum=Decimal("0.000001"),
                evidence_hash=evidence_hash(12),
            ),
        ),
        current_allowances=(
            AllowanceObservation(
                schema_version=1,
                asset_id="USDC",
                spender_address="0x" + "1" * 40,
                amount=Decimal(allowance_current),
                quantum=Decimal("0.000001"),
                evidence_hash=evidence_hash(13),
            ),
        ),
        opening_cumulative_fees=(asset("USDC", "0", "0.000001", evidence_hash(14)),),
        current_cumulative_fees=(asset("USDC", fee_current, "0.000001", evidence_hash(15)),),
        open_orders=open_orders,
        recent_trades=recent_trades,
        settlements=settlements,
        opening_cash_source_hash=evidence_hash(20),
        current_cash_source_hash=evidence_hash(21),
        opening_position_source_hash=evidence_hash(22),
        current_position_source_hash=evidence_hash(23),
        opening_allowance_source_hash=evidence_hash(24),
        current_allowance_source_hash=evidence_hash(25),
        opening_fee_source_hash=evidence_hash(26),
        current_fee_source_hash=evidence_hash(27),
        open_orders_source_hash=evidence_hash(28),
        recent_trades_source_hash=evidence_hash(29),
        settlements_source_hash=evidence_hash(30),
    )


def empty_snapshot() -> VenueAccountSnapshot:
    return VenueAccountSnapshot(
        schema_version=1,
        account_fingerprint=ACCOUNT,
        cutoff_at=NOW,
        observed_at=NOW + timedelta(seconds=1),
        opening_cash_balances=(),
        current_cash_balances=(),
        opening_token_positions=(),
        current_token_positions=(),
        opening_allowances=(),
        current_allowances=(),
        opening_cumulative_fees=(),
        current_cumulative_fees=(),
        open_orders=(),
        recent_trades=(),
        settlements=(),
        opening_cash_source_hash=evidence_hash(20),
        current_cash_source_hash=evidence_hash(21),
        opening_position_source_hash=evidence_hash(22),
        current_position_source_hash=evidence_hash(23),
        opening_allowance_source_hash=evidence_hash(24),
        current_allowance_source_hash=evidence_hash(25),
        opening_fee_source_hash=evidence_hash(26),
        current_fee_source_hash=evidence_hash(27),
        open_orders_source_hash=evidence_hash(28),
        recent_trades_source_hash=evidence_hash(29),
        settlements_source_hash=evidence_hash(30),
    )


def test_total_snapshot_reconciliation_closes_only_the_first_economics_view() -> None:
    source_intent = intent()
    future = exact_economics(
        source_intent,
        realized_pnl=Decimal("1.25"),
        information_cutoff=NOW + timedelta(seconds=5),
    )
    later_bypass = AuthoritativeTradeEconomics.model_validate(
        future.model_dump(mode="python"), strict=True
    )
    object.__setattr__(later_bypass, "information_cutoff", NOW + timedelta(seconds=3))
    event = trade_event(source_intent)
    postings = postings_for_confirmed_trades((source_intent,), (event,), (future,))
    snapshot = snapshot_for(
        source_intent,
        future,
        observed_at=NOW + timedelta(seconds=4),
    )
    changing = ScriptedSequence(((future,), (later_bypass,), (later_bypass,)))

    result = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (event,),
        changing,  # type: ignore[arg-type]
    )

    assert not result.complete
    assert result.differences == (f"ECONOMICS_OUTSIDE_SNAPSHOT:{TRADE_ID}",)
    assert changing.iterations == 1


def test_total_snapshot_pnl_never_retraverses_a_future_economics_view() -> None:
    source_intent = intent()
    future = exact_economics(
        source_intent,
        realized_pnl=Decimal("1.25"),
        information_cutoff=NOW + timedelta(seconds=5),
    )
    later_bypass = AuthoritativeTradeEconomics.model_validate(
        future.model_dump(mode="python"), strict=True
    )
    object.__setattr__(later_bypass, "information_cutoff", NOW + timedelta(seconds=3))
    event = trade_event(source_intent)
    postings = postings_for_confirmed_trades((source_intent,), (event,), (future,))
    snapshot = snapshot_for(
        source_intent,
        future,
        observed_at=NOW + timedelta(seconds=4),
    )
    false_complete = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (event,),
        ScriptedSequence(((future,), (later_bypass,))),  # type: ignore[arg-type]
    )
    changing = ScriptedSequence(((future,), (future,), (later_bypass,)))

    assert (
        reconciled_live_pnl(
            postings,
            false_complete,
            snapshot,
            (source_intent,),
            (event,),
            changing,  # type: ignore[arg-type]
        )
        is None
    )
    assert changing.iterations == 1


def test_total_snapshot_pnl_iterates_every_caller_collection_exactly_once() -> None:
    source_intent = intent()
    exact = exact_economics(source_intent, realized_pnl=Decimal("1.25"))
    event = trade_event(source_intent)
    order = order_event(source_intent)
    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))
    snapshot = snapshot_for(source_intent, exact)
    closed = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (event,),
        (exact,),
        (order,),
    )
    posting_values = ScriptedSequence((postings, ()))
    intent_values = ScriptedSequence(((source_intent,), ()))
    trade_values = ScriptedSequence(((event,), ()))
    economics_values = ScriptedSequence(((exact,), ()))
    order_values = ScriptedSequence(((order,), ()))

    assert reconciled_live_pnl(
        posting_values,  # type: ignore[arg-type]
        closed,
        snapshot,
        intent_values,  # type: ignore[arg-type]
        trade_values,  # type: ignore[arg-type]
        economics_values,  # type: ignore[arg-type]
        order_values,  # type: ignore[arg-type]
    ) == Decimal("1.25")
    assert tuple(
        values.iterations
        for values in (
            posting_values,
            intent_values,
            trade_values,
            economics_values,
            order_values,
        )
    ) == (1, 1, 1, 1, 1)


def test_total_snapshot_revalidates_an_alias_mutated_after_it_is_yielded() -> None:
    source_intent = intent()
    exact = exact_economics(source_intent)
    event = trade_event(source_intent)
    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))
    snapshot = snapshot_for(source_intent, exact)
    changing = MutatingAfterYieldSequence(
        AuthoritativeTradeEconomics.model_validate(exact.model_dump(mode="python"), strict=True)
    )

    result = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (event,),
        changing,  # type: ignore[arg-type]
    )

    assert not result.complete
    assert result.differences == ("POSTINGS_INVALID",)
    assert changing.iterations == 1


def test_empty_two_cut_snapshot_reconciles_but_has_no_publishable_pnl() -> None:
    snapshot = empty_snapshot()
    result = reconcile_live_account((), snapshot, (), (), ())

    assert result.complete
    assert result.differences == ()
    assert result.next_action is None
    assert reconciled_live_pnl((), result, snapshot, (), (), ()) is None


def test_exact_independent_deltas_and_identity_evidence_reconcile_deterministically() -> None:
    source_intent, exact, postings = exact_postings()
    snapshot = snapshot_for(source_intent, exact)

    forward = reconcile_with_evidence(postings, snapshot, source_intent, exact)
    reversed_result = reconcile_with_evidence(
        tuple(reversed(postings)), snapshot, source_intent, exact
    )

    assert forward == reversed_result
    assert forward.complete
    assert forward.account_fingerprint == ACCOUNT
    assert forward.expected_posting_ids == tuple(sorted(row.posting_id for row in postings))
    assert {SETTLEMENT_HASH, FEE_HASH, SOURCE_HASH} <= set(forward.evidence_hashes)
    assert forward.venue_trade_hashes == (TRADE_HASH,)
    assert BALANCE_HASH in forward.balance_hashes
    assert forward.reconciliation_id != UUID(int=0)
    assert pnl_with_evidence(postings, forward, snapshot, source_intent, exact) is None


def test_reconciliation_hash_families_separate_raw_events_and_account_reads() -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    snapshot = snapshot_for(source_intent, exact)

    result = reconcile_with_evidence(postings, snapshot, source_intent, exact)

    assert result.venue_order_hashes == ()
    assert result.venue_trade_hashes == (TRADE_HASH,)
    assert {
        snapshot.open_orders_source_hash,
        snapshot.recent_trades_source_hash,
        snapshot.settlements_source_hash,
        SOURCE_HASH,
        SETTLEMENT_HASH,
        FEE_HASH,
        COST_BASIS_HASH,
        exact.economics_fingerprint,
    } <= set(result.evidence_hashes)
    assert BALANCE_HASH in result.balance_hashes
    assert snapshot.opening_allowance_source_hash in result.allowance_hashes
    assert not set(result.venue_trade_hashes) & set(result.evidence_hashes)


def test_canonical_reconciliation_binds_exact_order_history_into_identity() -> None:
    source_intent, exact, postings = exact_postings()
    snapshot = snapshot_for(source_intent, exact)
    event = trade_event(source_intent)
    first_order = order_event(source_intent)
    second_order = order_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687e1"),
        venue_order_id="venue-order-2",
        raw_event_hash=evidence_hash(80),
        received_at=NOW + timedelta(milliseconds=750),
    )

    forward = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (event,),
        (exact,),
        (second_order, first_order, first_order),
    )
    reverse = reconcile_live_account(
        tuple(reversed(postings)),
        snapshot,
        (source_intent,),
        (event,),
        (exact,),
        (first_order, second_order),
    )
    orderless = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (event,),
        (exact,),
    )

    assert forward == reverse
    assert forward.complete
    assert forward.venue_order_hashes == tuple(sorted((ORDER_HASH, evidence_hash(80))))
    assert forward.reconciliation_id != orderless.reconciliation_id
    assert orderless.venue_order_hashes == ()


@pytest.mark.parametrize(
    "defect",
    [
        "malformed",
        "orphan",
        "cross_account",
        "future",
        "before_intent",
        "protocol_mismatch",
        "conflicting_event_id",
        "raw_hash_reuse",
        "venue_order_crosses_intent",
    ],
)
def test_canonical_reconciliation_rejects_invalid_order_history(
    defect: str,
) -> None:
    source_intent, exact, postings = exact_postings()
    snapshot = snapshot_for(source_intent, exact)
    supplied_intents = (source_intent,)
    first = order_event(source_intent)
    supplied_orders: object = (first,)
    expected_hashes = (ORDER_HASH,)
    if defect == "malformed":
        supplied_orders = (object(),)
        expected_hashes = ()
    elif defect == "orphan":
        supplied_orders = (first.model_copy(update={"intent_id": None}),)
    elif defect == "cross_account":
        other_intent = intent(
            account_fingerprint="b" * 64,
            leg_sequence=1,
            token_id="217427",
        )
        supplied_intents = (source_intent, other_intent)
        supplied_orders = (order_event(other_intent),)
    elif defect == "future":
        supplied_orders = (
            order_event(
                source_intent, received_at=snapshot.observed_at + timedelta(microseconds=1)
            ),
        )
    elif defect == "before_intent":
        supplied_orders = (
            order_event(
                source_intent,
                received_at=source_intent.created_at - timedelta(microseconds=1),
            ),
        )
    elif defect == "protocol_mismatch":
        supplied_orders = (order_event(source_intent, protocol_version="other-protocol"),)
    elif defect == "conflicting_event_id":
        supplied_orders = (
            first,
            order_event(
                source_intent,
                event_id=first.event_id,
                raw_event_hash=evidence_hash(81),
                received_at=NOW + timedelta(milliseconds=750),
            ),
        )
        expected_hashes = tuple(sorted((ORDER_HASH, evidence_hash(81))))
    elif defect == "raw_hash_reuse":
        supplied_orders = (
            first,
            order_event(
                source_intent,
                event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687e1"),
                venue_order_id="venue-order-2",
                raw_event_hash=ORDER_HASH,
                received_at=NOW + timedelta(milliseconds=750),
            ),
        )
    elif defect == "venue_order_crosses_intent":
        other_intent = intent(leg_sequence=1, token_id="217427")
        supplied_intents = (source_intent, other_intent)
        supplied_orders = (
            first,
            order_event(
                other_intent,
                event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687e1"),
                raw_event_hash=evidence_hash(81),
                received_at=NOW + timedelta(milliseconds=750),
            ),
        )
        expected_hashes = tuple(sorted((ORDER_HASH, evidence_hash(81))))

    result = reconcile_live_account(
        postings,
        snapshot,
        supplied_intents,
        (trade_event(source_intent),),
        (exact,),
        supplied_orders,  # type: ignore[arg-type]
    )

    assert not result.complete
    assert result.differences == ("ORDER_HISTORY_INVALID",)
    assert result.next_action == "HALT_AND_RECONCILE"
    assert result.venue_order_hashes == expected_hashes


def test_reconciliation_snapshots_order_history_before_caller_mutation() -> None:
    source_intent, exact, postings = exact_postings()
    snapshot = snapshot_for(source_intent, exact)
    mutable_order = order_event(source_intent)

    class MutatingOrderSequence(Sequence[object]):
        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> object:
            del index
            raise IndexError

        def __iter__(self) -> Iterator[object]:
            yield mutable_order
            object.__setattr__(mutable_order, "raw_event_hash", "not-a-hash")

    result = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (trade_event(source_intent),),
        (exact,),
        MutatingOrderSequence(),
    )

    assert not result.complete
    assert result.differences == ("ORDER_HISTORY_INVALID",)
    assert result.venue_order_hashes == ()


def test_pnl_reconstruction_requires_exact_order_history() -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    snapshot = snapshot_for(source_intent, exact)
    trade = trade_event(source_intent)
    order = order_event(source_intent)
    reconciliation = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (trade,),
        (exact,),
        (order,),
    )
    extra = order_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687e1"),
        venue_order_id="venue-order-2",
        raw_event_hash=evidence_hash(81),
        received_at=NOW + timedelta(milliseconds=750),
    )
    mismatched = order.model_copy(
        update={
            "raw_event_hash": evidence_hash(82),
            "lineage_hashes": (evidence_hash(82),),
        }
    )

    assert reconciled_live_pnl(
        postings,
        reconciliation,
        snapshot,
        (source_intent,),
        (trade,),
        (exact,),
        (order,),
    ) == Decimal("1.25")
    assert (
        reconciled_live_pnl(
            postings,
            reconciliation,
            snapshot,
            (source_intent,),
            (trade,),
            (exact,),
        )
        is None
    )
    assert (
        reconciled_live_pnl(
            postings,
            reconciliation,
            snapshot,
            (source_intent,),
            (trade,),
            (exact,),
            (order, extra),
        )
        is None
    )
    assert (
        reconciled_live_pnl(
            postings,
            reconciliation,
            snapshot,
            (source_intent,),
            (trade,),
            (exact,),
            (mismatched,),
        )
        is None
    )


@pytest.mark.parametrize(
    ("collision", "snapshot_updates"),
    [
        ("raw_evidence", {"open_orders_source_hash": TRADE_HASH}),
        ("raw_balance", {"current_fee_source_hash": TRADE_HASH}),
        ("raw_allowance", {"current_allowance_source_hash": TRADE_HASH}),
        ("evidence_balance", {"current_fee_source_hash": SOURCE_HASH}),
        ("evidence_allowance", {"current_allowance_source_hash": SOURCE_HASH}),
        ("balance_allowance", {"current_allowance_source_hash": BALANCE_HASH}),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_typed_hash_family_disjointness_rejects_every_pairwise_collision_in_any_order(
    collision: str,
    snapshot_updates: dict[str, object],
    reverse: bool,
) -> None:
    del collision
    source_intent = intent()
    exact = exact_economics(source_intent, realized_pnl=Decimal("1.25"))
    event = trade_event(source_intent)
    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))
    snapshot = snapshot_for(source_intent, exact).model_copy(
        update={**snapshot_updates, "snapshot_fingerprint": None}
    )
    supplied = tuple(reversed(postings)) if reverse else postings

    result = reconcile_live_account(
        supplied,
        snapshot,
        (source_intent,),
        (event,),
        (exact,),
    )

    assert not result.complete
    assert result.differences == ("HASH_FAMILY_OVERLAP",)
    assert result.next_action == "HALT_AND_RECONCILE"
    assert (
        reconciled_live_pnl(
            supplied,
            result,
            snapshot,
            (source_intent,),
            (event,),
            (exact,),
        )
        is None
    )


@pytest.mark.parametrize("early_path", ["contradiction", "topology"])
def test_typed_hash_family_disjointness_is_enforced_on_every_result_path(
    early_path: str,
) -> None:
    source_intent = intent()
    exact = exact_economics(source_intent)
    event = trade_event(source_intent)
    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))
    snapshot = snapshot_for(source_intent, exact).model_copy(
        update={
            "current_allowance_source_hash": SOURCE_HASH,
            "snapshot_fingerprint": None,
        }
    )
    supplied_trades = (event,)
    supplied_postings = postings
    expected_other = "POSTINGS_INVALID"
    if early_path == "contradiction":
        disguised = event.model_copy(
            update={
                "trade_event_id": UUID("42b33848-ff46-4c45-b9ab-0c74510687f4"),
                "received_at": NOW + timedelta(milliseconds=2500),
            }
        )
        supplied_trades = (event, disguised)
        expected_other = "TRADE_HISTORY_CONTRADICTION"
    else:
        supplied_postings = postings[:-2]

    result = reconcile_live_account(
        supplied_postings,
        snapshot,
        (source_intent,),
        supplied_trades,
        (exact,),
    )

    assert not result.complete
    assert result.differences == tuple(sorted(("HASH_FAMILY_OVERLAP", expected_other)))


def test_reconciliation_and_pnl_ignore_hostile_decimal_context() -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    snapshot = snapshot_for(source_intent, exact)
    expected = reconcile_with_evidence(postings, snapshot, source_intent, exact)
    assert pnl_with_evidence(postings, expected, snapshot, source_intent, exact) == Decimal("1.25")

    for precision, minimum_exponent, maximum_exponent in ((1, -2, 2), (100, -99, 99)):
        with localcontext() as context:
            context.prec = precision
            context.Emin = minimum_exponent
            context.Emax = maximum_exponent
            for signal in (Inexact, Rounded, Overflow, Underflow, InvalidOperation):
                context.traps[signal] = True
            before_flags = dict(context.flags)
            actual = reconcile_with_evidence(postings, snapshot, source_intent, exact)
            assert actual == expected
            assert pnl_with_evidence(postings, actual, snapshot, source_intent, exact) == Decimal(
                "1.25"
            )
            assert dict(context.flags) == before_flags


@pytest.mark.parametrize(
    ("information_cutoff", "cutoff_at", "observed_at", "complete", "difference"),
    [
        (
            NOW + timedelta(seconds=3),
            NOW,
            NOW + timedelta(seconds=4),
            True,
            None,
        ),
        (
            NOW + timedelta(seconds=4),
            NOW,
            NOW + timedelta(seconds=4),
            True,
            None,
        ),
        (
            NOW + timedelta(seconds=3),
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=4),
            False,
            "POSTING_OUTSIDE_SNAPSHOT:venue-trade-1",
        ),
        (
            NOW + timedelta(seconds=5),
            NOW,
            NOW + timedelta(seconds=4),
            False,
            "ECONOMICS_OUTSIDE_SNAPSHOT:venue-trade-1",
        ),
    ],
)
def test_reconciliation_closes_the_full_economics_interval_before_publishing_pnl(
    information_cutoff: datetime,
    cutoff_at: datetime,
    observed_at: datetime,
    complete: bool,
    difference: str | None,
) -> None:
    source_intent = intent()
    exact = exact_economics(
        source_intent,
        realized_pnl=Decimal("1.25"),
        information_cutoff=information_cutoff,
    )
    exact = AuthoritativeTradeEconomics.model_validate(exact.model_dump(mode="python"), strict=True)
    event = trade_event(source_intent)
    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))
    snapshot = snapshot_for(
        source_intent,
        exact,
        cutoff_at=cutoff_at,
        observed_at=observed_at,
    )

    result = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (event,),
        (exact,),
    )

    assert result.complete is complete
    if difference is not None:
        assert difference in result.differences
    expected_pnl = Decimal("1.25") if complete else None
    assert (
        reconciled_live_pnl(
            postings,
            result,
            snapshot,
            (source_intent,),
            (event,),
            (exact,),
        )
        == expected_pnl
    )


def test_one_future_economics_cutoff_prevents_multi_trade_closure_in_any_input_order() -> None:
    source_intent = intent(base_size=Decimal("10"), maximum_spend=Decimal("5.10"))
    first_event = trade_event(source_intent)
    second_event = trade_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f4"),
        venue_trade_id="venue-trade-2",
        raw_event_hash=evidence_hash(70),
        venue_timestamp=NOW + timedelta(seconds=2),
        received_at=NOW + timedelta(seconds=3),
    )
    first_economics = exact_economics(
        source_intent,
        realized_pnl=Decimal("1.25"),
        size=Decimal("5"),
        fee=Decimal("0.005"),
    )
    second_economics = exact_economics(
        source_intent,
        venue_trade_id="venue-trade-2",
        trade_event_hash=evidence_hash(70),
        size=Decimal("5"),
        fee=Decimal("0.005"),
        fee_hash=evidence_hash(71),
        settlement_hash=evidence_hash(72),
        source_hash=evidence_hash(73),
        balance_evidence_hashes=(evidence_hash(74),),
        occurred_at=NOW + timedelta(seconds=2),
        information_cutoff=NOW + timedelta(seconds=5),
    )
    events = (first_event, second_event)
    evidence = (first_economics, second_economics)
    postings = postings_for_confirmed_trades((source_intent,), events, evidence)
    base = snapshot_for(source_intent, first_economics)
    second_trade = base.recent_trades[0].model_copy(
        update={
            "venue_trade_id": "venue-trade-2",
            "trade_event_hash": evidence_hash(70),
            "settlement_hash": evidence_hash(72),
            "fee_hash": evidence_hash(71),
            "source_hash": evidence_hash(73),
            "balance_evidence_hashes": (evidence_hash(74),),
            "economics_fingerprint": second_economics.economics_fingerprint,
            "realized_pnl": None,
            "cost_basis_evidence_hash": None,
            "occurred_at": NOW + timedelta(seconds=2),
        }
    )
    second_settlement = base.settlements[0].model_copy(
        update={
            "venue_trade_id": "venue-trade-2",
            "settlement_hash": evidence_hash(72),
            "evidence_hash": evidence_hash(72),
            "occurred_at": NOW + timedelta(seconds=2),
        }
    )
    snapshot = base.model_copy(
        update={
            "recent_trades": (*base.recent_trades, second_trade),
            "settlements": (*base.settlements, second_settlement),
            "snapshot_fingerprint": None,
        }
    )

    results = []
    for supplied_events, supplied_economics in (
        (events, evidence),
        (tuple(reversed(events)), tuple(reversed(evidence))),
    ):
        result = reconcile_live_account(
            tuple(reversed(postings)),
            snapshot,
            (source_intent,),
            supplied_events,
            supplied_economics,
        )
        results.append(result)
        assert not result.complete
        assert "ECONOMICS_OUTSIDE_SNAPSHOT:venue-trade-2" in result.differences
        assert (
            reconciled_live_pnl(
                postings,
                result,
                snapshot,
                (source_intent,),
                supplied_events,
                supplied_economics,
            )
            is None
        )
    assert results[0] == results[1]


def test_reconciliation_revalidates_a_bypassed_economics_alias_before_closure() -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    snapshot = snapshot_for(source_intent, exact)
    object.__setattr__(exact, "information_cutoff", NOW + timedelta(seconds=5))

    result = reconcile_with_evidence(postings, snapshot, source_intent, exact)

    assert not result.complete
    assert pnl_with_evidence(postings, result, snapshot, source_intent, exact) is None


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (AssetAmountObservation, "amount", hostile_decimal(0, 1_000_000)),
        (AssetAmountObservation, "amount", hostile_decimal(0, -1_000_000)),
        (AssetAmountObservation, "amount", hostile_decimal(1, 1_000_000)),
        (AssetAmountObservation, "amount", hostile_decimal(1, -1_000_000)),
        (AssetAmountObservation, "quantum", hostile_decimal(1, 1_000_000)),
        (AllowanceObservation, "amount", hostile_decimal(0, 1_000_000)),
        (AllowanceObservation, "amount", hostile_decimal(0, -1_000_000)),
        (AllowanceObservation, "quantum", hostile_decimal(1, -1_000_000)),
    ],
)
def test_snapshot_amounts_reject_hostile_exponents_before_quantum_alignment(
    model: type[AssetAmountObservation] | type[AllowanceObservation],
    field: str,
    value: Decimal,
) -> None:
    fields: dict[str, object] = {
        "schema_version": 1,
        "asset_id": "USDC",
        "amount": Decimal("1"),
        "quantum": Decimal("0.000001"),
        "evidence_hash": evidence_hash(60),
    }
    if model is AllowanceObservation:
        fields["spender_address"] = "0x" + "1" * 40
    fields[field] = value

    with pytest.raises(ValueError, match="DECIMAL_RESOURCE_INVALID"):
        bounded_call(lambda: model(**fields))


def test_recent_trade_pnl_and_snapshot_alias_reject_hostile_decimal_resources() -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    snapshot = snapshot_for(source_intent, exact)
    hostile_pnl = snapshot.recent_trades[0].model_dump(mode="python")
    hostile_pnl["realized_pnl"] = hostile_decimal(1, 1_000_000)
    with pytest.raises(ValueError, match="DECIMAL_RESOURCE_INVALID"):
        bounded_call(lambda: RecentTradeObservation.model_validate(hostile_pnl, strict=True))

    hostile_snapshot = snapshot_for(source_intent, exact)
    object.__setattr__(
        hostile_snapshot.current_cash_balances[0],
        "amount",
        hostile_decimal(0, 1_000_000),
    )
    with pytest.raises(LiveReconciliationError, match="SNAPSHOT_INVALID"):
        bounded_call(
            lambda: reconcile_live_account(
                postings,
                hostile_snapshot,
                (source_intent,),
                (trade_event(source_intent),),
                (exact,),
            )
        )


@pytest.mark.parametrize(
    ("snapshot_change", "difference"),
    [
        ({"cash_current": "94.90"}, "CASH_DELTA_MISMATCH:USDC"),
        ({"position_current": "9.99"}, "POSITION_DELTA_MISMATCH:217426"),
        ({"fee_current": "0"}, "FEE_DELTA_MISMATCH:USDC"),
        ({"allowance_current": "99"}, "ALLOWANCE_DELTA_UNEXPLAINED:USDC"),
    ],
)
def test_any_unclosed_account_delta_halts_without_pnl(
    snapshot_change: dict[str, str], difference: str
) -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    snapshot = snapshot_for(source_intent, exact, **snapshot_change)
    result = reconcile_with_evidence(postings, snapshot, source_intent, exact)

    assert not result.complete
    assert difference in result.differences
    assert result.next_action == "HALT_AND_RECONCILE"
    assert pnl_with_evidence(postings, result, snapshot, source_intent, exact) is None


def test_open_order_recent_trade_and_settlement_must_close_exactly() -> None:
    source_intent, exact, postings = exact_postings()
    still_open = OpenOrderObservation(
        schema_version=1,
        venue_order_id=ORDER_ID,
        intent_id=source_intent.intent_id,
        position_asset_id=source_intent.token_id,
        state=VenueOrderState.PARTIALLY_FILLED,
        evidence_hash=evidence_hash(40),
    )

    open_result = reconcile_with_evidence(
        postings,
        snapshot_for(source_intent, exact, open_orders=(still_open,)),
        source_intent,
        exact,
    )
    trade_result = reconcile_with_evidence(
        postings,
        snapshot_for(source_intent, exact, include_trade=False),
        source_intent,
        exact,
    )
    settlement_result = reconcile_with_evidence(
        postings,
        snapshot_for(source_intent, exact, include_settlement=False),
        source_intent,
        exact,
    )
    failed_result = reconcile_with_evidence(
        postings,
        snapshot_for(source_intent, exact, settlement_state=VenueTradeState.FAILED),
        source_intent,
        exact,
    )

    assert "OPEN_ORDER_UNEXPLAINED:venue-order-1" in open_result.differences
    assert "TRADE_MISSING:venue-trade-1" in trade_result.differences
    assert "SETTLEMENT_MISSING:venue-trade-1" in settlement_result.differences
    assert "SETTLEMENT_STATE_MISMATCH:venue-trade-1" in failed_result.differences
    assert all(
        result.next_action == "HALT_AND_RECONCILE"
        for result in (open_result, trade_result, settlement_result, failed_result)
    )


def test_extra_authoritative_trade_and_settlement_facts_remain_differences() -> None:
    source_intent, exact, postings = exact_postings()
    base = snapshot_for(source_intent, exact)
    extra_trade = base.recent_trades[0].model_copy(update={"venue_trade_id": "venue-trade-2"})
    extra_settlement = base.settlements[0].model_copy(update={"venue_trade_id": "venue-trade-2"})
    with_extras = base.model_copy(
        update={
            "recent_trades": (*base.recent_trades, extra_trade),
            "settlements": (*base.settlements, extra_settlement),
            "snapshot_fingerprint": None,
        }
    )

    result = reconcile_with_evidence(postings, with_extras, source_intent, exact)

    assert "TRADE_UNEXPLAINED:venue-trade-2" in result.differences
    assert "SETTLEMENT_UNEXPLAINED:venue-trade-2" in result.differences
    assert result.next_action == "HALT_AND_RECONCILE"


def test_trade_side_assets_and_settlement_asset_must_match_posting_accounts() -> None:
    source_intent, exact, postings = exact_postings()
    base = snapshot_for(source_intent, exact)
    wrong_trade = base.recent_trades[0].model_copy(
        update={
            "cash_asset_id": "ZZZ",
            "position_asset_id": "217427",
            "side": "sell",
        }
    )
    wrong_settlement = base.settlements[0].model_copy(update={"position_asset_id": "217427"})
    mismatched = base.model_copy(
        update={
            "opening_cash_balances": (
                *base.opening_cash_balances,
                asset("ZZZ", "0", "0.000001", evidence_hash(50)),
            ),
            "current_cash_balances": (
                *base.current_cash_balances,
                asset("ZZZ", "0", "0.000001", evidence_hash(51)),
            ),
            "opening_token_positions": (
                *base.opening_token_positions,
                asset("217427", "0", "0.01", evidence_hash(52)),
            ),
            "current_token_positions": (
                *base.current_token_positions,
                asset("217427", "0", "0.01", evidence_hash(53)),
            ),
            "opening_cumulative_fees": (
                *base.opening_cumulative_fees,
                asset("ZZZ", "0", "0.000001", evidence_hash(54)),
            ),
            "current_cumulative_fees": (
                *base.current_cumulative_fees,
                asset("ZZZ", "0", "0.000001", evidence_hash(55)),
            ),
            "recent_trades": (wrong_trade,),
            "settlements": (wrong_settlement,),
            "snapshot_fingerprint": None,
        }
    )

    result = reconcile_with_evidence(postings, mismatched, source_intent, exact)

    assert "TRADE_EVIDENCE_MISMATCH:venue-trade-1" in result.differences
    assert "SETTLEMENT_EVIDENCE_MISMATCH:venue-trade-1" in result.differences


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("trade_event_hash", FEE_HASH),
        ("source_hash", SETTLEMENT_HASH),
        ("settlement_hash", FEE_HASH),
        ("fee_hash", SOURCE_HASH),
        ("balance_evidence_hashes", (SOURCE_HASH,)),
    ],
)
def test_recent_trade_evidence_hash_families_are_equality_bound_by_role(
    field: str,
    replacement: object,
) -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    base = snapshot_for(source_intent, exact)
    substituted = base.recent_trades[0].model_copy(update={field: replacement})
    snapshot_updates: dict[str, object] = {
        "recent_trades": (substituted,),
        "snapshot_fingerprint": None,
    }
    if field == "source_hash":
        snapshot_updates["open_orders_source_hash"] = SOURCE_HASH
    snapshot = base.model_copy(update=snapshot_updates)

    result = reconcile_with_evidence(postings, snapshot, source_intent, exact)

    assert "TRADE_EVIDENCE_MISMATCH:venue-trade-1" in result.differences
    assert pnl_with_evidence(postings, result, snapshot, source_intent, exact) is None


def test_late_nonterminal_trade_history_prevents_reconciliation_and_pnl() -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    confirmed = trade_event(source_intent)
    late_matched = trade_event(
        source_intent,
        state=VenueTradeState.MATCHED,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f4"),
        raw_event_hash=evidence_hash(70),
        received_at=NOW + timedelta(seconds=3),
    )
    snapshot = snapshot_for(source_intent, exact)

    result = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (confirmed, late_matched),
        (exact,),
    )

    assert not result.complete
    assert result.differences == ("TRADE_HISTORY_CONTRADICTION",)
    assert result.venue_trade_hashes == tuple(sorted((TRADE_HASH, evidence_hash(70))))
    assert (
        reconciled_live_pnl(
            postings,
            result,
            snapshot,
            (source_intent,),
            (confirmed, late_matched),
            (exact,),
        )
        is None
    )


@pytest.mark.parametrize(
    "state",
    [
        VenueTradeState.FAILED,
        VenueTradeState.MATCHED_NOT_BROADCASTED,
        VenueTradeState.MATCHED,
        VenueTradeState.MINED,
        VenueTradeState.RETRYING,
    ],
)
@pytest.mark.parametrize("reverse_input", [False, True])
def test_every_unposted_trade_history_group_blocks_mixed_reconciliation_and_pnl(
    state: VenueTradeState,
    reverse_input: bool,
) -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    confirmed = trade_event(source_intent)
    unposted_hash = evidence_hash(70)
    unposted_trade_id = f"unposted-{state.value.lower()}"
    unposted = trade_event(
        source_intent,
        state=state,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f4"),
        venue_order_id="venue-order-unposted",
        venue_trade_id=unposted_trade_id,
        raw_event_hash=unposted_hash,
        received_at=NOW + timedelta(seconds=3),
        sequence_number=1,
    )
    events = (unposted, confirmed) if reverse_input else (confirmed, unposted)
    snapshot = snapshot_for(source_intent, exact)

    result = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        events,
        (exact,),
    )

    expected_prefix = "SETTLEMENT_FAILED" if state is VenueTradeState.FAILED else "TRADE_UNRESOLVED"
    assert not result.complete
    assert f"{expected_prefix}:{unposted_trade_id}" in result.differences
    assert result.next_action == "HALT_AND_RECONCILE"
    assert result.venue_trade_hashes == tuple(sorted((TRADE_HASH, unposted_hash)))
    assert (
        reconciled_live_pnl(
            postings,
            result,
            snapshot,
            (source_intent,),
            events,
            (exact,),
        )
        is None
    )


@pytest.mark.parametrize(
    "state",
    [
        VenueTradeState.FAILED,
        VenueTradeState.MATCHED_NOT_BROADCASTED,
        VenueTradeState.MATCHED,
        VenueTradeState.MINED,
        VenueTradeState.RETRYING,
    ],
)
def test_unposted_trade_history_without_any_confirmed_posting_still_blocks_closure(
    state: VenueTradeState,
) -> None:
    source_intent = intent()
    unposted_hash = evidence_hash(70)
    unposted_trade_id = f"unposted-{state.value.lower()}"
    unposted = trade_event(
        source_intent,
        state=state,
        venue_trade_id=unposted_trade_id,
        raw_event_hash=unposted_hash,
        received_at=NOW + timedelta(milliseconds=500),
        sequence_number=1,
    )
    snapshot = empty_snapshot()

    result = reconcile_live_account(
        (),
        snapshot,
        (source_intent,),
        (unposted,),
        (),
    )

    expected_prefix = "SETTLEMENT_FAILED" if state is VenueTradeState.FAILED else "TRADE_UNRESOLVED"
    assert not result.complete
    assert result.differences == (f"{expected_prefix}:{unposted_trade_id}",)
    assert result.next_action == "HALT_AND_RECONCILE"
    assert result.venue_trade_hashes == (unposted_hash,)
    assert result.expected_posting_ids == ()
    assert reconciled_live_pnl((), result, snapshot, (source_intent,), (unposted,), ()) is None


def test_unposted_history_for_a_second_intent_is_not_hidden_by_confirmed_postings() -> None:
    first_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    second_intent = intent(
        plan_id=UUID("1f067bf8-b4b8-418d-85ce-cf7702b810c1"),
        intent_id=UUID("267931fd-9ea7-429e-a2ae-cc7458231cd4"),
    )
    unresolved_hash = evidence_hash(70)
    events = (
        trade_event(first_intent),
        trade_event(
            second_intent,
            state=VenueTradeState.MINED,
            event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f4"),
            venue_order_id="venue-order-second",
            venue_trade_id="venue-trade-second-intent",
            raw_event_hash=unresolved_hash,
            received_at=NOW + timedelta(seconds=3),
            sequence_number=1,
        ),
    )
    snapshot = snapshot_for(first_intent, exact)

    forward = reconcile_live_account(
        postings,
        snapshot,
        (first_intent, second_intent),
        events,
        (exact,),
    )
    reverse = reconcile_live_account(
        tuple(reversed(postings)),
        snapshot,
        (second_intent, first_intent),
        tuple(reversed(events)),
        (exact,),
    )

    assert forward == reverse
    assert not forward.complete
    assert "TRADE_UNRESOLVED:venue-trade-second-intent" in forward.differences
    assert forward.venue_trade_hashes == tuple(sorted((TRADE_HASH, unresolved_hash)))


def test_wrong_account_cross_cutoff_and_missing_balance_evidence_are_closed_differences() -> None:
    source_intent, exact, postings = exact_postings()

    wrong_account = reconcile_with_evidence(
        postings,
        snapshot_for(source_intent, exact, account_fingerprint="b" * 64),
        source_intent,
        exact,
    )
    cross_cutoff = reconcile_with_evidence(
        postings,
        snapshot_for(source_intent, exact, cutoff_at=NOW + timedelta(seconds=1)),
        source_intent,
        exact,
    )
    missing_evidence = reconcile_with_evidence(
        postings,
        snapshot_for(source_intent, exact, balance_hash=evidence_hash(41)),
        source_intent,
        exact,
    )

    assert "POSTING_ACCOUNT_MISMATCH" in wrong_account.differences
    assert "POSTING_OUTSIDE_SNAPSHOT:venue-trade-1" in cross_cutoff.differences
    assert "POSTING_EVIDENCE_MISSING:venue-trade-1" in missing_evidence.differences


@pytest.mark.parametrize("expected_pnl", [Decimal("1.25"), Decimal("-0.75")])
def test_explicit_balanced_cost_basis_pnl_is_publishable_only_after_exact_closure(
    expected_pnl: Decimal,
) -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=expected_pnl)
    snapshot = snapshot_for(source_intent, exact)
    result = reconcile_with_evidence(postings, snapshot, source_intent, exact)

    assert result.complete
    assert COST_BASIS_HASH in result.evidence_hashes
    assert pnl_with_evidence(postings, result, snapshot, source_intent, exact) == expected_pnl
    assert pnl_with_evidence(postings[:-1], result, snapshot, source_intent, exact) is None


def test_reconciliation_rejects_balanced_subset_that_omits_canonical_pnl_pair() -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    incomplete = tuple(
        row
        for row in postings
        if "realized_pnl"
        not in {row.debit_account.partition(":")[0], row.credit_account.partition(":")[0]}
    )

    result = reconcile_live_account(
        incomplete,
        snapshot_for(source_intent, exact),
        (source_intent,),
        (trade_event(source_intent),),
        (exact,),
    )

    assert "POSTING_TOPOLOGY_MISMATCH" in result.differences
    assert result.next_action == "HALT_AND_RECONCILE"


def test_recent_trade_observation_commits_to_exact_signed_realized_pnl() -> None:
    assert "realized_pnl" in RecentTradeObservation.model_fields


def test_public_round_tripped_reconciliation_revalidates_and_publishes_exact_pnl() -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    snapshot = snapshot_for(source_intent, exact)
    result = reconcile_live_account(
        postings,
        snapshot,
        (source_intent,),
        (trade_event(source_intent),),
        (exact,),
    )
    round_tripped = LiveReconciliation.model_validate_json(result.model_dump_json())

    assert type(round_tripped) is LiveReconciliation
    assert reconciled_live_pnl(
        postings,
        round_tripped,
        snapshot,
        (source_intent,),
        (trade_event(source_intent),),
        (exact,),
    ) == Decimal("1.25")


def test_pnl_amount_and_sign_must_match_the_exact_economics_commitment() -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    base = snapshot_for(source_intent, exact)
    wrong_trade = base.recent_trades[0].model_copy(update={"realized_pnl": Decimal("-1.25")})
    mismatched = base.model_copy(
        update={"recent_trades": (wrong_trade,), "snapshot_fingerprint": None}
    )

    result = reconcile_with_evidence(postings, mismatched, source_intent, exact)

    assert "TRADE_EVIDENCE_MISMATCH:venue-trade-1" in result.differences
    assert pnl_with_evidence(postings, result, mismatched, source_intent, exact) is None


def test_forged_public_reconciliation_cannot_publish_pnl() -> None:
    source_intent, exact, postings = exact_postings(realized_pnl=Decimal("1.25"))
    snapshot = snapshot_for(source_intent, exact)
    result = reconcile_with_evidence(postings, snapshot, source_intent, exact)
    forged = result.model_copy(
        update={"reconciliation_id": UUID("42b33848-ff46-4c45-b9ab-0c74510687f5")}
    )

    assert pnl_with_evidence(postings, forged, snapshot, source_intent, exact) is None


def test_snapshot_requires_exact_utc_ordering_sorted_unique_facts_and_matching_cuts() -> None:
    source_intent, exact, _ = exact_postings()
    valid = snapshot_for(source_intent, exact)

    with pytest.raises(ValueError, match="VENUE_ACCOUNT_SNAPSHOT_INVALID"):
        valid.model_copy(update={"observed_at": NOW})
    with pytest.raises(ValueError, match="VENUE_ACCOUNT_SNAPSHOT_INVALID"):
        valid.model_copy(update={"observed_at": NOW.replace(tzinfo=timezone(timedelta(hours=-4)))})
    with pytest.raises(ValueError, match="VENUE_ACCOUNT_SNAPSHOT_INVALID"):
        valid.model_copy(update={"current_cash_balances": ()})
    with pytest.raises(ValueError, match="VENUE_ACCOUNT_SNAPSHOT_INVALID"):
        valid.model_copy(
            update={
                "current_cash_balances": (
                    asset("ZZZ", "1", "0.01", evidence_hash(42)),
                    *valid.current_cash_balances,
                )
            }
        )
    with pytest.raises(ValueError, match="VENUE_ACCOUNT_SNAPSHOT_INVALID"):
        VenueAccountSnapshot.model_construct()


def test_snapshot_denies_reinitialization_and_reconciliation_is_publicly_round_trippable() -> None:
    source_intent, exact, postings = exact_postings()
    snapshot = snapshot_for(source_intent, exact)
    snapshot_alias = snapshot
    snapshot_fields = snapshot.model_dump(mode="python")
    snapshot_fields["account_fingerprint"] = "b" * 64

    with pytest.raises(ValueError, match="VENUE_ACCOUNT_SNAPSHOT_INVALID"):
        snapshot.__init__(**snapshot_fields)
    assert snapshot_alias.account_fingerprint == ACCOUNT
    with pytest.raises(TypeError, match="VENUE_ACCOUNT_SNAPSHOT_NOT_SUBCLASSABLE"):

        class ForkedSnapshot(VenueAccountSnapshot):
            pass

    reconciliation = reconcile_with_evidence(postings, snapshot, source_intent, exact)
    assert type(reconciliation) is LiveReconciliation
    assert (
        LiveReconciliation.model_validate_json(reconciliation.model_dump_json()) == reconciliation
    )
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(ValueError, match="VENUE_ACCOUNT_SNAPSHOT_INVALID"):
            operation(snapshot)
    with pytest.raises(ValueError, match="VENUE_ACCOUNT_SNAPSHOT_INVALID"):
        snapshot.__getstate__()


def test_reconciliation_snapshots_inputs_and_rejects_malformed_posting_sets_stably() -> None:
    source_intent, exact, postings = exact_postings()
    snapshot = snapshot_for(source_intent, exact)

    duplicate = reconcile_with_evidence((postings[0], postings[0]), snapshot, source_intent, exact)
    assert duplicate.differences == ("POSTINGS_INVALID",)
    assert duplicate.next_action == "HALT_AND_RECONCILE"

    with pytest.raises(LiveReconciliationError, match="SNAPSHOT_INVALID"):
        reconcile_live_account(
            postings,
            object(),  # type: ignore[arg-type]
            (source_intent,),
            (trade_event(source_intent),),
            (exact,),
        )
    with pytest.raises(ValidationError):
        snapshot.current_cash_balances[0].amount = Decimal("0")
