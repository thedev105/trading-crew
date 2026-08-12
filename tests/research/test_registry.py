from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from polytrading.research.models import (
    EvaluationWindow,
    ExperimentParameter,
    ExperimentRecord,
    ParameterValue,
    SuccessCriterion,
)
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from tests.domain.factories import NOW


def experiment_record(**overrides: object) -> ExperimentRecord:
    values: dict[str, object] = {
        "schema_version": 1,
        "experiment_id": UUID("00000000-0000-0000-0000-000000000010"),
        "hypothesis": "cross-venue funding persists after explicit costs",
        "feature_allowlist": ("venue_basis", "hourly_funding"),
        "parameters": (
            ExperimentParameter(
                name="minimum_spread",
                value=ParameterValue(kind="decimal", decimal_value=Decimal("0.0002")),
            ),
            ExperimentParameter(
                name="holding_hours",
                value=ParameterValue(kind="integer", integer_value=8),
            ),
        ),
        "evaluation_window": EvaluationWindow(
            starts_at=NOW - timedelta(days=30), ends_at=NOW - timedelta(days=1)
        ),
        "benchmark": "cash-after-operational-costs-v1",
        "success_criteria": (
            SuccessCriterion(metric="net_return", operator="gt", threshold=Decimal("0")),
            SuccessCriterion(
                metric="maximum_drawdown", operator="lt", threshold=Decimal("0.08")
            ),
        ),
        "code_revision": "0123456789abcdef",
        "data_cutoff": NOW,
        "fee_version": "bybit:VIP 0:2026-08-11T12:00:00Z",
        "trial_family_id": "carry-btc-eth-sol-v1",
    }
    values.update(overrides)
    return ExperimentRecord(**values)


def test_experiment_canonicalizes_features_parameters_and_criteria() -> None:
    record = experiment_record(
        feature_allowlist=("venue_basis", "hourly_funding"),
        parameters=tuple(reversed(experiment_record().parameters)),
        success_criteria=tuple(reversed(experiment_record().success_criteria)),
    )

    assert record.feature_allowlist == ("hourly_funding", "venue_basis")
    assert tuple(item.name for item in record.parameters) == ("holding_hours", "minimum_spread")
    assert tuple(item.metric for item in record.success_criteria) == (
        "maximum_drawdown",
        "net_return",
    )


def test_experiment_is_strict_and_frozen() -> None:
    record = experiment_record()

    with pytest.raises(ValidationError, match="frozen"):
        record.hypothesis = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs"):
        experiment_record(unregistered_field="not allowed")


def test_experiment_round_trips_unchanged_and_exact_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    record = experiment_record()

    assert store.append_experiment(record) is True
    assert store.append_experiment(record) is False
    assert store.get_experiment(record.experiment_id) == record
    store.close()


def test_conflicting_experiment_identity_is_rejected(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    original = experiment_record()
    conflict = experiment_record(hypothesis="a changed hypothesis")
    store.append_experiment(original)

    with pytest.raises(ConflictingRecordError, match="conflicting experiment"):
        store.append_experiment(conflict)

    store.close()
