import ast
import importlib
import pkgutil
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from zipfile import ZipFile

import polytrading
import polytrading.trial
from polytrading.carry.dossier import evaluate_dossier
from polytrading.carry.dossier_models import ContractCompatibilityDossier, DossierStatus

_FORBIDDEN_AUTHORITY_MODULE_LEAVES = {
    "account",
    "accounts",
    "auth",
    "authentication",
    "balance",
    "balances",
    "credential",
    "credentials",
    "execution",
    "fill",
    "fills",
    "order",
    "orders",
    "position",
    "positions",
    "private",
    "signer",
    "signing",
    "transfer",
    "transfers",
    "wallet",
    "wallets",
}


def _trial_module_names() -> tuple[str, ...]:
    child_modules = tuple(
        sorted(
            module_info.name
            for module_info in pkgutil.iter_modules(
                polytrading.trial.__path__, f"{polytrading.trial.__name__}."
            )
            if not module_info.name.rsplit(".", maxsplit=1)[-1].startswith("_")
        )
    )
    return (polytrading.trial.__name__, *child_modules)


def _forbidden_import_target(target: str) -> bool:
    components = tuple(part.casefold() for part in target.split(".") if part)
    if any(component in _FORBIDDEN_AUTHORITY_MODULE_LEAVES for component in components):
        return True
    for component in components:
        component_tokens = component.split("_")
        if "private" in component_tokens:
            return True
        if "execution" in component_tokens and component != "economics_execution":
            return True
    compact = "".join(character for character in target.casefold() if character.isalnum())
    return any(
        marker in compact
        for marker in (
            "privateclient",
            "credentialloader",
            "walletclient",
            "signerclient",
            "orderclient",
            "balanceclient",
            "positionclient",
            "fillclient",
            "transferclient",
            "executionclient",
        )
    )


def _authority_import_violations(source: str) -> tuple[str, ...]:
    violations: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        targets = [alias.name for alias in node.names]
        targets.extend(alias.asname for alias in node.names if alias.asname is not None)
        if isinstance(node, ast.ImportFrom):
            targets.append(node.module or "")
        for target in targets:
            if _forbidden_import_target(target):
                violations.append(target)
    return tuple(violations)


def test_package_exposes_installed_version() -> None:
    assert polytrading.__version__ == version("polytrading")


def test_readme_documents_prospective_trial_operations_contract() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())
    normalized_lower = normalized_readme.lower()

    assert "polytrading trial funding --current" in readme
    assert "polytrading trial books --duration-seconds 60 --interval-seconds 5" in readme
    assert "polytrading trial health --recent-hours 24" in readme
    assert "4.15 million normalized book levels" in readme
    assert "historical collection cannot repair prospective trial lineage" in readme.lower()
    assert "READY_FOR_ECONOMICS_EVALUATION is not trading authorization" in readme
    assert "complete several successful manual funding and book cycles" in normalized_lower
    assert "host clock synchronization" in normalized_lower
    assert "writable and free disk capacity" in normalized_lower
    assert "scheduler log paths and monitoring" in normalized_lower
    assert "loopback dashboard access" in normalized_lower
    assert "Prospective timing failures cannot be repaired later." in normalized_readme
    assert "scheduler trigger timezone must be UTC" in normalized_readme
    assert "half-hour or quarter-hour offset" in normalized_lower


def test_trial_module_audit_includes_package_initializer() -> None:
    assert polytrading.trial.__name__ in _trial_module_names()


def test_authority_import_audit_rejects_adversarial_import_forms() -> None:
    prohibited_sources = (
        "from polytrading.venues import orders",
        "from polytrading.venues import private_client",
        "from polytrading.venues.private import DydxPrivateClient",
        "import polytrading.venues.live_execution",
    )

    for source in prohibited_sources:
        assert _authority_import_violations(source), source


def test_authority_import_audit_allows_public_research_imports() -> None:
    benign_sources = (
        "from polytrading.venues.public import PublicVenueAdapter",
        "from polytrading.carry.economics_execution import PairedBookObservation",
    )

    for source in benign_sources:
        assert _authority_import_violations(source) == (), source


def test_every_public_trial_module_imports_without_authority_surfaces() -> None:
    module_names = _trial_module_names()
    assert module_names
    forbidden_symbols = {
        "cancel_order",
        "create_order",
        "load_credentials",
        "place_order",
        "private_client",
        "sign_order",
        "withdraw",
    }

    for module_name in module_names:
        module = importlib.import_module(module_name)
        source_path = Path(module.__file__ or "")
        source = source_path.read_text(encoding="utf-8")
        assert _authority_import_violations(source) == (), module_name
        tree = ast.parse(source, filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Name, ast.Attribute, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbol = getattr(node, "id", None) or getattr(node, "attr", None) or node.name
                assert symbol.lower() not in forbidden_symbols, (
                    f"{module_name} exposes authority symbol {symbol}"
                )


def test_built_wheel_contains_valid_contract_dossier(tmp_path: Path) -> None:
    source_checkout = tmp_path / "source"
    wheel_directory = tmp_path / "wheels"
    shutil.copytree(
        Path.cwd(),
        source_checkout,
        ignore=shutil.ignore_patterns(
            ".coverage", ".git", ".venv", ".worktrees", "*.egg-info", "__pycache__", "build"
        ),
    )
    wheel_directory.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            str(source_checkout),
            "--wheel-dir",
            str(wheel_directory),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=source_checkout,
    )
    wheel = next(wheel_directory.glob("polytrading-*.whl"))
    members = [
        "polytrading/carry/dossiers/hyperliquid-dydx-core-v1.json",
        "polytrading/carry/dossiers/lighter-dydx-core-v1.json",
    ]

    with ZipFile(wheel) as archive:
        dossier_members = [name for name in archive.namelist() if name.endswith(".json")]
        assert dossier_members == members
        migration_members = [
            name
            for name in archive.namelist()
            if name.startswith("polytrading/storage/schema/") and name.endswith(".sql")
        ]
        assert migration_members == [
            "polytrading/storage/schema/001_initial.sql",
            "polytrading/storage/schema/002_ai_registry.sql",
            "polytrading/storage/schema/003_forward_funding_cycles.sql",
            "polytrading/storage/schema/004_economic_evaluations.sql",
            "polytrading/storage/schema/005_lighter_dydx_trial_operations.sql",
        ]
        dossiers = tuple(
            ContractCompatibilityDossier.model_validate_json(archive.read(member))
            for member in members
        )

    legacy, candidate = (evaluate_dossier(dossier) for dossier in dossiers)
    assert legacy.status is DossierStatus.INELIGIBLE
    assert legacy.primary_reason_code == "quanto_structure_excluded"
    assert candidate.status is DossierStatus.MODEL_REQUIRED
    assert candidate.counts.blocking == 0
    assert candidate.counts.missing_evidence == 0
    assert not Path("build").exists()
