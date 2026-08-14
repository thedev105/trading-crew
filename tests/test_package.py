import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from zipfile import ZipFile

import polytrading
from polytrading.carry.dossier import evaluate_dossier
from polytrading.carry.dossier_models import ContractCompatibilityDossier, DossierStatus


def test_package_exposes_installed_version() -> None:
    assert polytrading.__version__ == version("polytrading")


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
