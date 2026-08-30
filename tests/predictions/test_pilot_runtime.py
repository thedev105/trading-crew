from __future__ import annotations

import argparse
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import pytest

from polytrading.predictions.cli import add_predictions_subcommands
from polytrading.predictions.pilot.models import PILOT_CEILINGS
from polytrading.predictions.pilot.passkeys import RP_ID
from polytrading.predictions.pilot.runtime import (
    KilledPilotServices,
    PilotPosture,
    PilotRuntimeError,
    build_pilot_runtime,
)
from polytrading.predictions.pilot.server import (
    SESSION_COOKIE,
    PilotRequest,
    PilotRequestError,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
)
from polytrading.predictions.storage.store import PredictionMarketStore

PORT = 8788
NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "pilot.duckdb"
    store = PredictionMarketStore(path)
    store.close()
    return path


def test_a_missing_database_never_starts_a_server(tmp_path: Path) -> None:
    with pytest.raises(PilotRuntimeError) as raised:
        build_pilot_runtime(tmp_path / "absent.duckdb", PORT)
    assert raised.value.code == "PILOT_DATABASE_MISSING"


def test_an_unsupported_platform_fails_closed(database: Path) -> None:
    with pytest.raises(PilotRuntimeError) as raised:
        build_pilot_runtime(database, PORT, platform="linux")
    assert raised.value.code == "PILOT_SECRET_STORE_UNAVAILABLE"


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
        build_pilot_runtime(path, PORT)
    assert raised.value.code == "PILOT_DATABASE_SCHEMA_STALE"


def test_a_launched_pilot_starts_killed_on_the_reviewed_checkpoint(database: Path) -> None:
    runtime = build_pilot_runtime(database, PORT)
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
    runtime = build_pilot_runtime(database, PORT)
    try:
        assert runtime.store._read_only is True
    finally:
        runtime.close()


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
    runtime = build_pilot_runtime(database, PORT)
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
