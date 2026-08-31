from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import secrets as runtime_secrets
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from eth_account import Account

import polytrading
from polytrading.predictions.cli import _run_polymarket_conformance
from polytrading.predictions.dashboard import build_prediction_dashboard_snapshot
from polytrading.predictions.dashboard_live import DashboardRevision
from polytrading.predictions.dashboard_models import DashboardDomain
from polytrading.predictions.dashboard_server import (
    PredictionDashboardApplication,
    _sse_event_frame,
)
from polytrading.predictions.execution.authority import AuthorityDecision
from polytrading.predictions.execution.models import ExecutionIntent, ExecutionOperation
from polytrading.predictions.polymarket_execution import user_stream as user_stream_module
from polytrading.predictions.polymarket_execution.auth import ClobCredentials
from polytrading.predictions.polymarket_execution.ipc import (
    ReadAccountPayload,
    ReadOrdersPayload,
    ReadTradesPayload,
    SanitizedOperationResult,
    SignerCapabilityProof,
    SignerProtocolError,
    SignerRequest,
    SubmitOrderPayload,
    canonical_request_bytes,
    canonical_response_bytes,
    parse_signer_request,
    read_frame,
)
from polytrading.predictions.polymarket_execution.order import sign_order
from polytrading.predictions.polymarket_execution.protocol import load_protocol_snapshot
from polytrading.predictions.polymarket_execution.rest import (
    HttpxPolymarketRestTransport,
    RestCode,
)
from polytrading.predictions.polymarket_execution.routes import (
    CancelOrderRequest,
    HeartbeatRequest,
    ReadOrderRequest,
    RouteKey,
    SubmitOrderRequest,
)
from polytrading.predictions.polymarket_execution.secrets import SecretMaterial
from polytrading.predictions.polymarket_execution.signer import (
    SignerOperationHandlers,
    SignerService,
)
from polytrading.predictions.polymarket_execution.user_stream import (
    UserStreamProtocolError,
    parse_user_event,
)
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.execution_helpers import execution_intent_fields
from tests.predictions.pilot_helpers import signer_capability_grant
from tests.predictions.test_execution_authority import (
    HASHES,
    MANIFEST_HASH,
    authority_context,
    verified_capability,
)

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(polytrading.__file__).resolve().parent
WEB_ASSETS_ROOT = PACKAGE_ROOT / "predictions/web_assets"
RUNBOOK = REPOSITORY_ROOT / "docs/predictions/polymarket-execution-hardening.md"


def _submit_material(
    *,
    private_key: bytes,
    api_key: bytes,
    api_secret: bytes,
    passphrase: bytes,
) -> tuple[SubmitOrderRequest, ClobCredentials]:
    account = Account.from_key(private_key)
    account_fingerprint = sha256(bytes.fromhex(account.address[2:])).hexdigest()
    grant = signer_capability_grant(account_fingerprint=account_fingerprint, now=NOW)
    intent = ExecutionIntent(
        **execution_intent_fields(
            account_fingerprint=account_fingerprint,
            capability_fingerprint=grant.digest,
        )
    )
    envelope = sign_order(intent, private_key, load_protocol_snapshot())
    return (
        SubmitOrderRequest(
            route=RouteKey.SUBMIT_ORDER,
            intent=intent,
            envelope=envelope,
        ),
        ClobCredentials(
            address=account.address,
            api_key=api_key,
            secret=api_secret,
            passphrase=passphrase,
        ),
    )


@dataclass(frozen=True)
class _Canaries:
    private_key: bytes
    api_key: bytes
    api_secret: bytes
    passphrase: bytes
    auth_header: bytes
    signed_body: bytes
    subscription_frame: bytes

    @classmethod
    def generate(cls) -> _Canaries:
        while True:
            private_key = runtime_secrets.token_bytes(32)
            try:
                Account.from_key(private_key)
            except ValueError:
                continue
            break
        api_key = runtime_secrets.token_urlsafe(24).encode("ascii")
        api_secret = base64.urlsafe_b64encode(runtime_secrets.token_bytes(32))
        passphrase = runtime_secrets.token_urlsafe(25).encode("ascii")
        submit_request, credentials = _submit_material(
            private_key=private_key,
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
        )
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                503,
                content=b'{"code":"offline-canary-capture"}',
                headers={"content-type": "application/json"},
            )

        transport = HttpxPolymarketRestTransport._for_test(
            httpx.MockTransport(handler),
            timestamp=lambda: "1787673600",
            clock=lambda: NOW,
        )

        async def capture_request() -> None:
            try:
                await transport.execute(submit_request, credentials=credentials)
            finally:
                await transport.aclose()

        asyncio.run(capture_request())
        if len(captured) != 1:
            raise AssertionError("EXACT_SUBMIT_REQUEST_NOT_CAPTURED") from None
        auth_header = captured[0].headers["POLY_SIGNATURE"].encode("ascii")
        signed_body = captured[0].content
        subscription_frame = json.dumps(
            {
                "auth": {
                    "apiKey": api_key.decode("ascii"),
                    "passphrase": passphrase.decode("ascii"),
                    "secret": api_secret.decode("ascii"),
                },
                "type": "user",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        generated = cls(
            private_key=private_key,
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            auth_header=auth_header,
            signed_body=signed_body,
            subscription_frame=subscription_frame,
        )
        if len(set(generated.values)) != len(generated.values):
            raise AssertionError("SECRET_CANARIES_NOT_DISTINCT") from None
        return generated

    @property
    def values(self) -> tuple[bytes, ...]:
        return (
            self.private_key,
            self.api_key,
            self.api_secret,
            self.passphrase,
            self.auth_header,
            self.signed_body,
            self.subscription_frame,
        )


def _observable_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return str(value).encode("utf-8", errors="backslashreplace")


def _assert_canaries_absent(
    observables: list[object],
    canaries: _Canaries,
    *,
    additional: tuple[bytes, ...] = (),
) -> None:
    rendered = b"\x00".join(_observable_bytes(value) for value in observables)
    if any(canary in rendered for canary in (*canaries.values, *additional)):
        raise AssertionError("SECRET_CANARY_DETECTED") from None


def _exception_observables(error: BaseException) -> tuple[object, ...]:
    cause = error.__cause__
    context = error.__context__
    return (
        str(error),
        repr(error),
        str(cause),
        repr(cause),
        str(context),
        repr(context),
        "".join(traceback.format_exception(error)),
    )


def _captured_protocol_error(operation: object) -> SignerProtocolError:
    try:
        operation()  # type: ignore[operator]
    except SignerProtocolError as error:
        return error
    raise AssertionError("IPC_PROTOCOL_ERROR_NOT_RAISED") from None


def _captured_stream_error(operation: object) -> UserStreamProtocolError:
    try:
        operation()  # type: ignore[operator]
    except UserStreamProtocolError as error:
        return error
    raise AssertionError("USER_STREAM_ERROR_NOT_RAISED") from None


def test_named_auth_header_and_signed_body_canaries_are_actual_request_bytes() -> None:
    canaries = _Canaries.generate()
    account = Account.from_key(canaries.private_key)
    account_fingerprint = sha256(bytes.fromhex(account.address[2:])).hexdigest()
    grant = signer_capability_grant(account_fingerprint=account_fingerprint, now=NOW)
    intent = ExecutionIntent(
        **execution_intent_fields(
            account_fingerprint=account_fingerprint,
            capability_fingerprint=grant.digest,
        )
    )
    envelope = sign_order(intent, canaries.private_key, load_protocol_snapshot())
    request = SubmitOrderRequest(
        route=RouteKey.SUBMIT_ORDER,
        intent=intent,
        envelope=envelope,
    )
    credentials = ClobCredentials(
        address=account.address,
        api_key=canaries.api_key,
        secret=canaries.api_secret,
        passphrase=canaries.passphrase,
    )
    captured: list[httpx.Request] = []

    async def handler(http_request: httpx.Request) -> httpx.Response:
        captured.append(http_request)
        return httpx.Response(
            503,
            content=b'{"code":"offline-failure"}',
            headers={"content-type": "application/json"},
        )

    transport = HttpxPolymarketRestTransport._for_test(
        httpx.MockTransport(handler),
        timestamp=lambda: "1787673600",
        clock=lambda: NOW,
    )

    async def exercise() -> object:
        try:
            return await transport.execute(request, credentials=credentials)
        finally:
            await transport.aclose()

    result = asyncio.run(exercise())
    if len(captured) != 1:
        raise AssertionError("EXACT_SUBMIT_REQUEST_NOT_CAPTURED") from None
    actual_headers = tuple(
        captured[0].headers[name].encode("ascii")
        for name in (
            "POLY_ADDRESS",
            "POLY_SIGNATURE",
            "POLY_TIMESTAMP",
            "POLY_API_KEY",
            "POLY_PASSPHRASE",
        )
    )
    if canaries.auth_header not in actual_headers:
        raise AssertionError("AUTH_HEADER_CANARY_NOT_GENERATED") from None
    if canaries.signed_body != captured[0].content:
        raise AssertionError("SIGNED_BODY_CANARY_NOT_EXACT_REQUEST") from None
    assert result.code is RestCode.ORDER_OUTCOME_UNKNOWN


def test_runtime_canaries_never_cross_any_public_observable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canaries = _Canaries.generate()
    observables: list[object] = []
    submit_request, credentials = _submit_material(
        private_key=canaries.private_key,
        api_key=canaries.api_key,
        api_secret=canaries.api_secret,
        passphrase=canaries.passphrase,
    )

    read_handler_calls: list[ExecutionOperation] = []
    read_guard_calls: list[ExecutionOperation] = []

    def handler_failure(payload: object) -> SanitizedOperationResult:
        if isinstance(payload, SubmitOrderPayload) and (
            payload.intent != submit_request.intent or payload.envelope != submit_request.envelope
        ):
            raise AssertionError("SIGNED_SUBMIT_PAYLOAD_MISMATCH") from None
        operation = getattr(payload, "operation", None)
        if operation in {
            ExecutionOperation.READ_ORDERS,
            ExecutionOperation.READ_TRADES,
            ExecutionOperation.READ_ACCOUNT,
        }:
            read_handler_calls.append(operation)
        raise RuntimeError(canaries.signed_body.decode("ascii"))

    def read_guard(request: SignerRequest, _now: datetime) -> AuthorityDecision:
        read_guard_calls.append(request.operation)
        return AuthorityDecision(True, None, ())

    material = SecretMaterial(
        bytearray(canaries.private_key),
        bytearray(canaries.api_key),
        bytearray(canaries.api_secret),
        bytearray(canaries.passphrase),
    )
    signer = SignerService(
        secrets=material,
        authority_context_factory=lambda request, observed_at: authority_context(
            now=observed_at,
            account_fingerprint=request.account_fingerprint,
            account_scope_account_fingerprint=request.account_fingerprint,
            account_scope_evidence_hash=HASHES[11],
            verified_capability=verified_capability(
                account_fingerprint=request.account_fingerprint,
                capability_digest=request.authority_digest,
            ),
            evidence_hashes=tuple(
                sorted((HASHES[2], request.authority_digest, HASHES[9], HASHES[11]))
            ),
        ),
        read_guard=read_guard,
        handlers=SignerOperationHandlers(
            submit_order=handler_failure,
            cancel_order=handler_failure,
            heartbeat=handler_failure,
            read_orders=handler_failure,
            read_trades=handler_failure,
            read_account=handler_failure,
        ),
        clock=lambda: NOW,
    )
    malformed_signer_request = json.dumps(
        {
            "operation": canaries.signed_body.decode("ascii"),
            "opaque": canaries.auth_header.decode("ascii"),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    malformed_response = signer.handle_raw(malformed_signer_request)
    assert malformed_response.error_code == "IPC_OPERATION_NOT_ALLOWED"
    observables.extend(
        (
            malformed_response,
            repr(malformed_response),
            malformed_response.model_dump_json(),
            canonical_response_bytes(malformed_response),
        )
    )

    grant = signer_capability_grant(
        account_fingerprint=submit_request.intent.account_fingerprint,
        now=NOW,
    )
    mutation_request = SignerRequest(
        schema_version=1,
        request_id=UUID("33333333-3333-4333-8333-333333333333"),
        intent_id=submit_request.intent.intent_id,
        intent_fingerprint=submit_request.intent.intent_fingerprint,
        capability_digest=submit_request.intent.capability_fingerprint,
        authority_digest=grant.digest,
        authority_proof=SignerCapabilityProof(
            grant=grant,
            signature=b"cHVibGljLXNpZ25hdHVyZQ==",
        ),
        manifest_digest=MANIFEST_HASH,
        account_fingerprint=submit_request.intent.account_fingerprint,
        protocol_version=submit_request.intent.protocol_version,
        operation=ExecutionOperation.SUBMIT_ORDER,
        deadline=submit_request.intent.deadline,
        payload=SubmitOrderPayload(
            operation=ExecutionOperation.SUBMIT_ORDER,
            intent=submit_request.intent,
            envelope=submit_request.envelope,
        ),
    )
    mutation_response = signer.handle(mutation_request)
    mutation_response, mutation_bytes = signer._sanitized_response_bytes(mutation_response)
    assert mutation_response.error_code == "EXECUTION_UNAVAILABLE"
    observables.extend(
        (
            mutation_response,
            repr(mutation_response),
            mutation_response.model_dump_json(),
            mutation_bytes,
        )
    )

    read_requests = tuple(
        SignerRequest(
            schema_version=1,
            request_id=request_id,
            intent_id=UUID("22222222-2222-4222-8222-222222222222"),
            intent_fingerprint="1" * 64,
            capability_digest="2" * 64,
            authority_digest="0" * 64,
            manifest_digest="3" * 64,
            account_fingerprint=submit_request.intent.account_fingerprint,
            protocol_version="polymarket-clob-2026-08-25-v1",
            operation=operation,
            deadline=NOW + timedelta(seconds=5),
            payload=payload,
        )
        for request_id, operation, payload in (
            (
                UUID("11111111-1111-4111-8111-111111111111"),
                ExecutionOperation.READ_ORDERS,
                ReadOrdersPayload(
                    operation=ExecutionOperation.READ_ORDERS,
                    venue_order_id=None,
                ),
            ),
            (
                UUID("55555555-5555-4555-8555-555555555555"),
                ExecutionOperation.READ_TRADES,
                ReadTradesPayload(operation=ExecutionOperation.READ_TRADES),
            ),
            (
                UUID("66666666-6666-4666-8666-666666666666"),
                ExecutionOperation.READ_ACCOUNT,
                ReadAccountPayload(
                    operation=ExecutionOperation.READ_ACCOUNT,
                    signature_type=0,
                    asset_type="COLLATERAL",
                    token_id=None,
                ),
            ),
        )
    )
    mismatched_account = (
        "0" * 64 if submit_request.intent.account_fingerprint != "0" * 64 else "f" * 64
    )
    mismatched_request = read_requests[0].model_copy(
        update={
            "request_id": UUID("77777777-7777-4777-8777-777777777777"),
            "account_fingerprint": mismatched_account,
        }
    )
    mismatch_response = signer.handle(mismatched_request)
    assert mismatch_response.error_code == "ACCOUNT_FINGERPRINT_MISMATCH"
    assert read_guard_calls == []
    assert read_handler_calls == []
    observables.extend(
        (
            mismatch_response,
            repr(mismatch_response),
            mismatch_response.model_dump_json(),
            canonical_response_bytes(mismatch_response),
        )
    )
    for read_request in read_requests:
        assert parse_signer_request(canonical_request_bytes(read_request)) == read_request
        handler_response = signer.handle(read_request)
        handler_bytes = canonical_response_bytes(handler_response)
        assert handler_response.error_code == "HANDLER_FAILED"
        observables.extend(
            (
                handler_response,
                repr(handler_response),
                handler_response.model_dump_json(),
                handler_bytes,
            )
        )
        expected_operations = [request.operation for request in read_requests][
            : len(read_guard_calls)
        ]
        assert read_guard_calls == expected_operations
        assert read_handler_calls == expected_operations
    assert read_guard_calls == [request.operation for request in read_requests]
    assert read_handler_calls == [request.operation for request in read_requests]
    observables.append(repr(material))

    invalid_ipc = _captured_protocol_error(
        lambda: parse_signer_request(
            json.dumps(
                {
                    "operation": canaries.auth_header.decode("ascii"),
                    "opaque": canaries.signed_body.decode("ascii"),
                },
                separators=(",", ":"),
            ).encode()
        )
    )
    truncated_ipc = _captured_protocol_error(
        lambda: read_frame(
            io.BytesIO((len(canaries.signed_body) + 1).to_bytes(4, "big") + canaries.signed_body)
        )
    )
    assert str(invalid_ipc) == "IPC_OPERATION_NOT_ALLOWED"
    assert str(truncated_ipc) == "IPC_FRAME_TRUNCATED"
    observables.extend(_exception_observables(invalid_ipc))
    observables.extend(_exception_observables(truncated_ipc))

    route_requests = (
        submit_request,
        CancelOrderRequest(route=RouteKey.CANCEL_ORDER, order_id="offline-order"),
        HeartbeatRequest(route=RouteKey.HEARTBEAT, heartbeat_id="offline-heartbeat"),
        ReadOrderRequest(route=RouteKey.READ_ORDER, order_id="observer-order"),
    )
    captured_requests: list[httpx.Request] = []
    generated_header_values: list[tuple[str, bytes]] = []

    async def offline_rest_failures() -> dict[RouteKey, object]:
        async def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            generated_header_values.extend(
                (name, request.headers[name].encode("ascii"))
                for name in (
                    "POLY_ADDRESS",
                    "POLY_SIGNATURE",
                    "POLY_TIMESTAMP",
                    "POLY_API_KEY",
                    "POLY_PASSPHRASE",
                )
            )
            return httpx.Response(
                503,
                content=b"\x00".join((*canaries.values, request.content)),
                headers={"content-type": "application/json"},
            )

        transport = HttpxPolymarketRestTransport._for_test(
            httpx.MockTransport(handler),
            timestamp=lambda: "1787673600",
            clock=lambda: NOW,
        )
        try:
            return {
                request.route: await transport.execute(request, credentials=credentials)
                for request in route_requests
            }
        finally:
            await transport.aclose()

    rest_failures = asyncio.run(offline_rest_failures())
    assert {route: result.code for route, result in rest_failures.items()} == {
        RouteKey.SUBMIT_ORDER: RestCode.ORDER_OUTCOME_UNKNOWN,
        RouteKey.CANCEL_ORDER: RestCode.CANCEL_OUTCOME_UNKNOWN,
        RouteKey.HEARTBEAT: RestCode.HEARTBEAT_OUTCOME_UNKNOWN,
        RouteKey.READ_ORDER: RestCode.READ_FAILED,
    }
    assert len(captured_requests) == len(route_requests)
    submit_http_request = captured_requests[0]
    if submit_http_request.content != canaries.signed_body:
        raise AssertionError("SIGNED_BODY_CANARY_NOT_EXACT_REQUEST") from None
    if submit_http_request.headers["POLY_SIGNATURE"].encode("ascii") != canaries.auth_header:
        raise AssertionError("AUTH_HEADER_CANARY_NOT_GENERATED") from None
    assert {name for name, _value in generated_header_values} == {
        "POLY_ADDRESS",
        "POLY_SIGNATURE",
        "POLY_TIMESTAMP",
        "POLY_API_KEY",
        "POLY_PASSPHRASE",
    }
    observables.extend(
        observable
        for result in rest_failures.values()
        for observable in (result, repr(result), result.model_dump_json())
    )
    observables.append(repr(credentials))

    malformed_stream = _captured_stream_error(
        lambda: parse_user_event(
            json.dumps(
                {
                    "frame": canaries.subscription_frame.decode("utf-8"),
                    "body": canaries.signed_body.decode("ascii"),
                },
                separators=(",", ":"),
            ).encode(),
            receipt_time=NOW,
        )
    )

    class FailingSubscriptionTransport:
        def send_user_subscription(self, frame: bytes) -> None:
            if sha256(frame).digest() != sha256(canaries.subscription_frame).digest():
                raise AssertionError("SUBSCRIPTION_FRAME_MISMATCH") from None
            raise RuntimeError(frame.decode("utf-8"))

    stream_material = SecretMaterial(
        bytearray(canaries.private_key),
        bytearray(canaries.api_key),
        bytearray(canaries.api_secret),
        bytearray(canaries.passphrase),
    )
    session = user_stream_module._SignerUserStreamSession(
        secrets=stream_material,
        transport=FailingSubscriptionTransport(),
        read_guard=lambda _now: AuthorityDecision(True, None, ()),
    )
    subscription_failure = _captured_stream_error(lambda: session.open(observed_at=NOW))
    assert str(malformed_stream) == "USER_STREAM_PROTOCOL_ERROR"
    assert str(subscription_failure) == "USER_STREAM_PROTOCOL_ERROR"
    observables.extend(_exception_observables(malformed_stream))
    observables.extend(_exception_observables(subscription_failure))
    observables.append(repr(session))

    secret_database = tmp_path / "secret-boundary.duckdb"
    secret_store = PredictionMarketStore(secret_database)
    unsafe_intent = ExecutionIntent.model_construct(
        **submit_request.intent.model_dump(),
        raw_secret=canaries.auth_header.decode("ascii"),
    )
    secret_store.append_execution_intent(unsafe_intent)
    secret_store.append_signed_order_envelope(submit_request.envelope)
    stored_intent_json = secret_store._connection.execute(
        "SELECT record_json FROM execution_intents WHERE intent_id = ?",
        [submit_request.intent.intent_id],
    ).fetchone()[0]
    stored_envelope_json = secret_store._connection.execute(
        "SELECT record_json FROM signed_order_envelopes WHERE intent_id = ?",
        [submit_request.intent.intent_id],
    ).fetchone()[0]
    database_export = json.dumps(
        {
            "intent": json.loads(stored_intent_json),
            "signed_order_envelope": json.loads(stored_envelope_json),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    secret_store.close()
    observables.extend(
        (
            stored_intent_json,
            stored_envelope_json,
            database_export,
            secret_database.read_bytes(),
        )
    )

    dashboard_database = tmp_path / "dashboard.duckdb"
    dashboard_store = PredictionMarketStore(dashboard_database)
    dashboard_store.close()
    snapshot = build_prediction_dashboard_snapshot(dashboard_database, now=NOW)
    dashboard = PredictionDashboardApplication(
        dashboard_database,
        clock=lambda: NOW,
        snapshot_provider=lambda: snapshot,
    )
    snapshot_response = dashboard.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1")
    revision_frame = _sse_event_frame(
        DashboardRevision(
            schema_version=1,
            event_id="1",
            revision_id=snapshot.revision_id,
            as_of=snapshot.as_of,
            emitted_at=NOW,
            changed_domains=(DashboardDomain.OVERVIEW,),
        )
    )
    observables.extend(
        (
            snapshot,
            snapshot.model_dump_json(),
            snapshot_response.body,
            snapshot_response.headers,
            revision_frame,
        )
    )

    caplog.set_level("ERROR")

    def failing_snapshot() -> object:
        raise RuntimeError(canaries.signed_body.decode("ascii"))

    failed_dashboard = PredictionDashboardApplication(
        dashboard_database,
        clock=lambda: NOW,
        snapshot_provider=failing_snapshot,  # type: ignore[arg-type]
    )
    dashboard_failure = failed_dashboard.respond(
        "GET", "/api/v1/predictions-dashboard", "127.0.0.1"
    )
    assert dashboard_failure.status.value == 503
    observables.extend((dashboard_failure.body, dashboard_failure.headers, caplog.text))

    output = io.StringIO()
    errors = io.StringIO()
    cli_code = _run_polymarket_conformance(
        argparse.Namespace(
            fixtures=tmp_path / canaries.auth_header.decode("ascii"),
            db=tmp_path / "cli.duckdb",
            output_format="json",
        ),
        stream=output,
        error_stream=errors,
    )
    assert cli_code == 64
    observables.extend((output.getvalue(), errors.getvalue()))

    browser_assets = {
        path.name: path.read_bytes()
        for path in WEB_ASSETS_ROOT.iterdir()
        if path.is_file() and path.suffix in {".html", ".css", ".js"}
    }
    assert set(browser_assets) == {
        "index.html",
        "app.css",
        "app.js",
        "api.js",
        "stream.js",
        "store.js",
        "charts.js",
        "views.js",
    }
    observables.extend(browser_assets.values())

    secret_header_values = tuple(
        {
            value
            for name, value in generated_header_values
            if name in {"POLY_SIGNATURE", "POLY_API_KEY", "POLY_PASSPHRASE"}
        }
    )
    _assert_canaries_absent(observables, canaries, additional=secret_header_values)
    signer.close()
    stream_material.close()


def test_runtime_only_secret_scan_is_documented_without_example_values() -> None:
    assert RUNBOOK.is_file(), "SECRET_SCAN_RUNBOOK_MISSING"
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "runtime-only" in text
    assert "canaries" in text
    assert "never persisted" in text
