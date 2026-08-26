import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from polytrading.predictions.polymarket_execution import (
    POLYMARKET_PROTOCOL_VERSION,
    bundled_fixture_path,
    load_protocol_snapshot,
    verify_protocol_sources,
)

EXPECTED_SOURCE_HASHES = {
    "https://docs.polymarket.com/getting-started/api": (
        "032d1475be0f58473955f9dc044ab73806c0f734e5b823c90fb9751e624818d1"
    ),
    "https://docs.polymarket.com/trading/place-orders": (
        "258b23a52c0c5949cfb2393aca627544029d20824a1825bf84074217928a1e11"
    ),
    "https://docs.polymarket.com/trading/manage-orders": (
        "348b90c982aeb822345791304724d958816bc47b1cae0c4ed6861d7034bacef4"
    ),
    "https://docs.polymarket.com/trading/realtime-order-updates": (
        "b54f6a355d93eb4f83a7734da4f2fb32bdd7737d90a954c796f52b3eba1818d7"
    ),
    "https://docs.polymarket.com/api-reference/trade/send-heartbeat": (
        "dd6ec5e859ba43353bb0a70de56f7aaa57c83a803175a2b43aa5b387535d36be"
    ),
    "https://docs.polymarket.com/api-reference/geoblock": (
        "080e785817f8298c07bcf51d896efcf9a09e54d716fc5bba99bacb4334b8c2a1"
    ),
    (
        "https://raw.githubusercontent.com/Polymarket/py-clob-client-v2/"
        "main/py_clob_client_v2/endpoints.py"
    ): "f75a6ea6fdd22b7b327556ea24e028976d0e1f5b4c57739bb25546218e2d40dc",
    (
        "https://raw.githubusercontent.com/Polymarket/py-clob-client-v2/"
        "main/py_clob_client_v2/client.py"
    ): "66604f43bf37f8482f3f50674b9f3e3834ff13ef83722c00c69520e37052bd3a",
}


def copy_bundled_fixtures(destination: Path) -> Path:
    root = destination / "fixtures"
    shutil.copytree(bundled_fixture_path(), root)
    return root


def test_bundled_protocol_snapshot_is_self_hashing_and_current() -> None:
    snapshot = load_protocol_snapshot()

    assert snapshot.version == POLYMARKET_PROTOCOL_VERSION
    assert snapshot.version == "polymarket-clob-2026-08-25-v1"
    assert snapshot.chain_id == 137
    assert snapshot.allowed_order_types == ("FAK", "FOK")
    assert verify_protocol_sources(snapshot).state == "CURRENT"
    assert {path.name for path in bundled_fixture_path().iterdir()} == {
        "event_vectors_v1.json",
        "order_vectors_v1.json",
        "protocol_v1.json",
        "sources_v1.json",
    }


@pytest.mark.parametrize(
    "fixture_name",
    ("protocol_v1.json", "sources_v1.json", "order_vectors_v1.json", "event_vectors_v1.json"),
)
def test_one_changed_fixture_byte_requires_review(tmp_path: Path, fixture_name: str) -> None:
    copied = copy_bundled_fixtures(tmp_path)
    path = copied / fixture_name
    path.write_bytes(path.read_bytes() + b" ")

    readiness = verify_protocol_sources(load_protocol_snapshot(copied))

    assert readiness.state == "PROTOCOL_REVIEW_REQUIRED"
    assert fixture_name in readiness.changed_paths


def test_snapshot_load_is_strict_and_rejects_unknown_protocol_fields(tmp_path: Path) -> None:
    copied = copy_bundled_fixtures(tmp_path)
    protocol_path = copied / "protocol_v1.json"
    document = json.loads(protocol_path.read_text(encoding="utf-8"))
    document["unreviewed_protocol_fact"] = True
    protocol_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValidationError, match="unreviewed_protocol_fact"):
        load_protocol_snapshot(copied)


def test_source_manifest_pins_normalized_official_source_hashes() -> None:
    snapshot = load_protocol_snapshot()

    actual_hashes = {
        source.canonical_url: source.normalized_content_sha256 for source in snapshot.sources
    }
    assert actual_hashes == EXPECTED_SOURCE_HASHES
    assert {source.retrieved_at for source in snapshot.sources} == {"2026-08-25T00:00:00Z"}
    assert {source.protocol_fixture_version for source in snapshot.sources} == {
        POLYMARKET_PROTOCOL_VERSION
    }
    assert all(source.derived_files for source in snapshot.sources)


def test_current_v2_order_domain_fields_and_wallet_paths_are_frozen() -> None:
    snapshot = load_protocol_snapshot()

    assert snapshot.eip712.order_domain.model_dump(by_alias=True) == {
        "name": "Polymarket CTF Exchange",
        "version": "2",
        "chainId": 137,
        "verifyingContract": "<exchange_address>",
    }
    assert tuple(field.name for field in snapshot.eip712.order_fields) == (
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
    assert snapshot.eip712.exchange_addresses.model_dump() == {
        "standard": "0xE111180000d2663C0091e4f400237545B87B996B",
        "negative_risk": "0xe2222d279d744050d28e00520010520000310F59",
    }
    assert {wallet.wallet: wallet.signature_type for wallet in snapshot.eip712.wallets} == {
        "EOA": 0,
        "PROXY": 1,
        "SAFE": 2,
        "DEPOSIT": 3,
    }
    assert snapshot.order_submission.fee_rate_binding.state == "ABSENT_SUPERSEDED"
    assert snapshot.order_submission.fee_rate_binding.signed_order_field is None


def test_authentication_routes_and_execution_contract_are_frozen() -> None:
    snapshot = load_protocol_snapshot()

    assert snapshot.authentication.clob_auth.domain.model_dump(by_alias=True) == {
        "name": "ClobAuthDomain",
        "version": "1",
        "chainId": 137,
    }
    assert tuple(field.name for field in snapshot.authentication.clob_auth.fields) == (
        "address",
        "timestamp",
        "nonce",
        "message",
    )
    assert snapshot.authentication.l2.headers == (
        "POLY_ADDRESS",
        "POLY_SIGNATURE",
        "POLY_TIMESTAMP",
        "POLY_API_KEY",
        "POLY_PASSPHRASE",
    )
    assert snapshot.authentication.l2.preimage_components == (
        "unix_timestamp_seconds",
        "uppercase_http_method",
        "route_path_without_query",
        "exact_serialized_request_body_if_present",
    )
    assert (snapshot.routes.place_order.method, snapshot.routes.place_order.path) == (
        "POST",
        "/order",
    )
    assert (snapshot.routes.heartbeat.method, snapshot.routes.heartbeat.path) == (
        "POST",
        "/v1/heartbeats",
    )
    assert snapshot.heartbeat.initial_compact_body == '{"heartbeat_id":""}'
    assert snapshot.heartbeat.cadence_seconds == 5
    assert snapshot.heartbeat.cancellation_timeout_seconds == 10
    assert snapshot.websocket.ping == "PING"
    assert snapshot.websocket.pong == "PONG"
    assert snapshot.websocket.ping_interval_seconds == 10
    assert snapshot.geoblock.response_fields == ("blocked", "ip", "country", "region")
    assert snapshot.trade_states == (
        "MATCHED_NOT_BROADCASTED",
        "MATCHED",
        "MINED",
        "CONFIRMED",
        "RETRYING",
        "FAILED",
    )


def test_stale_generated_heartbeat_route_is_evidence_not_an_allowed_route() -> None:
    snapshot = load_protocol_snapshot()

    assert snapshot.heartbeat.conflict_resolution.stale_generated_route == "/heartbeats"
    assert snapshot.heartbeat.conflict_resolution.authoritative_route == "/v1/heartbeats"
    assert "/heartbeats" not in {
        route["path"] for route in snapshot.routes.model_dump().values() if route is not None
    }


def test_array_and_compact_object_route_payload_shapes_are_frozen() -> None:
    routes = load_protocol_snapshot().routes

    assert routes.place_orders.request_body_shape == "array_of_order_envelopes_1_to_15"
    assert routes.cancel_orders.request_body_shape == "array_of_order_ids_1_to_3000"
    assert routes.cancel_order.compact_body_examples == ('{"orderID":"<order_id>"}',)
    assert routes.heartbeat.compact_body_examples == ('{"heartbeat_id":""}',)
