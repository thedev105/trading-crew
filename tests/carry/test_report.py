import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter

from polytrading.carry.audit import CarryAuditor
from polytrading.carry.report import render_json, render_text
from polytrading.storage.store import DuckDBStore
from polytrading.venues.public import AdapterBatch
from polytrading.venues.recorder import append_normalized

AS_OF = datetime(2026, 8, 12, 12, tzinfo=UTC)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "replay" / "public_snapshot.jsonl"
BYBIT_HASH = "cbfc35388e5103bdd9428027d6d6b124942ccc9cd7ce184166652be36d71dde0"
HYPERLIQUID_HASH = "edadab03d79952f09a39c9ec1848ae1b7c7fafae6812b7883d31c0947c992fa4"


def test_fixed_replay_fixture_renders_byte_deterministic_canonical_json(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "report.duckdb")
    _load_public_fixture(store)

    rendered = render_json(_auditor(store).audit(AS_OF))

    expected = {
        "as_of": "2026-08-12T12:00:00Z",
        "assets": [
            _expected_asset(
                "BTC", "BTCUSDT", "BTC", "0.0000125", "0.0002", "0.0001875", "1.6425000"
            ),
            _expected_asset("ETH", "ETHUSDT", "ETH", "0.00001", "0.00015", "0.00014", "1.22640"),
            _expected_asset("SOL", "SOLUSDT", "SOL", "-0.00001", "0.00025", "0.00026", "2.27760"),
        ],
        "schema_version": 1,
    }
    assert rendered == json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True)
    assert render_json(_auditor(store).audit(AS_OF)) == rendered
    store.close()


def test_text_report_has_stable_warnings_rows_and_activation_footer(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "report.duckdb")
    _load_public_fixture(store)

    rendered = render_text(_auditor(store).audit(AS_OF))

    assert rendered.splitlines()[:3] == [
        "RESEARCH ONLY — NOT A TRADE RECOMMENDATION",
        "No credentials, balances, positions, or orders were accessed.",
        "Instantaneous annualization is diagnostic, not a funding forecast.",
    ]
    assert [line.split(" | ", 1)[0] for line in rendered.splitlines() if " | " in line] == [
        "BTC",
        "ETH",
        "SOL",
    ]
    for evidence in (
        "12 months point-in-time history",
        "45 continuous days of synchronized books",
        "fee and slippage models",
        "reversal/forced-exit reserve",
        "complete stress suite",
        "90 forward days",
        "ledger reconciliation",
        "eligibility review",
    ):
        assert f"- {evidence}" in rendered
    store.close()


def test_replay_fixture_has_coherent_raw_provenance_for_every_normalized_record() -> None:
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        batch = TypeAdapter(AdapterBatch).validate_json(line)
        raw = batch.raw

        assert raw
        assert all(
            sha256(item.payload_json.encode()).hexdigest() == item.source_hash for item in raw
        )
        assert {item.source_hash for item in batch.normalized} <= {item.source_hash for item in raw}


def _auditor(store: DuckDBStore) -> CarryAuditor:
    return CarryAuditor(
        store,
        max_instrument_age=timedelta(hours=1),
        max_funding_age=timedelta(hours=2),
        max_book_age=timedelta(seconds=10),
        max_book_cycle_skew=timedelta(seconds=1),
    )


def _load_public_fixture(store: DuckDBStore) -> None:
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        batch = TypeAdapter(AdapterBatch).validate_json(line)
        with store.transaction() as transaction:
            for raw in batch.raw:
                transaction.append_raw(raw)
            for normalized in batch.normalized:
                append_normalized(transaction, normalized)


def _expected_asset(
    asset: str,
    bybit_symbol: str,
    hyperliquid_symbol: str,
    long_rate: str,
    short_rate: str,
    spread: str,
    annualized: str,
) -> dict[str, object]:
    return {
        "asset": asset,
        "book_cycle_id": None,
        "book_cycle_skew_ms": None,
        "book_evidence": [],
        "book_ready": False,
        "diagnostic": {
            "as_of": "2026-08-12T12:00:00Z",
            "asset": asset,
            "compatibility": {
                "compatible": False,
                "reasons": [
                    "mark_method_mismatch",
                    "liquidation_method_mismatch",
                    "collateral_mismatch",
                    "pnl_asset_mismatch",
                    "funding_formula_mismatch",
                    "funding_cap_mismatch",
                    "funding_interval_mismatch",
                ],
            },
            "diagnostic_annualized_spread": annualized,
            "forecast_status": "not_evaluated",
            "hourly_spread": spread,
            "long_hourly_rate": long_rate,
            "long_symbol": bybit_symbol,
            "long_venue": "bybit",
            "schema_version": 1,
            "short_hourly_rate": short_rate,
            "short_symbol": hyperliquid_symbol,
            "short_venue": "hyperliquid",
        },
        "forecast_status": "not_evaluated",
        "funding_evidence": [
            {
                "funding_effective_at": "2026-08-12T11:00:00Z",
                "funding_observed_at": "2026-08-12T11:59:30Z",
                "funding_source_hash": BYBIT_HASH,
                "hourly_rate": long_rate,
                "instrument_observed_at": "2026-08-12T11:59:00Z",
                "instrument_source_hash": BYBIT_HASH,
                "schema_version": 1,
                "symbol": bybit_symbol,
                "venue": "bybit",
            },
            {
                "funding_effective_at": "2026-08-12T11:00:00Z",
                "funding_observed_at": "2026-08-12T11:59:30Z",
                "funding_source_hash": HYPERLIQUID_HASH,
                "hourly_rate": short_rate,
                "instrument_observed_at": "2026-08-12T11:59:00Z",
                "instrument_source_hash": HYPERLIQUID_HASH,
                "schema_version": 1,
                "symbol": hyperliquid_symbol,
                "venue": "hyperliquid",
            },
        ],
        "funding_ready": True,
        "reason_codes": [
            "mark_method_mismatch",
            "liquidation_method_mismatch",
            "collateral_mismatch",
            "pnl_asset_mismatch",
            "funding_formula_mismatch",
            "funding_cap_mismatch",
            "funding_interval_mismatch",
            "BOOK_EVIDENCE_MISSING",
        ],
        "schema_version": 1,
        "status": "INELIGIBLE",
    }
