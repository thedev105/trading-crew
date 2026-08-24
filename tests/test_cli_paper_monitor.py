from datetime import UTC, datetime, timedelta
from pathlib import Path

from polytrading.cli import main


def test_monitor_with_no_open_positions_exits_zero(tmp_path: Path, capsys) -> None:
    from polytrading.storage.store import DuckDBStore

    db = tmp_path / "test.duckdb"
    DuckDBStore(db).close()
    exit_code = main(["trial", "paper", "monitor", "--db", str(db)])
    assert exit_code == 0
    assert "no open paper positions" in capsys.readouterr().out.lower()


def test_monitor_closes_at_max_horizon_despite_insufficient_funding_coverage(
    tmp_path: Path, capsys
) -> None:
    """Reproduces Fix 2: the 28-day max-horizon close must not be gated behind
    trailing-week funding coverage.

    A position open for 29 days with zero paired funding hours in its
    trailing week (no funding evidence seeded at all) must still be closed
    with MAX_HORIZON_REACHED by the monitor, rather than reported as "held".
    """
    from polytrading.domain.models import Asset, Venue
    from polytrading.storage.store import DuckDBStore
    from tests.domain.factories import instrument_spec
    from tests.storage.test_store_paper_positions import _position
    from tests.trial.test_book_evidence import append_pair

    db = tmp_path / "test.duckdb"
    opened_at = datetime(2026, 7, 1, tzinfo=UTC)
    as_of = opened_at + timedelta(days=29)

    store = DuckDBStore(db)
    try:
        position = _position(asset=Asset.BTC, opened_at=opened_at)
        store.append_paper_position(position)

        # A close-eligible Lighter/dYdX book pair, known as of `as_of`, with
        # deliberately NO funding evidence seeded anywhere — the trailing-week
        # coverage the regime check needs is entirely absent.
        append_pair(store, 1, as_of, asset=Asset.BTC)

        store.append_instrument(
            instrument_spec(
                instrument_id="lighter:BTC:linear_perpetual",
                venue=Venue.LIGHTER,
                symbol="BTC",
                asset=Asset.BTC,
                observed_at=opened_at,
            )
        )
        store.append_instrument(
            instrument_spec(
                instrument_id="dydx:BTC-USD:linear_perpetual",
                venue=Venue.DYDX,
                symbol="BTC-USD",
                asset=Asset.BTC,
                observed_at=opened_at,
            )
        )
    finally:
        store.close()

    exit_code = main(["trial", "paper", "monitor", "--db", str(db), "--as-of", as_of.isoformat()])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "MAX_HORIZON_REACHED" in out
    assert "held" not in out.lower()

    store = DuckDBStore(db, read_only=True)
    try:
        closure = store.paper_position_closure(position.position_id)
        assert closure is not None
        assert closure.close_reason.value == "MAX_HORIZON_REACHED"
    finally:
        store.close()


def test_monitor_isolates_one_asset_error_from_the_others(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Reproduces Fix 3: one asset's processing failure must not prevent the
    monitor from reporting the other assets' status for that run.
    """
    from polytrading.domain.models import Asset
    from polytrading.storage.store import DuckDBStore
    from tests.storage.test_store_paper_positions import _position

    db = tmp_path / "test.duckdb"
    opened_at = datetime(2026, 8, 1, tzinfo=UTC)
    as_of = opened_at + timedelta(hours=1)

    store = DuckDBStore(db)
    try:
        from uuid import uuid4

        store.append_paper_position(_position(asset=Asset.BTC, opened_at=opened_at))
        store.append_paper_position(
            _position(asset=Asset.ETH, opened_at=opened_at, position_id=uuid4())
        )
        store.append_paper_position(
            _position(asset=Asset.SOL, opened_at=opened_at, position_id=uuid4())
        )
    finally:
        store.close()

    original = DuckDBStore.open_paper_position_for_asset

    def _boom(self, asset):
        if asset is Asset.BTC:
            raise RuntimeError("simulated failure for BTC")
        return original(self, asset)

    monkeypatch.setattr(DuckDBStore, "open_paper_position_for_asset", _boom)

    exit_code = main(["trial", "paper", "monitor", "--db", str(db), "--as-of", as_of.isoformat()])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "BTC: error (simulated failure for BTC)" in out
    assert "ETH" in out
    assert "SOL" in out
