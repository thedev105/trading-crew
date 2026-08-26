"""Exact, evidence-bound live ledger postings for confirmed Polymarket trades."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    LiveLedgerPosting,
    VenueTradeEvent,
    VenueTradeState,
    canonical_execution_hash,
)

MAX_LEDGER_DECIMAL_PLACES = 6
MAX_LEDGER_AMOUNT = Decimal("1000000000000000000000000000000")
MAX_LIVE_EVIDENCE_ITEMS = 10_000
_POSTING_NAMESPACE = UUID("ddf6bc3f-af66-4435-92c0-d3c2713d5865")
_PUBLIC_TEXT = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[\x20-\x7e]+$"),
]
_POSITIVE_DECIMAL = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
_NONNEGATIVE_DECIMAL = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
_ACCOUNT_BASES = frozenset(
    {
        "venue_cash",
        "venue_position",
        "fees_paid",
        "settlement_receivable",
        "realized_pnl",
    }
)
_ECONOMICS_MODELS_SEALED = False


class LiveLedgerError(ValueError):
    """A stable, context-free live-ledger rejection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)


class AuthoritativeTradeEconomics(BaseModel):
    """Task-11-local exact economics retained from an authoritative account read."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal[1]
    account_fingerprint: Sha256
    intent_id: UUID
    venue_order_id: _PUBLIC_TEXT
    venue_trade_id: _PUBLIC_TEXT
    trade_event_hash: Sha256
    cash_asset_id: _PUBLIC_TEXT
    position_asset_id: _PUBLIC_TEXT
    side: Literal["buy", "sell"]
    price: _POSITIVE_DECIMAL
    size: _POSITIVE_DECIMAL
    fee: _NONNEGATIVE_DECIMAL
    cash_quantum: _POSITIVE_DECIMAL
    position_quantum: _POSITIVE_DECIMAL
    trade_state: Literal[VenueTradeState.CONFIRMED]
    settlement_state: Literal[VenueTradeState.CONFIRMED]
    fee_hash: Sha256
    settlement_hash: Sha256
    source_hash: Sha256
    balance_evidence_hashes: Annotated[
        tuple[Sha256, ...], Field(min_length=1, max_length=MAX_LIVE_EVIDENCE_ITEMS)
    ]
    occurred_at: datetime
    information_cutoff: datetime
    protocol_version: _PUBLIC_TEXT
    realized_pnl: Decimal | None = None
    cost_basis_evidence_hash: Sha256 | None = None
    economics_fingerprint: Sha256 | None = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        if _ECONOMICS_MODELS_SEALED:
            raise TypeError("TRADE_ECONOMICS_NOT_SUBCLASSABLE") from None
        super().__init_subclass__(**kwargs)

    def __init__(self, **data: object) -> None:
        try:
            object.__getattribute__(self, "__pydantic_fields_set__")
        except AttributeError:
            pass
        else:
            raise ValueError("TRADE_ECONOMICS_INVALID") from None
        super().__init__(**data)

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: object) -> Self:
        del _fields_set, values
        raise ValueError("TRADE_ECONOMICS_INVALID") from None

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
        raise ValueError("TRADE_ECONOMICS_INVALID") from None

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise ValueError("TRADE_ECONOMICS_INVALID") from None

    def __reduce__(self) -> object:
        raise ValueError("TRADE_ECONOMICS_INVALID") from None

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise ValueError("TRADE_ECONOMICS_INVALID") from None

    def __getstate__(self) -> object:
        raise ValueError("TRADE_ECONOMICS_INVALID") from None

    @field_validator("occurred_at", "information_cutoff")
    @classmethod
    def _canonical_utc(cls, value: datetime) -> datetime:
        if type(value) is not datetime or value.tzinfo is not UTC:
            raise ValueError("TRADE_ECONOMICS_INVALID") from None
        return value

    @field_validator("balance_evidence_hashes")
    @classmethod
    def _sorted_unique_hashes(cls, value: tuple[Sha256, ...]) -> tuple[Sha256, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("TRADE_ECONOMICS_INVALID") from None
        return value

    @model_validator(mode="after")
    def _exact_economics(self) -> AuthoritativeTradeEconomics:
        values = (
            self.price,
            self.size,
            self.fee,
            self.cash_quantum,
            self.position_quantum,
        )
        if any(not _bounded_decimal(value) for value in values):
            raise ValueError("TRADE_ECONOMICS_INVALID") from None
        if (
            self.price > Decimal("1")
            or self.cash_quantum < Decimal("0.000001")
            or self.position_quantum < Decimal("0.01")
            or not _is_quantized(self.size, self.position_quantum)
            or not _bounded_decimal(self.price * self.size)
            or not _is_quantized(self.price * self.size, self.cash_quantum)
            or not _is_quantized(self.fee, self.cash_quantum)
            or self.occurred_at > self.information_cutoff
            or (self.realized_pnl is None) != (self.cost_basis_evidence_hash is None)
            or (
                self.realized_pnl is not None
                and (
                    self.realized_pnl == 0
                    or not _bounded_decimal(self.realized_pnl.copy_abs())
                    or not _is_quantized(self.realized_pnl.copy_abs(), self.cash_quantum)
                )
            )
        ):
            raise ValueError("TRADE_ECONOMICS_INVALID") from None
        expected = _economics_fingerprint(self)
        if self.economics_fingerprint is None:
            object.__setattr__(self, "economics_fingerprint", expected)
        elif self.economics_fingerprint != expected:
            raise ValueError("TRADE_ECONOMICS_INVALID") from None
        return self


_ECONOMICS_MODELS_SEALED = True


def _bounded_decimal(value: Decimal) -> bool:
    if type(value) is not Decimal or not value.is_finite() or value.copy_abs() > MAX_LEDGER_AMOUNT:
        return False
    exponent = value.as_tuple().exponent
    return type(exponent) is int and exponent >= -MAX_LEDGER_DECIMAL_PLACES


def _is_quantized(value: Decimal, quantum: Decimal) -> bool:
    try:
        return value % quantum == 0
    except (InvalidOperation, ZeroDivisionError):
        return False


def _economics_fingerprint(evidence: AuthoritativeTradeEconomics) -> Sha256:
    payload = evidence.model_dump(mode="json")
    payload.pop("economics_fingerprint", None)
    return canonical_execution_hash(payload)


def _snapshot_model[ModelT: BaseModel](value: object, model: type[ModelT], code: str) -> ModelT:
    if type(value) is not model:
        raise LiveLedgerError(code) from None
    try:
        return model.model_validate(value.model_dump(mode="python"), strict=True)
    except (TypeError, ValueError):
        raise LiveLedgerError(code) from None


def _snapshot_sequence[ModelT: BaseModel](
    values: Sequence[object],
    model: type[ModelT],
    code: str,
) -> tuple[ModelT, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise LiveLedgerError(code) from None
    if len(values) > MAX_LIVE_EVIDENCE_ITEMS:
        raise LiveLedgerError(code) from None
    return tuple(_snapshot_model(value, model, code) for value in tuple(values))


def _dedupe_records[ModelT: BaseModel](
    values: tuple[ModelT, ...],
    *,
    identity: Callable[[ModelT], object],
    conflict_code: str,
) -> tuple[ModelT, ...]:
    by_identity: dict[object, tuple[Sha256, ModelT]] = {}
    for value in values:
        key = identity(value)
        fingerprint = canonical_execution_hash(value.model_dump(mode="json"))
        existing = by_identity.get(key)
        if existing is not None and existing[0] != fingerprint:
            raise LiveLedgerError(conflict_code) from None
        by_identity[key] = (fingerprint, value)
    return tuple(item[1] for item in by_identity.values())


def _account(base: str, asset_id: str) -> str:
    return f"{base}:{asset_id}"


def _posting_identity_payload(posting: LiveLedgerPosting) -> dict[str, object]:
    payload = posting.model_dump(mode="json")
    payload.pop("posting_id", None)
    return payload


def _posting_id(posting: LiveLedgerPosting) -> UUID:
    return uuid5(_POSTING_NAMESPACE, canonical_execution_hash(_posting_identity_payload(posting)))


def _posting(
    evidence: AuthoritativeTradeEconomics,
    *,
    debit_account: str,
    credit_account: str,
    asset_id: str,
    debit_amount: Decimal,
    credit_amount: Decimal,
    lineage_hashes: tuple[Sha256, ...],
) -> LiveLedgerPosting:
    fields: dict[str, object] = {
        "schema_version": 1,
        "posting_id": UUID(int=0),
        "account_fingerprint": evidence.account_fingerprint,
        "intent_id": evidence.intent_id,
        "venue_order_id": evidence.venue_order_id,
        "venue_trade_id": evidence.venue_trade_id,
        "settlement_hash": evidence.settlement_hash,
        "fee_hash": evidence.fee_hash,
        "balance_evidence_hashes": evidence.balance_evidence_hashes,
        "debit_account": debit_account,
        "credit_account": credit_account,
        "asset_id": asset_id,
        "debit_amount": debit_amount,
        "credit_amount": credit_amount,
        "occurred_at": evidence.occurred_at,
        "lineage_hashes": lineage_hashes,
    }
    provisional = LiveLedgerPosting(**fields)
    fields["posting_id"] = _posting_id(provisional)
    return LiveLedgerPosting(**fields)


def _paired_postings(
    evidence: AuthoritativeTradeEconomics,
    *,
    debit_account: str,
    credit_account: str,
    asset_id: str,
    amount: Decimal,
    lineage_hashes: tuple[Sha256, ...],
) -> tuple[LiveLedgerPosting, LiveLedgerPosting]:
    return (
        _posting(
            evidence,
            debit_account=debit_account,
            credit_account=credit_account,
            asset_id=asset_id,
            debit_amount=amount,
            credit_amount=Decimal("0"),
            lineage_hashes=lineage_hashes,
        ),
        _posting(
            evidence,
            debit_account=debit_account,
            credit_account=credit_account,
            asset_id=asset_id,
            debit_amount=Decimal("0"),
            credit_amount=amount,
            lineage_hashes=lineage_hashes,
        ),
    )


def _validated_trade_binding(
    intent: ExecutionIntent,
    trade: VenueTradeEvent,
    evidence: AuthoritativeTradeEconomics,
) -> None:
    notional = evidence.price * evidence.size
    fee_cap = notional * Decimal(intent.fee_rate_bps_cap) / Decimal(10_000)
    price_allowed = (
        evidence.price <= intent.limit_price
        if intent.side == "buy"
        else evidence.price >= intent.limit_price
    )
    if (
        trade.normalized_state is not VenueTradeState.CONFIRMED
        or not trade.terminal
        or trade.original_venue_state != VenueTradeState.CONFIRMED.value
        or evidence.trade_state is not VenueTradeState.CONFIRMED
        or evidence.settlement_state is not VenueTradeState.CONFIRMED
        or trade.intent_id != intent.intent_id
        or trade.venue_order_id != evidence.venue_order_id
        or trade.venue_trade_id != evidence.venue_trade_id
        or trade.raw_event_hash != evidence.trade_event_hash
        or evidence.intent_id != intent.intent_id
        or evidence.account_fingerprint != intent.account_fingerprint
        or evidence.position_asset_id != intent.token_id
        or evidence.side != intent.side
        or evidence.protocol_version != intent.protocol_version
        or trade.protocol_version != intent.protocol_version
        or not price_allowed
        or not _is_quantized(evidence.price, intent.tick_size)
        or (intent.base_size is not None and evidence.size > intent.base_size)
        or (intent.maximum_spend is not None and notional > intent.maximum_spend)
        or evidence.fee > fee_cap
        or intent.created_at > evidence.occurred_at
        or evidence.occurred_at > intent.deadline
        or evidence.occurred_at > trade.received_at
        or trade.received_at > evidence.information_cutoff
        or (trade.venue_timestamp is not None and trade.venue_timestamp != evidence.occurred_at)
    ):
        raise LiveLedgerError("TRADE_ECONOMICS_MISMATCH") from None


def postings_for_confirmed_trades(
    intents: Sequence[ExecutionIntent],
    trades: Sequence[VenueTradeEvent],
    economics: Sequence[AuthoritativeTradeEconomics],
) -> tuple[LiveLedgerPosting, ...]:
    """Create final postings only where exact confirmed event economics close."""
    intent_values = _dedupe_records(
        _snapshot_sequence(intents, ExecutionIntent, "INTENT_EVIDENCE_INVALID"),
        identity=lambda item: item.intent_id,
        conflict_code="INTENT_EVIDENCE_CONFLICT",
    )
    trade_values = _dedupe_records(
        _snapshot_sequence(trades, VenueTradeEvent, "TRADE_EVENT_INVALID"),
        identity=lambda item: item.trade_event_id,
        conflict_code="TRADE_EVENT_CONFLICT",
    )
    economics_values = _dedupe_records(
        _snapshot_sequence(
            economics,
            AuthoritativeTradeEconomics,
            "TRADE_ECONOMICS_INVALID",
        ),
        identity=lambda item: item.venue_trade_id,
        conflict_code="TRADE_ECONOMICS_CONFLICT",
    )
    intent_by_id = {item.intent_id: item for item in intent_values}
    economics_by_trade = {item.venue_trade_id: item for item in economics_values}
    confirmed_by_trade: dict[str, VenueTradeEvent] = {}
    terminal_by_trade: dict[str, VenueTradeEvent] = {}
    for trade in sorted(trade_values, key=lambda item: (item.received_at, item.trade_event_id)):
        existing = confirmed_by_trade.get(trade.venue_trade_id)
        if trade.normalized_state in {
            VenueTradeState.CONFIRMED,
            VenueTradeState.FAILED,
        }:
            terminal = terminal_by_trade.get(trade.venue_trade_id)
            if terminal is not None and canonical_execution_hash(
                terminal
            ) != canonical_execution_hash(trade):
                raise LiveLedgerError("TRADE_EVENT_CONFLICT") from None
            terminal_by_trade[trade.venue_trade_id] = trade
        if trade.normalized_state is VenueTradeState.CONFIRMED:
            if existing is not None and canonical_execution_hash(
                existing
            ) != canonical_execution_hash(trade):
                raise LiveLedgerError("TRADE_EVENT_CONFLICT") from None
            confirmed_by_trade[trade.venue_trade_id] = trade
    if set(economics_by_trade) != set(confirmed_by_trade):
        raise LiveLedgerError("TRADE_ECONOMICS_MISMATCH") from None

    rows: list[LiveLedgerPosting] = []
    for trade_id, trade in sorted(confirmed_by_trade.items()):
        if trade.intent_id is None or trade.intent_id not in intent_by_id:
            raise LiveLedgerError("TRADE_ECONOMICS_MISMATCH") from None
        source_intent = intent_by_id[trade.intent_id]
        exact = economics_by_trade[trade_id]
        _validated_trade_binding(source_intent, trade, exact)
        lineage = tuple(
            sorted(
                {
                    exact.trade_event_hash,
                    exact.settlement_hash,
                    exact.fee_hash,
                    exact.source_hash,
                    exact.economics_fingerprint,
                    *exact.balance_evidence_hashes,
                    *(
                        ()
                        if exact.cost_basis_evidence_hash is None
                        else (exact.cost_basis_evidence_hash,)
                    ),
                }
            )
        )
        notional = exact.price * exact.size
        if exact.side == "buy":
            rows.extend(
                _paired_postings(
                    exact,
                    debit_account=_account("settlement_receivable", exact.cash_asset_id),
                    credit_account=_account("venue_cash", exact.cash_asset_id),
                    asset_id=exact.cash_asset_id,
                    amount=notional,
                    lineage_hashes=lineage,
                )
            )
            rows.extend(
                _paired_postings(
                    exact,
                    debit_account=_account("venue_position", exact.position_asset_id),
                    credit_account=_account("settlement_receivable", exact.position_asset_id),
                    asset_id=exact.position_asset_id,
                    amount=exact.size,
                    lineage_hashes=lineage,
                )
            )
        else:
            rows.extend(
                _paired_postings(
                    exact,
                    debit_account=_account("venue_cash", exact.cash_asset_id),
                    credit_account=_account("settlement_receivable", exact.cash_asset_id),
                    asset_id=exact.cash_asset_id,
                    amount=notional,
                    lineage_hashes=lineage,
                )
            )
            rows.extend(
                _paired_postings(
                    exact,
                    debit_account=_account("settlement_receivable", exact.position_asset_id),
                    credit_account=_account("venue_position", exact.position_asset_id),
                    asset_id=exact.position_asset_id,
                    amount=exact.size,
                    lineage_hashes=lineage,
                )
            )
        if exact.fee:
            rows.extend(
                _paired_postings(
                    exact,
                    debit_account=_account("fees_paid", exact.cash_asset_id),
                    credit_account=_account("venue_cash", exact.cash_asset_id),
                    asset_id=exact.cash_asset_id,
                    amount=exact.fee,
                    lineage_hashes=lineage,
                )
            )
        if exact.realized_pnl is not None:
            if exact.realized_pnl > 0:
                debit_account = _account("settlement_receivable", exact.cash_asset_id)
                credit_account = _account("realized_pnl", exact.cash_asset_id)
            else:
                debit_account = _account("realized_pnl", exact.cash_asset_id)
                credit_account = _account("settlement_receivable", exact.cash_asset_id)
            rows.extend(
                _paired_postings(
                    exact,
                    debit_account=debit_account,
                    credit_account=credit_account,
                    asset_id=exact.cash_asset_id,
                    amount=exact.realized_pnl.copy_abs(),
                    lineage_hashes=lineage,
                )
            )
    result = tuple(sorted(rows, key=lambda item: item.posting_id))
    verify_live_conservation(result)
    return result


def _valid_account_name(value: str, asset_id: str) -> bool:
    if value in _ACCOUNT_BASES:
        return True
    base, separator, qualifier = value.partition(":")
    return separator == ":" and base in _ACCOUNT_BASES and qualifier == asset_id


def _valid_account_base(value: str) -> bool:
    return value.partition(":")[0] in _ACCOUNT_BASES


def verify_live_conservation(postings: Sequence[LiveLedgerPosting]) -> None:
    """Verify deterministic pairing and exact per-evidence/per-asset conservation."""
    values = _snapshot_sequence(postings, LiveLedgerPosting, "POSTING_INVALID")
    seen_ids: set[UUID] = set()
    pairs: dict[tuple[object, ...], list[LiveLedgerPosting]] = defaultdict(list)
    group_totals: dict[tuple[object, ...], list[Decimal]] = defaultdict(
        lambda: [Decimal("0"), Decimal("0")]
    )
    asset_totals: dict[str, list[Decimal]] = defaultdict(lambda: [Decimal("0"), Decimal("0")])
    for posting in values:
        if posting.posting_id in seen_ids:
            raise LiveLedgerError("POSTING_ID_DUPLICATE") from None
        seen_ids.add(posting.posting_id)
        amount = posting.debit_amount or posting.credit_amount
        if not _bounded_decimal(amount):
            raise LiveLedgerError("POSTING_AMOUNT_INVALID") from None
        if (
            posting.intent_id is None
            or posting.venue_order_id is None
            or posting.venue_trade_id is None
            or posting.settlement_hash is None
            or posting.fee_hash is None
            or not posting.balance_evidence_hashes
            or posting.debit_account == posting.credit_account
            or not _valid_account_base(posting.debit_account)
            or not _valid_account_base(posting.credit_account)
        ):
            raise LiveLedgerError("POSTING_ACCOUNT_INVALID") from None
        if posting.posting_id != _posting_id(posting):
            raise LiveLedgerError("POSTING_ID_MISMATCH") from None
        if not _valid_account_name(
            posting.debit_account, posting.asset_id
        ) or not _valid_account_name(posting.credit_account, posting.asset_id):
            raise LiveLedgerError("POSTING_ACCOUNT_INVALID") from None
        required_hashes = {
            posting.settlement_hash,
            posting.fee_hash,
            *posting.balance_evidence_hashes,
        }
        if not required_hashes <= set(posting.lineage_hashes):
            raise LiveLedgerError("POSTING_EVIDENCE_INVALID") from None
        pair_key = (
            posting.account_fingerprint,
            posting.intent_id,
            posting.venue_order_id,
            posting.venue_trade_id,
            posting.settlement_hash,
            posting.fee_hash,
            posting.balance_evidence_hashes,
            posting.debit_account,
            posting.credit_account,
            posting.asset_id,
            posting.occurred_at,
            posting.lineage_hashes,
            amount,
        )
        pairs[pair_key].append(posting)
        evidence_group = (
            posting.account_fingerprint,
            posting.intent_id,
            posting.venue_order_id,
            posting.venue_trade_id,
            posting.settlement_hash,
            posting.fee_hash,
            posting.balance_evidence_hashes,
            posting.occurred_at,
            posting.lineage_hashes,
            posting.asset_id,
        )
        group_totals[evidence_group][0] += posting.debit_amount
        group_totals[evidence_group][1] += posting.credit_amount
        asset_totals[posting.asset_id][0] += posting.debit_amount
        asset_totals[posting.asset_id][1] += posting.credit_amount
    for rows in pairs.values():
        if (
            len(rows) != 2
            or sum(row.debit_amount > 0 for row in rows) != 1
            or sum(row.credit_amount > 0 for row in rows) != 1
            or sum((row.debit_amount for row in rows), Decimal("0"))
            != sum((row.credit_amount for row in rows), Decimal("0"))
        ):
            raise LiveLedgerError("POSTING_PAIR_INVALID") from None
    if any(debit != credit for debit, credit in group_totals.values()) or any(
        debit != credit for debit, credit in asset_totals.values()
    ):
        raise LiveLedgerError("POSTING_CONSERVATION_INVALID") from None


__all__ = [
    "AuthoritativeTradeEconomics",
    "LiveLedgerError",
    "postings_for_confirmed_trades",
    "verify_live_conservation",
]
