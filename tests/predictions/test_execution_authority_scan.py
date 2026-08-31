# ruff: noqa: E402 -- sealed source bytes must be verified before project imports.

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

_REVIEWED_SOURCE_SHA256 = {
    Path("dashboard_server.py"): "15b29adc080889f002f8f95959db1343fb1d5e886c0cfaff53c93ee7c1a563ca",
    Path(
        "execution/__init__.py"
    ): "8ccd7ecf39674fc4644f01763716fcedb8b6201bcbc94cb9933c0f9ecda458de",
    Path(
        "execution/authority.py"
    ): "fbc19d3edc16e6a4882f33993dcc4738d48f89919e69880e2c42099638ef25b9",
    Path(
        "execution/coordinator.py"
    ): "2b6cc79272e9ae938236111a9619b5b0540722d7bd00cc2a82e39e1c795f47a2",
    Path(
        "execution/kill_switch.py"
    ): "2c718198102aceabf45437f07cb09ca44df081d98f67778c6029016be5307a42",
    Path("execution/ledger.py"): "3767947fd3a70eea588bff87a33442d662521fb0c8806dfa4d86d2322dee4e31",
    Path("execution/models.py"): "53ce057ec1198494e2429f839991ca4dd2b90f360fb65cfab74e9165377b8ef6",
    Path(
        "execution/reconciliation.py"
    ): "b49db0ea6c31cbb833b08eeac69ed1b8ad257e829ec6f95e8fc7af90f9c64dbc",
    Path(
        "polymarket_execution/__init__.py"
    ): "be3037b2f0f1f3ae77ee7b2d975aae897b72c32de62b7cdbee81ea477d36e41d",
    Path(
        "polymarket_execution/auth.py"
    ): "52e5d9001f26989678e23a448bdf04ab0a249d471cb2f13e762509ef5ea15f4d",
    Path(
        "polymarket_execution/conformance.py"
    ): "14195af77ad2f5fb18d0e5a9ef55865b84812ddd4d0fc5e7e9d0e974a85b0175",
    Path(
        "polymarket_execution/credential_client.py"
    ): "0ce3e5b31536236ed9bd736686f03ee3cd6781a117858bb1f0ad8a613001c25c",
    Path(
        "polymarket_execution/credentials.py"
    ): "2ca9e0735c66a1ad5cef5ea0aba5de8bb3b0e18409b4a0fabb982e1f9d6e89f6",
    Path(
        "polymarket_execution/keychain_macos.py"
    ): "cef992e7f01359e1aeb4828fc7b87b8e21299f66eb7b4d2e8d8200b65d5cf270",
    Path(
        "polymarket_execution/heartbeat.py"
    ): "47b74b57f6518acad5654c303bb4bd26bab35f140f842e2e673d7c1db023157d",
    Path(
        "polymarket_execution/ipc.py"
    ): "50a0ab53c1634e73a514c0737955ea5b6a8bdfa2c51e8ab7cce47ff030f92e51",
    Path(
        "polymarket_execution/order.py"
    ): "fb2c507d97fd9175de1250f8d00a86a0bd85e58bedb129abcd783cec6f326131",
    Path(
        "polymarket_execution/protocol.py"
    ): "88ad4405fcda17afaea96bfdcde710c598b68823c10da532b3ee761ead6db9fa",
    Path(
        "polymarket_execution/rest.py"
    ): "45eaec54dc3f9ea3a21e3b43612fb5b922a04ccbd14a26efef87e42aa97469b4",
    Path(
        "polymarket_execution/routes.py"
    ): "24a970b295ad02484bba1132d6826e6f1c9dad1f7bac35dd17080e1e9b14c324",
    Path(
        "polymarket_execution/secrets.py"
    ): "94bcbb65c49198eabebebca4a2b317a2c1893a95de59d4d647cff57ebf6a1743",
    Path(
        "polymarket_execution/signer.py"
    ): "1bf51d12a8a1c4c76cb561804531c312a42b3ec7fa543467d5f36fd683d8e15b",
    Path(
        "polymarket_execution/user_stream.py"
    ): "e1079354acef4210c564011f037907e77bc293642ba79704602977206ac900e5",
    Path("web_assets/api.js"): "834c1b5714da4bd534f901b1c4af3679889a4184b601a6fac2fb87e3897021ad",
    Path("web_assets/app.css"): "85b61f1ea0a74588bc1add20f0fb861342719ed232ffcbcc0f6c0868b450e5ed",
    Path("web_assets/app.js"): "b9bb0d52ba332f10fe363ed393d84f57a0c44b33ee72db9b670cd2d3b00fc4af",
    Path(
        "web_assets/charts.js"
    ): "5a7b332315b3dcd1585e60e3aa75cd5dea43f3705e41d75ab186f3230a150d74",
    Path(
        "web_assets/index.html"
    ): "4c8029f5aafb0f78f1145b3c75491f68d03005e2be2b0be4967202b8acc85b80",
    Path("web_assets/store.js"): "89394d5aad109854f51536ebff56881b67623c22d5f084dc6d13cf8bead0d0c8",
    Path(
        "web_assets/stream.js"
    ): "8c81068507be08d6472a51a6559b473d91a4b0f85fbe86a712f95053723f8353",
    Path("web_assets/views.js"): "95c52e9ef219949899b433e1df6113e24099d8e7482774ed82b6aea0365b0506",
}
_PREIMPORT_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PREIMPORT_PREDICTIONS_ROOT = _PREIMPORT_REPOSITORY_ROOT / "src/polytrading/predictions"
_PREIMPORT_SOURCE_PATHS = {
    *_PREIMPORT_PREDICTIONS_ROOT.joinpath("execution").glob("*.py"),
    *_PREIMPORT_PREDICTIONS_ROOT.joinpath("polymarket_execution").glob("*.py"),
    *(
        (_PREIMPORT_PREDICTIONS_ROOT / "dashboard_server.py",)
        if (_PREIMPORT_PREDICTIONS_ROOT / "dashboard_server.py").is_file()
        else ()
    ),
    *(
        path
        for path in _PREIMPORT_PREDICTIONS_ROOT.joinpath("web_assets").iterdir()
        if path.is_file() and path.name != "__init__.py"
    ),
}
_PREIMPORT_SOURCE_BYTES = {
    path.relative_to(_PREIMPORT_PREDICTIONS_ROOT): path.read_bytes()
    for path in sorted(_PREIMPORT_SOURCE_PATHS)
}
if _PREIMPORT_SOURCE_BYTES.keys() != _REVIEWED_SOURCE_SHA256.keys() or any(
    sha256(_PREIMPORT_SOURCE_BYTES[path]).hexdigest() != digest
    for path, digest in _REVIEWED_SOURCE_SHA256.items()
):
    raise RuntimeError("AUTHORITY_SOURCE_MANIFEST_MISMATCH")

import argparse
import ast
import json
import re
import tomllib
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from http import HTTPStatus
from uuid import UUID

import pytest

from polytrading.cli import build_parser
from polytrading.predictions.dashboard import build_prediction_dashboard_snapshot
from polytrading.predictions.dashboard_live import DashboardReset, DashboardRevision
from polytrading.predictions.dashboard_models import DashboardDomain
from polytrading.predictions.dashboard_server import (
    PredictionDashboardApplication,
    _sse_event_frame,
)
from polytrading.predictions.execution.authority import (
    AuthorityContext,
    UnavailableProductionCapabilityVerifier,
)
from polytrading.predictions.execution.coordinator import ExecutionCoordinator
from polytrading.predictions.execution.kill_switch import derive_kill_state
from polytrading.predictions.execution.models import ExecutionOperation
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.polymarket_execution.ipc import (
    HeartbeatPayload,
    SanitizedOperationResult,
    SignerRequest,
)
from polytrading.predictions.polymarket_execution.secrets import SecretMaterial
from polytrading.predictions.polymarket_execution.signer import (
    SignerOperationHandlers,
    SignerService,
)
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.manifest_helpers import venue_manifest
from tests.predictions.pilot_helpers import signer_capability_grant
from tests.predictions.test_execution_authority import (
    HASHES,
    MANIFEST_HASH,
    authority_context,
    verified_capability,
)
from tests.predictions.test_execution_coordinator import (
    ACCOUNT_FINGERPRINT,
    execution_intent,
    preflight_evidence,
)

NOW = datetime(2026, 8, 25, 16, tzinfo=UTC)
REPOSITORY_ROOT = _PREIMPORT_REPOSITORY_ROOT
PREDICTIONS_ROOT = _PREIMPORT_PREDICTIONS_ROOT
WEB_ASSETS_ROOT = PREDICTIONS_ROOT / "web_assets"
RUNBOOK = REPOSITORY_ROOT / "docs/predictions/polymarket-execution-hardening.md"
EXPECTED_ASSETS = frozenset(
    {
        "index.html",
        "app.css",
        "app.js",
        "api.js",
        "stream.js",
        "store.js",
        "charts.js",
        "views.js",
    }
)
_OBSERVER_DESTINATIONS = frozenset({"/api/v1/predictions-dashboard", "/api/v1/predictions-events"})


class _ObserverHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, {name: value or "" for name, value in attrs}))


def _authority_sensitive_source_bytes() -> dict[Path, bytes]:
    python_paths = {
        *PREDICTIONS_ROOT.joinpath("execution").glob("*.py"),
        *PREDICTIONS_ROOT.joinpath("polymarket_execution").glob("*.py"),
        PREDICTIONS_ROOT / "dashboard_server.py",
    }
    browser_paths = {
        path for path in WEB_ASSETS_ROOT.iterdir() if path.is_file() and path.name != "__init__.py"
    }
    return {
        path.relative_to(PREDICTIONS_ROOT): path.read_bytes()
        for path in sorted(python_paths | browser_paths)
    }


def _reviewed_source_manifest_violations(sources: dict[Path, bytes]) -> tuple[str, ...]:
    violations = [
        *(f"missing:{path}" for path in sorted(_REVIEWED_SOURCE_SHA256.keys() - sources.keys())),
        *(f"unexpected:{path}" for path in sorted(sources.keys() - _REVIEWED_SOURCE_SHA256.keys())),
    ]
    for path in sorted(_REVIEWED_SOURCE_SHA256.keys() & sources.keys()):
        if sha256(sources[path]).hexdigest() != _REVIEWED_SOURCE_SHA256[path]:
            violations.append(f"digest:{path}")
    return tuple(violations)


def _python_trees() -> dict[Path, ast.Module]:
    return {
        path.relative_to(PREDICTIONS_ROOT): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(PREDICTIONS_ROOT.rglob("*.py"))
    }


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _resolved_python_name(
    node: ast.expr,
    aliases: dict[str, str],
    constants: dict[str, str] | None = None,
) -> str | None:
    resolved_constants = constants or {}
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _resolved_python_name(node.value, aliases, resolved_constants)
        return f"{base}.{node.attr}" if base is not None else None
    if isinstance(node, ast.Subscript):
        base = _resolved_python_name(node.value, aliases, resolved_constants)
        key = _static_text(node.slice, resolved_constants)
        if base is not None and base.endswith(".__dict__") and key is not None:
            return f"{base.removesuffix('.__dict__')}.{key}"
        return None
    if (
        isinstance(node, ast.Call)
        and len(node.args) == 2
        and not node.keywords
        and _resolved_python_name(node.func, aliases, resolved_constants)
        in {"getattr", "builtins.getattr"}
    ):
        base = _resolved_python_name(node.args[0], aliases, resolved_constants)
        attribute = _static_text(node.args[1], resolved_constants)
        return f"{base}.{attribute}" if base is not None and attribute is not None else None
    if (
        isinstance(node, ast.Call)
        and len(node.args) == 1
        and not node.keywords
        and _resolved_python_name(node.func, aliases, resolved_constants)
        in {
            "__import__",
            "builtins.__import__",
            "importlib.import_module",
            "import_module",
        }
    ):
        return _static_text(node.args[0], resolved_constants)
    if (
        isinstance(node, ast.Call)
        and len(node.args) == 1
        and not node.keywords
        and _resolved_python_name(node.func, aliases, resolved_constants)
        in {"vars", "builtins.vars"}
    ):
        base = _resolved_python_name(node.args[0], aliases, resolved_constants)
        return f"{base}.__dict__" if base is not None else None
    return None


def _python_string_constants(tree: ast.Module) -> dict[str, str]:
    assignments: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            assignments.setdefault(node.targets[0].id, []).append(node.value)
    constants: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for name, expressions in assignments.items():
            if name in constants:
                continue
            values = [_static_text(expression, constants) for expression in expressions]
            if all(value is not None for value in values) and len(set(values)) == 1:
                stable_value = values[0]
                assert stable_value is not None
                constants[name] = stable_value
                changed = True
    return constants


def _httpx_constructor_inventory(
    trees: dict[Path, ast.Module],
) -> tuple[tuple[Path, int, str], ...]:
    inventory: list[tuple[Path, int, str]] = []
    for source_path, tree in trees.items():
        aliases: dict[str, str] = {}
        constants = _python_string_constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id not in aliases
                    and (resolved := _resolved_python_name(node.value, aliases, constants))
                    is not None
                    and (
                        resolved in _PROTECTED_PYTHON_SYMBOLS
                        or resolved in {"httpx", "importlib"}
                        or resolved.startswith(("httpx.", "importlib."))
                    )
                    and aliases.get(node.targets[0].id) != resolved
                ):
                    aliases[node.targets[0].id] = resolved
                    changed = True
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            resolved = _resolved_python_name(node.func, aliases, constants)
            if resolved is not None and resolved.startswith("httpx."):
                inventory.append((source_path, node.lineno, resolved))
    return tuple(sorted(inventory))


def _static_text(node: ast.expr, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_text(node.left, constants)
        right = _static_text(node.right, constants)
        return left + right if left is not None and right is not None else None
    return None


def _exact_mock_transport_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If) or len(node.body) != 1:
        return False
    expected_test = ast.parse("type(transport) is not httpx.MockTransport").body[0]
    expected_raise = ast.parse('raise TypeError("HTTPX_MOCK_TRANSPORT_REQUIRED")').body[0]
    return ast.dump(node.test, include_attributes=False) == ast.dump(
        expected_test.value,  # type: ignore[attr-defined]
        include_attributes=False,
    ) and ast.dump(node.body[0], include_attributes=False) == ast.dump(
        expected_raise,
        include_attributes=False,
    )


def _method_argument_annotation(method: ast.FunctionDef, name: str) -> str | None:
    argument = next(
        (
            item
            for item in (*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs)
            if item.arg == name
        ),
        None,
    )
    if argument is None or argument.annotation is None:
        return None
    return ast.unparse(argument.annotation)


def _method_argument_has_no_default(method: ast.FunctionDef, name: str) -> bool:
    positional = (*method.args.posonlyargs, *method.args.args)
    for index, argument in enumerate(positional):
        if argument.arg == name:
            return index < len(positional) - len(method.args.defaults)
    for argument, default in zip(method.args.kwonlyargs, method.args.kw_defaults, strict=True):
        if argument.arg == name:
            return default is None
    return False


def _has_exact_mock_guard_before(method: ast.FunctionDef, line: int) -> bool:
    return any(item.lineno < line and _exact_mock_transport_guard(item) for item in method.body)


def _transport_flow_is_exact(
    method: ast.FunctionDef,
    *,
    forwarded_call: ast.Call,
) -> bool:
    guards = [item for item in method.body if _exact_mock_transport_guard(item)]
    if len(guards) != 1 or guards[0].lineno >= forwarded_call.lineno:
        return False
    forwarded = [item for item in forwarded_call.keywords if item.arg == "transport"]
    method_transport_arguments = [
        item
        for item in (
            *method.args.posonlyargs,
            *method.args.args,
            *method.args.kwonlyargs,
        )
        if item.arg == "transport"
    ]
    if (
        len(forwarded) != 1
        or not isinstance(forwarded[0].value, ast.Name)
        or forwarded[0].value.id != "transport"
        or len(method_transport_arguments) != 1
    ):
        return False
    allowed_transport_names = {
        id(item)
        for item in (*ast.walk(guards[0].test), forwarded[0].value)
        if isinstance(item, ast.Name) and item.id == "transport"
    }
    for item in ast.walk(method):
        if isinstance(item, ast.Name) and item.id == "transport":
            if id(item) not in allowed_transport_names:
                return False
        elif isinstance(item, ast.arg) and item.arg == "transport":
            if item is not method_transport_arguments[0]:
                return False
        elif isinstance(item, ast.arg) and item.arg in {"type", "httpx", "MockTransport"}:
            return False
        elif isinstance(item, ast.Name) and item.id in {"type", "httpx", "MockTransport"}:
            if isinstance(item.ctx, ast.Store | ast.Del):
                return False
        elif (
            isinstance(item, ast.Global | ast.Nonlocal)
            and set(item.names) & {"transport", "type", "httpx", "MockTransport"}
        ) or (
            isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and item.name in {"transport", "type", "httpx", "MockTransport"}
        ):
            return False
    return True


def _method_arguments_are_exact(
    method: ast.FunctionDef,
    *,
    positional: tuple[str, ...],
    keyword_only: tuple[str, ...],
) -> bool:
    return (
        tuple(item.arg for item in (*method.args.posonlyargs, *method.args.args)) == positional
        and tuple(item.arg for item in method.args.kwonlyargs) == keyword_only
        and method.args.vararg is None
        and method.args.kwarg is None
    )


def _rest_client_owners_are_exact(tree: ast.Module) -> bool:
    client_attributes = [
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.Attribute)
        and item.attr == "_client"
        and isinstance(item.ctx, ast.Store | ast.Del)
    ]
    direct_owners = [
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.Assign)
        and len(item.targets) == 1
        and isinstance(item.targets[0], ast.Attribute)
        and isinstance(item.targets[0].value, ast.Name)
        and item.targets[0].value.id == "self"
        and item.targets[0].attr == "_client"
    ]
    reflective_writes = [
        item
        for item in ast.walk(tree)
        if isinstance(item, ast.Call)
        and _call_name(item) in {"setattr", "delattr"}
        and len(item.args) >= 2
        and isinstance(item.args[1], ast.Constant)
        and item.args[1].value == "_client"
    ]
    return (
        len(client_attributes) == 2
        and len(direct_owners) == 2
        and all(
            isinstance(item.value, ast.Call)
            and _resolved_python_name(item.value.func, {"httpx": "httpx"}) == "httpx.AsyncClient"
            for item in direct_owners
        )
        and not reflective_writes
    )


def _allowed_mock_async_client_call(
    source_path: Path,
    tree: ast.Module,
    call: ast.Call,
) -> bool:
    if source_path != Path("polymarket_execution/rest.py") or call.args:
        return False
    transport_keywords = [item for item in call.keywords if item.arg == "transport"]
    if len(transport_keywords) != 1 or not isinstance(transport_keywords[0].value, ast.Name):
        return False
    if transport_keywords[0].value.id != "transport":
        return False
    transport_class = next(
        (
            item
            for item in tree.body
            if isinstance(item, ast.ClassDef) and item.name == "HttpxPolymarketRestTransport"
        ),
        None,
    )
    if transport_class is None:
        return False
    methods = {
        item.name: item for item in transport_class.body if isinstance(item, ast.FunctionDef)
    }
    initialize = methods.get("_initialize")
    for_test = methods.get("_for_test")
    if initialize is None or for_test is None:
        return False
    if not (
        initialize.lineno <= call.lineno <= (initialize.end_lineno or 0)
        and _method_arguments_are_exact(
            initialize,
            positional=("self",),
            keyword_only=(
                "transport",
                "timestamp",
                "clock",
                "retry_policy",
                "sleeper",
                "timeouts",
            ),
        )
        and _method_arguments_are_exact(
            for_test,
            positional=("cls", "transport"),
            keyword_only=("timestamp", "clock", "retry_policy", "sleeper", "timeouts"),
        )
        and _method_argument_annotation(initialize, "transport") == "httpx.MockTransport"
        and _method_argument_annotation(for_test, "transport") == "httpx.MockTransport"
        and _method_argument_has_no_default(initialize, "transport")
        and _method_argument_has_no_default(for_test, "transport")
        and _transport_flow_is_exact(initialize, forwarded_call=call)
        and _rest_client_owners_are_exact(tree)
    ):
        return False
    initialize_calls = [
        item
        for item in ast.walk(for_test)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "_initialize"
    ]
    if len(initialize_calls) != 1:
        return False
    initialize_call = initialize_calls[0]
    forwarded = [item for item in initialize_call.keywords if item.arg == "transport"]
    return (
        {item.arg for item in initialize_call.keywords}
        == {"transport", "timestamp", "clock", "retry_policy", "sleeper", "timeouts"}
        and len(forwarded) == 1
        and isinstance(forwarded[0].value, ast.Name)
        and forwarded[0].value.id == "transport"
        and _transport_flow_is_exact(for_test, forwarded_call=initialize_call)
    )


def _allowed_hardened_live_async_client_call(
    source_path: Path,
    tree: ast.Module,
    call: ast.Call,
) -> bool:
    if source_path != Path("polymarket_execution/rest.py") or call.args:
        return False
    transport_class = next(
        (
            item
            for item in tree.body
            if isinstance(item, ast.ClassDef) and item.name == "HttpxPolymarketRestTransport"
        ),
        None,
    )
    if transport_class is None:
        return False
    methods = {
        item.name: item for item in transport_class.body if isinstance(item, ast.FunctionDef)
    }
    initialize = methods.get("__init__")
    if initialize is None or not (
        initialize.lineno <= call.lineno <= (initialize.end_lineno or 0)
        and _method_arguments_are_exact(
            initialize,
            positional=("self",),
            keyword_only=("timestamp", "clock", "retry_policy", "sleeper", "timeouts"),
        )
        and _rest_client_owners_are_exact(tree)
    ):
        return False
    keywords = {item.arg: item.value for item in call.keywords}
    timeout = keywords.get("timeout")
    return (
        set(keywords) == {"follow_redirects", "trust_env", "timeout", "headers", "cookies"}
        and isinstance(keywords["follow_redirects"], ast.Constant)
        and keywords["follow_redirects"].value is False
        and isinstance(keywords["trust_env"], ast.Constant)
        and keywords["trust_env"].value is False
        and isinstance(keywords["headers"], ast.Dict)
        and not keywords["headers"].keys
        and isinstance(keywords["cookies"], ast.Dict)
        and not keywords["cookies"].keys
        and isinstance(timeout, ast.Call)
        and not timeout.args
        and not timeout.keywords
        and isinstance(timeout.func, ast.Attribute)
        and isinstance(timeout.func.value, ast.Name)
        and timeout.func.value.id == "checked_timeouts"
        and timeout.func.attr == "as_httpx"
    )


def _expression_references_httpx(node: ast.expr, aliases: dict[str, str]) -> bool:
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and aliases.get(item.id, item.id).startswith("httpx"):
            return True
        if isinstance(item, ast.Constant) and item.value == "httpx":
            return True
    return False


_PROTECTED_PYTHON_SYMBOLS = frozenset(
    {
        "MockTransport",
        "__import__",
        "getattr",
        "globals",
        "httpx",
        "importlib",
        "locals",
        "type",
        "vars",
    }
)


def _protected_python_assignment_violations(
    tree: ast.Module,
    source_path: Path,
    aliases: dict[str, str],
    constants: dict[str, str],
) -> tuple[str, ...]:
    violations: list[str] = []
    protected_bindings = (
        _PROTECTED_PYTHON_SYMBOLS
        if source_path == Path("polymarket_execution/rest.py")
        else _PROTECTED_PYTHON_SYMBOLS - {"type"}
    )
    protected_aliases = {
        name
        for name, resolved in aliases.items()
        if name in protected_bindings
        or resolved in protected_bindings
        or resolved.startswith(("httpx", "importlib"))
    }

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                continue
            target = node.targets[0].id
            resolved = _resolved_python_name(node.value, aliases, constants)
            source_name = node.value.id if isinstance(node.value, ast.Name) else None
            tainted = (
                source_name in protected_bindings
                or source_name in protected_aliases
                or (
                    resolved is not None
                    and (
                        resolved in protected_bindings
                        or resolved.startswith(("httpx", "importlib"))
                    )
                )
            )
            if tainted and target not in protected_aliases:
                protected_aliases.add(target)
                changed = True

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                binding = item.asname or item.name
                if item.name in {"httpx", "importlib"} and binding != item.name:
                    violations.append(f"protected-alias:{source_path}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom) and node.module in {"httpx", "importlib"}:
            for item in node.names:
                if item.asname is not None:
                    violations.append(f"protected-alias:{source_path}:{node.lineno}")

        targets: tuple[ast.expr, ...] = ()
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = tuple(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            targets = (node.target,)
            value = node.value
        elif isinstance(node, ast.Delete):
            targets = tuple(node.targets)

        for target in targets:
            stored_names = {
                item.id
                for item in ast.walk(target)
                if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store | ast.Del)
            }
            if stored_names & protected_bindings:
                violations.append(f"protected-assignment:{source_path}:{node.lineno}")
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Call)
                and _resolved_python_name(target.value.func, aliases, constants)
                in {
                    "globals",
                    "builtins.globals",
                    "locals",
                    "builtins.locals",
                    "vars",
                    "builtins.vars",
                }
            ):
                key = _static_text(target.slice, constants)
                if key is None or key in protected_bindings:
                    violations.append(f"protected-assignment:{source_path}:{node.lineno}")
            if value is None or not isinstance(target, ast.Name):
                continue
            resolved = _resolved_python_name(value, aliases, constants)
            source_name = value.id if isinstance(value, ast.Name) else None
            if (
                source_name in protected_bindings
                or source_name in protected_aliases
                or (
                    resolved is not None
                    and (
                        resolved in protected_bindings
                        or resolved.startswith(("httpx", "importlib"))
                    )
                )
            ):
                violations.append(f"protected-alias:{source_path}:{node.lineno}")

        if isinstance(node, ast.arg) and node.arg in protected_bindings:
            violations.append(f"protected-argument:{source_path}:{node.lineno}")
        elif (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.name in protected_bindings
        ):
            violations.append(f"protected-definition:{source_path}:{node.lineno}")
        elif isinstance(node, ast.Global | ast.Nonlocal) and set(node.names) & set(
            protected_bindings
        ):
            violations.append(f"protected-assignment:{source_path}:{node.lineno}")
    return tuple(sorted(set(violations)))


def _normalize_javascript_identifier_escapes(source: str) -> str:
    def replace(match: re.Match[str]) -> str:
        value = match.group("fixed") or match.group("braced")
        assert value is not None
        try:
            character = chr(int(value, 16))
        except (OverflowError, ValueError):
            return "INVALID_IDENTIFIER_ESCAPE"
        if not re.fullmatch(r"[A-Za-z0-9_$]", character):
            return "INVALID_IDENTIFIER_ESCAPE"
        return character

    return re.sub(
        r"\\u(?:\{(?P<braced>[0-9A-Fa-f]{1,6})\}|(?P<fixed>[0-9A-Fa-f]{4}))",
        replace,
        source,
    )


def _javascript_lexical_code(source: str) -> str:
    lexical = list(source)

    def blank(start: int, end: int) -> None:
        for offset in range(start, end):
            if lexical[offset] != "\n":
                lexical[offset] = " "

    def scan_string(index: int, quote: str) -> int:
        start = index
        index += 1
        while index < len(source):
            if source[index] == "\\":
                index += 2
                continue
            index += 1
            if source[index - 1] == quote:
                break
        blank(start, min(index, len(source)))
        return index

    def scan_code(index: int, *, template_expression: bool = False) -> int:
        brace_depth = 0
        while index < len(source):
            if source.startswith("//", index):
                closing = source.find("\n", index + 2)
                end = len(source) if closing < 0 else closing
                blank(index, end)
                index = end
                continue
            if source.startswith("/*", index):
                closing = source.find("*/", index + 2)
                end = len(source) if closing < 0 else closing + 2
                blank(index, end)
                index = end
                continue
            character = source[index]
            if character in {'"', "'"}:
                index = scan_string(index, character)
                continue
            if character == "`":
                index = scan_template(index)
                continue
            if template_expression:
                if character == "{":
                    brace_depth += 1
                elif character == "}":
                    if brace_depth == 0:
                        return index
                    brace_depth -= 1
            index += 1
        return index

    def scan_template(index: int) -> int:
        blank(index, index + 1)
        index += 1
        inert_start = index
        while index < len(source):
            if source[index] == "\\":
                index += 2
                continue
            if source[index] == "`":
                blank(inert_start, index + 1)
                return index + 1
            if source.startswith("${", index):
                blank(inert_start, index + 2)
                closing = scan_code(index + 2, template_expression=True)
                if closing >= len(source):
                    return closing
                blank(closing, closing + 1)
                index = closing + 1
                inert_start = index
                continue
            index += 1
        blank(inert_start, len(source))
        return len(source)

    scan_code(0)
    return "".join(lexical)


def _balanced_javascript_parts(
    source: str,
    opening_index: int,
) -> tuple[tuple[str, ...], int] | None:
    pairs = {"(": ")", "[": "]", "{": "}"}
    opening = source[opening_index] if 0 <= opening_index < len(source) else ""
    if opening not in pairs:
        return None
    stack = [opening]
    parts: list[str] = []
    part_start = opening_index + 1
    quote: str | None = None
    index = opening_index + 1
    while index < len(source):
        character = source[index]
        if quote is not None:
            if character == "\\":
                index += 2
                continue
            if character == quote:
                quote = None
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            closing_comment = source.find("*/", index + 2)
            if closing_comment < 0:
                return None
            index = closing_comment + 2
            continue
        if character in {'"', "'", "`"}:
            quote = character
        elif character in pairs:
            stack.append(character)
        elif character in pairs.values():
            if not stack or pairs[stack[-1]] != character:
                return None
            stack.pop()
            if not stack:
                tail = source[part_start:index].strip()
                if tail:
                    parts.append(tail)
                return tuple(parts), index
        elif character == "," and len(stack) == 1:
            parts.append(source[part_start:index].strip())
            part_start = index + 1
        index += 1
    return None


def _javascript_call_arguments(
    source: str,
    lexical_code: str,
    alias: str,
    *,
    constructor: bool,
) -> tuple[tuple[str, ...], ...] | None:
    prefix = rf"\bnew\s+{re.escape(alias)}" if constructor else rf"\b{re.escape(alias)}"
    calls: list[tuple[str, ...]] = []
    for match in re.finditer(prefix + r"\s*\(", lexical_code):
        opening_index = lexical_code.find("(", match.start(), match.end())
        balanced = _balanced_javascript_parts(source, opening_index)
        if balanced is None:
            return None
        arguments, _closing_index = balanced
        calls.append(arguments)
    return tuple(calls)


def _javascript_static_string(expression: str, constants: dict[str, str]) -> str | None:
    stripped = expression.strip()
    if stripped in constants:
        return constants[stripped]
    literal = re.fullmatch(r'(["\'])([^"\'\\]*)\1', stripped)
    return literal.group(2) if literal is not None else None


def _fetch_options_are_exact(options: str) -> tuple[bool, bool]:
    stripped = options.strip()
    if not stripped.startswith("{"):
        return False, False
    balanced = _balanced_javascript_parts(stripped, 0)
    if balanced is None or balanced[1] != len(stripped) - 1:
        return False, False
    properties = balanced[0]
    method_properties = [item for item in properties if re.match(r"^method\s*:", item)]
    method_exact = (
        len(method_properties) == 1
        and re.fullmatch(r'method\s*:\s*(["\'])GET\1', method_properties[0]) is not None
    )
    exact_properties = {
        "method": r'method\s*:\s*(["\'])GET\1',
        "headers": (r'headers\s*:\s*\{\s*Accept\s*:\s*(["\'])application/json\1\s*\}'),
        "cache": r'cache\s*:\s*(["\'])no-store\1',
        "signal": r"signal",
    }
    matched_names: list[str] = []
    for property_text in properties:
        matches = [
            name
            for name, pattern in exact_properties.items()
            if re.fullmatch(pattern, property_text)
        ]
        if len(matches) != 1:
            return method_exact, False
        matched_names.extend(matches)
    return method_exact, sorted(matched_names) == sorted(exact_properties)


# The pilot package is the reviewed local capability authority (2026-08-27 pilot design, sections
# 4.3 and 9.5): it is the one place allowed to define capability issuance and operator kill
# clearance. Every other scanned surface -- the observer dashboard, the execution coordinator, and
# the signer -- stays free of both.
_PILOT_AUTHORITY_PREFIX = "pilot/"
# The reviewed adapter and signer boundary may project a verified pilot grant into the authority
# layer's sanitized capability. Every other module still may not name or construct that type.
_CAPABILITY_PROJECTION_PATHS = frozenset(
    {
        Path("execution/authority.py"),
        Path("pilot/verifier.py"),
        Path("polymarket_execution/signer.py"),
    }
)
# The one reviewed module allowed to start the signer sidecar, and only in a forked child after
# the operator has unlocked the keychain. Nothing else may reach that entry point.
_SIGNER_LAUNCH_PATHS = frozenset({Path("pilot/signer_bootstrap.py")})


def _is_pilot_authority(path: Path) -> bool:
    return path.as_posix().startswith(_PILOT_AUTHORITY_PREFIX)


def _production_policy_violations(sources: dict[Path, str]) -> tuple[str, ...]:
    violations: list[str] = []
    for source_path, source in sources.items():
        tree = ast.parse(source)
        aliases: dict[str, str] = {}
        constants = _python_string_constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
                    if alias.name == "tests" or alias.name.startswith("tests."):
                        violations.append(f"test-import:{source_path}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
                if node.module == "tests" or node.module.startswith("tests."):
                    violations.append(f"test-import:{source_path}:{node.lineno}")
        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id not in aliases
                    and (resolved := _resolved_python_name(node.value, aliases, constants))
                    is not None
                    and (
                        resolved in _PROTECTED_PYTHON_SYMBOLS
                        or resolved in {"httpx", "importlib"}
                        or resolved.startswith(("httpx.", "importlib."))
                    )
                    and aliases.get(node.targets[0].id) != resolved
                ):
                    aliases[node.targets[0].id] = resolved
                    changed = True
        violations.extend(
            _protected_python_assignment_violations(tree, source_path, aliases, constants)
        )
        if source_path == Path("polymarket_execution/rest.py"):
            async_client_calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and _resolved_python_name(node.func, aliases, constants) == "httpx.AsyncClient"
            ]
            if len(async_client_calls) != 2 or not _rest_client_owners_are_exact(tree):
                violations.append(f"rest-client-owner:{source_path}")
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                words = set(
                    re.findall(
                        r"[a-z0-9]+",
                        re.sub(r"(?<!^)(?=[A-Z])", "_", node.name).casefold(),
                    )
                )
                issues_capability = "capability" in words and words & {
                    "activate",
                    "build",
                    "create",
                    "generate",
                    "import",
                    "issue",
                    "mint",
                }
                clears_kill = "kill" in words and words & {
                    "clear",
                    "disengage",
                    "release",
                    "reset",
                }
                if (issues_capability or clears_kill) and not _is_pilot_authority(source_path):
                    violations.append(f"authority-definition:{source_path}:{node.lineno}")
                if "verifier" in words and words & {"configured", "enabled"}:
                    violations.append(f"configured-verifier:{source_path}:{node.lineno}")
            if isinstance(node, ast.Assign | ast.AnnAssign):
                if isinstance(node, ast.AnnAssign) and node.value is None:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in (
                        item.id for item in ast.walk(target) if isinstance(item, ast.Name)
                    ):
                        words = set(name.casefold().split("_"))
                        if "production" in words and words & {
                            "authority",
                            "capability",
                            "key",
                            "secret",
                            "verifier",
                        }:
                            violations.append(f"configured-authority:{source_path}:{node.lineno}")
            if isinstance(node, ast.Subscript):
                reflected = _resolved_python_name(node, aliases, constants)
                if (
                    (
                        (reflected is not None and reflected.startswith("httpx."))
                        or _expression_references_httpx(node, aliases)
                    )
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "__dict__"
                ):
                    violations.append(f"socket-http-constructor:{source_path}:{node.lineno}")
            if not isinstance(node, ast.Call):
                continue
            resolved = _resolved_python_name(node.func, aliases, constants)
            if resolved in {"__import__", "builtins.__import__", "importlib.import_module"}:
                module = _static_text(node.args[0], constants) if node.args else None
                if module is None:
                    violations.append(f"dynamic-import:{source_path}:{node.lineno}")
                elif module == "tests" or module.startswith("tests."):
                    violations.append(f"test-import:{source_path}:{node.lineno}")
                elif module == "httpx" or module.startswith("httpx."):
                    violations.append(f"socket-http-constructor:{source_path}:{node.lineno}")
            if resolved in {"getattr", "builtins.getattr"} and node.args:
                reflected_module = _resolved_python_name(node.args[0], aliases, constants)
                if (
                    reflected_module is not None and reflected_module.startswith("httpx")
                ) or _expression_references_httpx(node.args[0], aliases):
                    violations.append(f"socket-http-constructor:{source_path}:{node.lineno}")
            if resolved is not None and resolved.endswith(".KillState"):
                engaged = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "engaged"),
                    node.args[0] if node.args else None,
                )
                if not (isinstance(engaged, ast.Constant) and engaged.value is True):
                    violations.append(f"clear-kill:{source_path}:{node.lineno}")
            if resolved is not None and resolved.endswith(
                (".HttpxPolymarketRestTransport", ".SignerRestHandlers")
            ):
                violations.append(f"transport-composition:{source_path}:{node.lineno}")
            if resolved is not None and resolved.startswith("httpx."):
                allowed_httpx_call = resolved in {
                    "httpx.Request",
                    "httpx.Timeout",
                } or (
                    resolved == "httpx.AsyncClient"
                    and (
                        _allowed_mock_async_client_call(source_path, tree, node)
                        or _allowed_hardened_live_async_client_call(source_path, tree, node)
                    )
                )
                if not allowed_httpx_call:
                    violations.append(f"socket-http-constructor:{source_path}:{node.lineno}")
            elif _expression_references_httpx(node.func, aliases):
                violations.append(f"socket-http-constructor:{source_path}:{node.lineno}")
            if any(keyword.arg == "test_only_kill_state" for keyword in node.keywords):
                violations.append(f"test-kill-seam:{source_path}:{node.lineno}")
    return tuple(sorted(set(violations)))


def _browser_surface_inventory(
    assets: dict[str, str],
) -> tuple[dict[str, frozenset[str]], frozenset[tuple[str, str, str]], tuple[str, ...]]:
    imports: dict[str, frozenset[str]] = {}
    network: set[tuple[str, str, str]] = set()
    violations: list[str] = []
    javascript = {name: source for name, source in assets.items() if name.endswith(".js")}

    for name, source in javascript.items():
        source = _normalize_javascript_identifier_escapes(source)
        if "INVALID_IDENTIFIER_ESCAPE" in source:
            violations.append(f"identifier-escape:{name}")
        destinations = set(
            re.findall(
                r"\bimport\s+(?:[^;]*?\s+from\s+)?[\"']([^\"']+)[\"']\s*;",
                source,
            )
        )
        resolved: set[str] = set()
        for destination in destinations:
            if not destination.startswith("./") or "/" in destination[2:]:
                violations.append(f"module-destination:{name}")
                continue
            target = destination[2:]
            if target not in javascript:
                violations.append(f"module-missing:{name}")
                continue
            resolved.add(target)
        imports[name] = frozenset(resolved)

        constants = {
            identifier: value
            for identifier, value in re.findall(
                r"\bconst\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*[\"']([^\"']+)[\"']\s*;",
                source,
            )
        }
        lexical_code = _javascript_lexical_code(source)
        if re.search(r"\bimport\s*\(", lexical_code):
            violations.append(f"dynamic-module-import:{name}")
        if re.search(r"\b(?:eval|Function)\b", lexical_code) or re.search(
            r"\b(?:setTimeout|setInterval)\s*\(\s*[\"'`]", source
        ):
            violations.append(f"dynamic-code:{name}")

        network_keywords = (
            "fetch",
            "EventSource",
            "WebSocket",
            "XMLHttpRequest",
            "sendBeacon",
            "Worker",
            "SharedWorker",
            "serviceWorker",
            "WebTransport",
        )
        expected_keywords = {
            "api.js": {"fetch": 1},
            "stream.js": {"EventSource": 1},
        }
        for keyword in network_keywords:
            actual_count = len(re.findall(rf"\b{re.escape(keyword)}\b", lexical_code))
            expected_count = expected_keywords.get(name, {}).get(keyword, 0)
            if actual_count != expected_count:
                violations.append(f"network-keyword:{name}:{keyword}")
        expected_injected_identifiers = {
            "api.js": {"fetchImpl": 3},
            "stream.js": {"EventSourceConstructor": 3},
        }
        for identifier in ("fetchImpl", "EventSourceConstructor"):
            actual_count = len(re.findall(rf"\b{identifier}\b", lexical_code))
            expected_count = expected_injected_identifiers.get(name, {}).get(identifier, 0)
            if actual_count != expected_count:
                violations.append(f"network-keyword:{name}:{identifier}")
        expected_method_count = 1 if name == "api.js" else 0
        if len(re.findall(r"\bmethod\s*:", lexical_code)) != expected_method_count:
            violations.append(f"fetch-options:{name}")

        aliases: dict[str, str] = {}
        for alias, primitive in re.findall(
            r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*globalThis\.([A-Za-z_$][A-Za-z0-9_$]*)",
            lexical_code,
        ):
            if primitive in {"fetch", "EventSource"}:
                aliases[alias] = primitive
            elif primitive in {"WebSocket", "XMLHttpRequest"}:
                violations.append(f"network-primitive:{name}")
        bracket_binding = re.compile(
            r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*globalThis"
            r"\[\s*[\"']([A-Za-z_$][A-Za-z0-9_$]*)[\"']\s*\]"
        )
        for match in bracket_binding.finditer(source):
            if "globalThis" not in lexical_code[match.start() : match.end()]:
                continue
            alias, primitive = match.groups()
            if primitive in {"fetch", "EventSource"}:
                aliases[alias] = primitive
            else:
                violations.append(f"network-primitive:{name}")

        network_code = lexical_code
        allowed_bindings = {
            "api.js": (r"\bfetchImpl\s*=\s*globalThis\.fetch\b",),
            "stream.js": (r"\bEventSourceConstructor\s*=\s*globalThis\.EventSource\b",),
        }
        for pattern in allowed_bindings.get(name, ()):
            matches = tuple(re.finditer(pattern, network_code))
            if len(matches) != 1:
                violations.append(f"network-binding:{name}")
            for match in matches:
                network_code = (
                    network_code[: match.start()]
                    + " " * (match.end() - match.start())
                    + network_code[match.end() :]
                )
        direct_patterns = (
            r"(?<![A-Za-z0-9_$])(?:fetch|EventSource|WebSocket|XMLHttpRequest|Worker|SharedWorker|WebTransport)\b",
            r"\b(?:window|globalThis)\s*\.\s*(?:fetch|EventSource|WebSocket|XMLHttpRequest|Worker|SharedWorker|WebTransport)\b",
            r"\b(?:window|globalThis|navigator)\s*\[",
            r"\b(?:navigator\s*\.\s*)?(?:sendBeacon|serviceWorker)\b",
        )
        if any(re.search(pattern, network_code) for pattern in direct_patterns):
            violations.append(f"network-primitive:{name}")
        if re.search(r"\b(?:window|globalThis|navigator)\s*\[", network_code):
            violations.append(f"dynamic-global-property:{name}")

        changed = True
        while changed:
            changed = False
            for alias, target in re.findall(
                r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*;",
                lexical_code,
            ):
                if target in aliases and aliases.get(alias) != aliases[target]:
                    aliases[alias] = aliases[target]
                    changed = True

        for alias, primitive in aliases.items():
            calls = _javascript_call_arguments(
                source,
                lexical_code,
                alias,
                constructor=primitive == "EventSource",
            )
            expected_binding = (name, alias, primitive) in {
                ("api.js", "fetchImpl", "fetch"),
                ("stream.js", "EventSourceConstructor", "EventSource"),
            }
            if not expected_binding:
                violations.append(f"network-primitive:{name}")
            if calls is None:
                violations.append(f"unbalanced-network-call:{name}:{alias}")
                continue
            if expected_binding and len(calls) != 1:
                violations.append(f"network-call-count:{name}:{alias}")
            if not calls:
                violations.append(f"unused-network-binding:{name}:{alias}")
                continue
            for arguments in calls:
                destination = (
                    _javascript_static_string(arguments[0], constants) if arguments else None
                )
                if destination is None:
                    violations.append(f"dynamic-network-destination:{name}:{alias}")
                    continue
                method = "GET"
                if primitive == "fetch":
                    if len(arguments) != 2:
                        violations.append(f"fetch-options:{name}:{alias}")
                        continue
                    method_exact, options_exact = _fetch_options_are_exact(arguments[1])
                    if not method_exact:
                        violations.append(f"fetch-method:{name}:{alias}")
                    if not options_exact:
                        violations.append(f"fetch-options:{name}:{alias}")
                    if not method_exact or not options_exact:
                        continue
                elif len(arguments) != 1:
                    violations.append(f"eventsource-options:{name}:{alias}")
                    continue
                network.add((primitive, destination, method))
                if destination not in _OBSERVER_DESTINATIONS:
                    violations.append(f"network-destination:{name}:{alias}")

    html = assets.get("index.html", "")
    parser = _ObserverHtml()
    parser.feed(html)
    html_targets = {
        value
        for _tag, attrs in parser.elements
        for attribute, value in attrs.items()
        if attribute in {"href", "src"}
    }
    allowed_html = {
        "#main",
        "/assets/app.css",
        "/assets/app.js",
    }
    for target in html_targets:
        if target.startswith("data:image/svg+xml,"):
            continue
        if target not in allowed_html:
            violations.append("html-destination:index.html")

    css = assets.get("app.css", "")
    if re.search(r"@import\b", css, flags=re.IGNORECASE):
        violations.append("css-import:app.css")
    if re.search(r"url\s*\(", css, flags=re.IGNORECASE):
        violations.append("css-url:app.css")

    return imports, frozenset(network), tuple(sorted(set(violations)))


def _subcommands(parser: argparse.ArgumentParser, *path: str) -> frozenset[str]:
    current = parser
    for component in path:
        action = next(
            item for item in current._actions if isinstance(item, argparse._SubParsersAction)
        )
        current = action.choices[component]
    action = next(item for item in current._actions if isinstance(item, argparse._SubParsersAction))
    return frozenset(action.choices)


def _runbook_text() -> str:
    assert RUNBOOK.is_file(), "LIVE_DISABLED_EXECUTION_RUNBOOK_MISSING"
    return RUNBOOK.read_text(encoding="utf-8")


def test_shipped_posture_is_live_disabled_unverifiable_and_killed(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            implementation_state=AdapterImplementationState.LIVE_DISABLED,
            jurisdiction_review_status="ELIGIBILITY_REVIEWED",
        )
    )
    store.close()

    snapshot = build_prediction_dashboard_snapshot(database, now=NOW)
    readiness = snapshot.execution_readiness
    assert readiness.implementation_state == "LIVE_DISABLED"
    assert readiness.production_capability_available is False
    assert readiness.live_action_available is False
    assert readiness.kill_engaged is True
    assert {
        "CAPABILITY_VERIFIER_NOT_CONFIGURED",
        "EXECUTION_KILL_ENGAGED",
        "LIVE_NOT_ELIGIBLE",
    }.issubset(readiness.unmet_gates)
    assert snapshot.evidence_status.manifest_state == "LIVE_DISABLED"

    verifier = UnavailableProductionCapabilityVerifier().verify(capability_bundle=b"", now=NOW)
    assert verifier.allowed is False
    assert verifier.reason == "CAPABILITY_VERIFIER_NOT_CONFIGURED"
    assert derive_kill_state((), production=True).engaged is True

    shipped_manifests: list[Path] = []
    for path in sorted(PREDICTIONS_ROOT.rglob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and "implementation_state" in document:
            shipped_manifests.append(path.relative_to(PREDICTIONS_ROOT))
    assert shipped_manifests == []


def test_production_httpx_constructor_inventory_is_mock_only_and_nonvacuous() -> None:
    inventory = _httpx_constructor_inventory(_python_trees())
    assert len(inventory) == 4
    assert {(source_path, resolved) for source_path, _line, resolved in inventory} == {
        (Path("polymarket_execution/rest.py"), "httpx.AsyncClient"),
        (Path("polymarket_execution/rest.py"), "httpx.Request"),
        (Path("polymarket_execution/rest.py"), "httpx.Timeout"),
    }


def test_httpx_inventory_rejects_direct_aliased_and_dynamic_real_transports() -> None:
    probes = {
        "direct.py": "import httpx\nhttpx.AsyncHTTPTransport()\n",
        "aliased.py": (
            "from httpx import AsyncHTTPTransport as Wire\n"
            "Alias = Wire\n"
            "def differently_named_live_action():\n"
            "    return Alias()\n"
        ),
        "dynamic.py": (
            "import importlib as loader\nloader.import_module('httpx').AsyncHTTPTransport()\n"
        ),
        "getattr.py": "import httpx\ngetattr(httpx, 'AsyncHTTPTransport')()\n",
        "importlib-getattr.py": (
            "import importlib\ngetattr(importlib.import_module('httpx'), 'AsyncHTTPTransport')()\n"
        ),
        "module-dict.py": "import httpx\nhttpx.__dict__['AsyncHTTPTransport']()\n",
    }
    for name, source in probes.items():
        inventory = _httpx_constructor_inventory({Path(name): ast.parse(source)})
        assert any(resolved == "httpx.AsyncHTTPTransport" for _, _, resolved in inventory), name


def test_production_policy_has_no_dynamic_test_authority_or_transport_bypass() -> None:
    sources = {
        source_path.relative_to(PREDICTIONS_ROOT): source_path.read_text(encoding="utf-8")
        for source_path in sorted(PREDICTIONS_ROOT.rglob("*.py"))
    }
    assert sources
    assert _production_policy_violations(sources) == ()


def test_reviewed_authority_sensitive_source_manifest_is_exact_and_fail_closed() -> None:
    sources = _authority_sensitive_source_bytes()
    assert sources
    assert _reviewed_source_manifest_violations(sources) == ()

    rest_path = Path("polymarket_execution/rest.py")
    app_path = Path("web_assets/app.js")
    for name, mutated in {
        "changed-python": {
            **sources,
            rest_path: sources[rest_path] + b"\n# unreviewed authority change\n",
        },
        "changed-browser": {
            **sources,
            app_path: sources[app_path] + b"\n// unreviewed observer change\n",
        },
        "missing": {path: value for path, value in sources.items() if path != rest_path},
        "extra": {**sources, Path("execution/unreviewed.py"): b"pass\n"},
    }.items():
        assert _reviewed_source_manifest_violations(mutated), name


def test_stale_authority_source_is_rejected_before_import_time_effect(
    tmp_path: Path,
) -> None:
    import os
    import shutil
    import subprocess
    import sys

    sandbox = tmp_path / "sandbox"
    shutil.copytree(REPOSITORY_ROOT / "src/polytrading", sandbox / "src/polytrading")
    copied_scan = sandbox / "tests/predictions/test_execution_authority_scan.py"
    copied_scan.parent.mkdir(parents=True)
    shutil.copy2(Path(__file__), copied_scan)

    marker = sandbox / "IMPORT_EFFECT_MARKER"
    signer = sandbox / "src/polytrading/predictions/polymarket_execution/signer.py"
    signer_source = signer.read_text(encoding="utf-8")
    future_import = "from __future__ import annotations\n"
    signer.write_text(
        signer_source.replace(
            future_import,
            future_import
            + "from pathlib import Path\n"
            + f"Path({str(marker)!r}).write_text('EXECUTED', encoding='utf-8')\n",
            1,
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(sandbox / "src"), str(REPOSITORY_ROOT)))
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import runpy; runpy.run_path({str(copied_scan)!r})",
        ],
        cwd=sandbox,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "AUTHORITY_SOURCE_MANIFEST_MISMATCH" in completed.stderr
    assert not marker.exists()


def test_mock_transport_guard_symbols_and_client_owner_are_immutable() -> None:
    source_path = Path("polymarket_execution/rest.py")
    source = (PREDICTIONS_ROOT / source_path).read_text(encoding="utf-8")
    mutations = {
        "module-type-rebind": source.replace(
            "class HttpxPolymarketRestTransport:",
            "type = lambda _value: httpx.MockTransport\n\n\nclass HttpxPolymarketRestTransport:",
            1,
        ),
        "globals-type-rebind": source.replace(
            "        if type(transport) is not httpx.MockTransport:",
            '        globals()["type"] = lambda _value: httpx.MockTransport\n'
            "        if type(transport) is not httpx.MockTransport:",
            1,
        ),
        "client-overwrite": source.replace(
            "        self._timestamp = timestamp",
            "        self._client = client\n        self._timestamp = timestamp",
            1,
        ),
    }
    reviewed_sources = _authority_sensitive_source_bytes()
    for name, mutated in mutations.items():
        changed_sources = {
            **reviewed_sources,
            source_path: mutated.encode("utf-8"),
        }
        assert _reviewed_source_manifest_violations(changed_sources), name
        violations = _production_policy_violations({source_path: mutated})
        assert violations, name


@pytest.mark.parametrize(
    "source",
    (
        "import httpx\nlookup = getattr\nconstructor = lookup(httpx, "
        "'AsyncHTTPTransport')\nconstructor()\n",
        "import httpx\ntable = vars\nconstructors = table(httpx)\n"
        "constructor = constructors['AsyncHTTPTransport']\nconstructor()\n",
        "load = __import__\nwire = load('ht' + 'tpx')\nlookup = getattr\n"
        "constructor = lookup(wire, 'AsyncHTTPTransport')\nconstructor()\n",
    ),
)
def test_production_policy_rejects_staged_reflection_aliases(source: str) -> None:
    source_path = Path("polymarket_execution/rest.py")
    reviewed_sources = _authority_sensitive_source_bytes()
    changed_sources = {
        **reviewed_sources,
        source_path: reviewed_sources[source_path] + source.encode("utf-8"),
    }
    assert _reviewed_source_manifest_violations(changed_sources)
    violations = _production_policy_violations({Path("reflection.py"): source})
    assert violations
    inventory = _httpx_constructor_inventory({Path("reflection.py"): ast.parse(source)})
    assert any(resolved == "httpx.AsyncHTTPTransport" for _, _, resolved in inventory)


def test_mock_async_client_allowlist_requires_exact_guard_and_transport_provenance() -> None:
    source_path = Path("polymarket_execution/rest.py")
    source = (PREDICTIONS_ROOT / source_path).read_text(encoding="utf-8")
    guard = "if type(transport) is not httpx.MockTransport:"
    first_guard = source.replace(guard, "if not isinstance(transport, httpx.MockTransport):", 1)
    prefix, final_guard = source.rsplit(guard, 1)
    second_guard = prefix + "if not isinstance(transport, httpx.MockTransport):" + final_guard
    missing_transport = source.replace(
        "self._client = httpx.AsyncClient(\n            transport=transport,",
        "self._client = httpx.AsyncClient(",
        1,
    )
    rebound_transport = source.replace(
        "        self._client = httpx.AsyncClient(\n            transport=transport,",
        (
            "        transport = None\n"
            "        self._client = httpx.AsyncClient(\n            transport=transport,"
        ),
        1,
    )
    annotated_rebound = source.replace(
        "        self._client = httpx.AsyncClient(\n            transport=transport,",
        (
            "        transport: httpx.MockTransport | None = None\n"
            "        self._client = httpx.AsyncClient(\n            transport=transport,"
        ),
        1,
    )
    named_rebound = source.replace(
        "        self._client = httpx.AsyncClient(\n            transport=transport,",
        (
            "        (transport := None)\n"
            "        self._client = httpx.AsyncClient(\n            transport=transport,"
        ),
        1,
    )
    deleted_transport = source.replace(
        "        self._client = httpx.AsyncClient(\n            transport=transport,",
        (
            "        del transport\n"
            "        self._client = httpx.AsyncClient(\n            transport=transport,"
        ),
        1,
    )
    captured_transport = source.replace(
        "        self._client = httpx.AsyncClient(\n            transport=transport,",
        (
            "        replace_transport = lambda: transport\n"
            "        self._client = httpx.AsyncClient(\n            transport=transport,"
        ),
        1,
    )
    mutated_transport = source.replace(
        "        self._client = httpx.AsyncClient(\n            transport=transport,",
        (
            "        transport.close()\n"
            "        self._client = httpx.AsyncClient(\n            transport=transport,"
        ),
        1,
    )
    optional_transport = source.replace(
        "transport: httpx.MockTransport,\n        timestamp:",
        "transport: httpx.MockTransport | None = None,\n        timestamp:",
        1,
    )
    for name, adversarial in {
        "weak-for-test-guard": first_guard,
        "weak-initialize-guard": second_guard,
        "default-client-transport": missing_transport,
        "rebound-transport": rebound_transport,
        "annotated-rebound-transport": annotated_rebound,
        "named-rebound-transport": named_rebound,
        "deleted-transport": deleted_transport,
        "captured-transport": captured_transport,
        "mutated-transport": mutated_transport,
        "optional-default-transport": optional_transport,
    }.items():
        violations = _production_policy_violations({source_path: adversarial})
        assert any(item.startswith("socket-http-constructor:") for item in violations), name


@pytest.mark.parametrize(
    ("name", "source", "expected_prefix"),
    (
        (
            "dynamic-test-import.py",
            "import importlib as loader\nloader.import_module('te' + 'sts.predictions.fixture')\n",
            "test-import:",
        ),
        (
            "configured-key.py",
            "PRODUCTION_AUTHORITY_KEY = 'configured-key-source'\n",
            "configured-authority:",
        ),
        (
            "clear-kill.py",
            (
                "from polytrading.predictions.execution.models import KillState as State\n"
                "State(False, None)\n"
            ),
            "clear-kill:",
        ),
        (
            "renamed-live-send.py",
            (
                "import httpx as wire\n"
                "def differently_named_order_action():\n"
                "    return wire.AsyncHTTPTransport()\n"
            ),
            "socket-http-constructor:",
        ),
        (
            "default-http-client.py",
            "import httpx\nhttpx.AsyncClient()\n",
            "socket-http-constructor:",
        ),
        (
            "reflected-http-transport.py",
            "import httpx\ngetattr(httpx, 'AsyncHTTPTransport')()\n",
            "socket-http-constructor:",
        ),
        (
            "dynamic-reflected-http-transport.py",
            (
                "import importlib\n"
                "getattr(importlib.import_module('httpx'), 'AsyncHTTPTransport')()\n"
            ),
            "socket-http-constructor:",
        ),
        (
            "dict-reflected-http-transport.py",
            "import httpx\nhttpx.__dict__['AsyncHTTPTransport']()\n",
            "socket-http-constructor:",
        ),
        (
            "constant-folded-module.py",
            (
                "import importlib\n"
                "getattr(importlib.import_module('ht' + 'tpx'), "
                "'AsyncHTTPTransport')()\n"
            ),
            "socket-http-constructor:",
        ),
        (
            "variable-module.py",
            (
                "import importlib\n"
                "module_name = 'httpx'\n"
                "wire = importlib.import_module(module_name)\n"
                "getattr(wire, 'AsyncHTTPTransport')()\n"
            ),
            "socket-http-constructor:",
        ),
        (
            "unresolved-module.py",
            (
                "import importlib\n"
                "module_name = configured_module()\n"
                "wire = importlib.import_module(module_name)\n"
                "getattr(wire, 'AsyncHTTPTransport')()\n"
            ),
            "dynamic-import:",
        ),
        (
            "unresolved-attribute.py",
            (
                "import httpx\n"
                "attribute_name = configured_attribute()\n"
                "getattr(httpx, attribute_name)()\n"
            ),
            "socket-http-constructor:",
        ),
        (
            "variable-dict-attribute.py",
            (
                "import httpx\n"
                "attribute_name = 'AsyncHTTPTransport'\n"
                "httpx.__dict__[attribute_name]()\n"
            ),
            "socket-http-constructor:",
        ),
        (
            "aliased-handler.py",
            (
                "from polytrading.predictions.polymarket_execution.rest "
                "import SignerRestHandlers as Owner\n"
                "Owner(credentials=None, transport=None)\n"
            ),
            "transport-composition:",
        ),
    ),
)
def test_production_policy_rejects_adversarial_bypass_spellings(
    name: str,
    source: str,
    expected_prefix: str,
) -> None:
    violations = _production_policy_violations({Path(name): source})
    assert any(item.startswith(expected_prefix) for item in violations), name


def test_dashboard_reports_live_eligibility_without_offering_any_action(tmp_path: Path) -> None:
    database = tmp_path / "promoted.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            implementation_state=AdapterImplementationState.LIVE_ELIGIBLE,
            jurisdiction_review_status="ELIGIBILITY_REVIEWED",
        )
    )
    store.close()

    snapshot = build_prediction_dashboard_snapshot(database, now=NOW)

    # The observer mirrors the operator's own promotion and still grants nothing.
    assert snapshot.execution_readiness.implementation_state == "LIVE_ELIGIBLE"
    assert snapshot.execution_readiness.live_action_available is False
    assert snapshot.execution_readiness.production_capability_available is False
    assert snapshot.execution_readiness.kill_engaged is True


def test_dashboard_rejects_a_manifest_posture_it_does_not_understand(tmp_path: Path) -> None:
    database = tmp_path / "contradictory.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(implementation_state=AdapterImplementationState.SHADOW)
    )
    store.close()

    with pytest.raises(ValueError, match=r"^database manifest declares an unsupported posture$"):
        build_prediction_dashboard_snapshot(database, now=NOW)


def test_production_ast_has_no_issuer_kill_clearance_activation_or_test_reachability() -> None:
    trees = _python_trees()
    prohibited_definitions = {
        "activate_live_execution",
        "clear_execution_kill",
        "clear_kill_switch",
        "issue_execution_capability",
        "issue_live_capability",
    }
    definitions: set[str] = set()
    semantic_authority_definitions: list[tuple[Path, int, str]] = []
    capability_construction: list[tuple[Path, int, str]] = []
    capability_references: list[tuple[Path, int, str]] = []
    test_imports: list[tuple[Path, str]] = []
    test_only_kill_call_sites: list[tuple[Path, int]] = []
    signer_sidecar_call_sites: list[tuple[Path, int]] = []

    for path, tree in trees.items():
        capability_aliases = {"ExecutionCapability", "VerifiedExecutionCapability"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                for alias in node.names:
                    if alias.name in capability_aliases:
                        capability_aliases.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                definitions.add(node.name)
                words = set(
                    re.findall(r"[a-z0-9]+", re.sub(r"(?<!^)(?=[A-Z])", "_", node.name).casefold())
                )
                issues_capability = "capability" in words and words & {
                    "activate",
                    "build",
                    "create",
                    "generate",
                    "import",
                    "issue",
                    "mint",
                }
                clears_kill = "kill" in words and words & {
                    "clear",
                    "disengage",
                    "release",
                    "reset",
                }
                if (issues_capability or clears_kill) and not _is_pilot_authority(path):
                    semantic_authority_definitions.append((path, node.lineno, node.name))
            if isinstance(node, ast.Import):
                test_imports.extend(
                    (path, alias.name)
                    for alias in node.names
                    if alias.name == "tests" or alias.name.startswith("tests.")
                )
            if (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and (node.module == "tests" or node.module.startswith("tests."))
            ):
                test_imports.append((path, node.module))
            if not isinstance(node, ast.Call):
                if (
                    path not in _CAPABILITY_PROJECTION_PATHS
                    and not _is_pilot_authority(path)
                    and isinstance(node, ast.Name)
                    and node.id in capability_aliases
                ):
                    capability_references.append((path, node.lineno, node.id))
                continue
            name = _call_name(node)
            if name in capability_aliases and path not in _CAPABILITY_PROJECTION_PATHS:
                capability_construction.append((path, node.lineno, name))
            if name == "run_signer_sidecar" and path not in _SIGNER_LAUNCH_PATHS:
                signer_sidecar_call_sites.append((path, node.lineno))
            if any(keyword.arg == "test_only_kill_state" for keyword in node.keywords):
                test_only_kill_call_sites.append((path, node.lineno))

    assert definitions.isdisjoint(prohibited_definitions)
    assert semantic_authority_definitions == []
    assert capability_construction == []
    assert capability_references == []
    assert test_imports == []
    assert test_only_kill_call_sites == []
    assert signer_sidecar_call_sites == []

    scripts = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]
    assert scripts == {"polytrading": "polytrading.cli:main"}
    parser = build_parser()
    assert _subcommands(parser, "predictions", "execution") == {"conformance"}
    assert _subcommands(parser, "predictions", "execution", "conformance") == {"polymarket"}


def test_public_polymarket_adapter_is_separate_from_execution_modules() -> None:
    tree = ast.parse((PREDICTIONS_ROOT / "polymarket.py").read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden_roots = (
        "polytrading.predictions.execution",
        "polytrading.predictions.polymarket_execution",
    )
    assert all(not module.startswith(forbidden_roots) for module in imported_modules)


def test_coordinator_and_signer_make_independent_authority_decisions(tmp_path: Path) -> None:
    contexts: list[tuple[str, AuthorityContext]] = []

    class CoordinatorAuthority:
        def snapshot(self, intent, evidence, operation, now):
            del intent, evidence, operation
            context = authority_context(now=now)
            contexts.append(("coordinator", context))
            return context

    store = PredictionMarketStore(tmp_path / "authority.duckdb")
    intent = execution_intent()
    evidence = preflight_evidence()
    coordinator = ExecutionCoordinator(
        store=store,
        preflight=object(),  # type: ignore[arg-type]
        signer=object(),  # type: ignore[arg-type]
        account_reader=object(),  # type: ignore[arg-type]
        authority=CoordinatorAuthority(),  # type: ignore[arg-type]
        account_fingerprint=ACCOUNT_FINGERPRINT,
        clock=lambda: NOW,
    )
    assert coordinator._authority_allowed(intent, evidence, ExecutionOperation.HEARTBEAT, NOW)

    def signer_context(request: SignerRequest, observed_at: datetime) -> AuthorityContext:
        context = authority_context(
            now=observed_at,
            account_fingerprint=request.account_fingerprint,
            account_scope_account_fingerprint=request.account_fingerprint,
            manifest_record_hash=request.manifest_digest,
            verified_capability=verified_capability(capability_digest=request.capability_digest),
        )
        contexts.append(("signer", context))
        return context

    def unused_handler(_payload: object) -> SanitizedOperationResult:
        raise AssertionError("SIGNER_OPERATION_MUST_NOT_RUN")

    signer = SignerService(
        secrets=SecretMaterial(
            bytearray(b"\x01" * 32),
            bytearray(b"\x02"),
            bytearray(b"\x03"),
            bytearray(b"\x04"),
        ),
        authority_context_factory=signer_context,
        read_guard=lambda _request, _now: pytest.fail("READ_GUARD_MUST_NOT_RUN"),
        handlers=SignerOperationHandlers(
            submit_order=unused_handler,
            cancel_order=unused_handler,
            heartbeat=unused_handler,
            read_orders=unused_handler,
            read_trades=unused_handler,
            read_account=unused_handler,
        ),
        clock=lambda: NOW,
    )
    grant = signer_capability_grant(account_fingerprint=HASHES[0], now=NOW)
    request = SignerRequest(
        schema_version=1,
        request_id=UUID("11111111-1111-4111-8111-111111111111"),
        intent_id=intent.intent_id,
        intent_fingerprint=intent.intent_fingerprint,
        capability_digest=grant.digest,
        authority_proof={
            "grant": grant,
            "signature": b"cHVibGljLXNpZ25hdHVyZQ==",
        },
        manifest_digest=MANIFEST_HASH,
        account_fingerprint=HASHES[0],
        protocol_version="polymarket-clob-2026-08-25-v1",
        operation=ExecutionOperation.HEARTBEAT,
        deadline=NOW + timedelta(seconds=5),
        payload=HeartbeatPayload(
            operation=ExecutionOperation.HEARTBEAT,
            heartbeat_id="",
        ),
    )
    decision = signer._verify_mutation(
        request,
        NOW,
        verified_capability(capability_digest=request.capability_digest),
    )
    assert not isinstance(decision, str)
    assert decision.allowed
    assert [source for source, _context in contexts] == ["coordinator", "signer"]
    assert contexts[0][1] is not contexts[1][1]
    signer.close()
    store.close()

    trees = _python_trees()
    coordinator_tree = trees[Path("execution/coordinator.py")]
    signer_tree = trees[Path("polymarket_execution/signer.py")]
    coordinator_gate = next(
        node
        for node in ast.walk(coordinator_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_authority_allowed"
    )
    signer_gate = next(
        node
        for node in ast.walk(signer_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_verify_mutation"
    )
    assert "verify_mutation_authority" in {
        _call_name(node) for node in ast.walk(coordinator_gate) if isinstance(node, ast.Call)
    }
    assert "verify_mutation_authority" in {
        _call_name(node) for node in ast.walk(signer_gate) if isinstance(node, ast.Call)
    }


def test_mutation_auth_and_io_call_graph_is_closed_behind_signer_dispatch() -> None:
    trees = _python_trees()
    rest_tree = trees[Path("polymarket_execution/rest.py")]
    signer_tree = trees[Path("polymarket_execution/signer.py")]

    rest_functions = {
        node.name: node
        for node in ast.walk(rest_tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    transport_class = next(
        node
        for node in rest_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "HttpxPolymarketRestTransport"
    )
    transport_methods = {
        node.name: node
        for node in transport_class.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert {
        _call_name(node)
        for node in ast.walk(rest_functions["_build_submit_request"])
        if isinstance(node, ast.Call)
    } >= {"sign_l2_request"}
    assert {
        _call_name(node)
        for node in ast.walk(rest_functions["_build_request"])
        if isinstance(node, ast.Call)
    } >= {"sign_l2_request"}
    l2_call_lines = [
        node.lineno
        for node in ast.walk(rest_tree)
        if isinstance(node, ast.Call) and _call_name(node) == "sign_l2_request"
    ]
    assert len(l2_call_lines) == 2
    client_send_lines = [
        node.lineno
        for node in ast.walk(rest_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_client"
    ]
    assert len(client_send_lines) == 1
    assert (
        transport_methods["_execute"].lineno
        <= client_send_lines[0]
        <= (transport_methods["_execute"].end_lineno or 0)
    )
    live_client_calls = [
        node
        for node in ast.walk(transport_methods["__init__"])
        if isinstance(node, ast.Call)
        and (_resolved_python_name(node.func, {"httpx": "httpx"}) or "").startswith("httpx.")
    ]
    assert len(live_client_calls) == 1
    assert _allowed_hardened_live_async_client_call(
        Path("polymarket_execution/rest.py"), rest_tree, live_client_calls[0]
    )

    signer_functions = {
        node.name: node
        for node in ast.walk(signer_tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    handle_calls = sorted(
        (node.lineno, _call_name(node))
        for node in ast.walk(signer_functions["_handle_uncached"])
        if isinstance(node, ast.Call) and _call_name(node) in {"_verify_mutation", "_dispatch"}
    )
    assert handle_calls == [
        (
            next(line for line, name in handle_calls if name == "_verify_mutation"),
            "_verify_mutation",
        ),
        (next(line for line, name in handle_calls if name == "_dispatch"), "_dispatch"),
    ]
    assert handle_calls[0][0] < handle_calls[1][0]
    dispatch_calls = {
        _call_name(node)
        for node in ast.walk(signer_functions["_dispatch"])
        if isinstance(node, ast.Call)
    }
    assert "sign_order" in dispatch_calls


@pytest.mark.parametrize(
    "method", ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT")
)
def test_dashboard_routes_are_observer_only(tmp_path: Path, method: str) -> None:
    application = PredictionDashboardApplication(tmp_path / "unused.duckdb", clock=lambda: NOW)
    ordinary_routes = (
        "/",
        "/assets/app.css",
        "/assets/app.js",
        "/assets/api.js",
        "/assets/stream.js",
        "/assets/store.js",
        "/assets/charts.js",
        "/assets/views.js",
        "/healthz",
        "/api/v1/predictions-dashboard",
    )
    for route in ordinary_routes:
        response = application.respond(method, route, "127.0.0.1")
        assert response.status is HTTPStatus.METHOD_NOT_ALLOWED
        assert response.headers["Allow"] == "GET, HEAD"
    events = application.respond(method, "/api/v1/predictions-events", "127.0.0.1")
    assert events.status is HTTPStatus.METHOD_NOT_ALLOWED
    assert events.headers["Allow"] == "GET"


def test_browser_assets_use_only_exact_same_origin_observer_destinations() -> None:
    asset_names = {
        path.name
        for path in WEB_ASSETS_ROOT.iterdir()
        if path.is_file() and path.suffix in {".html", ".css", ".js"}
    }
    assert asset_names == EXPECTED_ASSETS

    parser = _ObserverHtml()
    parser.feed((WEB_ASSETS_ROOT / "index.html").read_text(encoding="utf-8"))
    assert not any(tag in {"form", "input", "select", "textarea"} for tag, _ in parser.elements)
    navigation_targets = {attrs["data-view"] for tag, attrs in parser.elements if tag == "button"}
    assert navigation_targets == {"overview", "markets", "execution", "ledger", "evidence"}
    linked_targets = {
        value
        for _tag, attrs in parser.elements
        for name, value in attrs.items()
        if name in {"href", "src"}
    }
    data_images = {value for value in linked_targets if value.startswith("data:image/svg+xml,")}
    assert len(data_images) == 1
    assert linked_targets == {
        "#main",
        "/assets/app.css",
        "/assets/app.js",
        *data_images,
    }

    assets = {
        path.name: path.read_text(encoding="utf-8")
        for path in WEB_ASSETS_ROOT.iterdir()
        if path.is_file() and path.suffix in {".html", ".css", ".js"}
    }
    imports, network, violations = _browser_surface_inventory(assets)
    assert imports == {
        "api.js": frozenset(),
        "app.js": frozenset({"store.js", "stream.js", "views.js"}),
        "charts.js": frozenset(),
        "store.js": frozenset(),
        "stream.js": frozenset({"api.js", "store.js"}),
        "views.js": frozenset({"charts.js"}),
    }
    assert network == {
        ("fetch", "/api/v1/predictions-dashboard", "GET"),
        ("EventSource", "/api/v1/predictions-events", "GET"),
    }
    assert violations == ()


@pytest.mark.parametrize(
    ("name", "source", "expected_prefix"),
    (
        (
            "alias.js",
            'const send = globalThis.fetch;\nsend("/api/v1/write", { method: "POST" });\n',
            "fetch-method:",
        ),
        (
            "bracket.js",
            "const Stream = globalThis['EventSource'];\nnew Stream('/external-events');\n",
            "network-destination:",
        ),
        (
            "dynamic.js",
            "const primitive = 'fetch';\nglobalThis[primitive]('/api/v1/write');\n",
            "dynamic-global-property:",
        ),
        (
            "dynamic-import.js",
            "import('./remote.js');\n",
            "dynamic-module-import:",
        ),
        (
            "bare-fetch.js",
            'fetch("/api/v1/write", { method: "POST" });\n',
            "network-primitive:",
        ),
        (
            "window-fetch.js",
            'window.fetch("https://example.invalid", { method: "POST" });\n',
            "network-primitive:",
        ),
        (
            "global-fetch.js",
            'globalThis.fetch("/api/v1/write", { method: "POST" });\n',
            "network-primitive:",
        ),
        (
            "websocket.js",
            'new WebSocket("wss://example.invalid");\n',
            "network-primitive:",
        ),
        (
            "xml-http-request.js",
            "new XMLHttpRequest();\n",
            "network-primitive:",
        ),
        (
            "computed-global.js",
            'globalThis["fet" + "ch"]("/api/v1/write", { method: "POST" });\n',
            "dynamic-global-property:",
        ),
        (
            "beacon.js",
            'navigator.sendBeacon("/api/v1/write", "body");\n',
            "network-primitive:",
        ),
        (
            "aliased-websocket.js",
            'const openSocket = WebSocket;\nnew openSocket("wss://example.invalid");\n',
            "network-primitive:",
        ),
        (
            "window-bracket-fetch.js",
            'window["fetch"]("/api/v1/write", { method: "POST" });\n',
            "dynamic-global-property:",
        ),
        (
            "computed-beacon.js",
            'navigator["send" + "Beacon"]("/api/v1/write", "body");\n',
            "dynamic-global-property:",
        ),
        (
            "decoy-method.js",
            (
                'const SNAPSHOT_PATH = "/api/v1/predictions-dashboard";\n'
                "export function x({ fetchImpl = globalThis.fetch } = {}) {\n"
                '  const decoy = { method: "GET" };\n'
                "  return fetchImpl(SNAPSHOT_PATH, { method: getMethod() });\n"
                "}\n"
            ),
            "fetch-method:",
        ),
        (
            "duplicate-method.js",
            (
                'const SNAPSHOT_PATH = "/api/v1/predictions-dashboard";\n'
                "export function x({ fetchImpl = globalThis.fetch } = {}) {\n"
                '  return fetchImpl(SNAPSHOT_PATH, { method: "GET", '
                'method: "GET" });\n'
                "}\n"
            ),
            "fetch-options:",
        ),
        (
            "spread-options.js",
            (
                'const SNAPSHOT_PATH = "/api/v1/predictions-dashboard";\n'
                "export function x({ fetchImpl = globalThis.fetch } = {}) {\n"
                '  return fetchImpl(SNAPSHOT_PATH, { ...options, method: "GET" });\n'
                "}\n"
            ),
            "fetch-options:",
        ),
        (
            "event-source-options.js",
            (
                'const EVENTS_PATH = "/api/v1/predictions-events";\n'
                "export function x({ EventSourceConstructor = globalThis.EventSource } = {}) "
                "{\n"
                "  return new EventSourceConstructor(EVENTS_PATH, { withCredentials: true });\n"
                "}\n"
            ),
            "eventsource-options:",
        ),
        (
            "function-codegen.js",
            'Function("return fetch")()("/api/v1/write");\n',
            "dynamic-code:",
        ),
        (
            "eval-codegen.js",
            'eval("fetch(\\"/api/v1/write\\")");\n',
            "dynamic-code:",
        ),
        (
            "timer-codegen.js",
            'setTimeout("fetch(\\"/api/v1/write\\")", 0);\n',
            "dynamic-code:",
        ),
    ),
)
def test_browser_inventory_rejects_alias_dynamic_and_external_network_spellings(
    name: str,
    source: str,
    expected_prefix: str,
) -> None:
    assets = {"index.html": "", "app.css": "", name: source}
    _imports, _network, violations = _browser_surface_inventory(assets)
    assert any(item.startswith(expected_prefix) for item in violations), name


@pytest.mark.parametrize(
    ("name", "source", "expected_prefix"),
    (
        (
            "template-post.js",
            'const unexpected = `${fetch("/api/v1/write", { method: "POST" })}`;\n',
            "network-primitive:",
        ),
        (
            "escaped-eval.js",
            r'\u0065val("fetch(\"/api/v1/write\", {method: \"POST\"})");' "\n",
            "dynamic-code:",
        ),
    ),
)
def test_browser_inventory_scans_template_expressions_and_escaped_identifiers(
    name: str,
    source: str,
    expected_prefix: str,
) -> None:
    source_path = Path("web_assets/app.js")
    reviewed_sources = _authority_sensitive_source_bytes()
    changed_sources = {
        **reviewed_sources,
        source_path: reviewed_sources[source_path] + source.encode("utf-8"),
    }
    assert _reviewed_source_manifest_violations(changed_sources)
    assets = {"index.html": "", "app.css": "", name: source}
    _imports, _network, violations = _browser_surface_inventory(assets)
    assert any(item.startswith(expected_prefix) for item in violations), name


def test_browser_inventory_rejects_html_and_css_external_destinations() -> None:
    assets = {
        "index.html": '<script src="https://example.invalid/app.js"></script>',
        "app.css": '@import url("https://example.invalid/theme.css");',
    }
    _imports, _network, violations = _browser_surface_inventory(assets)
    assert "html-destination:index.html" in violations
    assert "css-import:app.css" in violations
    assert "css-url:app.css" in violations


def test_sse_frames_are_revision_metadata_only() -> None:
    event = DashboardRevision(
        schema_version=1,
        event_id="7",
        revision_id="a" * 64,
        as_of=NOW,
        emitted_at=NOW + timedelta(seconds=1),
        changed_domains=(DashboardDomain.EXECUTION, DashboardDomain.LEDGER),
    )
    frame = _sse_event_frame(event)
    data_line = next(line for line in frame.splitlines() if line.startswith(b"data: "))
    document = json.loads(data_line.removeprefix(b"data: "))
    assert set(document) == {
        "schema_version",
        "revision_id",
        "as_of",
        "emitted_at",
        "changed_domains",
    }
    authoritative_totals = {
        "markets",
        "posting_count",
        "reconciliation_count",
        "realized_pnl_usd",
        "account_count",
        "opportunities",
    }
    assert set(document).isdisjoint(authoritative_totals)

    reset_frame = _sse_event_frame(
        DashboardReset(
            schema_version=1,
            event_id="8",
            latest_revision_id="b" * 64,
            emitted_at=NOW + timedelta(seconds=2),
            reason="CURSOR_NOT_AVAILABLE",
        )
    )
    reset_data_line = next(line for line in reset_frame.splitlines() if line.startswith(b"data: "))
    reset_document = json.loads(reset_data_line.removeprefix(b"data: "))
    assert set(reset_document) == {
        "schema_version",
        "latest_revision_id",
        "emitted_at",
        "reason",
    }
    assert reset_document["reason"] == "CURSOR_NOT_AVAILABLE"
    assert set(reset_document).isdisjoint(authoritative_totals)


def test_runbook_covers_every_required_recovery_and_remaining_gate() -> None:
    text = " ".join(_runbook_text().split()).casefold()
    required_contracts = (
        "inherited file descriptor",
        "FAK",
        "FOK",
        "unexpected resting order",
        "UNKNOWN",
        "cancellation ambiguity",
        "reconnect",
        "heartbeat",
        "settlement",
        "restart",
        "source-hash review",
        "kill",
        "Market Atlas",
        "45 continuous calendar days",
        "30 additional shadow calendar days",
        "Class G",
        "eligibility",
        "custody",
        "credentials",
        "capability issuer",
        "pilot review",
        "explicit user approval",
        "geographic circumvention",
        "profitability",
    )
    missing = tuple(contract for contract in required_contracts if contract.casefold() not in text)
    assert missing == (), f"RUNBOOK_CONTRACTS_MISSING:{','.join(missing)}"


# -- observer isolation from the pilot control plane ----------------------------------------

_PILOT_ONLY_MODULES = (
    "polytrading.predictions.pilot",
    "polytrading.predictions.polymarket_execution.signer",
    "polytrading.predictions.polymarket_execution.credentials",
    "polytrading.predictions.polymarket_execution.keychain_macos",
    "polytrading.predictions.polymarket_execution.auth",
    "polytrading.predictions.polymarket_execution.rest",
    "polytrading.predictions.polymarket_execution.user_stream",
    "polytrading.predictions.polymarket_execution.ipc",
)


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_the_observer_dashboard_cannot_reach_the_pilot_signer_or_credentials() -> None:
    trees = _python_trees()
    observer_paths = [
        path
        for path in trees
        if path.name.startswith("dashboard") or path.as_posix().startswith("web_assets/")
    ]

    assert observer_paths
    for path in observer_paths:
        imported = _imported_modules(trees[path])
        for module in _PILOT_ONLY_MODULES:
            assert not any(name == module or name.startswith(f"{module}.") for name in imported), (
                f"{path} imports {module}"
            )


def test_the_observer_browser_assets_never_reference_a_pilot_route() -> None:
    for path in WEB_ASSETS_ROOT.iterdir():
        if not path.is_file() or path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "/api/v1/pilot" not in text, path
        assert "pilot_session" not in text, path


def test_the_pilot_control_plane_never_imports_the_observer_server() -> None:
    trees = _python_trees()
    pilot_paths = [path for path in trees if path.as_posix().startswith("pilot/")]

    assert pilot_paths
    for path in pilot_paths:
        imported = _imported_modules(trees[path])
        assert "polytrading.predictions.dashboard_server" not in imported, path
        assert not any(name.startswith("tests") for name in imported), path
