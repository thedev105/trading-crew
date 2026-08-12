from importlib.metadata import version

import polytrading


def test_package_exposes_installed_version() -> None:
    assert polytrading.__version__ == version("polytrading")
