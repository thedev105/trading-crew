from __future__ import annotations

import argparse
import io
import json
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import polytrading.predictions.pilot.runtime as runtime_module
from polytrading.predictions.cli import add_predictions_subcommands
from polytrading.predictions.execution.models import ExecutionIntent, ExecutionOperation
from polytrading.predictions.pilot.capabilities import PilotCapabilityIssuer
from polytrading.predictions.pilot.execution_port import ExecutionEvidence, GeoblockEvidence
from polytrading.predictions.pilot.models import PILOT_CEILINGS
from polytrading.predictions.pilot.passkeys import RP_ID
from polytrading.predictions.pilot.runtime import (
    KilledPilotServices,
    PilotPosture,
    PilotRuntimeError,
    _mutation_evidence_for,
    build_launch_runtime,
    build_pilot_runtime,
)
from polytrading.predictions.pilot.selector import PilotAccountState
from polytrading.predictions.pilot.server import (
    SESSION_COOKIE,
    PilotRequest,
    PilotRequestError,
)
from polytrading.predictions.pilot.services import LivePilotServices
from polytrading.predictions.pilot.signer_bootstrap import (
    SignerBootstrapError,
    SignerChannel,
    SignerServiceFactory,
)
from polytrading.predictions.polymarket_execution.ipc import (
    GeoblockEvidenceResult,
    IdentityResult,
    SanitizedOperationResult,
    SignerCapabilityProof,
    SignerKillResult,
    SignerResponse,
    canonical_response_bytes,
    parse_signer_request,
    write_frame,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
)
from polytrading.predictions.polymarket_execution.routes import (
    AllowanceEntry,
    BalanceAllowancePayload,
    OrderReadPayload,
    OrdersReadPayload,
    RestCode,
    RouteKey,
    TradesReadPayload,
)
from polytrading.predictions.polymarket_execution.secrets import SecretStoreError
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.execution_helpers import execution_intent_fields
from tests.predictions.pilot_helpers import signer_capability_grant
from tests.predictions.test_execution_authority import ELIGIBLE_MANIFEST

PORT = 8788
NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "pilot.duckdb"
    store = PredictionMarketStore(path)
    store.close()
    return path


@pytest.fixture(autouse=True)
def reviewed_darwin_secret_store(monkeypatch: pytest.MonkeyPatch) -> None:
    def open_store(*, platform: str) -> object:
        if platform == "darwin":
            return object()
        raise SecretStoreError("SECRET_STORE_UNAVAILABLE")

    monkeypatch.setattr(runtime_module, "open_pilot_secret_store", open_store)


def test_a_missing_database_never_starts_a_server(tmp_path: Path) -> None:
    with pytest.raises(PilotRuntimeError) as raised:
        build_pilot_runtime(tmp_path / "absent.duckdb", PORT)
    assert raised.value.code == "PILOT_DATABASE_MISSING"


def test_an_unsupported_platform_fails_closed(database: Path) -> None:
    with pytest.raises(PilotRuntimeError) as raised:
        build_pilot_runtime(database, PORT, platform="freebsd")
    assert raised.value.code == "PILOT_SECRET_STORE_UNAVAILABLE"


def test_linux_runtime_uses_the_platform_factory_and_remains_killed(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[str] = []
    store = object()
    monkeypatch.setattr(
        runtime_module,
        "open_pilot_secret_store",
        lambda *, platform: observed.append(platform) or store,
    )

    built = build_pilot_runtime(database, PORT, platform="linux")
    try:
        assert built.posture.kill_engaged is True
        assert built.posture.secret_store_available is True
        assert observed == ["linux"]
    finally:
        built.close()


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_an_invalid_port_fails_closed(database: Path, port: int) -> None:
    with pytest.raises(PilotRuntimeError) as raised:
        build_pilot_runtime(database, port)
    assert raised.value.code == "PILOT_ORIGIN_INVALID"


def test_a_stale_schema_fails_closed(tmp_path: Path) -> None:
    import duckdb

    path = tmp_path / "stale.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE prediction_raw_envelopes (event_id VARCHAR)")

    with pytest.raises(PilotRuntimeError) as raised:
        build_pilot_runtime(path, PORT, platform="darwin")
    assert raised.value.code == "PILOT_DATABASE_SCHEMA_STALE"


def test_a_launched_pilot_starts_killed_on_the_reviewed_checkpoint(database: Path) -> None:
    runtime = build_pilot_runtime(database, PORT, platform="darwin")
    try:
        assert runtime.posture == PilotPosture(
            kill_engaged=True,
            protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
            rp_id=RP_ID,
            origin=f"http://localhost:{PORT}",
            secret_store_available=True,
        )
        assert runtime.application.origin == f"http://localhost:{PORT}"
    finally:
        runtime.close()


def test_the_runtime_opens_its_database_read_only(database: Path) -> None:
    runtime = build_pilot_runtime(database, PORT, platform="darwin")
    try:
        assert runtime.store._read_only is True
    finally:
        runtime.close()


def test_launch_falls_back_to_posture_only_when_signer_bootstrap_fails(
    database: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable(_service_factory: SignerServiceFactory) -> object:
        raise SignerBootstrapError("SECRET_ITEM_MISSING")

    runtime = build_launch_runtime(database, PORT, platform="darwin", bootstrap=unavailable)  # type: ignore[arg-type]
    try:
        assert isinstance(runtime.application._services, KilledPilotServices)
        diagnostics = capsys.readouterr().err
        assert "pilot: signer unavailable; serving posture only" in diagnostics
        assert "SECRET_ITEM_MISSING" not in diagnostics
    finally:
        runtime.close()


def test_launch_composes_live_services_when_the_signer_bootstraps(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    channel, bootstrap = live_launch_bootstrap()

    runtime = build_launch_runtime(
        database, PORT, platform="darwin", bootstrap=bootstrap, now=lambda: NOW
    )
    try:
        assert isinstance(runtime.application._services, LivePilotServices)
        readiness = runtime.application._services.readiness()
        assert readiness["kill_engaged"] is True
        assert "KILL_ENGAGED" in readiness["blockers"]
    finally:
        runtime.close()
    assert channel.request_stream.closed
    assert channel.response_stream.closed


def test_operator_stop_sends_signer_kill_and_parent_kill_survives_link_failure(
    database: Path,
) -> None:
    channel, bootstrap = live_launch_bootstrap(
        fail_operation=ExecutionOperation.SIGNER_KILL,
    )
    runtime = build_launch_runtime(
        database, PORT, platform="darwin", bootstrap=bootstrap, now=lambda: NOW
    )
    try:
        services = runtime.application.services
        assert isinstance(services, LivePilotServices)

        stopped = services.stop({})

        assert stopped == {"kill_engaged": True, "reason": "OPERATOR_STOP"}
        assert services.readiness()["kill_engaged"] is True
        assert channel.request_stream._channel.requests[-1].operation is (  # type: ignore[attr-defined]
            ExecutionOperation.SIGNER_KILL
        )
    finally:
        runtime.close()


def test_terminal_presence_sends_signer_kill_before_returning(database: Path) -> None:
    channel, bootstrap = live_launch_bootstrap()
    runtime = build_launch_runtime(
        database, PORT, platform="darwin", bootstrap=bootstrap, now=lambda: NOW
    )
    try:
        services = runtime.application.services
        assert isinstance(services, LivePilotServices)

        presence = services.presence({"kind": "PAGE_CLOSED"})

        assert presence == {
            "presence_state": "TERMINAL",
            "kill_reason": "PAGE_CLOSED",
            "kill_engaged": True,
        }
        assert channel.request_stream._channel.requests[-1].operation is (  # type: ignore[attr-defined]
            ExecutionOperation.SIGNER_KILL
        )
    finally:
        runtime.close()


class _LaunchSignerChannel:
    """Answer the production signer link in memory, without a venue or secret store."""

    def __init__(
        self,
        *,
        ambiguous: bool = False,
        fail_operation: ExecutionOperation | None = None,
    ) -> None:
        self.requests: list[Any] = []
        self.responses = io.BytesIO()
        self.request_stream = _LaunchRequestStream(self)
        self.ambiguous = ambiguous
        self.fail_operation = fail_operation

    def answer(self, frame: bytes) -> None:
        request = parse_signer_request(frame)
        self.requests.append(request)
        if request.operation is self.fail_operation:
            raise OSError("secret signer read detail")
        if request.operation is ExecutionOperation.DESCRIBE_IDENTITY:
            result: object = IdentityResult(
                operation=ExecutionOperation.DESCRIBE_IDENTITY,
                account_fingerprint="a" * 64,
                wallet_fingerprint="b" * 64,
            )
        elif request.operation is ExecutionOperation.READ_ACCOUNT:
            result = _read_result(
                ExecutionOperation.READ_ACCOUNT,
                RouteKey.READ_BALANCE_ALLOWANCE,
                BalanceAllowancePayload(
                    kind="BALANCE_ALLOWANCE",
                    balance="200",
                    allowances=(AllowanceEntry(address="0x" + "11" * 20, amount="200"),),
                ),
            )
        elif request.operation is ExecutionOperation.READ_ORDERS:
            items = (_open_order(),) if self.ambiguous else ()
            result = _read_result(
                ExecutionOperation.READ_ORDERS,
                RouteKey.READ_OPEN_ORDERS,
                OrdersReadPayload(kind="ORDERS_READ", items=items),
            )
        elif request.operation is ExecutionOperation.READ_TRADES:
            result = _read_result(
                ExecutionOperation.READ_TRADES,
                RouteKey.READ_TRADES,
                TradesReadPayload(kind="TRADES_READ", items=()),
            )
        elif request.operation is ExecutionOperation.READ_GEOBLOCK:
            result = GeoblockEvidenceResult(
                operation=ExecutionOperation.READ_GEOBLOCK,
                allowed=True,
                evidence_hash="9" * 64,
                observed_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
            )
        elif request.operation is ExecutionOperation.SIGNER_KILL:
            result = SignerKillResult(
                operation=ExecutionOperation.SIGNER_KILL,
                result_code="SIGNER_KILL_ENGAGED",
            )
        else:  # pragma: no cover - a regression exposes the unexpected request in the assertion
            raise AssertionError(request.operation)
        response = SignerResponse.accepted(request.request_id, result)  # type: ignore[arg-type]
        position = self.responses.tell()
        self.responses.seek(0, io.SEEK_END)
        write_frame(self.responses, canonical_response_bytes(response))
        self.responses.seek(position)


class _LaunchRequestStream(io.BytesIO):
    def __init__(self, channel: _LaunchSignerChannel) -> None:
        super().__init__()
        self._channel = channel
        self._pending = bytearray()

    def write(self, payload: bytes) -> int:
        self._pending.extend(payload)
        while len(self._pending) >= 4:
            size = int.from_bytes(self._pending[:4], "big")
            if len(self._pending) < size + 4:
                break
            frame = bytes(self._pending[4 : size + 4])
            del self._pending[: size + 4]
            self._channel.answer(frame)
        return len(payload)

    def flush(self) -> None:
        return None


def _read_result(
    operation: ExecutionOperation,
    route: RouteKey,
    payload: object,
) -> SanitizedOperationResult:
    return SanitizedOperationResult(
        operation=operation,  # type: ignore[arg-type]
        result_code=RestCode.READ_OK,
        evidence_hashes=("1" * 64,),
        route=route,
        observed_at=NOW,
        raw_body_hash="1" * 64,
        attempts=1,
        recovery_required=False,
        kill_required=False,
        public_payload=payload,  # type: ignore[arg-type]
    )


def _open_order() -> OrderReadPayload:
    return OrderReadPayload(
        kind="ORDER_READ",
        id="order-1",
        market="market-1",
        asset_id="217426",
        maker_address="0x" + "11" * 20,
        side="BUY",
        price="0.40",
        original_size="10",
        size_matched="0",
        outcome="YES",
        order_type="GTC",
        status="LIVE",
        associate_trades=(),
        created_at="1788177600",
        expiration="0",
    )


def live_launch_bootstrap(
    *,
    ambiguous: bool = False,
    fail_operation: ExecutionOperation | None = None,
) -> tuple[SignerChannel, Any]:
    fake = _LaunchSignerChannel(
        ambiguous=ambiguous,
        fail_operation=fail_operation,
    )
    channel = SignerChannel(
        request_stream=fake.request_stream,
        response_stream=fake.responses,
        child_pid=None,
        credentials_present=True,
    )

    def bootstrap(_service_factory: SignerServiceFactory) -> SignerChannel:
        return channel

    return channel, bootstrap


def test_launch_composes_live_session_from_signer_account_state(database: Path) -> None:
    channel, bootstrap = live_launch_bootstrap()

    runtime = build_launch_runtime(
        database, PORT, platform="darwin", bootstrap=bootstrap, now=lambda: NOW
    )
    try:
        services = runtime.application.services
        assert isinstance(services, LivePilotServices)
        assert services._environment.account_state().collateral_usd == Decimal("200")
        opened = runtime.application.respond(
            PilotRequest(
                method="POST",
                target="/api/v1/pilot/session",
                host=f"localhost:{PORT}",
                received_at=NOW,
                origin=f"http://localhost:{PORT}",
            )
        )
        token = opened.set_cookie.split(";", 1)[0].split("=", 1)[1]
        response = runtime.application.respond(
            PilotRequest(
                method="GET",
                target="/api/v1/pilot/live-session",
                host=f"localhost:{PORT}",
                received_at=NOW,
                cookies={SESSION_COOKIE: (token,)},
            )
        )
        assert response.status is HTTPStatus.OK
        assert json.loads(response.body)["session"]["active"] is False
    finally:
        runtime.close()
    assert channel.request_stream.closed
    assert channel.response_stream.closed


def test_launch_wires_geoblock_evidence_into_mutation_evidence_without_trading(
    database: Path,
) -> None:
    channel, bootstrap = live_launch_bootstrap()
    runtime = build_launch_runtime(
        database, PORT, platform="darwin", bootstrap=bootstrap, now=lambda: NOW
    )
    try:
        services = runtime.application.services
        assert isinstance(services, LivePilotServices)
        provider = services._environment.geoblock_provider
        assert provider is not None
        geoblock = provider()
        assert geoblock == GeoblockEvidence(
            allowed=True,
            evidence_hash="9" * 64,
            expires_at=NOW + timedelta(minutes=5),
        )
        grant = signer_capability_grant(account_fingerprint="a" * 64, now=NOW)
        proof = SignerCapabilityProof(
            grant=grant,
            signature=b64encode(b"production-launch-proof"),
        )
        target = ExecutionIntent(
            **execution_intent_fields(
                account_fingerprint="a" * 64,
                capability_fingerprint=grant.plan_hash,
                created_at=NOW,
                deadline=NOW + timedelta(seconds=30),
                protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
            )
        )
        account = PilotAccountState(
            account_fingerprint="a" * 64,
            wallet_fingerprint="b" * 64,
            collateral_usd=Decimal("200"),
            allowance_usd=Decimal("200"),
            kill_engaged=False,
            observed_at=NOW,
        )
        evidence = ExecutionEvidence(
            manifest=ELIGIBLE_MANIFEST,
            account=account,
            geoblock_allowed=geoblock.allowed,
            geoblock_evidence_hash=geoblock.evidence_hash,
            geoblock_expires_at=geoblock.expires_at,
            account_scope_evidence_hash="a" * 64,
            account_scope_expires_at=NOW + timedelta(minutes=1),
            kill_engaged=False,
            operator_present=True,
            reconciliation_hash="8" * 64,
            reconciliation_observed_at=NOW,
        )
        assert runtime.issuer is not None
        signed = _mutation_evidence_for(
            issuer=runtime.issuer,
            intent=target,
            operation=ExecutionOperation.SUBMIT_ORDER,
            proof=proof,
            request_id=UUID("11111111-1111-4111-8111-111111111111"),
            current_evidence=lambda: evidence,
            clock=lambda: NOW,
        )
        assert signed.evidence.geoblock_allowed is True
        assert signed.evidence.geoblock_evidence_hash == "9" * 64
        assert signed.evidence.geoblock_expires_at == NOW + timedelta(minutes=5)
    finally:
        runtime.close()

    operations = [request.operation for request in channel.request_stream._channel.requests]
    assert ExecutionOperation.READ_GEOBLOCK in operations
    assert {
        ExecutionOperation.SIGN_ORDER,
        ExecutionOperation.SUBMIT_ORDER,
        ExecutionOperation.CANCEL_ORDER,
    }.isdisjoint(operations)


def test_launch_remains_killed_when_reconciliation_is_not_complete(database: Path) -> None:
    _channel, bootstrap = live_launch_bootstrap(ambiguous=True)

    runtime = build_launch_runtime(
        database, PORT, platform="darwin", bootstrap=bootstrap, now=lambda: NOW
    )
    try:
        services = runtime.application.services
        assert isinstance(services, LivePilotServices)
        readiness = services.readiness()
        assert readiness["kill_engaged"] is True
        assert "RECONCILIATION_INCOMPLETE" in readiness["blockers"]
    finally:
        runtime.close()


def test_reconciliation_exception_closes_the_writer_before_posture_fallback(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, bootstrap = live_launch_bootstrap()

    def fail_composition(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("secret reconciliation detail")

    monkeypatch.setattr(
        "polytrading.predictions.pilot.runtime.compose_pilot_environment",
        fail_composition,
    )

    runtime = build_launch_runtime(
        database, PORT, platform="darwin", bootstrap=bootstrap, now=lambda: NOW
    )
    try:
        assert isinstance(runtime.application.services, KilledPilotServices)
        assert runtime.store._read_only is True
    finally:
        runtime.close()
    assert channel.request_stream.closed
    assert channel.response_stream.closed


def test_signer_read_exception_serves_only_killed_posture(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuers: list[PilotCapabilityIssuer] = []

    class TrackingIssuer(PilotCapabilityIssuer):
        def __init__(self, *, key_id: str) -> None:
            super().__init__(key_id=key_id)
            issuers.append(self)

    monkeypatch.setattr(
        "polytrading.predictions.pilot.runtime.PilotCapabilityIssuer",
        TrackingIssuer,
    )
    channel, bootstrap = live_launch_bootstrap(
        fail_operation=ExecutionOperation.READ_ORDERS,
    )

    runtime = build_launch_runtime(
        database, PORT, platform="darwin", bootstrap=bootstrap, now=lambda: NOW
    )
    try:
        assert isinstance(runtime.application.services, KilledPilotServices)
        assert runtime.store._read_only is True
    finally:
        runtime.close()
    assert channel.request_stream.closed
    assert channel.response_stream.closed
    assert len(issuers) == 1
    assert issuers[0].closed is True


def services() -> KilledPilotServices:
    posture = PilotPosture(
        kill_engaged=True,
        protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
        rp_id=RP_ID,
        origin=f"http://localhost:{PORT}",
        secret_store_available=True,
    )
    return KilledPilotServices(posture, ceilings=PILOT_CEILINGS.model_dump(mode="json"))


def test_a_killed_pilot_reports_posture_without_granting_authority() -> None:
    readiness = services().readiness()
    policy = services().policy()

    assert readiness["kill_engaged"] is True
    assert readiness["live_authority"] is False
    assert policy["ceilings"]["order_notional"] == "10"
    assert policy["requested_limits"] is None
    assert services().opportunities()["opportunities"] == []
    assert services().live_session()["session"] is None


@pytest.mark.parametrize(
    "route",
    [
        "update_policy",
        "register_options",
        "register_verify",
        "auth_options",
        "provision_credentials",
        "activate",
        "authorize",
        "presence",
        "stop",
        "clear_kill",
    ],
)
def test_every_mutating_route_refuses_while_killed(route: str) -> None:
    with pytest.raises(PilotRequestError) as raised:
        getattr(services(), route)({})
    assert raised.value.code == "PILOT_KILL_ENGAGED"
    assert raised.value.status is HTTPStatus.CONFLICT


def test_a_killed_pilot_still_answers_reads_over_the_control_plane(database: Path) -> None:
    runtime = build_pilot_runtime(database, PORT, platform="darwin")
    try:
        application = runtime.application
        opened = application.respond(
            PilotRequest(
                method="POST",
                target="/api/v1/pilot/session",
                host=f"localhost:{PORT}",
                received_at=NOW,
                origin=f"http://localhost:{PORT}",
            )
        )
        token = opened.set_cookie.split(";", 1)[0].split("=", 1)[1]
        readiness = application.respond(
            PilotRequest(
                method="GET",
                target="/api/v1/pilot/readiness",
                host=f"localhost:{PORT}",
                received_at=NOW,
                cookies={SESSION_COOKIE: (token,)},
            )
        )
        assert readiness.status is HTTPStatus.OK
        assert b'"kill_engaged":true' in readiness.body
    finally:
        runtime.close()


def test_the_pilot_cli_accepts_only_a_database_and_a_port() -> None:
    parser = argparse.ArgumentParser()
    add_predictions_subcommands(parser.add_subparsers(dest="command", required=True))

    arguments = parser.parse_args(
        ["predictions", "pilot", "polymarket", "--db", "pilot.duckdb", "--port", "8788"]
    )

    assert arguments.predictions_command == "pilot"
    assert arguments.predictions_pilot_command == "polymarket"
    assert vars(arguments).keys() >= {"db", "port"}
    for forbidden in (
        ["--api-key", "value"],
        ["--private-key", "value"],
        ["--activate"],
        ["--clear-kill"],
        ["--capability", "value"],
        ["--order", "value"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "predictions",
                    "pilot",
                    "polymarket",
                    "--db",
                    "pilot.duckdb",
                    "--port",
                    "8788",
                    *forbidden,
                ]
            )
