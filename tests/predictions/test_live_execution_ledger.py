from __future__ import annotations

import copy
import pickle
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import (
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Underflow,
    localcontext,
)
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from polytrading.predictions.execution.ledger import (
    MAX_LIVE_EVIDENCE_ITEMS,
    AuthoritativeTradeEconomics,
    LiveLedgerError,
    _decimal_coefficient,
    _ExactArithmeticError,
    _is_quantized,
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


class ScriptedSequence(Sequence[object]):
    """A bounded hostile Sequence with explicit traversal and length behavior."""

    def __init__(
        self,
        views: tuple[tuple[object, ...], ...],
        *,
        reported_length: int = 0,
    ) -> None:
        self._views = views
        self._reported_length = reported_length
        self.iterations = 0
        self.yields = 0

    def __len__(self) -> int:
        return self._reported_length

    def __getitem__(self, index: int) -> object:
        del index
        raise IndexError

    def __iter__(self) -> Iterator[object]:
        view = self._views[min(self.iterations, len(self._views) - 1)]
        self.iterations += 1
        for value in view:
            self.yields += 1
            yield value


class CappedInfiniteSequence(Sequence[object]):
    """Models an infinite Sequence while making a regression fail in bounded work."""

    def __init__(self, value: object) -> None:
        self._value = value
        self.iterations = 0
        self.yields = 0

    def __len__(self) -> int:
        return 0

    def __getitem__(self, index: int) -> object:
        del index
        raise IndexError

    def __iter__(self) -> Iterator[object]:
        self.iterations += 1
        for _ in range(MAX_LIVE_EVIDENCE_ITEMS + 1):
            self.yields += 1
            yield self._value
        raise AssertionError("sequence consumption exceeded the MAX+1 guard")


class RaisingIteratorSequence(Sequence[object]):
    def __len__(self) -> int:
        raise RuntimeError("hostile length")

    def __getitem__(self, index: int) -> object:
        del index
        raise IndexError

    def __iter__(self) -> Iterator[object]:
        raise RuntimeError("hostile iterator")


def hostile_decimal(coefficient: int, exponent: int) -> Decimal:
    return Decimal((int(coefficient < 0), tuple(map(int, str(abs(coefficient)))), exponent))


def bounded_call[ResultT](operation: object) -> ResultT:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(operation)  # type: ignore[arg-type]
        return future.result(timeout=1)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


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
        trade_event_id=event_id or uuid4(),
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


@pytest.mark.parametrize(
    ("actual_count", "accepted"),
    [
        (0, True),
        (MAX_LIVE_EVIDENCE_ITEMS, True),
        (MAX_LIVE_EVIDENCE_ITEMS + 1, False),
    ],
)
def test_total_snapshot_enforces_actual_empty_max_and_max_plus_one_boundaries(
    actual_count: int,
    accepted: bool,
) -> None:
    source_intent = intent()
    values = ScriptedSequence(
        ((source_intent,) * actual_count,),
        reported_length=0,
    )

    if accepted:
        assert bounded_call(lambda: postings_for_confirmed_trades(values, (), ())) == ()
    else:
        with pytest.raises(LiveLedgerError, match="INTENT_EVIDENCE_INVALID"):
            bounded_call(lambda: postings_for_confirmed_trades(values, (), ()))

    assert values.iterations == 1
    assert values.yields == min(actual_count, MAX_LIVE_EVIDENCE_ITEMS + 1)


def test_total_snapshot_does_not_trust_a_huge_reported_length_for_one_value() -> None:
    source_intent = intent()
    values = ScriptedSequence(
        ((source_intent,),),
        reported_length=MAX_LIVE_EVIDENCE_ITEMS + 1_000_000,
    )

    assert bounded_call(lambda: postings_for_confirmed_trades(values, (), ())) == ()
    assert values.iterations == values.yields == 1


def test_total_snapshot_caps_an_infinite_sequence_before_any_unbounded_work() -> None:
    values = CappedInfiniteSequence(intent())

    with pytest.raises(LiveLedgerError, match="INTENT_EVIDENCE_INVALID"):
        bounded_call(lambda: postings_for_confirmed_trades(values, (), ()))

    assert values.iterations == 1
    assert values.yields == MAX_LIVE_EVIDENCE_ITEMS + 1


def test_total_snapshot_translates_iterator_faults_to_the_public_stable_code() -> None:
    with pytest.raises(LiveLedgerError, match="INTENT_EVIDENCE_INVALID") as exc_info:
        bounded_call(lambda: postings_for_confirmed_trades(RaisingIteratorSequence(), (), ()))

    assert exc_info.value.__cause__ is None


def test_empty_live_ledger_is_conserved() -> None:
    assert verify_live_conservation((), (), (), ()) is None


def test_confirmed_buy_creates_exact_balanced_cash_position_and_fee_pairs() -> None:
    source_intent = intent()
    event = trade_event(source_intent)
    exact = economics(source_intent)

    postings = postings_for_confirmed_trades(
        (source_intent,),
        (event,),
        (exact,),
    )

    assert len(postings) == 6
    assert tuple(posting.posting_id for posting in postings) == tuple(
        sorted(posting.posting_id for posting in postings)
    )
    assert (
        verify_live_conservation(tuple(reversed(postings)), (source_intent,), (event,), (exact,))
        is None
    )
    assert account_net(postings, "venue_cash:USDC", "USDC") == Decimal("-5.11")
    assert account_net(postings, "venue_position:217426", "217426") == Decimal("10")
    assert account_net(postings, "fees_paid:USDC", "USDC") == Decimal("0.01")
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


def test_cumulative_fills_cannot_exceed_intent_size_or_spend() -> None:
    source_intent = intent(base_size=Decimal("10"), maximum_spend=Decimal("5.10"))
    first = trade_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
        venue_trade_id="venue-trade-1",
        raw_event_hash="1" * 64,
    )
    second = trade_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f4"),
        venue_trade_id="venue-trade-2",
        raw_event_hash="7" * 64,
        received_at=NOW + timedelta(seconds=3),
    )
    first_economics = economics(
        source_intent,
        venue_trade_id="venue-trade-1",
        trade_event_hash="1" * 64,
        size=Decimal("6"),
        fee=Decimal("0"),
    )
    second_economics = economics(
        source_intent,
        venue_trade_id="venue-trade-2",
        trade_event_hash="7" * 64,
        size=Decimal("5"),
        fee=Decimal("0"),
        information_cutoff=NOW + timedelta(seconds=4),
    )

    for events, evidence in (
        ((first, second), (first_economics, second_economics)),
        ((second, first), (second_economics, first_economics)),
    ):
        with pytest.raises(LiveLedgerError, match="INTENT_TRADE_BOUNDS_EXCEEDED"):
            postings_for_confirmed_trades((source_intent,), events, evidence)


def test_one_intent_cannot_bind_multiple_venue_orders() -> None:
    source_intent = intent(base_size=Decimal("10"), maximum_spend=Decimal("5.10"))
    first = trade_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
        venue_order_id="venue-order-1",
        venue_trade_id="venue-trade-1",
        raw_event_hash="1" * 64,
    )
    second = trade_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f4"),
        venue_order_id="venue-order-2",
        venue_trade_id="venue-trade-2",
        raw_event_hash="7" * 64,
        received_at=NOW + timedelta(seconds=3),
    )
    first_economics = economics(
        source_intent,
        venue_order_id="venue-order-1",
        venue_trade_id="venue-trade-1",
        trade_event_hash="1" * 64,
        size=Decimal("5"),
        fee=Decimal("0"),
    )
    second_economics = economics(
        source_intent,
        venue_order_id="venue-order-2",
        venue_trade_id="venue-trade-2",
        trade_event_hash="7" * 64,
        size=Decimal("5"),
        fee=Decimal("0"),
        information_cutoff=NOW + timedelta(seconds=4),
    )

    with pytest.raises(LiveLedgerError, match="INTENT_ORDER_GROUP_INVALID"):
        postings_for_confirmed_trades(
            (source_intent,),
            (second, first),
            (first_economics, second_economics),
        )


def test_one_venue_order_cannot_span_intents() -> None:
    first_intent = intent(base_size=Decimal("5"), maximum_spend=Decimal("2.55"))
    second_intent = intent(
        plan_id=UUID("0d7c250b-0a21-55f3-a897-8bc98c59f905"),
        leg_sequence=1,
        base_size=Decimal("5"),
        maximum_spend=Decimal("2.55"),
    )
    first = trade_event(
        first_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
        venue_trade_id="venue-trade-1",
        raw_event_hash="1" * 64,
    )
    second = trade_event(
        second_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f4"),
        venue_trade_id="venue-trade-2",
        raw_event_hash="7" * 64,
        received_at=NOW + timedelta(seconds=3),
    )
    first_economics = economics(
        first_intent,
        venue_trade_id="venue-trade-1",
        trade_event_hash="1" * 64,
        size=Decimal("5"),
        fee=Decimal("0"),
    )
    second_economics = economics(
        second_intent,
        venue_trade_id="venue-trade-2",
        trade_event_hash="7" * 64,
        size=Decimal("5"),
        fee=Decimal("0"),
        information_cutoff=NOW + timedelta(seconds=4),
    )

    with pytest.raises(LiveLedgerError, match="INTENT_ORDER_GROUP_INVALID"):
        postings_for_confirmed_trades(
            (first_intent, second_intent),
            (first, second),
            (first_economics, second_economics),
        )


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
    "terminal_state",
    [VenueTradeState.CONFIRMED, VenueTradeState.FAILED],
)
@pytest.mark.parametrize(
    "later_state",
    [
        VenueTradeState.MATCHED_NOT_BROADCASTED,
        VenueTradeState.MATCHED,
        VenueTradeState.MINED,
        VenueTradeState.RETRYING,
    ],
)
def test_every_nonterminal_event_after_either_terminal_state_is_contradictory(
    terminal_state: VenueTradeState,
    later_state: VenueTradeState,
) -> None:
    source_intent = intent()
    terminal = trade_event(
        source_intent,
        state=terminal_state,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f2"),
        sequence_number=10,
    )
    later = trade_event(
        source_intent,
        state=later_state,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f4"),
        raw_event_hash="7" * 64,
        received_at=NOW + timedelta(seconds=3),
        sequence_number=11,
    )
    exact = (economics(source_intent),) if terminal_state is VenueTradeState.CONFIRMED else ()

    for history in ((terminal, later), (later, terminal)):
        with pytest.raises(LiveLedgerError, match="TRADE_EVENT_CONFLICT"):
            postings_for_confirmed_trades((source_intent,), history, exact)


@pytest.mark.parametrize("later_sequence", [5, 4])
def test_later_distinct_trade_event_rejects_equal_or_regressing_sequence(
    later_sequence: int,
) -> None:
    source_intent = intent()
    matched = trade_event(
        source_intent,
        state=VenueTradeState.MATCHED,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f2"),
        raw_event_hash="7" * 64,
        received_at=NOW + timedelta(milliseconds=1500),
        sequence_number=5,
    )
    confirmed = trade_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
        sequence_number=later_sequence,
    )

    with pytest.raises(LiveLedgerError, match="TRADE_EVENT_CONFLICT"):
        postings_for_confirmed_trades(
            (source_intent,),
            (confirmed, matched),
            (economics(source_intent),),
        )


def test_nonterminal_progress_and_retry_before_one_terminal_outcome_remains_valid() -> None:
    source_intent = intent()
    progress = (
        trade_event(
            source_intent,
            state=VenueTradeState.MATCHED,
            event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f0"),
            raw_event_hash="7" * 64,
            received_at=NOW + timedelta(milliseconds=1250),
            sequence_number=1,
        ),
        trade_event(
            source_intent,
            state=VenueTradeState.RETRYING,
            event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f1"),
            raw_event_hash="8" * 64,
            received_at=NOW + timedelta(milliseconds=1500),
            sequence_number=2,
        ),
        trade_event(
            source_intent,
            state=VenueTradeState.MINED,
            event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f2"),
            raw_event_hash="9" * 64,
            received_at=NOW + timedelta(milliseconds=1750),
            sequence_number=3,
        ),
        trade_event(
            source_intent,
            event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
            sequence_number=4,
        ),
    )
    exact = economics(source_intent)

    forward = postings_for_confirmed_trades((source_intent,), progress, (exact,))
    reversed_result = postings_for_confirmed_trades(
        (source_intent,), tuple(reversed(progress)), (exact,)
    )

    assert forward == reversed_result
    assert len(forward) == 6


@pytest.mark.parametrize(
    ("matched_id", "confirmed_id"),
    [
        (
            UUID("42b33848-ff46-4c45-b9ab-0c74510687f0"),
            UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
        ),
        (
            UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
            UUID("42b33848-ff46-4c45-b9ab-0c74510687f0"),
        ),
    ],
)
@pytest.mark.parametrize("reverse_input", [False, True])
def test_terminal_receipt_tie_uses_complete_sequence_evidence_not_uuid_or_input_order(
    matched_id: UUID,
    confirmed_id: UUID,
    reverse_input: bool,
) -> None:
    source_intent = intent()
    receipt = NOW + timedelta(seconds=2)
    matched = trade_event(
        source_intent,
        state=VenueTradeState.MATCHED,
        event_id=matched_id,
        raw_event_hash="7" * 64,
        received_at=receipt,
        sequence_number=1,
    )
    confirmed = trade_event(
        source_intent,
        event_id=confirmed_id,
        received_at=receipt,
        sequence_number=2,
    )
    history = (confirmed, matched) if reverse_input else (matched, confirmed)

    postings = postings_for_confirmed_trades((source_intent,), history, (economics(source_intent),))

    assert len(postings) == 6


@pytest.mark.parametrize(
    ("matched_sequence", "terminal_sequence"),
    [(None, None), (1, None), (None, 2), (1, 1), (2, 1)],
)
@pytest.mark.parametrize(
    "terminal_state",
    [VenueTradeState.CONFIRMED, VenueTradeState.FAILED],
)
def test_terminal_receipt_tie_rejects_missing_mixed_equal_or_reversed_sequences(
    matched_sequence: int | None,
    terminal_sequence: int | None,
    terminal_state: VenueTradeState,
) -> None:
    source_intent = intent()
    receipt = NOW + timedelta(seconds=2)
    matched = trade_event(
        source_intent,
        state=VenueTradeState.MATCHED,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f0"),
        raw_event_hash="7" * 64,
        received_at=receipt,
        sequence_number=matched_sequence,
    )
    terminal = trade_event(
        source_intent,
        state=terminal_state,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
        received_at=receipt,
        sequence_number=terminal_sequence,
    )
    exact = (economics(source_intent),) if terminal_state is VenueTradeState.CONFIRMED else ()

    for history in ((matched, terminal), (terminal, matched)):
        with pytest.raises(LiveLedgerError, match="TRADE_EVENT_CONFLICT"):
            postings_for_confirmed_trades((source_intent,), history, exact)


@pytest.mark.parametrize(
    "terminal_state",
    [VenueTradeState.CONFIRMED, VenueTradeState.FAILED],
)
def test_complete_distinct_sequences_close_either_terminal_state_at_a_receipt_tie(
    terminal_state: VenueTradeState,
) -> None:
    source_intent = intent()
    receipt = NOW + timedelta(seconds=2)
    matched = trade_event(
        source_intent,
        state=VenueTradeState.MATCHED,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
        raw_event_hash="7" * 64,
        received_at=receipt,
        sequence_number=1,
    )
    terminal = trade_event(
        source_intent,
        state=terminal_state,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f0"),
        received_at=receipt,
        sequence_number=2,
    )
    exact = (economics(source_intent),) if terminal_state is VenueTradeState.CONFIRMED else ()

    result = postings_for_confirmed_trades((source_intent,), (terminal, matched), exact)

    assert len(result) == (6 if terminal_state is VenueTradeState.CONFIRMED else 0)


def test_all_nonterminal_receipt_tie_needs_no_invented_order_before_a_later_terminal() -> None:
    source_intent = intent()
    receipt = NOW + timedelta(milliseconds=1500)
    tied = tuple(
        trade_event(
            source_intent,
            state=state,
            event_id=UUID(f"42b33848-ff46-4c45-b9ab-0c74510687f{index}"),
            raw_event_hash=f"{index + 7:x}" * 64,
            received_at=receipt,
        )
        for index, state in enumerate(
            (
                VenueTradeState.MATCHED_NOT_BROADCASTED,
                VenueTradeState.MATCHED,
                VenueTradeState.MINED,
                VenueTradeState.RETRYING,
            )
        )
    )
    confirmed = trade_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f5"),
        received_at=NOW + timedelta(seconds=2),
    )

    forward = postings_for_confirmed_trades(
        (source_intent,), (*tied, confirmed), (economics(source_intent),)
    )
    reverse = postings_for_confirmed_trades(
        (source_intent,), (confirmed, *reversed(tied)), (economics(source_intent),)
    )

    assert forward == reverse
    assert len(forward) == 6


def test_nonterminal_only_histories_across_trade_ids_remain_unresolved_without_postings() -> None:
    source_intent = intent()
    states = (
        VenueTradeState.MATCHED_NOT_BROADCASTED,
        VenueTradeState.MATCHED,
        VenueTradeState.MINED,
        VenueTradeState.RETRYING,
    )
    histories = tuple(
        trade_event(
            source_intent,
            state=state,
            event_id=UUID(f"42b33848-ff46-4c45-b9ab-0c74510687{index:02x}"),
            venue_trade_id=f"venue-trade-{index}",
            raw_event_hash=f"{index + 7:x}" * 64,
            received_at=NOW + timedelta(seconds=index + 1),
            sequence_number=1,
        )
        for index, state in enumerate(states, start=1)
    )

    assert postings_for_confirmed_trades((source_intent,), histories, ()) == ()
    assert postings_for_confirmed_trades((source_intent,), tuple(reversed(histories)), ()) == ()


def test_distinct_event_id_cannot_reuse_a_raw_trade_record_hash() -> None:
    source_intent = intent()
    first = trade_event(
        source_intent,
        state=VenueTradeState.MATCHED,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f0"),
        raw_event_hash="7" * 64,
        received_at=NOW + timedelta(milliseconds=1500),
    )
    disguised_replay = trade_event(
        source_intent,
        state=VenueTradeState.RETRYING,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f1"),
        raw_event_hash="7" * 64,
        received_at=NOW + timedelta(milliseconds=1750),
    )

    with pytest.raises(LiveLedgerError, match="TRADE_EVENT_CONFLICT"):
        postings_for_confirmed_trades((source_intent,), (first, disguised_replay), ())


@pytest.mark.parametrize("identity_field", ["intent_id", "venue_order_id", "protocol_version"])
def test_one_trade_history_rejects_conflicting_nonnull_identity(
    identity_field: str,
) -> None:
    source_intent = intent()
    first = trade_event(
        source_intent,
        state=VenueTradeState.MATCHED,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f0"),
        raw_event_hash="7" * 64,
        received_at=NOW + timedelta(milliseconds=1500),
    )
    replacement: object = {
        "intent_id": uuid4(),
        "venue_order_id": "venue-order-conflict",
        "protocol_version": "protocol-conflict",
    }[identity_field]
    second = trade_event(
        source_intent,
        state=VenueTradeState.RETRYING,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f1"),
        raw_event_hash="8" * 64,
        received_at=NOW + timedelta(milliseconds=1750),
    ).model_copy(update={identity_field: replacement})

    with pytest.raises(LiveLedgerError, match="TRADE_EVENT_CONFLICT"):
        postings_for_confirmed_trades((source_intent,), (second, first), ())


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("price", Decimal("1.01"), "TRADE_ECONOMICS_INVALID"),
        ("price", Decimal("0.5100001"), "DECIMAL_RESOURCE_INVALID"),
        ("size", Decimal("10.001"), "TRADE_ECONOMICS_INVALID"),
        ("fee", Decimal("0.0000001"), "DECIMAL_RESOURCE_INVALID"),
        ("cash_quantum", Decimal("0.0000001"), "DECIMAL_RESOURCE_INVALID"),
        ("position_quantum", Decimal("0.001"), "TRADE_ECONOMICS_INVALID"),
    ],
)
def test_economics_rejects_excess_or_nondivisible_precision(
    field: str,
    value: Decimal,
    error_code: str,
) -> None:
    source_intent = intent()
    with pytest.raises(ValueError, match=error_code):
        economics(source_intent, **{field: value})


def test_authoritative_notional_never_uses_ambient_decimal_rounding() -> None:
    source_intent = intent(
        base_size=Decimal("11"),
        maximum_spend=Decimal("5.61"),
    )

    with localcontext() as context:
        context.prec = 2
        with pytest.raises(ValueError, match="TRADE_ECONOMICS_INVALID"):
            economics(
                source_intent,
                price=Decimal("0.51"),
                size=Decimal("11"),
                fee=Decimal("0"),
                cash_quantum=Decimal("0.1"),
                position_quantum=Decimal("1"),
            )


@pytest.mark.parametrize(
    ("field", "value", "extra"),
    [
        ("price", hostile_decimal(1, 1_000_000), {}),
        ("size", hostile_decimal(1, -1_000_000), {}),
        ("fee", hostile_decimal(0, 1_000_000), {}),
        ("cash_quantum", hostile_decimal(1, 1_000_000), {}),
        ("position_quantum", hostile_decimal(1, -1_000_000), {}),
        (
            "realized_pnl",
            hostile_decimal(1, -1_000_000),
            {"cost_basis_evidence_hash": COST_BASIS_HASH},
        ),
    ],
)
def test_every_economics_decimal_rejects_hostile_resource_shapes_before_arithmetic(
    field: str,
    value: Decimal,
    extra: dict[str, object],
) -> None:
    source_intent = intent()

    with pytest.raises(ValueError, match="DECIMAL_RESOURCE_INVALID"):
        bounded_call(lambda: economics(source_intent, **{field: value}, **extra))


@pytest.mark.parametrize(
    "value",
    [
        hostile_decimal(0, 1_000_000),
        hostile_decimal(0, -1_000_000),
        hostile_decimal(1, 1_000_000),
        hostile_decimal(1, -1_000_000),
        hostile_decimal(int("9" * 38), -6),
        hostile_decimal(9, 30),
    ],
)
def test_exact_helpers_reject_unbounded_coefficients_and_exponents_before_alignment(
    value: Decimal,
) -> None:
    with pytest.raises(_ExactArithmeticError, match="DECIMAL_RESOURCE_INVALID"):
        bounded_call(lambda: _decimal_coefficient(value))
    assert bounded_call(lambda: _is_quantized(value, Decimal("0.000001"))) is False


def test_posting_rejects_a_hostile_zero_on_the_inactive_side_before_netting() -> None:
    source_intent = intent()
    event = trade_event(source_intent)
    exact = economics(source_intent)
    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))
    hostile = postings[0].model_copy(update={"credit_amount": hostile_decimal(0, 1_000_000)})

    with pytest.raises(LiveLedgerError, match="POSTING_AMOUNT_INVALID"):
        bounded_call(
            lambda: verify_live_conservation(
                (hostile, *postings[1:]),
                (source_intent,),
                (event,),
                (exact,),
            )
        )


def test_valid_economics_and_posting_identities_ignore_hostile_decimal_context() -> None:
    source_intent = intent()
    event = trade_event(
        source_intent,
        event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
    )
    expected_economics = economics(source_intent)
    expected_postings = postings_for_confirmed_trades(
        (source_intent,),
        (event,),
        (expected_economics,),
    )

    for precision, minimum_exponent, maximum_exponent in ((1, -2, 2), (100, -99, 99)):
        with localcontext() as context:
            context.prec = precision
            context.Emin = minimum_exponent
            context.Emax = maximum_exponent
            for signal in (Inexact, Rounded, Overflow, Underflow, InvalidOperation):
                context.traps[signal] = True
            before_flags = dict(context.flags)
            exact = economics(source_intent)
            actual = postings_for_confirmed_trades(
                (source_intent,),
                (event,),
                (exact,),
            )
            assert exact == expected_economics
            assert actual == expected_postings
            assert dict(context.flags) == before_flags


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
    event = trade_event(source_intent)

    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))

    assert len(postings) == 8
    assert account_net(postings, "realized_pnl:USDC", "USDC") == -realized_pnl
    assert all(
        COST_BASIS_HASH in row.lineage_hashes
        for row in postings
        if "realized_pnl:USDC" in (row.debit_account, row.credit_account)
    )
    assert verify_live_conservation(postings, (source_intent,), (event,), (exact,)) is None


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
    event = trade_event(source_intent)
    exact = economics(source_intent)
    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))

    with pytest.raises(LiveLedgerError, match="POSTING_ID_DUPLICATE"):
        verify_live_conservation((postings[0], postings[0]), (source_intent,), (event,), (exact,))
    with pytest.raises(LiveLedgerError, match="POSTING_PAIR_INVALID"):
        verify_live_conservation(postings[:-1], (source_intent,), (event,), (exact,))
    changed = postings[0].model_copy(update={"asset_id": "other"})
    with pytest.raises(LiveLedgerError, match="POSTING_ID_MISMATCH"):
        verify_live_conservation((changed, *postings[1:]), (source_intent,), (event,), (exact,))


@pytest.mark.parametrize(
    "omitted_account_base",
    ["venue_cash", "venue_position", "fees_paid", "realized_pnl"],
)
def test_conservation_rejects_balanced_but_incomplete_canonical_topology(
    omitted_account_base: str,
) -> None:
    source_intent = intent()
    event = trade_event(source_intent)
    exact = economics(
        source_intent,
        realized_pnl=Decimal("1.25"),
        cost_basis_evidence_hash=COST_BASIS_HASH,
    )
    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))
    incomplete = tuple(
        row
        for row in postings
        if omitted_account_base
        not in {row.debit_account.partition(":")[0], row.credit_account.partition(":")[0]}
    )

    with pytest.raises(LiveLedgerError, match="POSTING_TOPOLOGY_MISMATCH"):
        verify_live_conservation(incomplete, (source_intent,), (event,), (exact,))


def test_conservation_rejects_unclosed_account_names() -> None:
    source_intent = intent()
    event = trade_event(source_intent)
    exact = economics(source_intent)
    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))
    changed = postings[0].model_copy(update={"debit_account": "wallet_cash"})

    with pytest.raises(LiveLedgerError, match="POSTING_ACCOUNT_INVALID"):
        verify_live_conservation((changed, *postings[1:]), (source_intent,), (event,), (exact,))


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
    event = trade_event(source_intent)

    postings = postings_for_confirmed_trades((source_intent,), (event,), (exact,))

    assert verify_live_conservation(postings, (source_intent,), (event,), (exact,)) is None
    for asset in {row.asset_id for row in postings}:
        assert sum(
            (row.debit_amount for row in postings if row.asset_id == asset), Decimal("0")
        ) == sum((row.credit_amount for row in postings if row.asset_id == asset), Decimal("0"))
