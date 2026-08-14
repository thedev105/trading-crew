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


def test_package_exposes_installed_version() -> None:
    assert polytrading.__version__ == version("polytrading")


def test_readme_documents_prospective_trial_operations_contract() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "polytrading trial funding --current" in readme
    assert "polytrading trial books --duration-seconds 60 --interval-seconds 5" in readme
    assert "polytrading trial health --recent-hours 24" in readme
    assert "4.15 million normalized book levels" in readme
    assert "historical collection cannot repair prospective trial lineage" in readme.lower()
    assert "READY_FOR_ECONOMICS_EVALUATION is not trading authorization" in readme


def test_every_public_trial_module_imports_without_authority_surfaces() -> None:
    module_names = tuple(
        sorted(
            module_info.name
            for module_info in pkgutil.iter_modules(
                polytrading.trial.__path__, f"{polytrading.trial.__name__}."
            )
            if not module_info.name.rsplit(".", maxsplit=1)[-1].startswith("_")
        )
    )
    assert module_names

    forbidden_module_leaves = {
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
        "signer",
        "signing",
        "transfer",
        "transfers",
        "wallet",
        "wallets",
    }
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
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported_modules = (
                    tuple(alias.name for alias in node.names)
                    if isinstance(node, ast.Import)
                    else ((node.module or ""),)
                )
                for imported_module in imported_modules:
                    leaf = imported_module.rsplit(".", maxsplit=1)[-1]
                    assert leaf not in forbidden_module_leaves, (
                        f"{module_name} imports authority module {imported_module}"
                    )
                    assert "private_client" not in imported_module, (
                        f"{module_name} imports private venue client {imported_module}"
                    )
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
