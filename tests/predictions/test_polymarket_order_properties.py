from __future__ import annotations

import json
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from polytrading.predictions.polymarket_execution import load_protocol_snapshot
from polytrading.predictions.polymarket_execution.order import (
    OrderAmountError,
    PolymarketOrder,
    order_amounts,
    order_fingerprint,
)

TICKS = ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001")
TICK_AMOUNT_DECIMALS = {
    "0.1": 3,
    "0.01": 4,
    "0.005": 5,
    "0.0025": 6,
    "0.001": 5,
    "0.0001": 6,
}
ZERO_BYTES32 = "0x" + "00" * 32


def _order(**overrides: object) -> PolymarketOrder:
    fields: dict[str, object] = {
        "salt": 1,
        "maker": "0x" + "11" * 20,
        "signer": "0x" + "11" * 20,
        "tokenId": 123,
        "makerAmount": 5_200_000,
        "takerAmount": 10_000_000,
        "side": 0,
        "signatureType": 0,
        "timestamp": 1_787_673_600_000,
        "metadata": ZERO_BYTES32,
        "builder": ZERO_BYTES32,
        "expiration": 0,
        "exchange_kind": "standard",
    }
    fields.update(overrides)
    return PolymarketOrder(**fields)


@pytest.mark.parametrize(
    ("side", "price", "size", "tick", "kind", "expected"),
    (
        ("buy", "0.52", "10", "0.01", "limit", (5_200_000, 10_000_000)),
        ("sell", "0.52", "10", "0.01", "limit", (10_000_000, 5_200_000)),
        ("buy", "0.52", "10", "0.01", "market", (10_000_000, 19_230_800)),
        ("sell", "0.52", "10", "0.01", "market", (10_000_000, 5_200_000)),
    ),
)
def test_amounts_match_every_frozen_official_vector_and_side_pair(
    side: str,
    price: str,
    size: str,
    tick: str,
    kind: str,
    expected: tuple[int, int],
) -> None:
    assert (
        order_amounts(
            side=side,
            price=Decimal(price),
            size=Decimal(size),
            tick_size=Decimal(tick),
            kind=kind,
            rounding=load_protocol_snapshot().rounding,
        )
        == expected
    )


def test_all_frozen_amount_vectors_are_exercised_from_the_authenticated_fixture() -> None:
    snapshot = load_protocol_snapshot()
    vectors = json.loads(
        (snapshot.fixture_root / "order_vectors_v1.json").read_text(encoding="utf-8")
    )["amount_vectors"]

    assert {vector["vector_id"] for vector in vectors} == {
        "limit_buy_10_at_0_52_tick_0_01",
        "limit_sell_10_at_0_52_tick_0_01",
        "market_buy_10_usd_max_0_52_tick_0_01",
    }
    for vector in vectors:
        assert order_amounts(
            side=vector["side"].lower(),
            price=Decimal(vector["price"]),
            size=Decimal(vector["input_amount"]),
            tick_size=Decimal(vector["tick_size"]),
            kind=vector["kind"].lower(),
            rounding=snapshot.rounding,
        ) == (int(vector["makerAmount"]), int(vector["takerAmount"]))


@given(
    tick=st.sampled_from(TICKS),
    side=st.sampled_from(("buy", "sell")),
    price_ticks=st.integers(min_value=1, max_value=9),
    size_cents=st.integers(min_value=1, max_value=10_000),
)
def test_limit_amount_property_covers_every_frozen_tick_and_side(
    tick: str,
    side: str,
    price_ticks: int,
    size_cents: int,
) -> None:
    tick_value = Decimal(tick)
    price = tick_value * price_ticks
    size = Decimal(size_cents) / 100
    expected_shares = int(size * 1_000_000)
    expected_usd = int(price * size * 1_000_000)

    maker, taker = order_amounts(
        side=side,
        price=price,
        size=size + Decimal("0.009999"),
        tick_size=tick_value,
        kind="limit",
        rounding=load_protocol_snapshot().rounding,
    )

    assert (maker, taker) == (
        (expected_usd, expected_shares) if side == "buy" else (expected_shares, expected_usd)
    )


@given(
    tick=st.sampled_from(TICKS),
    side=st.sampled_from(("buy", "sell")),
    price_ticks=st.integers(min_value=1, max_value=9),
    input_cents=st.integers(min_value=1, max_value=10_000),
)
def test_market_amount_property_covers_every_frozen_tick_and_side(
    tick: str,
    side: str,
    price_ticks: int,
    input_cents: int,
) -> None:
    tick_value = Decimal(tick)
    price = tick_value * price_ticks
    rounded_input = Decimal(input_cents) / 100
    input_units = int(rounded_input * 1_000_000)

    maker, taker = order_amounts(
        side=side,
        price=price,
        size=rounded_input + Decimal("0.009999"),
        tick_size=tick_value,
        kind="market",
        rounding=load_protocol_snapshot().rounding,
    )

    amount_quantum = Decimal(1).scaleb(-TICK_AMOUNT_DECIMALS[tick])
    if side == "buy":
        shares = (rounded_input / price).quantize(amount_quantum, rounding=ROUND_CEILING)
        assert (maker, taker) == (input_units, int(shares * 1_000_000))
    else:
        quote = (rounded_input * price).quantize(amount_quantum, rounding=ROUND_DOWN)
        assert (maker, taker) == (input_units, int(quote * 1_000_000))


@pytest.mark.parametrize("tick", TICKS)
@pytest.mark.parametrize("side", ("buy", "sell"))
def test_nonconforming_prices_and_zero_after_rounding_fail_for_every_tick_and_side(
    tick: str,
    side: str,
) -> None:
    with pytest.raises(OrderAmountError, match="PRICE_TICK_INVALID"):
        order_amounts(
            side=side,
            price=Decimal(tick) + Decimal(tick) / 10,
            size=Decimal("1"),
            tick_size=Decimal(tick),
            kind="limit",
            rounding=load_protocol_snapshot().rounding,
        )
    with pytest.raises(OrderAmountError, match="ROUNDED_AMOUNT_INVALID"):
        order_amounts(
            side=side,
            price=Decimal(tick),
            size=Decimal("0.009"),
            tick_size=Decimal(tick),
            kind="limit",
            rounding=load_protocol_snapshot().rounding,
        )


@pytest.mark.parametrize(
    ("kwargs", "error"),
    (
        ({"side": "hold"}, "SIDE_UNSUPPORTED"),
        ({"kind": "stop"}, "ORDER_KIND_UNSUPPORTED"),
        ({"price": Decimal("0")}, "PRICE_INVALID"),
        ({"size": Decimal("0")}, "SIZE_INVALID"),
        ({"tick_size": Decimal("0.02")}, "TICK_SIZE_UNSUPPORTED"),
    ),
)
def test_amount_contract_rejects_out_of_scope_or_nonpositive_inputs(
    kwargs: dict[str, object],
    error: str,
) -> None:
    arguments: dict[str, object] = {
        "side": "buy",
        "price": Decimal("0.52"),
        "size": Decimal("10"),
        "tick_size": Decimal("0.01"),
        "kind": "limit",
        "rounding": load_protocol_snapshot().rounding,
    }
    arguments.update(kwargs)

    with pytest.raises(OrderAmountError, match=error):
        order_amounts(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("maker", "0x" + "22" * 20),
        ("signer", "0x" + "22" * 20),
        ("token_id", 124),
        ("maker_amount", 5_200_001),
        ("taker_amount", 10_000_001),
        ("side", 1),
        ("signature_type", 1),
        ("timestamp", 1_787_673_600_001),
        ("metadata", "0x" + "01" * 32),
        ("builder", "0x" + "01" * 32),
        ("expiration", 1),
    ),
)
def test_each_current_order_field_independently_changes_the_order_fingerprint(
    field: str,
    changed: object,
) -> None:
    snapshot = load_protocol_snapshot()
    baseline = _order()

    assert order_fingerprint(
        baseline.model_copy(update={field: changed}),
        snapshot,
    ) != order_fingerprint(baseline, snapshot)


def test_chain_exchange_and_exchange_kind_independently_change_order_fingerprint() -> None:
    snapshot = load_protocol_snapshot()
    baseline = _order()
    changed_domain = snapshot.eip712.order_domain.model_copy(update={"chain_id": 1})
    chain_snapshot = snapshot.model_copy(
        update={"eip712": snapshot.eip712.model_copy(update={"order_domain": changed_domain})}
    )
    changed_addresses = snapshot.eip712.exchange_addresses.model_copy(
        update={"standard": "0x" + "33" * 20}
    )
    address_snapshot = snapshot.model_copy(
        update={
            "eip712": snapshot.eip712.model_copy(update={"exchange_addresses": changed_addresses})
        }
    )

    original = order_fingerprint(baseline, snapshot)
    assert order_fingerprint(baseline, chain_snapshot) != original
    assert order_fingerprint(baseline, address_snapshot) != original
    assert (
        order_fingerprint(
            baseline.model_copy(update={"exchange_kind": "negative_risk"}),
            snapshot,
        )
        != original
    )
