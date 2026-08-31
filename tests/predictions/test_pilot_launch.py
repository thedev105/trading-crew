from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polytrading.predictions.pilot.launch import compose_pilot_environment
from polytrading.predictions.pilot.server import PilotRequestError
from polytrading.predictions.storage.store import PredictionMarketStore

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def test_compose_from_an_empty_store_yields_a_blocked_valid_environment(tmp_path) -> None:
    store = PredictionMarketStore(tmp_path / "pilot.duckdb")
    try:
        environment = compose_pilot_environment(
            store,
            account_fingerprint="a" * 64,
            wallet_fingerprint="a" * 64,
            credentials_present=False,
            now=lambda: NOW,
        )
        assert environment.manifest is None
        assert environment.manifest_state == "MISSING"
        assert environment.venue_binding is None
        assert environment.credentials_present is False
        assert environment.executor_factory is None
        assert environment.reconciliation.reconciliation_complete is False
        with pytest.raises(PilotRequestError, match="EXECUTION_UNAVAILABLE"):
            environment.account_state()
    finally:
        store.close()
