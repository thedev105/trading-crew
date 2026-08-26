"""Independent, exact reconciliation for live Polymarket execution evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from polytrading.predictions.domain import Sha256
from polytrading.predictions.execution.ledger import (
    MAX_LIVE_EVIDENCE_ITEMS,
    AuthoritativeTradeEconomics,
    LiveLedgerError,
    _bounded_decimal,
    _decimal_resource_components,
    _exact_add,
    _exact_difference,
    _ExactArithmeticError,
    _is_quantized,
    postings_for_confirmed_trades,
    verify_live_conservation,
)
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    LiveLedgerPosting,
    LiveReconciliation,
    VenueOrderState,
    VenueTradeEvent,
    VenueTradeState,
    canonical_execution_hash,
)

_RECONCILIATION_NAMESPACE = UUID("cc34beb0-472f-4d4f-ab9d-038e8336323f")
_PUBLIC_TEXT = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$"),
]
_EVM_ADDRESS = Annotated[
    str,
    StringConstraints(pattern=r"^0x[0-9a-f]{40}$"),
]
_POSITIVE_DECIMAL = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
_NONNEGATIVE_DECIMAL = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
_OBSERVATION_MODELS_SEALED = False


class LiveReconciliationError(ValueError):
    """A stable, context-free reconciliation rejection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)


class _StrictObservation(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    def __init_subclass__(cls, **kwargs: object) -> None:
        if _OBSERVATION_MODELS_SEALED:
            raise TypeError("VENUE_ACCOUNT_SNAPSHOT_NOT_SUBCLASSABLE") from None
        super().__init_subclass__(**kwargs)

    def __init__(self, **data: object) -> None:
        try:
            object.__getattribute__(self, "__pydantic_fields_set__")
        except AttributeError:
            pass
        else:
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        super().__init__(**data)

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: object) -> Self:
        del _fields_set, values
        raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None

    def model_copy(
        self,
        *,
        update: Mapping[str, object] | None = None,
        deep: bool = False,
    ) -> Self:
        del deep
        values = self.model_dump(mode="python")
        if update is not None:
            values.update(update)
        return type(self).model_validate(values, strict=True)

    def __copy__(self) -> object:
        raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None

    def __reduce__(self) -> object:
        raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None

    def __getstate__(self) -> object:
        raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None


class AssetAmountObservation(_StrictObservation):
    """One exact independently observed balance in one asset unit."""

    schema_version: Literal[1]
    asset_id: _PUBLIC_TEXT
    amount: _NONNEGATIVE_DECIMAL
    quantum: _POSITIVE_DECIMAL
    evidence_hash: Sha256

    @field_validator("amount", "quantum")
    @classmethod
    def _bounded_decimal_resource(cls, value: Decimal) -> Decimal:
        _decimal_resource_components(value)
        return value

    @model_validator(mode="after")
    def _exact_amount(self) -> AssetAmountObservation:
        if not _bounded_decimal(self.amount) or not _bounded_decimal(self.quantum):
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        if self.quantum < Decimal("0.000001") or not _is_quantized(self.amount, self.quantum):
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        return self


class AllowanceObservation(_StrictObservation):
    """One exact independently observed asset allowance."""

    schema_version: Literal[1]
    asset_id: _PUBLIC_TEXT
    spender_address: _EVM_ADDRESS
    amount: _NONNEGATIVE_DECIMAL
    quantum: _POSITIVE_DECIMAL
    evidence_hash: Sha256

    @field_validator("amount", "quantum")
    @classmethod
    def _bounded_decimal_resource(cls, value: Decimal) -> Decimal:
        _decimal_resource_components(value)
        return value

    @model_validator(mode="after")
    def _exact_amount(self) -> AllowanceObservation:
        if (
            not _bounded_decimal(self.amount)
            or not _bounded_decimal(self.quantum)
            or self.quantum < Decimal("0.000001")
            or not _is_quantized(self.amount, self.quantum)
        ):
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        return self


class OpenOrderObservation(_StrictObservation):
    """Identity and state from an independently fetched open-order family."""

    schema_version: Literal[1]
    venue_order_id: _PUBLIC_TEXT
    intent_id: UUID
    position_asset_id: _PUBLIC_TEXT
    state: VenueOrderState
    evidence_hash: Sha256


class RecentTradeObservation(_StrictObservation):
    """Identity, state, and exact lineage from an independently fetched trade family."""

    schema_version: Literal[1]
    venue_trade_id: _PUBLIC_TEXT
    venue_order_id: _PUBLIC_TEXT
    intent_id: UUID
    cash_asset_id: _PUBLIC_TEXT
    position_asset_id: _PUBLIC_TEXT
    side: Literal["buy", "sell"]
    state: VenueTradeState
    trade_event_hash: Sha256
    settlement_hash: Sha256
    fee_hash: Sha256
    source_hash: Sha256
    balance_evidence_hashes: Annotated[
        tuple[Sha256, ...], Field(min_length=1, max_length=MAX_LIVE_EVIDENCE_ITEMS)
    ]
    economics_fingerprint: Sha256
    realized_pnl: Decimal | None = None
    cost_basis_evidence_hash: Sha256 | None = None
    occurred_at: datetime

    @field_validator("realized_pnl")
    @classmethod
    def _bounded_decimal_resource(cls, value: Decimal | None) -> Decimal | None:
        if value is not None:
            _decimal_resource_components(value)
        return value

    @field_validator("occurred_at")
    @classmethod
    def _canonical_utc(cls, value: datetime) -> datetime:
        if type(value) is not datetime or value.tzinfo is not UTC:
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        return value

    @field_validator("balance_evidence_hashes")
    @classmethod
    def _sorted_unique_hashes(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        return value

    @model_validator(mode="after")
    def _exact_realized_pnl_commitment(self) -> RecentTradeObservation:
        if (self.realized_pnl is None) != (self.cost_basis_evidence_hash is None) or (
            self.realized_pnl is not None and not _bounded_decimal(self.realized_pnl)
        ):
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        return self


class SettlementObservation(_StrictObservation):
    """Identity and state from an independently fetched settlement family."""

    schema_version: Literal[1]
    venue_trade_id: _PUBLIC_TEXT
    venue_order_id: _PUBLIC_TEXT
    intent_id: UUID
    position_asset_id: _PUBLIC_TEXT
    state: VenueTradeState
    settlement_hash: Sha256
    evidence_hash: Sha256
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _canonical_utc(cls, value: datetime) -> datetime:
        if type(value) is not datetime or value.tzinfo is not UTC:
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        return value


class VenueAccountSnapshot(_StrictObservation):
    """Two-cut independent account evidence used to close live ledger effects."""

    schema_version: Literal[1]
    account_fingerprint: Sha256
    cutoff_at: datetime
    observed_at: datetime
    opening_cash_balances: tuple[AssetAmountObservation, ...]
    current_cash_balances: tuple[AssetAmountObservation, ...]
    opening_token_positions: tuple[AssetAmountObservation, ...]
    current_token_positions: tuple[AssetAmountObservation, ...]
    opening_allowances: tuple[AllowanceObservation, ...]
    current_allowances: tuple[AllowanceObservation, ...]
    opening_cumulative_fees: tuple[AssetAmountObservation, ...]
    current_cumulative_fees: tuple[AssetAmountObservation, ...]
    open_orders: tuple[OpenOrderObservation, ...]
    recent_trades: tuple[RecentTradeObservation, ...]
    settlements: tuple[SettlementObservation, ...]
    opening_cash_source_hash: Sha256
    current_cash_source_hash: Sha256
    opening_position_source_hash: Sha256
    current_position_source_hash: Sha256
    opening_allowance_source_hash: Sha256
    current_allowance_source_hash: Sha256
    opening_fee_source_hash: Sha256
    current_fee_source_hash: Sha256
    open_orders_source_hash: Sha256
    recent_trades_source_hash: Sha256
    settlements_source_hash: Sha256
    snapshot_fingerprint: Sha256 | None = None

    @field_validator("cutoff_at", "observed_at")
    @classmethod
    def _canonical_utc(cls, value: datetime) -> datetime:
        if type(value) is not datetime or value.tzinfo is not UTC:
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        return value

    @model_validator(mode="after")
    def _closed_snapshot(self) -> VenueAccountSnapshot:
        collections = (
            self.opening_cash_balances,
            self.current_cash_balances,
            self.opening_token_positions,
            self.current_token_positions,
            self.opening_allowances,
            self.current_allowances,
            self.opening_cumulative_fees,
            self.current_cumulative_fees,
            self.open_orders,
            self.recent_trades,
            self.settlements,
        )
        if (
            self.cutoff_at >= self.observed_at
            or sum(map(len, collections)) > MAX_LIVE_EVIDENCE_ITEMS
        ):
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        for values, identity in (
            (self.opening_cash_balances, _asset_identity),
            (self.current_cash_balances, _asset_identity),
            (self.opening_token_positions, _asset_identity),
            (self.current_token_positions, _asset_identity),
            (self.opening_allowances, _allowance_identity),
            (self.current_allowances, _allowance_identity),
            (self.opening_cumulative_fees, _asset_identity),
            (self.current_cumulative_fees, _asset_identity),
            (self.open_orders, _order_identity),
            (self.recent_trades, _trade_identity),
            (self.settlements, _settlement_identity),
        ):
            keys = tuple(identity(value) for value in values)
            if keys != tuple(sorted(set(keys))):
                raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        for opening, current, identity in (
            (self.opening_cash_balances, self.current_cash_balances, _asset_identity),
            (
                self.opening_token_positions,
                self.current_token_positions,
                _asset_identity,
            ),
            (self.opening_allowances, self.current_allowances, _allowance_identity),
            (
                self.opening_cumulative_fees,
                self.current_cumulative_fees,
                _asset_identity,
            ),
        ):
            opening_by_id = {identity(value): value for value in opening}
            current_by_id = {identity(value): value for value in current}
            if set(opening_by_id) != set(current_by_id) or any(
                opening_by_id[key].quantum != current_by_id[key].quantum for key in opening_by_id
            ):
                raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        position_assets = {value.asset_id for value in self.current_token_positions}
        cash_assets = {value.asset_id for value in self.current_cash_balances}
        fee_assets = {value.asset_id for value in self.current_cumulative_fees}
        if any(value.position_asset_id not in position_assets for value in self.open_orders):
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        if any(
            value.position_asset_id not in position_assets
            or value.cash_asset_id not in cash_assets
            or value.cash_asset_id not in fee_assets
            or value.occurred_at > self.observed_at
            for value in self.recent_trades
        ):
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        if any(
            value.position_asset_id not in position_assets or value.occurred_at > self.observed_at
            for value in self.settlements
        ):
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        expected = _snapshot_fingerprint(self)
        if self.snapshot_fingerprint is None:
            object.__setattr__(self, "snapshot_fingerprint", expected)
        elif self.snapshot_fingerprint != expected:
            raise ValueError("VENUE_ACCOUNT_SNAPSHOT_INVALID") from None
        return self


_OBSERVATION_MODELS_SEALED = True


def _asset_identity(value: AssetAmountObservation) -> str:
    return value.asset_id


def _allowance_identity(value: AllowanceObservation) -> tuple[str, str]:
    return (value.asset_id, value.spender_address)


def _order_identity(value: OpenOrderObservation) -> str:
    return value.venue_order_id


def _trade_identity(value: RecentTradeObservation) -> str:
    return value.venue_trade_id


def _settlement_identity(value: SettlementObservation) -> str:
    return value.venue_trade_id


def _snapshot_fingerprint(snapshot: VenueAccountSnapshot) -> Sha256:
    payload = snapshot.model_dump(mode="json")
    payload.pop("snapshot_fingerprint", None)
    return canonical_execution_hash(payload)


def _snapshot_input(snapshot: object) -> VenueAccountSnapshot:
    if type(snapshot) is not VenueAccountSnapshot:
        raise LiveReconciliationError("SNAPSHOT_INVALID") from None
    try:
        return VenueAccountSnapshot.model_validate(snapshot.model_dump(mode="python"), strict=True)
    except (TypeError, ValueError):
        raise LiveReconciliationError("SNAPSHOT_INVALID") from None


def _snapshot_postings(
    postings: object,
    intents: Sequence[ExecutionIntent],
    trades: Sequence[VenueTradeEvent],
    economics: Sequence[AuthoritativeTradeEconomics],
) -> tuple[LiveLedgerPosting, ...]:
    if not isinstance(postings, Sequence) or isinstance(postings, (str, bytes, bytearray)):
        raise LiveLedgerError("POSTING_INVALID") from None
    if len(postings) > MAX_LIVE_EVIDENCE_ITEMS:
        raise LiveLedgerError("POSTING_INVALID") from None
    values: list[LiveLedgerPosting] = []
    for posting in tuple(postings):
        if type(posting) is not LiveLedgerPosting:
            raise LiveLedgerError("POSTING_INVALID") from None
        try:
            values.append(
                LiveLedgerPosting.model_validate(posting.model_dump(mode="python"), strict=True)
            )
        except (TypeError, ValueError):
            raise LiveLedgerError("POSTING_INVALID") from None
    result = tuple(values)
    verify_live_conservation(result, intents, trades, economics)
    return result


def _snapshot_evidence(snapshot: VenueAccountSnapshot) -> dict[str, tuple[Sha256, ...]]:
    evidence_hashes = {
        snapshot.snapshot_fingerprint,
        snapshot.open_orders_source_hash,
        snapshot.recent_trades_source_hash,
        snapshot.settlements_source_hash,
        *(value.evidence_hash for value in snapshot.open_orders),
    }
    for value in snapshot.recent_trades:
        evidence_hashes.update(
            {
                value.settlement_hash,
                value.fee_hash,
                value.source_hash,
                value.economics_fingerprint,
            }
        )
        if value.cost_basis_evidence_hash is not None:
            evidence_hashes.add(value.cost_basis_evidence_hash)
    for value in snapshot.settlements:
        evidence_hashes.update({value.settlement_hash, value.evidence_hash})
    balance_hashes = {
        snapshot.opening_cash_source_hash,
        snapshot.current_cash_source_hash,
        snapshot.opening_position_source_hash,
        snapshot.current_position_source_hash,
        snapshot.opening_fee_source_hash,
        snapshot.current_fee_source_hash,
    }
    for values in (
        snapshot.opening_cash_balances,
        snapshot.current_cash_balances,
        snapshot.opening_token_positions,
        snapshot.current_token_positions,
        snapshot.opening_cumulative_fees,
        snapshot.current_cumulative_fees,
    ):
        balance_hashes.update(value.evidence_hash for value in values)
    for value in snapshot.recent_trades:
        balance_hashes.update(value.balance_evidence_hashes)
    allowance_hashes = {
        snapshot.opening_allowance_source_hash,
        snapshot.current_allowance_source_hash,
        *(value.evidence_hash for value in snapshot.opening_allowances),
        *(value.evidence_hash for value in snapshot.current_allowances),
    }
    all_hashes = evidence_hashes | balance_hashes | allowance_hashes
    return {
        "all": tuple(sorted(all_hashes)),
        "evidence": tuple(sorted(evidence_hashes)),
        "balances": tuple(sorted(balance_hashes)),
        "allowances": tuple(sorted(allowance_hashes)),
    }


def _base_account(account: str) -> str:
    return account.partition(":")[0]


def _posting_nets(postings: tuple[LiveLedgerPosting, ...], base_account: str) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for posting in postings:
        try:
            if _base_account(posting.debit_account) == base_account:
                totals[posting.asset_id] = _exact_add(
                    totals[posting.asset_id], posting.debit_amount
                )
            if _base_account(posting.credit_account) == base_account:
                totals[posting.asset_id] = _exact_difference(
                    totals[posting.asset_id], posting.credit_amount
                )
        except _ExactArithmeticError:
            raise LiveLedgerError("POSTING_AMOUNT_INVALID") from None
    return dict(totals)


def _observed_deltas(
    opening: tuple[AssetAmountObservation, ...],
    current: tuple[AssetAmountObservation, ...],
) -> dict[str, Decimal]:
    opening_values = {value.asset_id: value.amount for value in opening}
    try:
        return {
            value.asset_id: _exact_difference(value.amount, opening_values[value.asset_id])
            for value in current
        }
    except _ExactArithmeticError:
        raise LiveReconciliationError("SNAPSHOT_INVALID") from None


def _append_delta_differences(
    differences: set[str],
    *,
    label: str,
    observed: dict[str, Decimal],
    expected: dict[str, Decimal],
) -> None:
    for asset_id in sorted(set(observed) | set(expected)):
        if observed.get(asset_id, Decimal("0")) != expected.get(asset_id, Decimal("0")):
            differences.add(f"{label}_DELTA_MISMATCH:{asset_id}")


def _result(
    snapshot: VenueAccountSnapshot,
    postings: tuple[LiveLedgerPosting, ...],
    differences: set[str],
    trade_event_hashes: tuple[Sha256, ...] = (),
) -> LiveReconciliation:
    evidence = _snapshot_evidence(snapshot)
    expected_ids = tuple(sorted(posting.posting_id for posting in postings))
    fields: dict[str, object] = {
        "schema_version": 1,
        "reconciliation_id": UUID(int=0),
        "account_fingerprint": snapshot.account_fingerprint,
        "observed_at": snapshot.observed_at,
        "complete": not differences,
        "differences": tuple(sorted(differences)),
        "evidence_hashes": evidence["evidence"],
        "next_action": None if not differences else "HALT_AND_RECONCILE",
        "venue_order_hashes": (),
        "venue_trade_hashes": tuple(sorted(set(trade_event_hashes))),
        "balance_hashes": evidence["balances"],
        "allowance_hashes": evidence["allowances"],
        "expected_posting_ids": expected_ids,
        "lineage_hashes": (snapshot.snapshot_fingerprint,),
    }
    fields["reconciliation_id"] = _reconciliation_id(fields)
    return LiveReconciliation(**fields)


def _reconciliation_id(fields: Mapping[str, object]) -> UUID:
    payload = dict(fields)
    payload.pop("reconciliation_id", None)
    return uuid5(_RECONCILIATION_NAMESPACE, canonical_execution_hash(payload))


def reconcile_live_account(
    postings: Sequence[LiveLedgerPosting],
    snapshot: VenueAccountSnapshot,
    intents: Sequence[ExecutionIntent],
    trades: Sequence[VenueTradeEvent],
    economics: Sequence[AuthoritativeTradeEconomics],
) -> LiveReconciliation:
    """Close posting effects against independent two-cut authoritative evidence."""
    observed = _snapshot_input(snapshot)
    trade_event_hashes = tuple(
        sorted({trade.raw_event_hash for trade in trades if type(trade) is VenueTradeEvent})
    )
    try:
        rows = _snapshot_postings(postings, intents, trades, economics)
    except LiveLedgerError as exc:
        difference = (
            "POSTING_TOPOLOGY_MISMATCH"
            if str(exc) == "POSTING_TOPOLOGY_MISMATCH"
            else "POSTINGS_INVALID"
        )
        try:
            canonical_rows = postings_for_confirmed_trades(intents, trades, economics)
        except LiveLedgerError:
            canonical_rows = ()
        return _result(observed, canonical_rows, {difference}, trade_event_hashes)
    differences: set[str] = set()
    snapshot_evidence = set(_snapshot_evidence(observed)["all"]) | set(trade_event_hashes)
    task1_trades_by_trade = {
        value.venue_trade_id: value
        for value in trades
        if type(value) is VenueTradeEvent and value.normalized_state is VenueTradeState.CONFIRMED
    }
    economics_by_trade = {value.venue_trade_id: value for value in economics}
    if any(row.account_fingerprint != observed.account_fingerprint for row in rows):
        differences.add("POSTING_ACCOUNT_MISMATCH")
    for trade_id in sorted({row.venue_trade_id for row in rows}):
        trade_rows = tuple(row for row in rows if row.venue_trade_id == trade_id)
        exact = economics_by_trade.get(trade_id)
        if any(
            not observed.cutoff_at < row.occurred_at <= observed.observed_at for row in trade_rows
        ):
            differences.add(f"POSTING_OUTSIDE_SNAPSHOT:{trade_id}")
        if exact is None or not (
            observed.cutoff_at
            < exact.occurred_at
            <= exact.information_cutoff
            <= observed.observed_at
        ):
            differences.add(f"ECONOMICS_OUTSIDE_SNAPSHOT:{trade_id}")
        if any(not set(row.lineage_hashes) <= snapshot_evidence for row in trade_rows):
            differences.add(f"POSTING_EVIDENCE_MISSING:{trade_id}")
    _append_delta_differences(
        differences,
        label="CASH",
        observed=_observed_deltas(observed.opening_cash_balances, observed.current_cash_balances),
        expected=_posting_nets(rows, "venue_cash"),
    )
    _append_delta_differences(
        differences,
        label="POSITION",
        observed=_observed_deltas(
            observed.opening_token_positions, observed.current_token_positions
        ),
        expected=_posting_nets(rows, "venue_position"),
    )
    _append_delta_differences(
        differences,
        label="FEE",
        observed=_observed_deltas(
            observed.opening_cumulative_fees, observed.current_cumulative_fees
        ),
        expected=_posting_nets(rows, "fees_paid"),
    )
    opening_allowances = {
        _allowance_identity(value): value.amount for value in observed.opening_allowances
    }
    for allowance in observed.current_allowances:
        if allowance.amount != opening_allowances[_allowance_identity(allowance)]:
            differences.add(f"ALLOWANCE_DELTA_UNEXPLAINED:{allowance.asset_id}")
    for order in observed.open_orders:
        differences.add(f"OPEN_ORDER_UNEXPLAINED:{order.venue_order_id}")
    expected_trade_ids = {row.venue_trade_id for row in rows}
    trades = {value.venue_trade_id: value for value in observed.recent_trades}
    settlements = {value.venue_trade_id: value for value in observed.settlements}
    for trade_id in sorted(expected_trade_ids):
        trade_rows = tuple(row for row in rows if row.venue_trade_id == trade_id)
        first = trade_rows[0]
        cash_nets = _posting_nets(trade_rows, "venue_cash")
        position_nets = _posting_nets(trade_rows, "venue_position")
        expected_cash_assets = {asset_id for asset_id, amount in cash_nets.items() if amount != 0}
        expected_position_assets = {
            asset_id for asset_id, amount in position_nets.items() if amount != 0
        }
        expected_side = None
        if len(expected_position_assets) == 1:
            position_amount = position_nets[next(iter(expected_position_assets))]
            expected_side = "buy" if position_amount > 0 else "sell"
        trade = trades.get(trade_id)
        exact = economics_by_trade.get(trade_id)
        task1_trade = task1_trades_by_trade.get(trade_id)
        if trade is None:
            differences.add(f"TRADE_MISSING:{trade_id}")
        elif (
            exact is None
            or task1_trade is None
            or trade.state is not VenueTradeState.CONFIRMED
            or task1_trade.normalized_state is not VenueTradeState.CONFIRMED
            or not task1_trade.terminal
            or trade.venue_trade_id != exact.venue_trade_id
            or trade.venue_trade_id != task1_trade.venue_trade_id
            or trade.intent_id != exact.intent_id
            or trade.intent_id != task1_trade.intent_id
            or trade.intent_id != first.intent_id
            or trade.venue_order_id != exact.venue_order_id
            or trade.venue_order_id != task1_trade.venue_order_id
            or trade.venue_order_id != first.venue_order_id
            or trade.trade_event_hash != exact.trade_event_hash
            or trade.trade_event_hash != task1_trade.raw_event_hash
            or trade.source_hash != exact.source_hash
            or trade.settlement_hash != exact.settlement_hash
            or trade.settlement_hash != first.settlement_hash
            or trade.fee_hash != exact.fee_hash
            or trade.fee_hash != first.fee_hash
            or trade.balance_evidence_hashes != exact.balance_evidence_hashes
            or trade.balance_evidence_hashes != first.balance_evidence_hashes
            or trade.economics_fingerprint != exact.economics_fingerprint
            or trade.realized_pnl != exact.realized_pnl
            or trade.cost_basis_evidence_hash != exact.cost_basis_evidence_hash
            or exact.account_fingerprint != observed.account_fingerprint
            or exact.account_fingerprint != first.account_fingerprint
            or trade.cash_asset_id != exact.cash_asset_id
            or trade.position_asset_id != exact.position_asset_id
            or trade.side != exact.side
            or exact.trade_state is not VenueTradeState.CONFIRMED
            or exact.settlement_state is not VenueTradeState.CONFIRMED
            or trade.occurred_at != exact.occurred_at
            or trade.occurred_at != first.occurred_at
            or (
                task1_trade.venue_timestamp is not None
                and task1_trade.venue_timestamp != trade.occurred_at
            )
            or {trade.cash_asset_id} != expected_cash_assets
            or {trade.position_asset_id} != expected_position_assets
            or trade.side != expected_side
            or any(
                not {
                    trade.trade_event_hash,
                    trade.source_hash,
                    trade.economics_fingerprint,
                }
                <= set(row.lineage_hashes)
                for row in trade_rows
            )
        ):
            differences.add(f"TRADE_EVIDENCE_MISMATCH:{trade_id}")
        settlement = settlements.get(trade_id)
        if settlement is None:
            differences.add(f"SETTLEMENT_MISSING:{trade_id}")
        elif settlement.state is not VenueTradeState.CONFIRMED:
            differences.add(f"SETTLEMENT_STATE_MISMATCH:{trade_id}")
        elif (
            exact is None
            or settlement.intent_id != exact.intent_id
            or settlement.intent_id != first.intent_id
            or settlement.venue_order_id != exact.venue_order_id
            or settlement.venue_order_id != first.venue_order_id
            or settlement.settlement_hash != exact.settlement_hash
            or settlement.settlement_hash != first.settlement_hash
            or settlement.occurred_at != exact.occurred_at
            or settlement.occurred_at != first.occurred_at
            or settlement.position_asset_id != exact.position_asset_id
            or {settlement.position_asset_id} != expected_position_assets
        ):
            differences.add(f"SETTLEMENT_EVIDENCE_MISMATCH:{trade_id}")
        pnl_rows = tuple(
            row
            for row in trade_rows
            if "realized_pnl"
            in {
                _base_account(row.debit_account),
                _base_account(row.credit_account),
            }
        )
        if pnl_rows and (
            trade is None
            or trade.cost_basis_evidence_hash is None
            or any(trade.cost_basis_evidence_hash not in row.lineage_hashes for row in pnl_rows)
        ):
            differences.add(f"PNL_COST_BASIS_MISSING:{trade_id}")
    for trade_id in sorted(set(trades) - expected_trade_ids):
        differences.add(f"TRADE_UNEXPLAINED:{trade_id}")
    for trade_id in sorted(set(settlements) - expected_trade_ids):
        differences.add(f"SETTLEMENT_UNEXPLAINED:{trade_id}")
    return _result(observed, rows, differences, trade_event_hashes)


def reconciled_live_pnl(
    postings: Sequence[LiveLedgerPosting],
    reconciliation: LiveReconciliation | None,
    snapshot: VenueAccountSnapshot,
    intents: Sequence[ExecutionIntent],
    trades: Sequence[VenueTradeEvent],
    economics: Sequence[AuthoritativeTradeEconomics],
) -> Decimal | None:
    """Reconstruct all evidence before publishing explicitly posted realized P&L."""
    if type(reconciliation) is not LiveReconciliation:
        return None
    try:
        closed = LiveReconciliation.model_validate(
            reconciliation.model_dump(mode="python"), strict=True
        )
        rows = _snapshot_postings(postings, intents, trades, economics)
        reconstructed = reconcile_live_account(rows, snapshot, intents, trades, economics)
    except (TypeError, ValueError, LiveLedgerError):
        return None
    closed_hashes = {
        *closed.evidence_hashes,
        *closed.venue_order_hashes,
        *closed.venue_trade_hashes,
        *closed.balance_hashes,
        *closed.allowance_hashes,
    }
    if (
        closed != reconstructed
        or not closed.complete
        or closed.differences
        or closed.next_action is not None
        or closed.reconciliation_id != _reconciliation_id(closed.model_dump(mode="python"))
        or closed.expected_posting_ids != tuple(sorted(row.posting_id for row in rows))
        or any(row.account_fingerprint != closed.account_fingerprint for row in rows)
        or any(not set(row.lineage_hashes) <= closed_hashes for row in rows)
    ):
        return None
    pnl_rows = tuple(
        row
        for row in rows
        if "realized_pnl" in {_base_account(row.debit_account), _base_account(row.credit_account)}
    )
    assets = {row.asset_id for row in pnl_rows}
    if not pnl_rows or len(assets) != 1:
        return None
    pnl = Decimal("0")
    for row in pnl_rows:
        try:
            if _base_account(row.credit_account) == "realized_pnl":
                pnl = _exact_add(pnl, row.credit_amount)
            if _base_account(row.debit_account) == "realized_pnl":
                pnl = _exact_difference(pnl, row.debit_amount)
        except _ExactArithmeticError:
            return None
    return pnl


__all__ = [
    "AllowanceObservation",
    "AssetAmountObservation",
    "LiveReconciliationError",
    "OpenOrderObservation",
    "RecentTradeObservation",
    "SettlementObservation",
    "VenueAccountSnapshot",
    "reconcile_live_account",
    "reconciled_live_pnl",
]
