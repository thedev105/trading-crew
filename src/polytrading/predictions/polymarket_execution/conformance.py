"""Offline-only conformance checks for the frozen Polymarket protocol."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID, uuid5

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.execution.models import (
    ExecutionIntent,
    ImmediateOrderType,
    ProtocolConformanceResult,
)
from polytrading.predictions.polymarket_execution.auth import (
    ClobCredentials,
    clob_auth_typed_data,
    l2_preimage,
    sign_clob_auth,
    sign_l2_request,
)
from polytrading.predictions.polymarket_execution.order import (
    PolymarketOrder,
    order_amounts,
    order_typed_data,
    recover_order_signer,
    sign_order,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PROTOCOL_VERSION,
    PolymarketProtocolSnapshot,
    load_protocol_snapshot,
    verify_protocol_sources,
)
from polytrading.predictions.polymarket_execution.routes import (
    ROUTE_SET_HASH,
    ROUTE_SET_VERSION,
    ROUTE_SPECS,
    RouteKey,
)
from polytrading.predictions.polymarket_execution.user_stream import parse_user_event

_FIXTURE_NAMES = frozenset(
    {
        "event_vectors_v1.json",
        "order_vectors_v1.json",
        "protocol_v1.json",
        "sources_v1.json",
    }
)
_MAX_FIXTURE_BYTES = 1_048_576
_MAX_FAILURES = 32
_RESULT_NAMESPACE = UUID("9ce0811c-7292-5ad9-a703-a64744c9ee25")
_ROUTE_SET_HASH = "3429c248a6caec950da2ed46643bb8810ff028740f967888c6b77de1fb127bec"
_ROUTE_SET_VERSION = "polymarket-mutations-v1"
_VECTOR_ADDRESS = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"
_VECTOR_TIMESTAMP = "1787673600"
_ORDER_SIGNATURE_HASH = "fe995ca2b8462511db012086b861761641eeac2b0ffb4456806564d1e96c4707"
_L1_SIGNATURE_HASH = "f4872e0ad43bc211048d24f1d9feadfb2c46c4679d132d2756151c034cbaec96"
_L2_SIGNATURE_HASH = "beb8d495e4c17f7f26d746b6aff6668bb809f5ddfa744ee9a3b36c6ae0241793"
_ORDER_EXPECTATIONS = {
    "domain_fingerprint": "1feee49d347d5eb4a787b7c3ec99e591830fe6712fc635bdf3c82db17ebf3cc0",
    "exact_body_hash": "2201756cba2fdcd84f704de2a5c63fc70676ec14a705cedc1bd852f8098b4ef7",
    "order_fingerprint": "0d35a463727531d7bbe786c32ca2d7ef0724d5bb08446a185a47a880bb0da5c1",
    "exchange_fingerprint": "85c145f2427d2871411c94a3d91d4b9768f95b4a3057a6830d67ecac8dc7c56e",
}


@dataclass(frozen=True, slots=True)
class _CapturedMember:
    contents: bytes
    identity: tuple[int, int, int, int, int, int]
    digest: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _failure_fingerprint(code: str, path_identity: str) -> str:
    return sha256(f"task-12:{code}:{path_identity}".encode("ascii")).hexdigest()


def _record_failure(failures: set[str], code: str, path_identity: str) -> None:
    if len(failures) < _MAX_FAILURES:
        failures.add(_failure_fingerprint(code, path_identity))


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_member(root_fd: int, name: str) -> _CapturedMember | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=root_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FIXTURE_BYTES:
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, _MAX_FIXTURE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_FIXTURE_BYTES:
                return None
        after = os.fstat(descriptor)
        contents = b"".join(chunks)
        identity = _stat_identity(before)
        if identity != _stat_identity(after) or len(contents) != before.st_size:
            return None
        return _CapturedMember(contents, identity, sha256(contents).hexdigest())
    finally:
        os.close(descriptor)


def _open_fixture_root(root: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(root, flags)


def _capture_fixture_root(
    fixture_root: Path,
    failures: set[str],
) -> tuple[dict[str, _CapturedMember], bool]:
    captured: dict[str, _CapturedMember] = {}
    try:
        root_fd = _open_fixture_root(fixture_root)
    except (OSError, TypeError, ValueError):
        _record_failure(failures, "ROOT_INVALID", "fixture-root")
        return captured, False
    try:
        try:
            member_names = set(os.listdir(root_fd))
        except OSError:
            _record_failure(failures, "ROOT_READ_FAILED", "fixture-root")
            return captured, False
        exact_set = member_names == _FIXTURE_NAMES
        if not exact_set:
            _record_failure(failures, "FIXTURE_SET_INVALID", "fixture-set")
        for name in sorted(_FIXTURE_NAMES):
            if name not in member_names:
                _record_failure(failures, "MEMBER_MISSING", name)
                continue
            try:
                member = _read_member(root_fd, name)
            except OSError:
                member = None
            if member is None:
                _record_failure(failures, "MEMBER_INVALID", name)
                continue
            captured[name] = member
        return captured, exact_set and len(captured) == len(_FIXTURE_NAMES)
    finally:
        os.close(root_fd)


def _stable_fixture_view(
    fixture_root: Path,
    captured: Mapping[str, _CapturedMember],
) -> bool:
    try:
        root_fd = _open_fixture_root(fixture_root)
    except (OSError, TypeError, ValueError):
        return False
    try:
        try:
            if set(os.listdir(root_fd)) != _FIXTURE_NAMES:
                return False
        except OSError:
            return False
        for name in sorted(_FIXTURE_NAMES):
            expected = captured.get(name)
            if expected is None:
                return False
            try:
                observed = _read_member(root_fd, name)
            except OSError:
                return False
            if observed is None or observed != expected:
                return False
        return True
    finally:
        os.close(root_fd)


def _strict_json(contents: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_JSON_MEMBER")
            result[key] = value
        return result

    return json.loads(contents, object_pairs_hook=reject_duplicates)


def _exact_keys(value: object, expected: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == expected


def _is_plain_string(value: object) -> bool:
    return type(value) is str


def _check_amount_vectors(
    snapshot: PolymarketProtocolSnapshot,
    order_vectors: Mapping[str, object],
) -> bool:
    vectors = order_vectors.get("amount_vectors")
    if type(vectors) is not list or len(vectors) != 3:
        return False
    required = {
        "vector_id",
        "kind",
        "side",
        "tick_size",
        "price",
        "input_amount",
        "makerAmount",
        "takerAmount",
    }
    for vector in vectors:
        if not _exact_keys(vector, required) or not all(
            _is_plain_string(value) for value in vector.values()
        ):
            return False
        maker, taker = order_amounts(
            side=vector["side"].lower(),
            price=Decimal(vector["price"]),
            size=Decimal(vector["input_amount"]),
            tick_size=Decimal(vector["tick_size"]),
            kind=vector["kind"].lower(),
            rounding=snapshot.rounding,
        )
        if (str(maker), str(taker)) != (vector["makerAmount"], vector["takerAmount"]):
            return False
    return True


def _check_order_wire_vector(
    snapshot: PolymarketProtocolSnapshot,
    order_vectors: Mapping[str, object],
) -> bool:
    vector = order_vectors.get("wire_vector")
    if not _exact_keys(
        vector,
        {"vector_id", "route", "method", "order_type", "typed_data", "request_payload"},
    ):
        return False
    if vector["route"] != snapshot.routes.place_order.path or vector["method"] != "POST":
        return False
    if vector["order_type"] not in snapshot.allowed_order_types:
        return False
    request = vector["request_payload"]
    if not _exact_keys(request, set(snapshot.order_submission.outer_payload_fields)):
        return False
    if request["orderType"] != vector["order_type"] or type(request["deferExec"]) is not bool:
        return False
    if not _is_plain_string(request["owner"]):
        return False
    signed_order = request["order"]
    if not isinstance(signed_order, dict) or set(signed_order) != {
        *snapshot.order_submission.posted_order_fields,
        "signature",
    }:
        return False
    signature = signed_order["signature"]
    if type(signature) is not str or not signature.startswith("0x") or len(signature) != 132:
        return False
    try:
        bytes.fromhex(signature[2:])
    except ValueError:
        return False
    public_order = {key: value for key, value in signed_order.items() if key != "signature"}
    order = PolymarketOrder.from_public_order(public_order, exchange_kind="standard")
    typed = order_typed_data(order, snapshot)
    frozen_typed = vector["typed_data"]
    if not _exact_keys(frozen_typed, {"domain", "primaryType", "message"}):
        return False
    frozen_message = dict(frozen_typed["message"])
    for integer_field in ("salt", "tokenId", "makerAmount", "takerAmount", "timestamp"):
        frozen_message[integer_field] = int(frozen_message[integer_field])
    if (
        typed["domain"] != frozen_typed["domain"]
        or typed["primaryType"] != frozen_typed["primaryType"]
        or typed["message"] != frozen_message
    ):
        return False
    if order.public_order() != public_order:
        return False

    private_material = (1).to_bytes(32, "big")
    intent = ExecutionIntent(
        schema_version=1,
        intent_id=UUID("30039691-5392-5511-9d27-40ec90530584"),
        plan_id=UUID("0d7c250b-0a21-55f3-a897-8bc98c59f904"),
        leg_sequence=0,
        venue=PredictionVenue.POLYMARKET,
        token_id="217426",
        side="buy",
        limit_price=Decimal("0.51"),
        tick_size=Decimal("0.01"),
        exchange_kind="standard",
        base_size=Decimal("10"),
        maximum_spend=Decimal("5.10"),
        order_type=ImmediateOrderType.FAK,
        fee_rate_bps_cap=100,
        rounding_mode="ROUND_DOWN",
        account_fingerprint=("d6f781065c489e6513f45bc3dab82156055056d393c42f49a4defec22b5ee73f"),
        capability_fingerprint="b" * 64,
        created_at=datetime(2026, 8, 25, 16, tzinfo=UTC),
        deadline=datetime(2026, 8, 25, 16, 0, 5, tzinfo=UTC),
        protocol_version=POLYMARKET_PROTOCOL_VERSION,
        intent_fingerprint=("ead5be0906f78d056f447fdc1ff6bd7c3ce5c897ad9c6696c412c3ce835d8090"),
    )
    envelope = sign_order(intent, private_material, snapshot)
    if recover_order_signer(envelope, snapshot) != _VECTOR_ADDRESS:
        return False
    if sha256(envelope.public_signature.encode("ascii")).hexdigest() != _ORDER_SIGNATURE_HASH:
        return False
    return all(getattr(envelope, key) == value for key, value in _ORDER_EXPECTATIONS.items())


def _check_l1_auth_vector(snapshot: PolymarketProtocolSnapshot, *_unused: object) -> bool:
    private_material = (1).to_bytes(32, "big")
    typed = clob_auth_typed_data(_VECTOR_ADDRESS, _VECTOR_TIMESTAMP, snapshot)
    contract = snapshot.authentication.clob_auth
    if typed["primaryType"] != contract.primary_type:
        return False
    if typed["domain"] != contract.domain.model_dump(mode="json", by_alias=True):
        return False
    if typed["message"] != {
        "address": _VECTOR_ADDRESS,
        "timestamp": _VECTOR_TIMESTAMP,
        "nonce": contract.default_nonce,
        "message": contract.message,
    }:
        return False
    signature = sign_clob_auth(private_material, _VECTOR_TIMESTAMP, snapshot)
    return sha256(signature.encode("ascii")).hexdigest() == _L1_SIGNATURE_HASH


def _vector_credentials() -> ClobCredentials:
    return ClobCredentials(
        address=_VECTOR_ADDRESS,
        api_key=b"task-" + b"6-api-key",
        secret=b"cG9seW1hcmtldC10" + b"YXNrLTYtaG1hYy1rZXk=",
        passphrase=b"task-" + b"6-passphrase",
    )


def _check_l2_auth_vector(
    snapshot: PolymarketProtocolSnapshot,
    order_vectors: Mapping[str, object],
) -> bool:
    vector = order_vectors.get("heartbeat_preimage_vector")
    if not _exact_keys(vector, {"timestamp", "method", "path", "body", "preimage"}):
        return False
    if not all(_is_plain_string(item) for item in vector.values()):
        return False
    if vector["path"] != snapshot.routes.heartbeat.path:
        return False
    body = vector["body"].encode("utf-8")
    preimage = l2_preimage(vector["timestamp"], vector["method"], vector["path"], body)
    if preimage != vector["preimage"].encode("utf-8"):
        return False
    credentials = _vector_credentials()
    headers = sign_l2_request(
        credentials,
        timestamp=vector["timestamp"],
        method=vector["method"],
        route=vector["path"],
        body=body,
    )
    if tuple(headers) != snapshot.authentication.l2.headers:
        return False
    expected_headers = {
        "POLY_ADDRESS": credentials.address,
        "POLY_TIMESTAMP": vector["timestamp"],
        "POLY_API_KEY": credentials.api_key.decode("ascii"),
        "POLY_PASSPHRASE": credentials.passphrase.decode("ascii"),
    }
    return (
        all(headers[name] == value for name, value in expected_headers.items())
        and sha256(headers.signature.encode("ascii")).hexdigest() == _L2_SIGNATURE_HASH
    )


def _check_route_catalog(snapshot: PolymarketProtocolSnapshot, *_unused: object) -> bool:
    if ROUTE_SET_VERSION != _ROUTE_SET_VERSION or ROUTE_SET_HASH != _ROUTE_SET_HASH:
        return False
    selected = {
        RouteKey.SUBMIT_ORDER: (snapshot.routes.place_order, True),
        RouteKey.CANCEL_ORDER: (snapshot.routes.cancel_order, True),
        RouteKey.READ_ORDER: (snapshot.routes.get_order, False),
        RouteKey.READ_OPEN_ORDERS: (snapshot.routes.list_orders, False),
        RouteKey.READ_TRADES: (snapshot.routes.list_trades, False),
        RouteKey.READ_BALANCE_ALLOWANCE: (snapshot.routes.balance_allowance, False),
        RouteKey.HEARTBEAT: (snapshot.routes.heartbeat, True),
        RouteKey.GEOBLOCK: (snapshot.routes.geoblock, False),
    }
    if set(ROUTE_SPECS) != set(selected):
        return False
    for key, (route, mutation) in selected.items():
        spec = ROUTE_SPECS[key]
        if (
            spec.key is not key
            or spec.host != route.host
            or spec.method != route.method
            or spec.path_template != route.path
            or spec.auth_level != route.auth_level
            or spec.mutation is not mutation
            or spec.query_fields != route.query_fields
            or spec.request_fields != route.request_fields
        ):
            return False
    exact_bodies = (
        *snapshot.routes.cancel_order.compact_body_examples,
        *snapshot.routes.heartbeat.compact_body_examples,
    )
    if exact_bodies != ('{"orderID":"<order_id>"}', '{"heartbeat_id":""}'):
        return False
    return all(_canonical_json(_strict_json(body.encode("utf-8"))) == body for body in exact_bodies)


def _materialized_order_frame(vector: Mapping[str, object]) -> bytes:
    materialized = dict(vector)
    materialized["asset_id"] = "217426"
    materialized["maker_address"] = "0x" + "11" * 20
    return _canonical_json(materialized).encode("utf-8")


def _materialized_trade_frame(vector: Mapping[str, object], state: str) -> bytes:
    materialized = dict(vector)
    materialized["asset_id"] = "217426"
    materialized["maker_address"] = "0x" + "11" * 20
    materialized["status"] = state
    maker_orders = materialized.get("maker_orders")
    if type(maker_orders) is not list:
        raise ValueError("EVENT_SCHEMA_INVALID")
    materialized["maker_orders"] = [
        {
            **item,
            "asset_id": "217426",
            "maker_address": "0x" + "22" * 20,
        }
        for item in maker_orders
        if isinstance(item, dict)
    ]
    if len(materialized["maker_orders"]) != len(maker_orders):
        raise ValueError("EVENT_SCHEMA_INVALID")
    return _canonical_json(materialized).encode("utf-8")


def _check_event_vectors(
    snapshot: PolymarketProtocolSnapshot,
    event_vectors: Mapping[str, object],
) -> bool:
    if set(event_vectors) != {
        "schema_version",
        "protocol_version",
        "order_acknowledgement_states",
        "trade_settlement_states",
        "user_order_event",
        "user_trade_event",
    }:
        return False
    acknowledgements = event_vectors["order_acknowledgement_states"]
    if acknowledgements != [
        {"wire": "live", "meaning": "RESTING"},
        {"wire": "matched", "meaning": "MATCHED_IMMEDIATELY"},
        {"wire": "delayed", "meaning": "MATCHING_DELAY_PENDING"},
        {
            "wire": "unmatched",
            "meaning": "MARKETABLE_DELAY_FAILED_PLACEMENT_SUCCEEDED",
        },
    ]:
        return False
    order_vector = event_vectors["user_order_event"]
    trade_vector = event_vectors["user_trade_event"]
    if not isinstance(order_vector, dict) or not isinstance(trade_vector, dict):
        return False
    receipt_time = datetime(2026, 8, 25, 16, tzinfo=UTC)
    order_event = parse_user_event(
        _materialized_order_frame(order_vector),
        receipt_time=receipt_time,
    )
    if (
        order_event.normalized_state.value != "ACK_LIVE_UNEXPECTED"
        or order_event.terminal
        or order_event.protocol_version != snapshot.version
    ):
        return False
    states = event_vectors["trade_settlement_states"]
    if type(states) is not list or len(states) != 6:
        return False
    expected_states = [(state, state in {"CONFIRMED", "FAILED"}) for state in snapshot.trade_states]
    observed_states: list[tuple[str, bool]] = []
    for state in states:
        if not _exact_keys(state, {"wire", "terminal"}):
            return False
        wire_state = state["wire"]
        terminal = state["terminal"]
        if type(wire_state) is not str or type(terminal) is not bool:
            return False
        event = parse_user_event(
            _materialized_trade_frame(trade_vector, wire_state),
            receipt_time=receipt_time,
        )
        if (
            event.normalized_state.value != wire_state
            or event.terminal is not terminal
            or event.protocol_version != snapshot.version
            or "owner" in event.model_dump(mode="json")
        ):
            return False
        observed_states.append((wire_state, terminal))
    return observed_states == expected_states


def _validate_vector_documents(
    snapshot: PolymarketProtocolSnapshot,
    captured: Mapping[str, _CapturedMember],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    order_vectors = _strict_json(captured["order_vectors_v1.json"].contents)
    event_vectors = _strict_json(captured["event_vectors_v1.json"].contents)
    if not isinstance(order_vectors, dict) or not isinstance(event_vectors, dict):
        raise ValueError("VECTOR_SCHEMA_INVALID")
    if set(order_vectors) != {
        "schema_version",
        "protocol_version",
        "amount_vectors",
        "wire_vector",
        "heartbeat_preimage_vector",
    }:
        raise ValueError("VECTOR_SCHEMA_INVALID")
    for document in (order_vectors, event_vectors):
        if document.get("schema_version") != 1:
            raise ValueError("VECTOR_SCHEMA_INVALID")
        if document.get("protocol_version") != snapshot.version:
            raise ValueError("PROTOCOL_VERSION_MISMATCH")
    return order_vectors, event_vectors


def _result(
    *,
    fixture_hashes: tuple[str, ...],
    source_hashes: tuple[str, ...],
    implementation_revision: str,
    checks: set[str],
    failures: set[str],
) -> ProtocolConformanceResult:
    observed_at = _utc_now()
    sorted_checks = tuple(sorted(checks))
    sorted_failures = tuple(sorted(failures))[:_MAX_FAILURES]
    result = "CONFORMANT" if not sorted_failures else "PROTOCOL_REVIEW_REQUIRED"
    identity = _canonical_json(
        {
            "executed_checks": sorted_checks,
            "failure_fingerprints": sorted_failures,
            "fixture_hashes": fixture_hashes,
            "implementation_revision": implementation_revision,
            "observed_at": observed_at.isoformat(),
            "result": result,
            "source_hashes": source_hashes,
        }
    )
    return ProtocolConformanceResult(
        schema_version=1,
        conformance_result_id=uuid5(_RESULT_NAMESPACE, identity),
        fixture_hashes=fixture_hashes,
        source_hashes=source_hashes,
        implementation_revision=implementation_revision,
        executed_checks=sorted_checks,
        result=result,
        observed_at=observed_at,
        failure_fingerprints=sorted_failures,
    )


def run_conformance(
    fixture_root: Path,
    implementation_revision: str,
) -> ProtocolConformanceResult:
    """Validate one local frozen fixture root without constructing transport."""
    if (
        type(implementation_revision) is not str
        or not implementation_revision
        or len(implementation_revision) > 256
    ):
        raise ValueError("IMPLEMENTATION_REVISION_INVALID") from None
    failures: set[str] = set()
    checks: set[str] = {"fixture_trust"}
    try:
        root = Path(fixture_root)
    except (TypeError, ValueError, OSError):
        root = Path(".")
        _record_failure(failures, "ROOT_INVALID", "fixture-root")
        captured: dict[str, _CapturedMember] = {}
        complete = False
    else:
        captured, complete = _capture_fixture_root(root, failures)

    fixture_hashes = tuple(sorted(member.digest for member in captured.values()))
    source_hashes: tuple[str, ...] = ()
    if complete:
        try:
            with TemporaryDirectory(prefix="polytrading-conformance-") as temporary:
                stable_root = Path(temporary)
                for name, member in captured.items():
                    (stable_root / name).write_bytes(member.contents)
                readiness = verify_protocol_sources(root=stable_root)
                if readiness.state != "CURRENT" or readiness.changed_paths:
                    _record_failure(failures, "TRUST_ROOT_MISMATCH", "fixture-trust")
                else:
                    snapshot = load_protocol_snapshot(stable_root)
                    if snapshot.version != POLYMARKET_PROTOCOL_VERSION:
                        _record_failure(failures, "PROTOCOL_VERSION_MISMATCH", "protocol")
                    else:
                        source_hashes = tuple(
                            sorted(source.normalized_content_sha256 for source in snapshot.sources)
                        )
                        order_vectors, event_vectors = _validate_vector_documents(
                            snapshot, captured
                        )
                        check_operations: tuple[
                            tuple[str, Callable[..., bool], tuple[object, ...]], ...
                        ] = (
                            ("amount_vectors", _check_amount_vectors, (snapshot, order_vectors)),
                            (
                                "order_wire_vector",
                                _check_order_wire_vector,
                                (snapshot, order_vectors),
                            ),
                            ("l1_auth_vector", _check_l1_auth_vector, (snapshot, order_vectors)),
                            ("l2_auth_vector", _check_l2_auth_vector, (snapshot, order_vectors)),
                            ("route_catalog", _check_route_catalog, (snapshot, order_vectors)),
                            ("event_vectors", _check_event_vectors, (snapshot, event_vectors)),
                        )
                        for check_name, operation, arguments in check_operations:
                            checks.add(check_name)
                            try:
                                passed = operation(*arguments)
                            except Exception:
                                passed = False
                            if not passed:
                                _record_failure(
                                    failures,
                                    "CHECK_FAILED",
                                    check_name,
                                )
        except Exception:
            _record_failure(failures, "FIXTURE_VALIDATION_FAILED", "fixture-trust")

    checks.add("stable_fixture_view")
    if not complete or not _stable_fixture_view(root, captured):
        _record_failure(failures, "CHANGING_FIXTURE_VIEW", "fixture-root")
    return _result(
        fixture_hashes=fixture_hashes,
        source_hashes=source_hashes,
        implementation_revision=implementation_revision,
        checks=checks,
        failures=failures,
    )


__all__ = ["run_conformance"]
