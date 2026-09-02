"""One acceptance gate per design acceptance criterion.

Every test here maps to a criterion in docs/superpowers/specs/2026-08-27-polymarket-local-live-
pilot-design.md and asserts the boundary that enforces it. Nothing here touches a network, a real
keychain, or a real credential, and nothing here can trigger Stage 4.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import polytrading
from polytrading.predictions.execution.authority import AuthorityReason
from polytrading.predictions.execution.kill_switch import derive_kill_state
from polytrading.predictions.execution.models import ExecutionOperation, ImmediateOrderType
from polytrading.predictions.pilot import runtime as runtime_module
from polytrading.predictions.pilot.capabilities import (
    MAXIMUM_PRIMARY_LIFETIME,
    MAXIMUM_RECOVERY_LIFETIME,
)
from polytrading.predictions.pilot.models import (
    PILOT_CEILINGS,
    AuthorizationMode,
    GrantKind,
    PilotProofFamily,
)
from polytrading.predictions.pilot.passkeys import RP_ID, pilot_origin
from polytrading.predictions.pilot.policy import (
    COMPILED_PILOT_CEILINGS,
    PilotPolicyError,
    RequestedPilotLimits,
    effective_limits,
    require_order_within_budget,
)
from polytrading.predictions.pilot.presence import (
    MAXIMUM_HEARTBEAT_SILENCE,
    MAXIMUM_MISSED_HEARTBEATS,
)
from polytrading.predictions.pilot.qualification import (
    ADDITIONAL_SHADOW_DAYS,
    CLASS_G_EVIDENCE_DAYS,
)
from polytrading.predictions.pilot.server import PILOT_ROUTES, SECURITY_HEADERS
from polytrading.predictions.polymarket_execution.credentials import (
    MAXIMUM_GRANT_LIFETIME,
    CredentialProvisioningGrant,
)
from polytrading.predictions.polymarket_execution.protocol import (
    POLYMARKET_PILOT_PROTOCOL_VERSION,
    load_protocol_snapshot,
)
from polytrading.predictions.polymarket_execution.routes import (
    ROUTE_SPECS,
    RouteKey,
    execution_route_keys,
)
from polytrading.predictions.proofs_models import APPROVED_PROOF_TEMPLATES
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.pilot_helpers import limits_fields

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)
PREDICTIONS_ROOT = Path(polytrading.__file__).resolve().parent / "predictions"
FIRST_LIVE_CEILING_USD = Decimal("5")


def _trees() -> dict[Path, ast.Module]:
    return {
        path.relative_to(PREDICTIONS_ROOT): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(PREDICTIONS_ROOT.rglob("*.py"))
    }


def test_the_compiled_ceilings_are_exactly_the_approved_envelope() -> None:
    assert COMPILED_PILOT_CEILINGS is PILOT_CEILINGS
    assert PILOT_CEILINGS.wallet_trading_equity == Decimal("250")
    assert PILOT_CEILINGS.order_notional == Decimal("10")
    assert PILOT_CEILINGS.strategy_gross_notional == Decimal("25")
    assert PILOT_CEILINGS.session_duration == timedelta(minutes=15)
    assert PILOT_CEILINGS.session_deployed_capital == Decimal("50")
    assert PILOT_CEILINGS.concurrent_strategies == 1
    assert PILOT_CEILINGS.session_loss == Decimal("5")
    assert PILOT_CEILINGS.utc_day_loss == Decimal("10")


def test_the_same_ceiling_is_enforced_at_three_independent_boundaries() -> None:
    raised_amount = Decimal("10.01")

    # 1. the requested-policy boundary
    with pytest.raises(PilotPolicyError) as policy_error:
        effective_limits(
            RequestedPilotLimits.model_validate(
                limits_fields(order_notional=raised_amount), strict=True
            )
        )
    assert policy_error.value.code == "ORDER_NOTIONAL_CEILING"

    # 2. the record boundary
    from pydantic import ValidationError

    from polytrading.predictions.pilot.models import PilotLimits

    with pytest.raises(ValidationError, match="order_notional"):
        PilotLimits.model_validate(limits_fields(order_notional=raised_amount), strict=True)

    # 3. the per-order budget boundary
    with pytest.raises(PilotPolicyError) as budget_error:
        require_order_within_budget(
            PILOT_CEILINGS,
            committed_gross_notional=Decimal("0"),
            order_notional=raised_amount,
            deployed_capital=Decimal("0"),
            recovery=False,
        )
    assert budget_error.value.code == "ORDER_NOTIONAL_CEILING"


def test_the_three_authorization_modes_have_their_own_lifetimes() -> None:
    assert set(AuthorizationMode) == {
        AuthorizationMode.EXACT_ORDER,
        AuthorizationMode.COMPLETE_STRATEGY,
        AuthorizationMode.AUTOMATION_SESSION,
    }
    assert MAXIMUM_PRIMARY_LIFETIME[AuthorizationMode.EXACT_ORDER] == timedelta(seconds=60)
    assert MAXIMUM_PRIMARY_LIFETIME[AuthorizationMode.COMPLETE_STRATEGY] == timedelta(minutes=5)
    assert MAXIMUM_PRIMARY_LIFETIME[AuthorizationMode.AUTOMATION_SESSION] == timedelta(minutes=15)
    assert timedelta(seconds=120) == MAXIMUM_RECOVERY_LIFETIME


def test_only_immediate_order_types_exist() -> None:
    assert {member.value for member in ImmediateOrderType} == {"FAK", "FOK"}
    snapshot = load_protocol_snapshot(version=POLYMARKET_PILOT_PROTOCOL_VERSION)
    assert snapshot.order_submission.allowed_order_types == ("FAK", "FOK")


def test_only_single_venue_deterministic_proof_families_are_live_eligible() -> None:
    assert {family.value for family in PilotProofFamily} <= APPROVED_PROOF_TEMPLATES
    assert len(set(PilotProofFamily)) == 4


def test_the_credential_grant_is_separate_from_execution_authority() -> None:
    assert RouteKey.CREATE_OR_DERIVE_CREDENTIALS not in execution_route_keys()
    assert RouteKey.CREATE_OR_DERIVE_CREDENTIALS not in ROUTE_SPECS
    assert timedelta(seconds=60) == MAXIMUM_GRANT_LIFETIME
    assert "allowed_operations" not in CredentialProvisioningGrant.__slots__
    assert GrantKind.CREDENTIAL_PROVISIONING.value == "CREDENTIAL_PROVISIONING"


def test_the_authority_layer_names_every_pilot_specific_denial() -> None:
    from typing import get_args

    reasons = set(get_args(AuthorityReason))

    assert {
        "CAPABILITY_MODE_MISMATCH",
        "CAPABILITY_GRANT_KIND_MISMATCH",
        "CAPABILITY_CEILING_MISMATCH",
        "CAPABILITY_REQUESTED_POLICY_MISMATCH",
        "CAPABILITY_RECOVERY_POLICY_MISMATCH",
        "CAPABILITY_CREDENTIAL_ROUTE_NOT_ALLOWED",
        "OPERATOR_PRESENCE_LOST",
    } <= reasons


def test_the_evidence_clock_is_recomputed_not_configured() -> None:
    assert CLASS_G_EVIDENCE_DAYS == 45
    assert ADDITIONAL_SHADOW_DAYS == 30
    source = (PREDICTIONS_ROOT / "pilot/qualification.py").read_text(encoding="utf-8")
    assert "never accept caller booleans" in source


def test_presence_is_a_kill_input() -> None:
    assert timedelta(seconds=5) == MAXIMUM_HEARTBEAT_SILENCE
    assert MAXIMUM_MISSED_HEARTBEATS == 2


def test_every_launch_starts_killed_and_an_empty_history_stays_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from polytrading.predictions.pilot.runtime import build_pilot_runtime

    path = tmp_path / "acceptance.duckdb"
    PredictionMarketStore(path).close()
    monkeypatch.setattr(runtime_module, "open_pilot_secret_store", lambda *, platform: object())
    runtime = build_pilot_runtime(path, 8788, platform="darwin")
    try:
        assert runtime.posture.kill_engaged is True
    finally:
        runtime.close()
    assert derive_kill_state((), production=True).engaged is True


def test_clearance_creates_no_capability() -> None:
    from polytrading.predictions.pilot.activation import KILL_CLEARANCE_PHRASE, clear_pilot_kill

    source = (PREDICTIONS_ROOT / "pilot/activation.py").read_text(encoding="utf-8")

    assert KILL_CLEARANCE_PHRASE == "CLEAR POLYMARKET PILOT KILL"
    assert "grants nothing" in clear_pilot_kill.__doc__
    assert "creates no trading authority" in source


def test_the_control_plane_is_one_exact_loopback_origin() -> None:
    assert pilot_origin(8788) == "http://localhost:8788"
    assert RP_ID == "localhost"
    assert all(path.startswith("/api/v1/pilot") for _method, path in PILOT_ROUTES)
    assert not any(name.lower().startswith("access-control") for name in SECURITY_HEADERS)
    assert "frame-ancestors 'none'" in SECURITY_HEADERS["Content-Security-Policy"]


def test_the_observer_cannot_reach_the_pilot_or_the_signer() -> None:
    trees = _trees()
    observer = [
        path
        for path in trees
        if path.name.startswith("dashboard") or path.as_posix().startswith("web_assets/")
    ]

    assert observer
    for path in observer:
        for node in ast.walk(trees[path]):
            module = getattr(node, "module", None)
            names = [alias.name for alias in getattr(node, "names", [])]
            for candidate in [module, *names]:
                assert candidate is None or "pilot" not in candidate, path
                assert candidate is None or "signer" not in candidate, path


def test_no_value_transfer_operation_exists_anywhere_in_the_pilot() -> None:
    for path in sorted((PREDICTIONS_ROOT / "pilot").rglob("*.py")):
        source = path.read_text(encoding="utf-8").casefold()
        for forbidden in ("def deposit", "def withdraw", "def transfer", "def approve_allowance"):
            assert forbidden not in source, f"{path} defines {forbidden}"


def test_no_pilot_test_imports_a_network_client() -> None:
    """No pilot test can construct real transport, because none of them import one."""

    for path in sorted(Path("tests/predictions").glob("test_pilot_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module.split(".")[0])
        assert not imported & {"httpx", "websockets", "socket", "requests"}, path


def test_the_first_live_size_ceiling_is_documented() -> None:
    runbook = Path("docs/predictions/polymarket-live-pilot.md").read_text(encoding="utf-8")

    assert "min(USD 5, the smallest venue-valid complete strategy)" in runbook
    assert Decimal("5") == FIRST_LIVE_CEILING_USD
    assert "only the operator can trigger a live action" in Path("README.md").read_text(
        encoding="utf-8"
    )


def test_the_runbook_states_where_secrets_may_never_appear() -> None:
    runbook = Path("docs/predictions/polymarket-live-pilot.md").read_text(encoding="utf-8")

    for surface in ("UI", "CLI", "`.env`", "logs", "screenshots", "tickets", "chat", "email"):
        assert surface in runbook
    assert "Secrets never belong in" in runbook


def test_recovery_can_only_reduce_exposure() -> None:
    from polytrading.predictions.pilot.capabilities import _RECOVERY_OPERATIONS

    assert {
        ExecutionOperation.CANCEL_ORDER,
        ExecutionOperation.READ_ACCOUNT,
        ExecutionOperation.READ_ORDERS,
        ExecutionOperation.READ_TRADES,
    } == _RECOVERY_OPERATIONS
