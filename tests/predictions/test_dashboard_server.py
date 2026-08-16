from pathlib import Path

import pytest

from polytrading.predictions.dashboard_server import (
    PredictionDashboardLifecycleError,
    validate_prediction_dashboard_database,
)
from polytrading.predictions.storage.store import PredictionMarketStore
from polytrading.storage.store import DuckDBStore


def test_validate_requires_an_existing_database_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing database file"):
        validate_prediction_dashboard_database(tmp_path / "missing.duckdb")
    with pytest.raises(ValueError, match="existing database file"):
        validate_prediction_dashboard_database(tmp_path)


def test_validate_accepts_a_fresh_predictions_database(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    validate_prediction_dashboard_database(database)


def test_prediction_dashboard_rejects_a_perpetual_futures_database(tmp_path: Path) -> None:
    perpetual_futures_db = tmp_path / "forward.duckdb"
    DuckDBStore(perpetual_futures_db).close()

    with pytest.raises(PredictionDashboardLifecycleError):
        validate_prediction_dashboard_database(perpetual_futures_db)


def test_existing_dashboard_equally_rejects_a_fresh_predictions_database(tmp_path: Path) -> None:
    from polytrading.web.server import DashboardLifecycleError, validate_dashboard_database

    predictions_db = tmp_path / "predictions.duckdb"
    PredictionMarketStore(predictions_db).close()

    with pytest.raises(DashboardLifecycleError):
        validate_dashboard_database(predictions_db)
