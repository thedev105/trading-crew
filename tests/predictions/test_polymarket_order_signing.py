from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

import pytest
from eth_account import Account

from polytrading.predictions.execution.models import ExecutionIntent, SignedOrderEnvelope
from polytrading.predictions.polymarket_execution import load_protocol_snapshot
from polytrading.predictions.polymarket_execution.order import (
    OrderSigningError,
    PolymarketOrder,
    order_fingerprint,
    order_typed_data,
    recover_order_signer,
    sign_order,
    stable_order_salt,
)
from tests.predictions.execution_helpers import execution_intent_fields

PRIVATE_KEY = bytes.fromhex("00" * 31 + "01")
MAKER = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
ACCOUNT_FINGERPRINT = "d6f781065c489e6513f45bc3dab82156055056d393c42f49a4defec22b5ee73f"
ZERO_BYTES32 = "0x" + "00" * 32


def _intent(**overrides: object) -> ExecutionIntent:
    return ExecutionIntent(
        **execution_intent_fields(
            **{"account_fingerprint": ACCOUNT_FINGERPRINT, **overrides},
        )
    )


def _wire_vector_order() -> PolymarketOrder:
    vector_path = load_protocol_snapshot().fixture_root / "order_vectors_v1.json"
    vector = json.loads(vector_path.read_text(encoding="utf-8"))["wire_vector"]
    message = vector["typed_data"]["message"]
    return PolymarketOrder(
        salt=int(message["salt"]),
        maker=message["maker"],
        signer=message["signer"],
        tokenId=int(message["tokenId"]),
        makerAmount=int(message["makerAmount"]),
        takerAmount=int(message["takerAmount"]),
        side=message["side"],
        signatureType=message["signatureType"],
        timestamp=int(message["timestamp"]),
        metadata=message["metadata"],
        builder=message["builder"],
        expiration=0,
        exchange_kind="standard",
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _envelope_with_signature_type(signature_type: int) -> SignedOrderEnvelope:
    snapshot = load_protocol_snapshot()
    envelope = sign_order(_intent(), PRIVATE_KEY, snapshot)
    public_order = {
        **json.loads(envelope.canonical_order_json),
        "signatureType": signature_type,
    }
    if signature_type == 3:
        return envelope.model_copy(
            update={
                "signature_type": signature_type,
                "canonical_order_json": _canonical_json(public_order),
            }
        )
    order = PolymarketOrder.from_public_order(public_order, exchange_kind="standard")
    typed_data = order_typed_data(order, snapshot)
    signature = (
        "0x" + bytes(Account.sign_typed_data(PRIVATE_KEY, full_message=typed_data).signature).hex()
    )
    return envelope.model_copy(
        update={
            "signature_type": signature_type,
            "public_signature": signature,
            "domain_fingerprint": _digest(typed_data["domain"]),
            "exact_body_hash": _digest({**public_order, "signature": signature}),
            "order_fingerprint": order_fingerprint(order, snapshot),
            "canonical_order_json": _canonical_json(public_order),
        }
    )


def test_stable_order_salt_pins_canonical_preimage_digest_and_integer() -> None:
    intent = ExecutionIntent.model_construct(
        protocol_version="polymarket-clob-2026-08-25-v1",
        intent_id=UUID("00000000-0000-0000-0000-000000000001"),
        intent_fingerprint="ab" * 32,
    )
    canonical = (
        b'{"intent_fingerprint":"abababababababababababababababababababababababababababababababab",'
        b'"intent_id":"00000000-0000-0000-0000-000000000001",'
        b'"protocol_version":"polymarket-clob-2026-08-25-v1"}'
    )

    assert sha256(canonical).hexdigest() == (
        "a0f67ca003541b87fcd598516cd241003f58e2daf9a1b161fce0c53ec1038f51"
    )
    assert stable_order_salt(intent) == (
        72805560281747110170466192385594033112525788951575137691416771914830480379729
    )


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("protocol_version", "polymarket-clob-future"),
        ("intent_id", UUID("00000000-0000-0000-0000-000000000002")),
        ("intent_fingerprint", "cd" * 32),
    ),
)
def test_each_salt_preimage_component_changes_the_salt(field: str, changed: object) -> None:
    intent = ExecutionIntent.model_construct(
        protocol_version="polymarket-clob-2026-08-25-v1",
        intent_id=UUID("00000000-0000-0000-0000-000000000001"),
        intent_fingerprint="ab" * 32,
    )

    changed_salt = stable_order_salt(intent.model_copy(update={field: changed}))
    assert changed_salt != stable_order_salt(intent)


def test_typed_data_matches_frozen_current_v2_wire_vector() -> None:
    snapshot = load_protocol_snapshot()
    order = _wire_vector_order()
    vector = json.loads(
        (snapshot.fixture_root / "order_vectors_v1.json").read_text(encoding="utf-8")
    )["wire_vector"]["typed_data"]

    typed_data = order_typed_data(order, snapshot)

    assert typed_data["domain"] == vector["domain"]
    assert typed_data["primaryType"] == "Order"
    assert typed_data["message"] == {
        **vector["message"],
        "salt": int(vector["message"]["salt"]),
        "tokenId": int(vector["message"]["tokenId"]),
        "makerAmount": int(vector["message"]["makerAmount"]),
        "takerAmount": int(vector["message"]["takerAmount"]),
        "timestamp": int(vector["message"]["timestamp"]),
    }
    assert tuple(field["name"] for field in typed_data["types"]["Order"]) == (
        "salt",
        "maker",
        "signer",
        "tokenId",
        "makerAmount",
        "takerAmount",
        "side",
        "signatureType",
        "timestamp",
        "metadata",
        "builder",
    )
    assert "expiration" not in typed_data["message"]


@pytest.mark.parametrize("signature_type", (0, 1, 2))
def test_current_standard_eip712_wallet_paths_construct_the_frozen_domain(
    signature_type: int,
) -> None:
    snapshot = load_protocol_snapshot()
    order = _wire_vector_order().model_copy(update={"signature_type": signature_type})

    typed_data = order_typed_data(order, snapshot)

    assert typed_data["domain"] == {
        "name": "Polymarket CTF Exchange",
        "version": "2",
        "chainId": 137,
        "verifyingContract": snapshot.eip712.exchange_addresses.standard,
    }


def test_deposit_wrapper_requires_future_signing_context() -> None:
    order = _wire_vector_order().model_copy(update={"signature_type": 3})

    with pytest.raises(OrderSigningError, match="SIGNATURE_CONTEXT_UNSUPPORTED"):
        order_typed_data(order, load_protocol_snapshot())


def test_sign_recover_and_retry_are_byte_equivalent() -> None:
    intent = _intent()
    snapshot = load_protocol_snapshot()

    first = sign_order(intent, PRIVATE_KEY, snapshot)
    second = sign_order(intent, PRIVATE_KEY, snapshot)

    assert first == second
    assert recover_order_signer(first, snapshot) == MAKER
    assert recover_order_signer(first) == MAKER
    assert first.public_signature.startswith("0x")
    assert len(first.public_signature) == 132
    assert first.public_signature == (
        "0x5b9e22fa0365f8892338a51e47fcc15c5e603f20fd0d8d2bbc05e66357a407da"
        "7a91157af6113ddcd53e5be1969f8e18a00142839e74173f341b85fb8bf044011c"
    )
    assert first.domain_fingerprint == (
        "1feee49d347d5eb4a787b7c3ec99e591830fe6712fc635bdf3c82db17ebf3cc0"
    )
    assert first.exact_body_hash == (
        "2201756cba2fdcd84f704de2a5c63fc70676ec14a705cedc1bd852f8098b4ef7"
    )
    assert first.order_fingerprint == (
        "0d35a463727531d7bbe786c32ca2d7ef0724d5bb08446a185a47a880bb0da5c1"
    )
    assert first.exchange_fingerprint == (
        "85c145f2427d2871411c94a3d91d4b9768f95b4a3057a6830d67ecac8dc7c56e"
    )
    assert first.signer_version == "eth-account==0.13.7"
    assert first.salt == stable_order_salt(intent)
    assert first.canonical_order_json == (
        '{"builder":"0x0000000000000000000000000000000000000000000000000000000000000000",'
        '"expiration":"0","maker":"0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf",'
        '"makerAmount":"5100000",'
        '"metadata":"0x0000000000000000000000000000000000000000000000000000000000000000",'
        '"salt":33259949623945454295236646213411715008699519455707620003231606628366979226768,'
        '"side":"BUY","signatureType":0,'
        '"signer":"0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf",'
        '"takerAmount":"10000000","timestamp":"1787673600000","tokenId":"217426"}'
    )
    public_order = json.loads(first.canonical_order_json)
    assert public_order == {
        "builder": ZERO_BYTES32,
        "expiration": "0",
        "maker": MAKER,
        "makerAmount": "5100000",
        "metadata": ZERO_BYTES32,
        "salt": first.salt,
        "side": "BUY",
        "signatureType": 0,
        "signer": MAKER,
        "takerAmount": "10000000",
        "timestamp": "1787673600000",
        "tokenId": "217426",
    }
    assert first.order_fingerprint == order_fingerprint(
        PolymarketOrder.from_public_order(public_order, exchange_kind="standard"),
        snapshot,
    )


def test_limit_buy_rejects_final_protocol_maker_amount_above_explicit_spend_cap() -> None:
    intent = _intent(
        base_size=Decimal("10"),
        limit_price=Decimal("0.51"),
        maximum_spend=Decimal("0.01"),
    )

    with pytest.raises(OrderSigningError, match="MAXIMUM_SPEND_EXCEEDED"):
        sign_order(intent, PRIVATE_KEY, load_protocol_snapshot())


@pytest.mark.parametrize(
    ("maximum_spend", "exceeds_cap"),
    (
        (Decimal("5.099999"), True),
        (Decimal("5.100000"), False),
        (Decimal("5.100001"), False),
    ),
    ids=("one-protocol-unit-below", "exact", "one-protocol-unit-above"),
)
def test_limit_buy_spend_cap_uses_final_protocol_amount_quantum(
    maximum_spend: Decimal,
    exceeds_cap: bool,
) -> None:
    intent = _intent(base_size=Decimal("10"), maximum_spend=maximum_spend)

    if exceeds_cap:
        with pytest.raises(OrderSigningError, match="MAXIMUM_SPEND_EXCEEDED"):
            sign_order(intent, PRIVATE_KEY, load_protocol_snapshot())
        return

    envelope = sign_order(intent, PRIVATE_KEY, load_protocol_snapshot())
    assert json.loads(envelope.canonical_order_json)["makerAmount"] == "5100000"


def test_market_buy_accepts_exact_final_protocol_maker_amount_cap() -> None:
    envelope = sign_order(
        _intent(base_size=None, maximum_spend=Decimal("5.10")),
        PRIVATE_KEY,
        load_protocol_snapshot(),
    )

    assert json.loads(envelope.canonical_order_json)["makerAmount"] == "5100000"


@pytest.mark.parametrize("signature_type", (1, 2, 3))
def test_recovery_rejects_every_non_eoa_signature_context(signature_type: int) -> None:
    envelope = _envelope_with_signature_type(signature_type)

    with pytest.raises(OrderSigningError, match="SIGNATURE_CONTEXT_UNSUPPORTED") as rejected:
        recover_order_signer(envelope, load_protocol_snapshot())
    assert rejected.value.__cause__ is None


@pytest.mark.parametrize(
    ("field", "changed", "error_code"),
    (
        ("salt", 2, "ENVELOPE_ORDER_MISMATCH"),
        ("maker", "0x" + "22" * 20, "PUBLIC_ORDER_INVALID"),
        ("signer", "0x" + "22" * 20, "PUBLIC_ORDER_INVALID"),
        ("tokenId", "217427", "ORDER_FINGERPRINT_MISMATCH"),
        ("makerAmount", "5100001", "ORDER_FINGERPRINT_MISMATCH"),
        ("takerAmount", "10000001", "ORDER_FINGERPRINT_MISMATCH"),
        ("side", "SELL", "ORDER_FINGERPRINT_MISMATCH"),
        ("signatureType", 1, "ENVELOPE_ORDER_MISMATCH"),
        ("timestamp", "1787673600001", "ORDER_FINGERPRINT_MISMATCH"),
        ("metadata", "0x" + "01" * 32, "ORDER_FINGERPRINT_MISMATCH"),
        ("builder", "0x" + "01" * 32, "ORDER_FINGERPRINT_MISMATCH"),
        ("expiration", "1", "ORDER_FINGERPRINT_MISMATCH"),
    ),
)
def test_recovery_rejects_each_independently_mutated_public_order_field(
    field: str,
    changed: object,
    error_code: str,
) -> None:
    snapshot = load_protocol_snapshot()
    envelope = sign_order(_intent(), PRIVATE_KEY, snapshot)
    public_order = {**json.loads(envelope.canonical_order_json), field: changed}
    changed_envelope = envelope.model_copy(
        update={"canonical_order_json": _canonical_json(public_order)}
    )

    with pytest.raises(OrderSigningError, match=error_code):
        recover_order_signer(changed_envelope, snapshot)


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("name", "Changed Exchange"),
        ("version", "3"),
        ("chain_id", 1),
    ),
)
def test_recovery_rejects_each_mutated_domain_identity_field(
    field: str,
    changed: object,
) -> None:
    snapshot = load_protocol_snapshot()
    envelope = sign_order(_intent(), PRIVATE_KEY, snapshot)
    changed_domain = snapshot.eip712.order_domain.model_copy(update={field: changed})
    changed_snapshot = snapshot.model_copy(
        update={"eip712": snapshot.eip712.model_copy(update={"order_domain": changed_domain})}
    )

    with pytest.raises(OrderSigningError, match="DOMAIN_FINGERPRINT_MISMATCH"):
        recover_order_signer(envelope, changed_snapshot)


def test_recovery_rejects_mutated_verifying_contract() -> None:
    snapshot = load_protocol_snapshot()
    envelope = sign_order(_intent(), PRIVATE_KEY, snapshot)
    changed_addresses = snapshot.eip712.exchange_addresses.model_copy(
        update={"standard": "0x" + "33" * 20}
    )
    changed_snapshot = snapshot.model_copy(
        update={
            "eip712": snapshot.eip712.model_copy(update={"exchange_addresses": changed_addresses})
        }
    )

    with pytest.raises(OrderSigningError, match="EXCHANGE_FINGERPRINT_MISMATCH"):
        recover_order_signer(envelope, changed_snapshot)


def test_signing_negative_risk_binds_its_exchange_domain() -> None:
    standard = sign_order(_intent(exchange_kind="standard"), PRIVATE_KEY, load_protocol_snapshot())
    negative = sign_order(
        _intent(exchange_kind="negative_risk"),
        PRIVATE_KEY,
        load_protocol_snapshot(),
    )

    assert standard.intent_id != negative.intent_id
    assert standard.domain_fingerprint != negative.domain_fingerprint
    assert standard.exchange_fingerprint != negative.exchange_fingerprint
    assert standard.order_fingerprint != negative.order_fingerprint
    assert standard.public_signature != negative.public_signature


def test_wrong_domain_recovery_fails_closed() -> None:
    snapshot = load_protocol_snapshot()
    envelope = sign_order(_intent(), PRIVATE_KEY, snapshot)
    changed_domain = snapshot.eip712.order_domain.model_copy(update={"chain_id": 1})
    changed_eip712 = snapshot.eip712.model_copy(update={"order_domain": changed_domain})
    wrong_snapshot = snapshot.model_copy(update={"eip712": changed_eip712})

    with pytest.raises(OrderSigningError, match="DOMAIN_FINGERPRINT_MISMATCH"):
        recover_order_signer(envelope, wrong_snapshot)


def test_changed_public_signature_fails_exact_wire_hash_before_recovery() -> None:
    snapshot = load_protocol_snapshot()
    envelope = sign_order(_intent(), PRIVATE_KEY, snapshot)
    replacement = "00" if envelope.public_signature[-2:] != "00" else "01"
    changed = envelope.model_copy(
        update={"public_signature": envelope.public_signature[:-2] + replacement}
    )

    with pytest.raises(OrderSigningError, match="EXACT_BODY_HASH_MISMATCH"):
        recover_order_signer(changed, snapshot)


def test_invalid_signature_recovery_error_is_sanitized() -> None:
    snapshot = load_protocol_snapshot()
    envelope = sign_order(_intent(), PRIVATE_KEY, snapshot)
    invalid_signature = "0x00"
    signed_public_order = {
        **json.loads(envelope.canonical_order_json),
        "signature": invalid_signature,
    }
    exact_body_hash = sha256(
        json.dumps(
            signed_public_order,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    changed = envelope.model_copy(
        update={"public_signature": invalid_signature, "exact_body_hash": exact_body_hash}
    )

    with pytest.raises(OrderSigningError, match="ORDER_SIGNATURE_INVALID") as invalid:
        recover_order_signer(changed, snapshot)
    assert invalid.value.__cause__ is None


def test_signer_rejects_account_mismatch_without_reflecting_private_material() -> None:
    canary = b"private-key-canary"

    with pytest.raises(OrderSigningError, match="PRIVATE_KEY_INVALID") as invalid:
        sign_order(_intent(), canary, load_protocol_snapshot())
    assert canary.decode() not in str(invalid.value)
    assert canary.decode() not in repr(invalid.value)
    assert invalid.value.__cause__ is None

    with pytest.raises(OrderSigningError, match="ACCOUNT_FINGERPRINT_MISMATCH") as mismatch:
        sign_order(
            _intent(account_fingerprint="f" * 64),
            PRIVATE_KEY,
            load_protocol_snapshot(),
        )
    assert PRIVATE_KEY.hex() not in str(mismatch.value)
    assert PRIVATE_KEY.hex() not in repr(mismatch.value)


def test_signing_rejects_nonfrozen_tick_rounding_mode_and_intent_collision() -> None:
    snapshot = load_protocol_snapshot()

    with pytest.raises(OrderSigningError, match="TICK_SIZE_UNSUPPORTED"):
        sign_order(_intent(tick_size=Decimal("0.02")), PRIVATE_KEY, snapshot)
    with pytest.raises(OrderSigningError, match="ROUNDING_MODE_UNSUPPORTED"):
        sign_order(_intent(rounding_mode="ROUND_HALF_UP"), PRIVATE_KEY, snapshot)

    intent = _intent()
    collision = intent.model_copy(update={"intent_fingerprint": "f" * 64})
    with pytest.raises(OrderSigningError, match="INTENT_COLLISION"):
        sign_order(collision, PRIVATE_KEY, snapshot)


def test_timestamp_is_utc_unix_milliseconds_with_fractional_flooring() -> None:
    intent = _intent(created_at=datetime(2026, 8, 25, 16, 0, 0, 123999, tzinfo=UTC))

    envelope = sign_order(intent, PRIVATE_KEY, load_protocol_snapshot())

    assert json.loads(envelope.canonical_order_json)["timestamp"] == "1787673600123"


@pytest.mark.parametrize("forbidden_field", ("feeRateBps", "nonce", "taker"))
def test_stale_or_superseded_order_fields_are_rejected(forbidden_field: str) -> None:
    envelope = sign_order(_intent(), PRIVATE_KEY, load_protocol_snapshot())
    order = json.loads(envelope.canonical_order_json)
    order[forbidden_field] = 0
    invalid_payload = {
        **envelope.model_dump(mode="python"),
        "canonical_order_json": json.dumps(order, sort_keys=True, separators=(",", ":")),
    }

    with pytest.raises(ValueError, match="public order fields"):
        type(envelope).model_validate(invalid_payload)
