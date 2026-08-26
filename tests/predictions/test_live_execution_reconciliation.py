from __future__ import annotations

import copy
import pickle
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


def trade_event(source_intent: ExecutionIntent) -> VenueTradeEvent:
    return VenueTradeEvent(
        schema_version=1,
        trade_event_id=UUID("42b33848-ff46-4c45-b9ab-0c74510687f3"),
        venue="polymarket",
        raw_event_hash=TRADE_HASH,
        source_channel="recovery_read",
        venue_trade_id=TRADE_ID,
        venue_order_id=ORDER_ID,
        intent_id=source_intent.intent_id,
        original_venue_state=VenueTradeState.CONFIRMED.value,
        normalized_state=VenueTradeState.CONFIRMED,
        terminal=True,
        venue_timestamp=NOW + timedelta(seconds=1),
        received_at=NOW + timedelta(seconds=2),
        sequence_number=None,
        protocol_version=source_intent.protocol_version,
        lineage_hashes=(TRADE_HASH,),
    )


def exact_economics(
    source_intent: ExecutionIntent,
    *,
    realized_pnl: Decimal | None = None,
) -> AuthoritativeTradeEconomics:
    return AuthoritativeTradeEconomics(
        schema_version=1,
        account_fingerprint=source_intent.account_fingerprint,
        intent_id=source_intent.intent_id,
        venue_order_id=ORDER_ID,
        venue_trade_id=TRADE_ID,
        trade_event_hash=TRADE_HASH,
        cash_asset_id="USDC",
        position_asset_id=source_intent.token_id,
        side=source_intent.side,
        price=Decimal("0.51"),
        size=Decimal("10"),
        fee=Decimal("0.01"),
        cash_quantum=Decimal("0.000001"),
        position_quantum=Decimal("0.01"),
        trade_state=VenueTradeState.CONFIRMED,
        settlement_state=VenueTradeState.CONFIRMED,
        fee_hash=FEE_HASH,
        settlement_hash=SETTLEMENT_HASH,
        source_hash=SOURCE_HASH,
        balance_evidence_hashes=(BALANCE_HASH,),
        occurred_at=NOW + timedelta(seconds=1),
        information_cutoff=NOW + timedelta(seconds=3),
        protocol_version=source_intent.protocol_version,
        realized_pnl=realized_pnl,
        cost_basis_evidence_hash=None if realized_pnl is None else COST_BASIS_HASH,
    )


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
        current_cumulative_fees=(asset("USDC", fee_current, "0.000001", FEE_HASH),),
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
