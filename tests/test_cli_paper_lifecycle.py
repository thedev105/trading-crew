"""End-to-end integration test for the forward paper-execution CLI.

Exercises `polytrading.cli.main` for `trial paper open`, `trial paper monitor`,
and `trial paper close` back to back against a real `DuckDBStore`, wiring
together the writer lease, book-cycle eligibility lookup, instrument lookup,
symbol mapping, and transactional writes — none of which the smaller,
per-command tests in `tests/test_cli_paper.py` and
`tests/test_cli_paper_monitor.py` exercise together.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import polytrading.cli as cli_module
from polytrading.cli import main
from polytrading.domain.models import Asset, Venue
from polytrading.storage.store import DuckDBStore
from polytrading.web.dashboard import DashboardBuilder
from tests.carry.test_economics_models import KNOWN_AS_OF, report
from tests.domain.factories import instrument_spec
from tests.trial.funding_helpers import trial_funding_cycle
from tests.trial.test_book_evidence import append_pair
from tests.trial.test_funding_lineage import funding_observation as trial_funding_observation

_TRAILING_WEEK_HOURS = 168
_FUNDING_SOURCE_HASH = "2" * 64


def _seed_evaluation_and_evidence(
    store: DuckDBStore,
    *,
    evaluation_id: UUID,
    opened_at: datetime,
) -> None:
    """Seed a SHADOW_CANDIDATE evaluation plus a current, eligible book pair
    and instrument specs for both venues — everything `trial paper open`
    needs to succeed for BTC.
    """
    economics_report = report(
        evaluation_id=evaluation_id,
        evaluated_at=KNOWN_AS_OF + timedelta(minutes=30),
    )
    store.append_economic_evaluation(economics_report)

    append_pair(store, 1, opened_at, asset=Asset.BTC)

    store.append_instrument(
        instrument_spec(
            instrument_id="lighter:BTC:linear_perpetual",
            venue=Venue.LIGHTER,
            symbol="BTC",
            asset=Asset.BTC,
            observed_at=opened_at - timedelta(days=1),
        )
    )
    store.append_instrument(
        instrument_spec(
            instrument_id="dydx:BTC-USD:linear_perpetual",
            venue=Venue.DYDX,
            symbol="BTC-USD",
            asset=Asset.BTC,
            observed_at=opened_at - timedelta(days=1),
        )
    )


def _seed_trailing_week_of_paired_funding(
    store: DuckDBStore, opened_at: datetime
) -> tuple[datetime, ...]:
    """Seed exactly 168 consecutive hourly boundaries of paired, captured
    Lighter/dYdX BTC funding, starting two hours after `opened_at`.

    Returns the boundaries in order; the last one is the hour the monitor
    should accrue against.
    """
    boundaries = tuple(
        opened_at + timedelta(hours=2 + offset) for offset in range(_TRAILING_WEEK_HOURS)
    )
    for index, boundary in enumerate(boundaries):
        cycle = trial_funding_cycle(
            cycle_id=UUID(int=10_000 + index),
            cycle_end=boundary,
            request_started_at=boundary + timedelta(seconds=10),
            request_completed_at=boundary + timedelta(seconds=20),
        )
        store.append_lighter_dydx_funding_cycle(cycle)
        store.append_funding(
            trial_funding_observation(
                venue=Venue.LIGHTER,
                symbol="BTC",
                asset=Asset.BTC,
                rate=Decimal("0.0002"),
                interval_hours=Decimal("1"),
                effective_at=boundary,
                observed_at=boundary + timedelta(seconds=12),
                source_hash=_FUNDING_SOURCE_HASH,
            )
        )
        store.append_funding(
            trial_funding_observation(
                venue=Venue.DYDX,
                symbol="BTC-USD",
                asset=Asset.BTC,
                rate=Decimal("0.0001"),
                interval_hours=Decimal("1"),
                effective_at=boundary,
                observed_at=boundary + timedelta(seconds=12),
                source_hash=_FUNDING_SOURCE_HASH,
            )
        )
    return boundaries


def test_paper_position_full_lifecycle_through_the_cli(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    db = tmp_path / "test.duckdb"
    evaluation_id = uuid4()
    opened_at = KNOWN_AS_OF + timedelta(hours=1)

    store = DuckDBStore(db)
    try:
        _seed_evaluation_and_evidence(store, evaluation_id=evaluation_id, opened_at=opened_at)
        boundaries = _seed_trailing_week_of_paired_funding(store, opened_at)
    finally:
        store.close()

    current_time = {"value": opened_at}
    monkeypatch.setattr(cli_module, "_utc_now", lambda: current_time["value"])

    # 1. Open.
    exit_code = main(
        [
            "trial",
            "paper",
            "open",
            "--evaluation-id",
            str(evaluation_id),
            "--db",
            str(db),
            "--confirm",
        ]
    )
    assert exit_code == 0
    capsys.readouterr()

    store = DuckDBStore(db, read_only=True)
    try:
        position = store.open_paper_position_for_asset(Asset.BTC)
    finally:
        store.close()
    assert position is not None
    assert position.source_evaluation_id == evaluation_id
    position_id = position.position_id

    # 2. Monitor — accrues funding for the latest fully-known paired hour.
    # The last seeded boundary is exactly `as_of - 60s` known-by-cutoff-wise
    # (its `observed_at` is 12s after the boundary, well inside the 60s
    # margin below), so it is the hour the monitor accrues against.
    as_of_monitor = boundaries[-1] + timedelta(seconds=60)
    exit_code = main(
        ["trial", "paper", "monitor", "--db", str(db), "--as-of", as_of_monitor.isoformat()]
    )
    assert exit_code == 0
    monitor_out = capsys.readouterr().out
    assert "accrued funding" in monitor_out.lower()

    store = DuckDBStore(db, read_only=True)
    try:
        realized_funding = store.paper_position_realized_funding(position_id)
    finally:
        store.close()
    assert realized_funding != Decimal(0)

    # 3. Close.
    close_time = as_of_monitor + timedelta(minutes=10)
    current_time["value"] = close_time
    store = DuckDBStore(db)
    try:
        append_pair(store, 2, close_time, asset=Asset.BTC)
    finally:
        store.close()

    exit_code = main(
        [
            "trial",
            "paper",
            "close",
            "--position-id",
            str(position_id),
            "--db",
            str(db),
            "--confirm",
        ]
    )
    assert exit_code == 0
    close_out = capsys.readouterr().out
    assert "realized pnl" in close_out.lower()

    store = DuckDBStore(db, read_only=True)
    try:
        closure = store.paper_position_closure(position_id)
        assert closure is not None
        assert closure.realized_funding_usd == realized_funding
        # Sensible: funding-only pnl plus a bounded round-trip spread cost,
        # not None/NaN and not wildly divergent from the funding component.
        assert closure.realized_pnl_usd.is_finite()
        assert abs(closure.realized_pnl_usd - closure.realized_funding_usd) < Decimal("10")

        # 4. Dashboard snapshot renders the closed position correctly.
        snapshot = DashboardBuilder(store, db).build(close_time + timedelta(minutes=1))
    finally:
        store.close()

    rows_by_id = {row.position_id: row for row in snapshot.paper_position_rows}
    assert position_id in rows_by_id
    row = rows_by_id[position_id]
    assert row.status == "CLOSED_OPERATOR_CLOSED"
    assert row.current_pnl_usd == closure.realized_pnl_usd
