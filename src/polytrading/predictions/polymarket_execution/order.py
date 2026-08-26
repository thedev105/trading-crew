"""Deterministic offline encoding and EOA signing for immediate Polymarket orders."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from hashlib import sha256
from typing import Annotated, Literal

from eth_account import Account
from eth_account.messages import encode_typed_data
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from polytrading.predictions.execution.models import (
    ExecutionIntent,
    SignedOrderEnvelope,
    _intent_fingerprint,
    deterministic_intent_id,
)
from polytrading.predictions.polymarket_execution.protocol import (
    PolymarketProtocolSnapshot,
    RoundingContract,
    TickSizeRule,
    load_protocol_snapshot,
)

EvmAddress = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{40}$")]
Bytes32 = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{64}$")]
ExchangeKind = Literal["standard", "negative_risk"]
OrderSide = Literal["buy", "sell"]
OrderKind = Literal["limit", "market"]

_ZERO_BYTES32 = "0x" + "00" * 32
_PUBLIC_ORDER_FIELDS = frozenset(
    {
        "builder",
        "expiration",
        "maker",
        "makerAmount",
        "metadata",
        "salt",
        "side",
        "signatureType",
        "signer",
        "takerAmount",
        "timestamp",
        "tokenId",
    }
)
_EIP712_DOMAIN_FIELDS = (
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
)
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ASCII_INTEGER = re.compile(r"[0-9]+")


class OrderAmountError(ValueError):
    """A stable, sanitized amount-encoding rejection."""


class OrderSigningError(ValueError):
    """A stable, sanitized signing or recovery rejection."""


class PolymarketOrder(BaseModel):
    """Current-v2 typed order fields plus the two local wire/domain selectors."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    salt: Annotated[int, Field(ge=0, le=2**256 - 1)]
    maker: EvmAddress
    signer: EvmAddress
    token_id: Annotated[int, Field(alias="tokenId", ge=0, le=2**256 - 1)]
    maker_amount: Annotated[int, Field(alias="makerAmount", gt=0, le=2**256 - 1)]
    taker_amount: Annotated[int, Field(alias="takerAmount", gt=0, le=2**256 - 1)]
    side: Annotated[int, Field(ge=0, le=1)]
    signature_type: Annotated[int, Field(alias="signatureType", ge=0, le=3)]
    timestamp: Annotated[int, Field(ge=0, le=2**256 - 1)]
    metadata: Bytes32
    builder: Bytes32
    expiration: Annotated[int, Field(ge=0, le=2**256 - 1)] = 0
    exchange_kind: ExchangeKind

    @model_validator(mode="after")
    def _validate_wallet_relationship(self) -> PolymarketOrder:
        if self.signature_type in {0, 3} and self.maker.casefold() != self.signer.casefold():
            raise ValueError("maker and signer must match for this signature type")
        return self

    def typed_message(self) -> dict[str, object]:
        return {
            "salt": self.salt,
            "maker": self.maker,
            "signer": self.signer,
            "tokenId": self.token_id,
            "makerAmount": self.maker_amount,
            "takerAmount": self.taker_amount,
            "side": self.side,
            "signatureType": self.signature_type,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "builder": self.builder,
        }

    def public_order(self) -> dict[str, object]:
        return {
            "builder": self.builder,
            "expiration": str(self.expiration),
            "maker": self.maker,
            "makerAmount": str(self.maker_amount),
            "metadata": self.metadata,
            "salt": self.salt,
            "side": "BUY" if self.side == 0 else "SELL",
            "signatureType": self.signature_type,
            "signer": self.signer,
            "takerAmount": str(self.taker_amount),
            "timestamp": str(self.timestamp),
            "tokenId": str(self.token_id),
        }

    @classmethod
    def from_public_order(
        cls,
        value: Mapping[str, object],
        *,
        exchange_kind: ExchangeKind,
    ) -> PolymarketOrder:
        if set(value) != _PUBLIC_ORDER_FIELDS:
            raise OrderSigningError("PUBLIC_ORDER_INVALID")
        try:
            side = {"BUY": 0, "SELL": 1}[value["side"]]
            return cls(
                salt=_wire_int(value["salt"]),
                maker=value["maker"],
                signer=value["signer"],
                tokenId=_wire_int(value["tokenId"]),
                makerAmount=_wire_int(value["makerAmount"]),
                takerAmount=_wire_int(value["takerAmount"]),
                side=side,
                signatureType=_wire_int(value["signatureType"]),
                timestamp=_wire_int(value["timestamp"]),
                metadata=value["metadata"],
                builder=value["builder"],
                expiration=_wire_int(value["expiration"]),
                exchange_kind=exchange_kind,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise OrderSigningError("PUBLIC_ORDER_INVALID") from error


def _wire_int(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and _ASCII_INTEGER.fullmatch(value) is not None:
        return int(value)
    raise ValueError("wire integer invalid")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _stable_digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def stable_order_salt(intent: ExecutionIntent) -> int:
    """Hash stable intent identity as one unambiguous canonical UTF-8 object."""
    preimage = {
        "intent_fingerprint": intent.intent_fingerprint,
        "intent_id": str(intent.intent_id),
        "protocol_version": intent.protocol_version,
    }
    digest = sha256(_canonical_json(preimage).encode("utf-8")).digest()
    return int.from_bytes(digest, "big", signed=False)


def _tick_rule(tick_size: Decimal, rounding: RoundingContract) -> TickSizeRule:
    for rule in rounding.tick_size_rules:
        if Decimal(rule.tick_size) == tick_size:
            return rule
    raise OrderAmountError("TICK_SIZE_UNSUPPORTED")


def _positive_decimal(value: Decimal, error_code: str) -> Decimal:
    try:
        valid = value.is_finite() and value > 0
    except (AttributeError, InvalidOperation):
        valid = False
    if not valid:
        raise OrderAmountError(error_code)
    return value


def _quantum(decimal_places: int) -> Decimal:
    return Decimal(1).scaleb(-decimal_places)


def _round_down(value: Decimal, decimal_places: int) -> Decimal:
    return value.quantize(_quantum(decimal_places), rounding=ROUND_DOWN)


def _round_up(value: Decimal, decimal_places: int) -> Decimal:
    return value.quantize(_quantum(decimal_places), rounding=ROUND_UP)


def _limit_quote(price: Decimal, shares: Decimal, amount_decimals: int) -> Decimal:
    raw = price * shares
    rounded = _round_up(raw, amount_decimals + 4)
    return _round_down(rounded, amount_decimals)


def _protocol_units(value: Decimal, token_decimals: int) -> int:
    scaled = value * (Decimal(10) ** token_decimals)
    if scaled != scaled.to_integral_value() or scaled <= 0:
        raise OrderAmountError("ROUNDED_AMOUNT_INVALID")
    return int(scaled)


def order_amounts(
    *,
    side: OrderSide,
    price: Decimal,
    size: Decimal,
    tick_size: Decimal,
    kind: OrderKind,
    rounding: RoundingContract,
) -> tuple[int, int]:
    """Convert public decimals to frozen six-decimal protocol integer units."""
    if side not in {"buy", "sell"}:
        raise OrderAmountError("SIDE_UNSUPPORTED")
    if kind not in {"limit", "market"}:
        raise OrderAmountError("ORDER_KIND_UNSUPPORTED")
    price = _positive_decimal(price, "PRICE_INVALID")
    size = _positive_decimal(size, "SIZE_INVALID")
    tick_size = _positive_decimal(tick_size, "TICK_SIZE_UNSUPPORTED")
    rule = _tick_rule(tick_size, rounding)

    if kind == "limit":
        normalized_price = _round_down(price, rule.price_decimals)
        if normalized_price != price or normalized_price % tick_size != 0:
            raise OrderAmountError("PRICE_TICK_INVALID")
    else:
        normalized_price = _round_down(price, rule.price_decimals)
        if normalized_price <= 0 or normalized_price % tick_size != 0:
            raise OrderAmountError("PRICE_TICK_INVALID")
    if normalized_price > 1:
        raise OrderAmountError("PRICE_INVALID")

    rounded_input = _round_down(size, rule.size_decimals)
    if rounded_input <= 0:
        raise OrderAmountError("ROUNDED_AMOUNT_INVALID")

    if kind == "limit":
        shares = rounded_input
        usd = _limit_quote(normalized_price, shares, rule.amount_decimals)
    elif side == "buy":
        usd = rounded_input
        shares = _round_up(
            _round_up(usd / normalized_price, rule.amount_decimals + 4),
            rule.amount_decimals,
        )
    else:
        shares = rounded_input
        usd = _limit_quote(normalized_price, shares, rule.amount_decimals)

    usd_units = _protocol_units(usd, rounding.token_decimals)
    share_units = _protocol_units(shares, rounding.token_decimals)
    return (usd_units, share_units) if side == "buy" else (share_units, usd_units)


def _exchange_address(order: PolymarketOrder, snapshot: PolymarketProtocolSnapshot) -> str:
    return getattr(snapshot.eip712.exchange_addresses, order.exchange_kind)


def _order_domain(
    order: PolymarketOrder,
    snapshot: PolymarketProtocolSnapshot,
) -> dict[str, object]:
    domain = snapshot.eip712.order_domain.model_dump(mode="json", by_alias=True)
    domain["verifyingContract"] = _exchange_address(order, snapshot)
    return domain


def order_typed_data(
    order: PolymarketOrder,
    snapshot: PolymarketProtocolSnapshot,
) -> dict[str, object]:
    """Build only the exact frozen current-v2 typed fields in their frozen order."""
    wallet = next(
        (
            candidate
            for candidate in snapshot.eip712.wallets
            if candidate.signature_type == order.signature_type
        ),
        None,
    )
    if (
        wallet is None
        or wallet.signing_primary_type != "Order"
        or wallet.signature_encoding != "standard_eip712"
    ):
        raise OrderSigningError("SIGNATURE_CONTEXT_UNSUPPORTED")
    return {
        "types": {
            "EIP712Domain": [dict(field) for field in _EIP712_DOMAIN_FIELDS],
            "Order": [field.model_dump() for field in snapshot.eip712.order_fields],
        },
        "primaryType": snapshot.eip712.order_primary_type,
        "domain": _order_domain(order, snapshot),
        "message": order.typed_message(),
    }


def order_fingerprint(
    order: PolymarketOrder,
    snapshot: PolymarketProtocolSnapshot,
) -> str:
    """Bind canonical public order material to its exact EIP-712 domain."""
    return _stable_digest(
        {
            "domain": _order_domain(order, snapshot),
            "order": order.public_order(),
        }
    )


def _address_fingerprint(address: str) -> str:
    """SHA-256 the decoded 20-byte EVM address; display case is not identity."""
    if len(address) != 42 or not address.startswith("0x"):
        raise OrderSigningError("EVM_ADDRESS_INVALID")
    try:
        decoded = bytes.fromhex(address[2:])
    except ValueError as error:
        raise OrderSigningError("EVM_ADDRESS_INVALID") from error
    if len(decoded) != 20:
        raise OrderSigningError("EVM_ADDRESS_INVALID")
    return sha256(decoded).hexdigest()


def _timestamp_milliseconds(value: datetime) -> int:
    value_utc = value.astimezone(UTC)
    delta = value_utc - _UNIX_EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _require_intent_integrity(intent: ExecutionIntent) -> None:
    try:
        expected_fingerprint = _intent_fingerprint(intent)
        expected_id = deterministic_intent_id(intent)
    except Exception as error:
        raise OrderSigningError("INTENT_INVALID") from error
    if intent.intent_fingerprint != expected_fingerprint or intent.intent_id != expected_id:
        raise OrderSigningError("INTENT_COLLISION")


def _order_from_intent(
    intent: ExecutionIntent,
    maker: str,
    snapshot: PolymarketProtocolSnapshot,
) -> PolymarketOrder:
    if intent.protocol_version != snapshot.version:
        raise OrderSigningError("PROTOCOL_VERSION_MISMATCH")
    if intent.rounding_mode != "ROUND_DOWN":
        raise OrderSigningError("ROUNDING_MODE_UNSUPPORTED")
    try:
        _tick_rule(intent.tick_size, snapshot.rounding)
    except OrderAmountError as error:
        raise OrderSigningError(str(error)) from error

    if intent.base_size is not None:
        kind: OrderKind = "limit"
        amount = intent.base_size
    elif intent.maximum_spend is not None and intent.side == "buy":
        kind = "market"
        amount = intent.maximum_spend
    else:
        raise OrderSigningError("ORDER_AMOUNT_CONTEXT_UNSUPPORTED")

    try:
        maker_amount, taker_amount = order_amounts(
            side=intent.side,
            price=intent.limit_price,
            size=amount,
            tick_size=intent.tick_size,
            kind=kind,
            rounding=snapshot.rounding,
        )
    except OrderAmountError as error:
        raise OrderSigningError(str(error)) from error
    try:
        token_id = int(intent.token_id)
    except ValueError as error:
        raise OrderSigningError("TOKEN_ID_INVALID") from error
    if token_id < 0 or str(token_id) != intent.token_id:
        raise OrderSigningError("TOKEN_ID_INVALID")

    return PolymarketOrder(
        salt=stable_order_salt(intent),
        maker=maker,
        signer=maker,
        tokenId=token_id,
        makerAmount=maker_amount,
        takerAmount=taker_amount,
        side=snapshot.order_submission.side_encodings.buy
        if intent.side == "buy"
        else snapshot.order_submission.side_encodings.sell,
        signatureType=0,
        timestamp=_timestamp_milliseconds(intent.created_at),
        metadata=_ZERO_BYTES32,
        builder=_ZERO_BYTES32,
        expiration=int(snapshot.order_submission.market_order_expiration),
        exchange_kind=intent.exchange_kind,
    )


def sign_order(
    intent: ExecutionIntent,
    private_key: bytes,
    snapshot: PolymarketProtocolSnapshot,
) -> SignedOrderEnvelope:
    """Sign one deterministic EOA order without retaining or reflecting private material."""
    _require_intent_integrity(intent)
    try:
        maker = Account.from_key(private_key).address
    except Exception:
        raise OrderSigningError("PRIVATE_KEY_INVALID") from None
    if _address_fingerprint(maker) != intent.account_fingerprint:
        raise OrderSigningError("ACCOUNT_FINGERPRINT_MISMATCH")

    order = _order_from_intent(intent, maker, snapshot)
    typed_data = order_typed_data(order, snapshot)
    try:
        signed = Account.sign_typed_data(private_key, full_message=typed_data)
    except Exception:
        raise OrderSigningError("ORDER_SIGNING_FAILED") from None
    public_signature = "0x" + bytes(signed.signature).hex()
    public_order = order.public_order()
    canonical_order_json = _canonical_json(public_order)
    exact_body_hash = _stable_digest({**public_order, "signature": public_signature})
    exchange_address = _exchange_address(order, snapshot)
    envelope = SignedOrderEnvelope(
        schema_version=1,
        intent_id=intent.intent_id,
        intent_fingerprint=intent.intent_fingerprint,
        protocol_version=intent.protocol_version,
        salt=order.salt,
        signature_type=order.signature_type,
        public_signature=public_signature,
        domain_fingerprint=_stable_digest(typed_data["domain"]),
        exact_body_hash=exact_body_hash,
        order_fingerprint=order_fingerprint(order, snapshot),
        signer_version="eth-account==0.13.7",
        canonical_order_json=canonical_order_json,
        exchange_fingerprint=_address_fingerprint(exchange_address),
    )
    if recover_order_signer(envelope, snapshot).casefold() != maker.casefold():
        raise OrderSigningError("RECOVERED_SIGNER_MISMATCH")
    return envelope


def _envelope_exchange_kind(
    envelope: SignedOrderEnvelope,
    snapshot: PolymarketProtocolSnapshot,
) -> ExchangeKind:
    for kind in ("standard", "negative_risk"):
        address = getattr(snapshot.eip712.exchange_addresses, kind)
        if envelope.exchange_fingerprint == _address_fingerprint(address):
            return kind
    raise OrderSigningError("EXCHANGE_FINGERPRINT_MISMATCH")


def recover_order_signer(
    envelope: SignedOrderEnvelope,
    snapshot: PolymarketProtocolSnapshot | None = None,
) -> str:
    """Verify envelope bindings and recover the EIP-712 signer address."""
    snapshot = snapshot or load_protocol_snapshot()
    if envelope.protocol_version != snapshot.version:
        raise OrderSigningError("PROTOCOL_VERSION_MISMATCH")
    try:
        public_order = json.loads(envelope.canonical_order_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise OrderSigningError("PUBLIC_ORDER_INVALID") from error
    if not isinstance(public_order, dict):
        raise OrderSigningError("PUBLIC_ORDER_INVALID")
    order = PolymarketOrder.from_public_order(
        public_order,
        exchange_kind=_envelope_exchange_kind(envelope, snapshot),
    )
    if order.salt != envelope.salt or order.signature_type != envelope.signature_type:
        raise OrderSigningError("ENVELOPE_ORDER_MISMATCH")
    typed_data = order_typed_data(order, snapshot)
    if _stable_digest(typed_data["domain"]) != envelope.domain_fingerprint:
        raise OrderSigningError("DOMAIN_FINGERPRINT_MISMATCH")
    if order_fingerprint(order, snapshot) != envelope.order_fingerprint:
        raise OrderSigningError("ORDER_FINGERPRINT_MISMATCH")
    if (
        _stable_digest({**public_order, "signature": envelope.public_signature})
        != envelope.exact_body_hash
    ):
        raise OrderSigningError("EXACT_BODY_HASH_MISMATCH")
    try:
        recovered = Account.recover_message(
            encode_typed_data(full_message=typed_data),
            signature=envelope.public_signature,
        )
    except Exception:
        raise OrderSigningError("ORDER_SIGNATURE_INVALID") from None
    if recovered.casefold() != order.maker.casefold():
        raise OrderSigningError("RECOVERED_SIGNER_MISMATCH")
    return recovered


__all__ = [
    "OrderAmountError",
    "OrderSigningError",
    "PolymarketOrder",
    "order_amounts",
    "order_fingerprint",
    "order_typed_data",
    "recover_order_signer",
    "sign_order",
    "stable_order_salt",
]
