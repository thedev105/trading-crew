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
    member = "polytrading/carry/dossiers/hyperliquid-dydx-core-v1.json"

    with ZipFile(wheel) as archive:
        dossier_members = [name for name in archive.namelist() if name.endswith(".json")]
        assert dossier_members == [member]
        dossier = ContractCompatibilityDossier.model_validate_json(archive.read(member))

    report = evaluate_dossier(dossier)
    assert report.status is DossierStatus.INELIGIBLE
    assert report.primary_reason_code == "quanto_structure_excluded"
    assert not Path("build").exists()
