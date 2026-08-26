import asyncio
import base64
import copy
import hashlib
import json
import pickle
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from polytrading.predictions.execution.models import ExecutionIntent, ExecutionOperation
from polytrading.predictions.polymarket_execution.auth import (
    ClobAuthError,
    ClobCredentials,
    sign_l2_request,
)
from polytrading.predictions.polymarket_execution.ipc import (
    ReadAccountPayload,
    ReadOrdersPayload,
    ReadTradesPayload,
    SanitizedOperationResult,
    SignerResponse,
    SubmitOrderPayload,
    canonical_response_bytes,
)
from polytrading.predictions.polymarket_execution.order import sign_order
from polytrading.predictions.polymarket_execution.protocol import load_protocol_snapshot
from polytrading.predictions.polymarket_execution.rest import (
    HttpxPolymarketRestTransport,
    ReadRetryPolicy,
    RestCode,
    RestResult,
    RestrictedGeoblockEvidence,
    RestrictedGeoblockResponse,
    RestTimeouts,
    SignerRestHandlers,
)
from polytrading.predictions.polymarket_execution.routes import (
    ROUTE_SET_HASH,
    ROUTE_SET_VERSION,
    ROUTE_SPECS,
    AllowanceEntry,
    BalanceAllowancePayload,
    CancelOrderRequest,
    GeoblockRequest,
    GeoblockResult,
    HeartbeatRequest,
    MakerOrderReadPayload,
    OrderReadPayload,
    ReadBalanceAllowanceRequest,
    ReadOpenOrdersRequest,
    ReadOrderRequest,
    ReadTradesRequest,
    RouteKey,
    SubmitOrderRequest,
    TradeReadPayload,
    TradesReadPayload,
)
from tests.predictions.execution_helpers import execution_intent_fields

PRIVATE_KEY = (1).to_bytes(32, "big")
NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)
TIMESTAMP = "1787673600"
ACCOUNT_FINGERPRINT = "d6f781065c489e6513f45bc3dab82156055056d393c42f49a4defec22b5ee73f"


def submit_order_request() -> SubmitOrderRequest:
    intent = ExecutionIntent(**execution_intent_fields(account_fingerprint=ACCOUNT_FINGERPRINT))
    envelope = sign_order(intent, PRIVATE_KEY, load_protocol_snapshot())
    return SubmitOrderRequest(
        route=RouteKey.SUBMIT_ORDER,
        intent=intent,
        envelope=envelope,
    )


def clob_credentials() -> ClobCredentials:
    return ClobCredentials(
        address="0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf",
        api_key=b"task8-api-key",
        secret=base64.urlsafe_b64encode(b"task8-hmac-secret"),
        passphrase=b"task8-passphrase",
    )


def test_route_set_is_the_exact_frozen_execution_allowlist() -> None:
    assert tuple(RouteKey) == (
        RouteKey.SUBMIT_ORDER,
        RouteKey.CANCEL_ORDER,
        RouteKey.READ_ORDER,
        RouteKey.READ_OPEN_ORDERS,
        RouteKey.READ_TRADES,
        RouteKey.READ_BALANCE_ALLOWANCE,
        RouteKey.HEARTBEAT,
        RouteKey.GEOBLOCK,
    )
    assert frozenset(ROUTE_SPECS) == frozenset(RouteKey)


def test_route_set_has_no_value_transfer_operations() -> None:
    rendered = " ".join(
        f"{route_key.value} {spec.path}" for route_key, spec in ROUTE_SPECS.items()
    ).casefold()

    for forbidden in (
        "withdraw",
        "deposit",
        "transfer",
        "approve",
        "redeem",
        "relayer",
    ):
        assert forbidden not in rendered


def test_route_specs_match_the_frozen_protocol_and_authority_digest() -> None:
    assert ROUTE_SET_VERSION == "polymarket-mutations-v1"
    assert ROUTE_SET_HASH == "3429c248a6caec950da2ed46643bb8810ff028740f967888c6b77de1fb127bec"
    assert {
        key: (
            spec.host,
            spec.method,
            spec.path_template,
            spec.auth_level,
            spec.mutation,
            spec.query_fields,
            spec.request_fields,
        )
        for key, spec in ROUTE_SPECS.items()
    } == {
        RouteKey.SUBMIT_ORDER: (
            "https://clob.polymarket.com",
            "POST",
            "/order",
            "L2_AND_ORDER_SIGNATURE",
            True,
            (),
            ("deferExec", "order", "orderType", "owner"),
        ),
        RouteKey.CANCEL_ORDER: (
            "https://clob.polymarket.com",
            "DELETE",
            "/order",
            "L2",
            True,
            (),
            ("orderID",),
        ),
        RouteKey.READ_ORDER: (
            "https://clob.polymarket.com",
            "GET",
            "/data/order/{order_id}",
            "L2",
            False,
            (),
            (),
        ),
        RouteKey.READ_OPEN_ORDERS: (
            "https://clob.polymarket.com",
            "GET",
            "/data/orders",
            "L2",
            False,
            ("id", "market", "asset_id"),
            (),
        ),
        RouteKey.READ_TRADES: (
            "https://clob.polymarket.com",
            "GET",
            "/data/trades",
            "L2",
            False,
            ("id", "market", "asset_id", "maker_address", "after", "before"),
            (),
        ),
        RouteKey.READ_BALANCE_ALLOWANCE: (
            "https://clob.polymarket.com",
            "GET",
            "/balance-allowance",
            "L2",
            False,
            ("signature_type", "asset_type", "token_id"),
            (),
        ),
        RouteKey.HEARTBEAT: (
            "https://clob.polymarket.com",
            "POST",
            "/v1/heartbeats",
            "L2",
            True,
            (),
            ("heartbeat_id",),
        ),
        RouteKey.GEOBLOCK: (
            "https://polymarket.com",
            "GET",
            "/api/geoblock",
            "PUBLIC",
            False,
            (),
            (),
        ),
    }


def test_route_specs_mapping_is_immutable() -> None:
    with pytest.raises(TypeError):
        ROUTE_SPECS[RouteKey.GEOBLOCK] = ROUTE_SPECS[RouteKey.SUBMIT_ORDER]  # type: ignore[index]


def test_route_request_models_expose_only_their_frozen_fields() -> None:
    assert CancelOrderRequest(
        route=RouteKey.CANCEL_ORDER,
        order_id="venue-order-1",
    ).model_dump(mode="json") == {
        "route": "CANCEL_ORDER",
        "order_id": "venue-order-1",
    }
    assert ReadOpenOrdersRequest(
        route=RouteKey.READ_OPEN_ORDERS,
        id="venue-order-1",
        market="condition-1",
        asset_id="217426",
    ).model_dump(mode="json", exclude_none=True) == {
        "route": "READ_OPEN_ORDERS",
        "id": "venue-order-1",
        "market": "condition-1",
        "asset_id": "217426",
    }
    assert ReadTradesRequest(
        route=RouteKey.READ_TRADES,
        id="trade-1",
        market="condition-1",
        asset_id="217426",
        maker_address="0x" + "11" * 20,
        after=1787673600,
        before=1787677200,
    ).model_dump(mode="json", exclude_none=True) == {
        "route": "READ_TRADES",
        "id": "trade-1",
        "market": "condition-1",
        "asset_id": "217426",
        "maker_address": "0x" + "11" * 20,
        "after": 1787673600,
        "before": 1787677200,
    }
    assert (
        HeartbeatRequest(
            route=RouteKey.HEARTBEAT,
            heartbeat_id="",
        ).heartbeat_id
        == ""
    )
    assert GeoblockRequest(route=RouteKey.GEOBLOCK).model_dump(mode="json") == {"route": "GEOBLOCK"}


def test_read_order_rejects_path_separator_and_control_ambiguity() -> None:
    assert ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="order %?#").order_id == (
        "order %?#"
    )
    for invalid in ("order/child", "order\\child", "line\nbreak", ""):
        with pytest.raises(ValidationError):
            ReadOrderRequest(route=RouteKey.READ_ORDER, order_id=invalid)


def test_balance_allowance_requires_the_exact_eoa_asset_selector() -> None:
    collateral = ReadBalanceAllowanceRequest(
        route=RouteKey.READ_BALANCE_ALLOWANCE,
        signature_type=0,
        asset_type="COLLATERAL",
        token_id=None,
    )
    conditional = ReadBalanceAllowanceRequest(
        route=RouteKey.READ_BALANCE_ALLOWANCE,
        signature_type=0,
        asset_type="CONDITIONAL",
        token_id="217426",
    )
    assert collateral.token_id is None
    assert conditional.token_id == "217426"

    for values in (
        {"signature_type": 1, "asset_type": "COLLATERAL", "token_id": None},
        {"signature_type": 0, "asset_type": "CONDITIONAL", "token_id": None},
        {"signature_type": 0, "asset_type": "COLLATERAL", "token_id": "217426"},
        {"signature_type": True, "asset_type": "COLLATERAL", "token_id": None},
        {"signature_type": 0, "asset_type": "UNKNOWN", "token_id": None},
    ):
        with pytest.raises(ValidationError):
            ReadBalanceAllowanceRequest(
                route=RouteKey.READ_BALANCE_ALLOWANCE,
                **values,
            )


def test_route_request_models_reject_unknown_fields_and_type_coercion() -> None:
    with pytest.raises(ValidationError):
        GeoblockRequest(route=RouteKey.GEOBLOCK, url="https://attacker.invalid")
    with pytest.raises(ValidationError):
        ReadTradesRequest(route=RouteKey.READ_TRADES, after="1787673600")
    with pytest.raises(ValidationError):
        CancelOrderRequest(route=RouteKey.CANCEL_ORDER, order_id=1)
    geoblock = GeoblockRequest(route=RouteKey.GEOBLOCK)
    with pytest.raises(ValidationError):
        geoblock.model_copy(update={"route": RouteKey.SUBMIT_ORDER})
    with pytest.raises(ValueError, match="ROUTE_MODEL_CONSTRUCTION_INVALID"):
        GeoblockRequest.model_construct(route=RouteKey.SUBMIT_ORDER)


@pytest.mark.parametrize(
    "values",
    (
        {
            "route": RouteKey.SUBMIT_ORDER,
            "code": RestCode.ORDER_OUTCOME_UNKNOWN,
            "request_body_hash": "a" * 64,
            "attempts": 2,
            "recovery_required": True,
            "kill_required": True,
        },
        {
            "route": RouteKey.CANCEL_ORDER,
            "code": RestCode.CANCEL_OUTCOME_UNKNOWN,
            "request_body_hash": None,
            "attempts": 1,
            "recovery_required": True,
            "kill_required": True,
        },
        {
            "route": RouteKey.HEARTBEAT,
            "code": RestCode.AUTH_REQUEST_BUILD_FAILED,
            "request_body_hash": "a" * 64,
            "attempts": 0,
            "recovery_required": True,
            "kill_required": True,
        },
        {
            "route": RouteKey.READ_ORDER,
            "code": RestCode.READ_NOT_FOUND,
            "request_body_hash": "a" * 64,
            "attempts": 1,
            "recovery_required": False,
            "kill_required": False,
        },
    ),
    ids=("mutation-retry", "mutation-missing-hash", "build-extra-hash", "get-extra-hash"),
)
def test_rest_result_rejects_impossible_route_evidence(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RestResult(
            observed_at=NOW,
            raw_body_hash=None,
            payload=None,
            **values,
        )


def test_rest_result_rejects_non_order_read_not_found() -> None:
    with pytest.raises(ValidationError):
        RestResult(
            route=RouteKey.READ_OPEN_ORDERS,
            code=RestCode.READ_NOT_FOUND,
            observed_at=NOW,
            raw_body_hash="b" * 64,
            request_body_hash=None,
            attempts=1,
            recovery_required=False,
            kill_required=False,
            payload=None,
        )


def test_submit_request_binds_the_exact_task5_intent_and_envelope() -> None:
    request = submit_order_request()

    assert request.envelope.intent_id == request.intent.intent_id
    assert request.envelope.intent_fingerprint == request.intent.intent_fingerprint
    assert request.envelope.protocol_version == request.intent.protocol_version

    relabelled = request.envelope.model_copy(update={"intent_fingerprint": "f" * 64})
    with pytest.raises(ValidationError):
        SubmitOrderRequest(
            route=RouteKey.SUBMIT_ORDER,
            intent=request.intent,
            envelope=relabelled,
        )


def order_read_payload() -> OrderReadPayload:
    return OrderReadPayload(
        kind="ORDER_READ",
        id="order-1",
        market="condition-1",
        asset_id="217426",
        maker_address="0x" + "11" * 20,
        side="BUY",
        price="0.52",
        original_size="10",
        size_matched="4.5",
        outcome="Yes",
        order_type="FAK",
        status="MATCHED",
        associate_trades=None,
        created_at="1787673600",
        expiration="0",
    )


def maker_order_read_payload() -> MakerOrderReadPayload:
    return MakerOrderReadPayload(
        order_id="maker-order-1",
        maker_address="0x" + "22" * 20,
        matched_amount="4.5",
        price="0.52",
        fee_rate_bps="0",
        asset_id="217426",
        outcome="Yes",
        outcome_index=0,
        side="SELL",
    )


def trade_read_payload() -> TradeReadPayload:
    return TradeReadPayload(
        kind="TRADE_READ",
        id="trade-1",
        market="condition-1",
        asset_id="217426",
        maker_address="0x" + "11" * 20,
        taker_order_id="order-1",
        side="BUY",
        trader_side="TAKER",
        price="0.52",
        size="4.5",
        outcome="Yes",
        status="MATCHED",
        fee_rate_bps="0",
        bucket_index=0,
        transaction_hash=None,
        maker_orders=(maker_order_read_payload(),),
        match_time="1787673600",
        last_update="1787673601",
    )


def test_public_read_payloads_are_strict_and_omit_secret_owner_fields() -> None:
    order = order_read_payload()
    trade = trade_read_payload()

    assert "owner" not in order.model_dump(mode="json")
    assert "owner" not in trade.model_dump(mode="json")
    assert "owner" not in trade.maker_orders[0].model_dump(mode="json")
    assert order.associate_trades is None
    assert trade.transaction_hash is None

    with pytest.raises(ValidationError):
        OrderReadPayload(**order.model_dump(mode="python"), owner="private-api-key")
    with pytest.raises(ValidationError):
        TradeReadPayload(**trade.model_dump(mode="python"), owner="private-api-key")


def test_balance_allowance_payload_accepts_only_integer_strings_by_evm_address() -> None:
    payload = BalanceAllowancePayload(
        kind="BALANCE_ALLOWANCE",
        balance="1000000",
        allowances=(AllowanceEntry(address="0x" + "11" * 20, amount="500000"),),
    )
    assert payload.balance == "1000000"

    for allowance in (
        {"address": "not-an-address", "amount": "500000"},
        {"address": "0x" + "11" * 20, "amount": "0.5"},
        {"address": "0x" + "11" * 20, "amount": 500000},
    ):
        with pytest.raises(ValidationError):
            BalanceAllowancePayload(
                kind="BALANCE_ALLOWANCE",
                balance="1000000",
                allowances=(allowance,),
            )


def test_geoblock_public_result_has_no_raw_ip_surface() -> None:
    result = GeoblockResult(
        kind="GEOBLOCK",
        blocked=False,
        country="US",
        region=None,
    )
    assert result.model_dump(mode="json") == {
        "kind": "GEOBLOCK",
        "blocked": False,
        "country": "US",
        "region": None,
    }
    with pytest.raises(ValidationError):
        GeoblockResult(
            kind="GEOBLOCK",
            blocked=False,
            country="US",
            region=None,
            ip="192.0.2.1",
        )


def test_read_arrays_are_bounded_to_ten_thousand_items() -> None:
    with pytest.raises(ValidationError):
        TradesReadPayload(
            kind="TRADES_READ",
            items=(trade_read_payload(),) * 10_001,
        )
    with pytest.raises(ValidationError):
        BalanceAllowancePayload(
            kind="BALANCE_ALLOWANCE",
            balance="1",
            allowances=(AllowanceEntry(address="0x" + "11" * 20, amount="1"),) * 10_001,
        )


def test_balance_and_cancel_wire_mappings_are_bounded_to_ten_thousand_items() -> None:
    balance_result, _ = execute_with_response(
        ReadBalanceAllowanceRequest(
            route=RouteKey.READ_BALANCE_ALLOWANCE,
            signature_type=0,
            asset_type="COLLATERAL",
            token_id=None,
        ),
        {
            "balance": "1",
            "allowances": {f"0x{index:040x}": "1" for index in range(10_001)},
        },
        credentials=clob_credentials(),
    )
    cancel_result, _ = execute_with_response(
        CancelOrderRequest(route=RouteKey.CANCEL_ORDER, order_id="venue-order-1"),
        {
            "canceled": [],
            "not_canceled": {f"order-{index}": "not found" for index in range(10_001)},
        },
        credentials=clob_credentials(),
    )

    assert balance_result.code is RestCode.PROTOCOL_RESPONSE_INVALID
    assert balance_result.payload is None
    assert cancel_result.code is RestCode.CANCEL_OUTCOME_UNKNOWN
    assert cancel_result.payload is None


def test_balance_allowances_are_deeply_immutable_and_deterministic() -> None:
    low_address = "0x" + "11" * 20
    high_address = "0x" + "22" * 20
    result, _ = execute_with_response(
        ReadBalanceAllowanceRequest(
            route=RouteKey.READ_BALANCE_ALLOWANCE,
            signature_type=0,
            asset_type="COLLATERAL",
            token_id=None,
        ),
        {
            "balance": "1000000",
            "allowances": {high_address: "2", low_address: "1"},
        },
        credentials=clob_credentials(),
    )

    assert result.code is RestCode.READ_OK
    with pytest.raises(TypeError):
        result.payload.allowances[0] = "not-an-integer"
    assert result.payload.model_dump(mode="json")["allowances"] == [
        {"address": low_address, "amount": "1"},
        {"address": high_address, "amount": "2"},
    ]


def test_submit_signs_and_sends_the_exact_canonical_outer_body_once() -> None:
    captured: list[httpx.Request] = []
    response_body = json.dumps(
        {
            "success": True,
            "errorMsg": "",
            "orderID": "venue-order-1",
            "status": "matched",
            "makingAmount": "5.1",
            "takingAmount": "10",
            "transactionsHashes": ["0x" + "22" * 32],
            "tradeIDs": ["trade-1"],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            content=response_body,
            headers={"content-type": "application/json"},
        )

    timestamp_calls = 0

    def timestamp() -> str:
        nonlocal timestamp_calls
        timestamp_calls += 1
        return TIMESTAMP

    request = submit_order_request()
    credentials = clob_credentials()
    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=timestamp,
        clock=lambda: NOW,
    )

    async def exercise():
        try:
            return await transport.execute(request, credentials=credentials)
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())
    public_order = json.loads(request.envelope.canonical_order_json)
    expected_body = json.dumps(
        {
            "deferExec": False,
            "order": {**public_order, "signature": request.envelope.public_signature},
            "orderType": request.intent.order_type.value,
            "owner": credentials.api_key.decode("ascii"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    expected_auth = sign_l2_request(
        credentials,
        timestamp=TIMESTAMP,
        method="POST",
        route="/order",
        body=expected_body,
    )

    assert len(captured) == 1
    assert timestamp_calls == 1
    assert captured[0].method == "POST"
    assert str(captured[0].url) == "https://clob.polymarket.com/order"
    assert captured[0].content == expected_body
    assert {
        name: captured[0].headers[name]
        for name in (
            "POLY_ADDRESS",
            "POLY_SIGNATURE",
            "POLY_TIMESTAMP",
            "POLY_API_KEY",
            "POLY_PASSPHRASE",
        )
    } == dict(expected_auth)
    assert result.route is RouteKey.SUBMIT_ORDER
    assert result.code is RestCode.ORDER_ACK_MATCHED
    assert result.observed_at == NOW
    assert result.raw_body_hash == hashlib.sha256(response_body).hexdigest()
    assert result.request_body_hash == hashlib.sha256(expected_body).hexdigest()
    assert result.attempts == 1
    assert result.recovery_required is False
    assert result.kill_required is False
    assert result.payload is not None
    assert result.payload.order_id == "venue-order-1"
    with pytest.raises(ValidationError):
        result.model_copy(update={"code": RestCode.ORDER_OUTCOME_UNKNOWN})


def test_submit_timeout_is_unknown_and_called_once() -> None:
    calls = 0

    async def lose_response(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("secret venue exception", request=request)

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(lose_response),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
    )

    async def exercise():
        try:
            return await transport.execute(
                submit_order_request(),
                credentials=clob_credentials(),
            )
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())

    assert result.code is RestCode.ORDER_OUTCOME_UNKNOWN
    assert result.attempts == 1
    assert result.recovery_required is True
    assert result.kill_required is True
    assert calls == 1
    assert "secret venue exception" not in repr(result)


def wire_order(*, order_id: str = "venue-order-1") -> dict[str, object]:
    return {
        "id": order_id,
        "market": "condition-1",
        "asset_id": "217426",
        "owner": "task8-api-key",
        "maker_address": "0x" + "11" * 20,
        "side": "BUY",
        "price": "0.52",
        "original_size": "10",
        "size_matched": "4.5",
        "outcome": "Yes",
        "order_type": "FAK",
        "status": "MATCHED",
        "associate_trades": None,
        "created_at": "1787673600",
        "expiration": "0",
    }


def wire_trade() -> dict[str, object]:
    return {
        "id": "trade-1",
        "market": "condition-1",
        "asset_id": "217426",
        "owner": "task8-api-key",
        "maker_address": "0x" + "11" * 20,
        "taker_order_id": "venue-order-1",
        "side": "BUY",
        "trader_side": "TAKER",
        "price": "0.52",
        "size": "4.5",
        "outcome": "Yes",
        "status": "MATCHED",
        "fee_rate_bps": "0",
        "bucket_index": 0,
        "transaction_hash": None,
        "maker_orders": [
            {
                "order_id": "maker-order-1",
                "owner": "maker-api-key",
                "maker_address": "0x" + "22" * 20,
                "matched_amount": "4.5",
                "price": "0.52",
                "fee_rate_bps": "0",
                "asset_id": "217426",
                "outcome": "Yes",
                "outcome_index": 0,
                "side": "SELL",
            }
        ],
        "match_time": "1787673600",
        "last_update": "1787673601",
    }


def execute_with_response(
    request,
    response_body: object,
    *,
    status_code: int = 200,
    credentials: ClobCredentials | None = None,
) -> tuple[object, httpx.Request]:
    captured: list[httpx.Request] = []
    body = json.dumps(response_body, separators=(",", ":"), sort_keys=True).encode()

    async def handler(http_request: httpx.Request) -> httpx.Response:
        captured.append(http_request)
        return httpx.Response(
            status_code,
            content=body,
            headers={"content-type": "application/json"},
        )

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
    )

    async def exercise():
        try:
            return await transport.execute(request, credentials=credentials)
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())
    assert len(captured) == 1
    return result, captured[0]


def test_cancel_uses_compact_body_but_requires_authoritative_confirmation() -> None:
    result, request = execute_with_response(
        CancelOrderRequest(route=RouteKey.CANCEL_ORDER, order_id="venue-order-1"),
        {"canceled": ["venue-order-1"], "not_canceled": {}},
        credentials=clob_credentials(),
    )

    assert request.method == "DELETE"
    assert str(request.url) == "https://clob.polymarket.com/order"
    assert request.content == b'{"orderID":"venue-order-1"}'
    assert result.code is RestCode.CANCEL_ACKNOWLEDGED
    assert result.payload.order_id == "venue-order-1"
    assert result.payload.confirmation_required is True
    assert result.recovery_required is True
    assert result.kill_required is False


def test_heartbeat_uses_the_frozen_route_and_returns_only_the_next_id() -> None:
    result, request = execute_with_response(
        HeartbeatRequest(route=RouteKey.HEARTBEAT, heartbeat_id=""),
        {"heartbeat_id": "heartbeat-1"},
        credentials=clob_credentials(),
    )

    assert request.method == "POST"
    assert str(request.url) == "https://clob.polymarket.com/v1/heartbeats"
    assert request.content == b'{"heartbeat_id":""}'
    assert result.code is RestCode.HEARTBEAT_ACCEPTED
    assert result.payload.heartbeat_id == "heartbeat-1"


def test_read_order_percent_encodes_one_segment_and_omits_owner() -> None:
    result, request = execute_with_response(
        ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="order %?#"),
        wire_order(order_id="order %?#"),
        credentials=clob_credentials(),
    )

    assert request.method == "GET"
    assert str(request.url) == ("https://clob.polymarket.com/data/order/order%20%25%3F%23")
    assert request.content == b""
    assert result.code is RestCode.READ_OK
    assert result.payload.id == "order %?#"
    assert "owner" not in result.payload.model_dump(mode="json")


def test_open_orders_and_trades_render_only_frozen_queries_in_frozen_order() -> None:
    orders, order_request = execute_with_response(
        ReadOpenOrdersRequest(
            route=RouteKey.READ_OPEN_ORDERS,
            id="venue-order-1",
            market="condition 1",
            asset_id="217426",
        ),
        [wire_order()],
        credentials=clob_credentials(),
    )
    trades, trade_request = execute_with_response(
        ReadTradesRequest(
            route=RouteKey.READ_TRADES,
            id="trade-1",
            market="condition 1",
            asset_id="217426",
            maker_address="0x" + "11" * 20,
            after=1787673600,
            before=1787677200,
        ),
        [wire_trade()],
        credentials=clob_credentials(),
    )

    assert str(order_request.url) == (
        "https://clob.polymarket.com/data/orders?"
        "id=venue-order-1&market=condition+1&asset_id=217426"
    )
    assert str(trade_request.url) == (
        "https://clob.polymarket.com/data/trades?"
        "id=trade-1&market=condition+1&asset_id=217426&"
        "maker_address=0x1111111111111111111111111111111111111111&"
        "after=1787673600&before=1787677200"
    )
    assert orders.code is RestCode.READ_OK
    assert orders.payload.items[0].id == "venue-order-1"
    assert trades.code is RestCode.READ_OK
    assert trades.payload.items[0].id == "trade-1"
    assert "owner" not in trades.payload.items[0].model_dump(mode="json")
    assert "owner" not in trades.payload.items[0].maker_orders[0].model_dump(mode="json")


def test_balance_allowance_uses_only_the_eoa_conditional_selector() -> None:
    result, request = execute_with_response(
        ReadBalanceAllowanceRequest(
            route=RouteKey.READ_BALANCE_ALLOWANCE,
            signature_type=0,
            asset_type="CONDITIONAL",
            token_id="217426",
        ),
        {"balance": "1000000", "allowances": {"0x" + "22" * 20: "500000"}},
        credentials=clob_credentials(),
    )

    assert str(request.url) == (
        "https://clob.polymarket.com/balance-allowance?"
        "signature_type=0&asset_type=CONDITIONAL&token_id=217426"
    )
    assert result.code is RestCode.READ_OK
    assert result.payload.balance == "1000000"


def test_geoblock_uses_the_public_host_and_never_returns_the_raw_ip() -> None:
    response = {
        "blocked": False,
        "ip": "192.0.2.1",
        "country": "US",
        "region": "NY",
    }
    result, request = execute_with_response(
        GeoblockRequest(route=RouteKey.GEOBLOCK),
        response,
    )
    exact_body = json.dumps(response, separators=(",", ":"), sort_keys=True).encode()

    assert str(request.url) == "https://polymarket.com/api/geoblock"
    assert "POLY_SIGNATURE" not in request.headers
    assert result.code is RestCode.GEOBLOCK_OK
    assert result.raw_body_hash == hashlib.sha256(exact_body).hexdigest()
    assert result.payload.model_dump(mode="json") == {
        "kind": "GEOBLOCK",
        "blocked": False,
        "country": "US",
        "region": "NY",
    }
    assert "192.0.2.1" not in repr(result)


def test_restricted_geoblock_evidence_seals_raw_ip_and_bytes_from_generic_surfaces() -> None:
    evidence = RestrictedGeoblockEvidence(
        raw_ip="192.0.2.1",
        exact_bytes=b'{"ip":"192.0.2.1"}',
    )

    assert repr(evidence) == "RestrictedGeoblockEvidence(<restricted>)"
    assert str(evidence) == "RestrictedGeoblockEvidence(<restricted>)"
    assert not hasattr(evidence, "__dict__")
    with pytest.raises(AttributeError, match="GEOBLOCK_EVIDENCE_IMMUTABLE"):
        evidence._raw_ip = "198.51.100.2"
    for method in (
        lambda: evidence.__getstate__(),
        lambda: evidence.__reduce__(),
        lambda: copy.copy(evidence),
        lambda: copy.deepcopy(evidence),
        lambda: pickle.dumps(evidence),
        lambda: hash(evidence),
        lambda: evidence == evidence,
    ):
        with pytest.raises(ValueError, match="GEOBLOCK_EVIDENCE_RESTRICTED"):
            method()


def test_restricted_geoblock_execution_retains_same_response_evidence_once() -> None:
    calls = 0
    exact_body = b'{"blocked":false,"country":"US","ip":"192.0.2.1","region":"NY"}'

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=exact_body,
            headers={"content-type": "application/json"},
        )

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
    )

    async def exercise():
        try:
            return await transport.execute_geoblock_restricted(
                GeoblockRequest(route=RouteKey.GEOBLOCK)
            )
        finally:
            await transport.aclose()

    restricted = asyncio.run(exercise())

    assert type(restricted) is RestrictedGeoblockResponse
    assert calls == 1
    assert restricted.result.code is RestCode.GEOBLOCK_OK
    assert restricted.result.raw_body_hash == hashlib.sha256(exact_body).hexdigest()
    assert restricted.evidence is not None
    assert restricted.evidence.raw_evidence_hash == restricted.result.raw_body_hash
    assert "192.0.2.1" not in restricted.result.model_dump_json()
    assert repr(restricted) == "RestrictedGeoblockResponse(<restricted>)"
    assert str(restricted) == "RestrictedGeoblockResponse(<restricted>)"
    assert not hasattr(restricted, "__dict__")
    with pytest.raises(
        AttributeError,
        match="GEOBLOCK_RESTRICTED_RESPONSE_IMMUTABLE",
    ):
        restricted._evidence = None
    for method in (
        lambda: restricted.__getstate__(),
        lambda: restricted.__reduce__(),
        lambda: copy.copy(restricted),
        lambda: copy.deepcopy(restricted),
        lambda: pickle.dumps(restricted),
        lambda: hash(restricted),
        lambda: restricted == restricted,
    ):
        with pytest.raises(
            ValueError,
            match="GEOBLOCK_RESTRICTED_RESPONSE_RESTRICTED",
        ):
            method()
    with pytest.raises(TypeError):
        json.dumps(restricted)


@pytest.mark.parametrize(
    ("kind", "slot", "error_code"),
    (
        ("evidence", "_raw_ip", "GEOBLOCK_EVIDENCE_IMMUTABLE"),
        ("evidence", "_exact_bytes", "GEOBLOCK_EVIDENCE_IMMUTABLE"),
        ("evidence", "_sealed", "GEOBLOCK_EVIDENCE_IMMUTABLE"),
        ("response", "_result", "GEOBLOCK_RESTRICTED_RESPONSE_IMMUTABLE"),
        ("response", "_evidence", "GEOBLOCK_RESTRICTED_RESPONSE_IMMUTABLE"),
        ("response", "_sealed", "GEOBLOCK_RESTRICTED_RESPONSE_IMMUTABLE"),
    ),
)
def test_restricted_geoblock_slots_cannot_be_deleted(
    kind: str,
    slot: str,
    error_code: str,
) -> None:
    exact_body = b'{"blocked":false,"country":"US","ip":"192.0.2.1","region":null}'
    evidence = RestrictedGeoblockEvidence(raw_ip="192.0.2.1", exact_bytes=exact_body)
    result = RestResult(
        route=RouteKey.GEOBLOCK,
        code=RestCode.GEOBLOCK_OK,
        observed_at=NOW,
        raw_body_hash=hashlib.sha256(exact_body).hexdigest(),
        request_body_hash=None,
        attempts=1,
        recovery_required=False,
        kill_required=False,
        payload=GeoblockResult(
            kind="GEOBLOCK",
            blocked=False,
            country="US",
            region=None,
        ),
    )
    restricted = RestrictedGeoblockResponse(result=result, evidence=evidence)
    target = evidence if kind == "evidence" else restricted

    with pytest.raises(AttributeError, match=error_code):
        delattr(target, slot)


def test_restricted_geoblock_failure_has_no_restricted_evidence() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            content=b'{"private":"failure"}',
            headers={"content-type": "application/json"},
        )

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
    )

    async def exercise():
        try:
            return await transport.execute_geoblock_restricted(
                GeoblockRequest(route=RouteKey.GEOBLOCK)
            )
        finally:
            await transport.aclose()

    restricted = asyncio.run(exercise())

    assert calls == 1
    assert restricted.result.code is RestCode.GEOBLOCK_FAILED
    assert restricted.result.kill_required is True
    assert restricted.evidence is None
    assert "failure" not in repr(restricted)


def test_public_geoblock_auth_status_is_closed_without_evidence() -> None:
    result, _ = execute_with_response(
        GeoblockRequest(route=RouteKey.GEOBLOCK),
        {"private": "failure"},
        status_code=401,
    )

    assert result.code is RestCode.AUTH_REJECTED
    assert result.kill_required is True
    assert result.payload is None


@pytest.mark.parametrize(
    ("status", "expected", "recovery", "kill"),
    (
        ("matched", RestCode.ORDER_ACK_MATCHED, False, False),
        ("delayed", RestCode.ORDER_ACK_DELAYED, True, True),
        ("live", RestCode.ORDER_ACK_LIVE_UNEXPECTED, True, True),
        ("unmatched", RestCode.ORDER_ACK_UNMATCHED, True, True),
    ),
)
def test_submit_ack_states_have_distinct_closed_outcomes(
    status: str,
    expected: RestCode,
    recovery: bool,
    kill: bool,
) -> None:
    result, _ = execute_with_response(
        submit_order_request(),
        {
            "success": True,
            "errorMsg": "",
            "orderID": "venue-order-1",
            "status": status,
            "makingAmount": "5.1",
            "takingAmount": "10",
            "transactionsHashes": [],
            "tradeIDs": [],
        },
        credentials=clob_credentials(),
    )

    assert result.code is expected
    assert result.recovery_required is recovery
    assert result.kill_required is kill


@pytest.mark.parametrize(
    "response_body",
    (
        {
            "success": False,
            "errorMsg": "venue text",
            "orderID": "venue-order-1",
            "status": "matched",
            "makingAmount": "5.1",
            "takingAmount": "10",
            "transactionsHashes": [],
            "tradeIDs": [],
        },
        {
            "success": True,
            "errorMsg": "",
            "orderID": "venue-order-1",
            "status": "unsupported",
            "makingAmount": "5.1",
            "takingAmount": "10",
            "transactionsHashes": [],
            "tradeIDs": [],
        },
        {"success": True},
    ),
)
def test_ambiguous_submit_bodies_are_unknown(response_body: object) -> None:
    result, _ = execute_with_response(
        submit_order_request(),
        response_body,
        credentials=clob_credentials(),
    )

    assert result.code is RestCode.ORDER_OUTCOME_UNKNOWN
    assert result.payload is None


@pytest.mark.parametrize(
    ("response_body", "expected"),
    (
        (
            {"canceled": [], "not_canceled": {"venue-order-1": "not found"}},
            RestCode.CANCEL_NOT_CONFIRMED,
        ),
        (
            {"canceled": [], "not_canceled": {}},
            RestCode.CANCEL_NOT_CONFIRMED,
        ),
        (
            {
                "canceled": ["venue-order-1"],
                "not_canceled": {"venue-order-1": "contradiction"},
            },
            RestCode.CANCEL_NOT_CONFIRMED,
        ),
        ({"canceled": "venue-order-1", "not_canceled": {}}, RestCode.CANCEL_OUTCOME_UNKNOWN),
    ),
)
def test_cancel_requires_one_uncontradicted_target(
    response_body: object,
    expected: RestCode,
) -> None:
    result, _ = execute_with_response(
        CancelOrderRequest(route=RouteKey.CANCEL_ORDER, order_id="venue-order-1"),
        response_body,
        credentials=clob_credentials(),
    )

    assert result.code is expected


def test_heartbeat_400_exposes_only_the_expected_next_id() -> None:
    result, _ = execute_with_response(
        HeartbeatRequest(route=RouteKey.HEARTBEAT, heartbeat_id="stale-id"),
        {"error_msg": "invalid heartbeat id", "heartbeat_id": "expected-id"},
        status_code=400,
        credentials=clob_credentials(),
    )

    assert result.code is RestCode.HEARTBEAT_ID_MISMATCH
    assert result.payload.heartbeat_id == "expected-id"
    assert "invalid heartbeat id" not in repr(result)


def test_read_retry_reuses_the_identical_signed_request_without_resigning() -> None:
    captured: list[tuple[str, bytes, dict[str, str]]] = []
    sleeps: list[float] = []
    timestamp_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append((str(request.url), request.content, dict(request.headers)))
        if len(captured) == 1:
            raise httpx.ConnectTimeout("secret transport detail", request=request)
        return httpx.Response(
            200,
            content=json.dumps(wire_order(), separators=(",", ":"), sort_keys=True).encode(),
            headers={"content-type": "application/json"},
        )

    def timestamp() -> str:
        nonlocal timestamp_calls
        timestamp_calls += 1
        return TIMESTAMP

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=timestamp,
        clock=lambda: NOW,
        retry_policy=ReadRetryPolicy(max_attempts=2, delay_seconds=0.25),
        sleeper=sleeper,
    )

    async def exercise():
        try:
            return await transport.execute(
                ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="venue-order-1"),
                credentials=clob_credentials(),
            )
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())

    assert result.code is RestCode.READ_OK
    assert result.attempts == 2
    assert captured[0] == captured[1]
    assert timestamp_calls == 1
    assert sleeps == [0.25]


@pytest.mark.parametrize("route_kind", ("submit", "cancel", "heartbeat"))
def test_mutations_never_retry_even_when_a_read_retry_policy_is_enabled(
    route_kind: str,
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("secret failure", request=request)

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    requests = {
        "submit": submit_order_request(),
        "cancel": CancelOrderRequest(
            route=RouteKey.CANCEL_ORDER,
            order_id="venue-order-1",
        ),
        "heartbeat": HeartbeatRequest(route=RouteKey.HEARTBEAT, heartbeat_id=""),
    }
    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
        retry_policy=ReadRetryPolicy(max_attempts=2, delay_seconds=0.25),
        sleeper=sleeper,
    )

    async def exercise():
        try:
            return await transport.execute(
                requests[route_kind],
                credentials=clob_credentials(),
            )
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())

    expected = {
        "submit": RestCode.ORDER_OUTCOME_UNKNOWN,
        "cancel": RestCode.CANCEL_OUTCOME_UNKNOWN,
        "heartbeat": RestCode.HEARTBEAT_OUTCOME_UNKNOWN,
    }
    assert result.code is expected[route_kind]
    assert calls == 1
    assert sleeps == []


def test_retryable_read_status_retries_once_and_ignores_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "cookie" not in request.headers
        if calls == 1:
            return httpx.Response(
                503,
                content=b'{"private":"body"}',
                headers={
                    "content-type": "application/json",
                    "retry-after": "999",
                    "set-cookie": "session=private",
                },
            )
        return httpx.Response(
            200,
            content=b'{"balance":"10","allowances":{}}',
            headers={"content-type": "application/json"},
        )

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
        retry_policy=ReadRetryPolicy(max_attempts=2, delay_seconds=0.4),
        sleeper=sleeper,
    )

    async def exercise():
        try:
            return await transport.execute(
                ReadBalanceAllowanceRequest(
                    route=RouteKey.READ_BALANCE_ALLOWANCE,
                    signature_type=0,
                    asset_type="COLLATERAL",
                    token_id=None,
                ),
                credentials=clob_credentials(),
            )
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())

    assert result.code is RestCode.READ_OK
    assert result.attempts == 2
    assert calls == 2
    assert sleeps == [0.4]


@pytest.mark.parametrize(
    ("status_code", "expected"),
    (
        (401, RestCode.AUTH_REJECTED),
        (403, RestCode.AUTH_REJECTED),
        (404, RestCode.READ_NOT_FOUND),
        (429, RestCode.RATE_LIMITED),
        (500, RestCode.READ_FAILED),
    ),
)
def test_read_http_failures_have_stable_sanitized_codes(
    status_code: int,
    expected: RestCode,
) -> None:
    result, _ = execute_with_response(
        ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="venue-order-1"),
        {"secret": "venue detail"},
        status_code=status_code,
        credentials=clob_credentials(),
    )

    assert result.code is expected
    assert "venue detail" not in repr(result)
    if status_code == 404:
        assert (result.recovery_required, result.kill_required) == (False, False)


@pytest.mark.parametrize(
    "route_request",
    (
        ReadOpenOrdersRequest(route=RouteKey.READ_OPEN_ORDERS),
        ReadTradesRequest(route=RouteKey.READ_TRADES),
        ReadBalanceAllowanceRequest(
            route=RouteKey.READ_BALANCE_ALLOWANCE,
            signature_type=0,
            asset_type="COLLATERAL",
            token_id=None,
        ),
    ),
    ids=("open-orders", "trades", "balance-allowance"),
)
def test_only_single_order_404_is_a_nonhalting_not_found(route_request: object) -> None:
    result, _ = execute_with_response(
        route_request,
        {"private": "venue detail"},
        status_code=404,
        credentials=clob_credentials(),
    )

    assert result.code is RestCode.READ_FAILED
    assert (result.recovery_required, result.kill_required) == (True, True)


def test_exhausted_authenticated_safety_read_failures_require_recovery_and_kill() -> None:
    rate_limited, _ = execute_with_response(
        ReadOpenOrdersRequest(route=RouteKey.READ_OPEN_ORDERS),
        {"private": "venue detail"},
        status_code=429,
        credentials=clob_credentials(),
    )
    malformed, _ = execute_with_response(
        ReadTradesRequest(route=RouteKey.READ_TRADES),
        {"unexpected": "venue detail"},
        credentials=clob_credentials(),
    )

    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private transport detail", request=request)

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(unavailable),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
    )

    async def exercise():
        try:
            return await transport.execute(
                ReadBalanceAllowanceRequest(
                    route=RouteKey.READ_BALANCE_ALLOWANCE,
                    signature_type=0,
                    asset_type="COLLATERAL",
                    token_id=None,
                ),
                credentials=clob_credentials(),
            )
        finally:
            await transport.aclose()

    transport_failed = asyncio.run(exercise())

    assert (
        rate_limited.code,
        malformed.code,
        transport_failed.code,
    ) == (
        RestCode.RATE_LIMITED,
        RestCode.PROTOCOL_RESPONSE_INVALID,
        RestCode.TRANSPORT_UNAVAILABLE,
    )
    for result in (rate_limited, malformed, transport_failed):
        assert (result.recovery_required, result.kill_required) == (True, True)


def test_malformed_and_oversized_read_responses_are_protocol_invalid() -> None:
    async def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{" + b"x" * 1_048_576,
            headers={"content-type": "application/json"},
        )

    async def exercise(handler):
        transport = HttpxPolymarketRestTransport._for_test(
            httpx.MockTransport(handler),
            timestamp=lambda: TIMESTAMP,
            clock=lambda: NOW,
        )
        try:
            return await transport.execute(
                ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="venue-order-1"),
                credentials=clob_credentials(),
            )
        finally:
            await transport.aclose()

    oversized_result = asyncio.run(exercise(oversized))
    malformed_result, _ = execute_with_response(
        ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="venue-order-1"),
        {**wire_order(), "unexpected": "private"},
        credentials=clob_credentials(),
    )

    assert oversized_result.code is RestCode.PROTOCOL_RESPONSE_INVALID
    assert malformed_result.code is RestCode.PROTOCOL_RESPONSE_INVALID


def test_duplicate_response_keys_are_protocol_invalid() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"allowances":{},"balance":"1","balance":"2"}',
            headers={"content-type": "application/json"},
        )

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
    )

    async def exercise():
        try:
            return await transport.execute(
                ReadBalanceAllowanceRequest(
                    route=RouteKey.READ_BALANCE_ALLOWANCE,
                    signature_type=0,
                    asset_type="COLLATERAL",
                    token_id=None,
                ),
                credentials=clob_credentials(),
            )
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())

    assert result.code is RestCode.PROTOCOL_RESPONSE_INVALID


@pytest.mark.parametrize(
    "values",
    (
        {"max_attempts": 0, "delay_seconds": 0.1},
        {"max_attempts": 3, "delay_seconds": 0.1},
        {"max_attempts": True, "delay_seconds": 0.1},
        {"max_attempts": 2, "delay_seconds": -0.1},
        {"max_attempts": 2, "delay_seconds": 1.01},
        {"max_attempts": 2, "delay_seconds": float("inf")},
    ),
)
def test_read_retry_policy_is_strict_and_bounded(values: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        ReadRetryPolicy(**values)


def test_public_geoblock_does_not_request_a_timestamp_or_auth_headers() -> None:
    timestamp_calls = 0

    def timestamp() -> str:
        nonlocal timestamp_calls
        timestamp_calls += 1
        raise AssertionError("public route must not request a timestamp")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert not any(name.startswith("poly_") for name in request.headers)
        return httpx.Response(
            200,
            content=b'{"blocked":false,"country":"US","ip":"192.0.2.1","region":null}',
            headers={"content-type": "application/json"},
        )

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=timestamp,
        clock=lambda: NOW,
    )

    async def exercise():
        try:
            return await transport.execute(GeoblockRequest(route=RouteKey.GEOBLOCK))
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())

    assert result.code is RestCode.GEOBLOCK_OK
    assert timestamp_calls == 0


def test_clock_and_timestamp_failures_are_closed_build_failures() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("request must not be sent")

    def bad_clock() -> datetime:
        raise RuntimeError("private clock text")

    def bad_timestamp() -> str:
        raise RuntimeError("private timestamp text")

    async def execute(clock, timestamp):
        transport = HttpxPolymarketRestTransport._for_test(
            httpx.MockTransport(handler),
            timestamp=timestamp,
            clock=clock,
        )
        try:
            return await transport.execute(
                ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="venue-order-1"),
                credentials=clob_credentials(),
            )
        finally:
            await transport.aclose()

    clock_result = asyncio.run(execute(bad_clock, lambda: TIMESTAMP))
    timestamp_result = asyncio.run(execute(lambda: NOW, bad_timestamp))

    assert clock_result.code is RestCode.AUTH_REQUEST_BUILD_FAILED
    assert timestamp_result.code is RestCode.AUTH_REQUEST_BUILD_FAILED
    assert clock_result.attempts == timestamp_result.attempts == 0
    assert calls == 0
    assert "private" not in repr((clock_result, timestamp_result))


def test_redirect_is_rejected_without_following_the_location() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            307,
            content=b'{"redirect":"private"}',
            headers={
                "content-type": "application/json",
                "location": "https://attacker.invalid/private",
            },
        )

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
    )

    async def exercise():
        try:
            return await transport.execute(
                ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="venue-order-1"),
                credentials=clob_credentials(),
            )
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())

    assert result.code is RestCode.PROTOCOL_RESPONSE_INVALID
    assert calls == 1
    assert "attacker.invalid" not in repr(result)


def test_trusted_transport_test_seam_uses_exclusively_owned_bounded_client() -> None:
    captured_extensions: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_extensions.append(dict(request.extensions))
        assert "x-caller-header" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(
            200,
            content=b'{"blocked":false,"country":"US","ip":"192.0.2.1","region":null}',
            headers={"content-type": "application/json"},
        )

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
        timeouts=RestTimeouts(connect=1.5, read=2.5, write=1.25, pool=0.75),
    )

    async def exercise():
        try:
            return await transport.execute(GeoblockRequest(route=RouteKey.GEOBLOCK))
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())

    assert result.code is RestCode.GEOBLOCK_OK
    assert captured_extensions == [
        {"timeout": {"connect": 1.5, "read": 2.5, "write": 1.25, "pool": 0.75}}
    ]


def test_caller_owned_async_client_cannot_observe_later_auth_headers() -> None:
    observed_headers: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(wire_order(), separators=(",", ":"), sort_keys=True).encode(),
            headers={"content-type": "application/json"},
        )

    async def observe(request: httpx.Request) -> None:
        observed_headers.append(dict(request.headers))

    async def exercise():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            try:
                transport = HttpxPolymarketRestTransport(
                    client=client,  # type: ignore[call-arg]
                    timestamp=lambda: TIMESTAMP,
                    clock=lambda: NOW,
                )
            except TypeError:
                return None
            client.event_hooks["request"].append(observe)
            return await transport.execute(
                ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="venue-order-1"),
                credentials=clob_credentials(),
            )
        finally:
            await client.aclose()

    result = asyncio.run(exercise())

    assert observed_headers == []
    assert result is None


def test_caller_owned_async_client_cannot_forward_auth_on_later_redirect_change() -> None:
    forwarded_headers: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "clob.polymarket.com":
            return httpx.Response(
                307,
                content=b'{"redirect":"private"}',
                headers={
                    "content-type": "application/json",
                    "location": "https://attacker.invalid/leak",
                },
            )
        forwarded_headers.append(dict(request.headers))
        return httpx.Response(
            200,
            content=json.dumps(wire_order(), separators=(",", ":"), sort_keys=True).encode(),
            headers={"content-type": "application/json"},
        )

    async def exercise():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            try:
                transport = HttpxPolymarketRestTransport(
                    client=client,  # type: ignore[call-arg]
                    timestamp=lambda: TIMESTAMP,
                    clock=lambda: NOW,
                )
            except TypeError:
                return None
            client.follow_redirects = True
            return await transport.execute(
                ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="venue-order-1"),
                credentials=clob_credentials(),
            )
        finally:
            await client.aclose()

    result = asyncio.run(exercise())

    assert forwarded_headers == []
    assert result is None


@pytest.mark.parametrize(
    "value",
    (0, -1, 30.1, float("inf"), float("nan"), True, "2"),
)
def test_transport_timeout_values_are_finite_positive_and_bounded(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        RestTimeouts(connect=value, read=3, write=2, pool=1)


def test_task7_read_payloads_expose_only_exact_task8_filters() -> None:
    orders = ReadOrdersPayload(
        operation=ExecutionOperation.READ_ORDERS,
        id="venue-order-1",
        market="condition-1",
        asset_id="217426",
    )
    trades = ReadTradesPayload(
        operation=ExecutionOperation.READ_TRADES,
        id="trade-1",
        market="condition-1",
        asset_id="217426",
        maker_address="0x" + "11" * 20,
        after=1787673600,
        before=1787677200,
    )
    account = ReadAccountPayload(
        operation=ExecutionOperation.READ_ACCOUNT,
        signature_type=0,
        asset_type="CONDITIONAL",
        token_id="217426",
    )

    assert orders.model_dump(mode="json", exclude_none=True) == {
        "operation": "READ_ORDERS",
        "id": "venue-order-1",
        "market": "condition-1",
        "asset_id": "217426",
    }
    assert trades.model_dump(mode="json") == {
        "operation": "READ_TRADES",
        "id": "trade-1",
        "market": "condition-1",
        "asset_id": "217426",
        "maker_address": "0x" + "11" * 20,
        "after": 1787673600,
        "before": 1787677200,
    }
    assert account.model_dump(mode="json") == {
        "operation": "READ_ACCOUNT",
        "signature_type": 0,
        "asset_type": "CONDITIONAL",
        "token_id": "217426",
    }


def test_task7_private_route_handler_returns_only_the_closed_task8_result() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            content=b'{"balance":"10","allowances":{}}',
            headers={"content-type": "application/json"},
        )

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
    )
    owner = SignerRestHandlers(
        credentials=clob_credentials(),
        transport=transport,
    )
    handlers = owner.as_operation_handlers()

    try:
        result = handlers.read_account(
            ReadAccountPayload(
                operation=ExecutionOperation.READ_ACCOUNT,
                signature_type=0,
                asset_type="COLLATERAL",
                token_id=None,
            )
        )
    finally:
        handlers.close()

    assert type(result) is SanitizedOperationResult
    assert result.operation is ExecutionOperation.READ_ACCOUNT
    assert result.route is RouteKey.READ_BALANCE_ALLOWANCE
    assert result.result_code is RestCode.READ_OK
    assert result.public_payload.balance == "10"
    assert result.attempts == 1
    assert result.raw_body_hash == hashlib.sha256(b'{"balance":"10","allowances":{}}').hexdigest()
    rendered = result.model_dump_json()
    for secret in ("task8-api-key", "task8-hmac-secret", "task8-passphrase"):
        assert secret not in rendered
    assert len(captured) == 1
    response = SignerResponse.accepted(
        UUID("11111111-1111-4111-8111-111111111111"),
        result,
    )
    round_tripped = SignerResponse.model_validate_json(
        canonical_response_bytes(response),
        strict=True,
    )
    assert isinstance(round_tripped.result, SanitizedOperationResult)
    assert round_tripped.result.result_code is RestCode.READ_OK
    assert round_tripped.result.public_payload == result.public_payload
    with pytest.raises(ValidationError):
        result.public_payload.model_copy(update={"balance": "not-an-integer"})


def test_task7_submit_handler_is_fixed_to_submit_and_closes_with_owner() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b'{"errorMsg":"","makingAmount":"5.1","orderID":"venue-order-1",'
                b'"status":"matched","success":true,"takingAmount":"10",'
                b'"tradeIDs":[],"transactionsHashes":[]}'
            ),
            headers={"content-type": "application/json"},
        )

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: TIMESTAMP,
        clock=lambda: NOW,
    )
    owner = SignerRestHandlers(credentials=clob_credentials(), transport=transport)
    handlers = owner.as_operation_handlers()
    request = submit_order_request()
    try:
        result = handlers.submit_order(
            SubmitOrderPayload(
                operation=ExecutionOperation.SUBMIT_ORDER,
                intent=request.intent,
                envelope=request.envelope,
            )
        )
    finally:
        handlers.close()

    assert result.route is RouteKey.SUBMIT_ORDER
    assert result.result_code is RestCode.ORDER_ACK_MATCHED
    assert result.venue_order_id == "venue-order-1"
    assert result.public_payload.order_id == "venue-order-1"
    with pytest.raises(ClobAuthError, match="SIGNER_REST_HANDLERS_CLOSED"):
        handlers.read_trades(ReadTradesPayload(operation=ExecutionOperation.READ_TRADES))
