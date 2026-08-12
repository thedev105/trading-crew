import asyncio
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
from pydantic import TypeAdapter

from polytrading.carry.audit import CarryAuditor
from polytrading.carry.report import render_json, render_text
from polytrading.domain.models import Asset
from polytrading.registry.instruments import InstrumentRegistry
from polytrading.storage.store import DuckDBStore
from polytrading.venues.bybit import BybitPublicAdapter
from polytrading.venues.hyperliquid import HyperliquidPublicAdapter
from polytrading.venues.public import AdapterBatch
from polytrading.venues.recorder import append_normalized

AS_OF = datetime(2026, 8, 12, 12, tzinfo=UTC)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "replay" / "public_snapshot.jsonl"
BYBIT_INSTRUMENT_HASH = "5f7f6b210a933e27014fcff8ce7ba9fad818bb47bc468dca876cc105be2a65be"
HYPERLIQUID_INSTRUMENT_HASH = "39f07abcf32422ef52f766231e28285f3fa5cfa4fdffc5689b71f71ea9dfcde3"
BYBIT_FUNDING_HASHES = {
    "BTC": "892f64c74b53a941814ee2ae2108626cfbb5308cd7fa4ea4ca0ebf88982109d8",
    "ETH": "609022ee277fc3ea08ecb14edf65801ecfbab81bb234666670b11b10629627d2",
    "SOL": "b9eab29c6039671cf1b6816234f5f4fb452d79878b5d645ebf02e97dfd46e6d0",
}
HYPERLIQUID_FUNDING_HASHES = {
    "BTC": "0e359749b76c1ba39c334a52a361e45e5095b0c194af3f4c642e7413f59938d4",
    "ETH": "3bc17cb379b110cfdedc527d2665854121aeff2a523522e5981f1eaf279947af",
    "SOL": "2e5c023a5024ca3f078dee0964633d935aae0a0ed1035b40a18e4a53b95a79b8",
}


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


def test_json_keeps_two_venue_evidence_rows_with_null_missing_sides(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "missing.duckdb")

    payload = json.loads(render_json(_auditor(store).audit(AS_OF)))

    assert payload["assets"][0]["funding_evidence"] == [
        {
            "funding_effective_at": None,
            "funding_observed_at": None,
            "funding_source_hash": None,
            "hourly_rate": None,
            "instrument_observed_at": None,
            "instrument_source_hash": None,
            "schema_version": 1,
            "symbol": "BTCUSDT",
            "venue": "bybit",
        },
        {
            "funding_effective_at": None,
            "funding_observed_at": None,
            "funding_source_hash": None,
            "hourly_rate": None,
            "instrument_observed_at": None,
            "instrument_source_hash": None,
            "schema_version": 1,
            "symbol": "BTC",
            "venue": "hyperliquid",
        },
    ]
    store.close()


def test_replay_fixture_regenerates_normalized_records_through_public_adapters(
    tmp_path: Path,
) -> None:
    batches = _fixture_batches()
    expected_endpoints = (
        ("/v5/market/instruments-info",),
        ("/v5/market/funding/history",),
        ("/v5/market/funding/history",),
        ("/v5/market/funding/history",),
        ("/info",),
        ("/info",),
        ("/info",),
        ("/info",),
    )

    assert (
        tuple(tuple(raw.endpoint for raw in batch.raw) for batch in batches) == expected_endpoints
    )
    assert all(
        sha256(raw.payload_json.encode()).hexdigest() == raw.source_hash
        for batch in batches
        for raw in batch.raw
    )
    assert all(
        {record.source_hash for record in batch.normalized}
        <= {raw.source_hash for raw in batch.raw}
        for batch in batches
    )

    rebuilt = asyncio.run(_derive_fixture_batches(batches, tmp_path))

    assert len(rebuilt) == len(batches)
    for stored, derived in zip(batches, rebuilt, strict=True):
        assert tuple(raw.payload_json for raw in derived.raw) == tuple(
            raw.payload_json for raw in stored.raw
        )
        assert derived.normalized == stored.normalized


def _fixture_batches() -> tuple[AdapterBatch, ...]:
    return tuple(
        TypeAdapter(AdapterBatch).validate_json(line)
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
    )


class _Sequence:
    def __init__(self, values: list[Any]) -> None:
        self._values = iter(values)

    def __call__(self) -> Any:
        return next(self._values)


async def _derive_fixture_batches(
    stored: tuple[AdapterBatch, ...], tmp_path: Path
) -> tuple[AdapterBatch, ...]:
    bybit_raw = tuple(raw for batch in stored[:4] for raw in batch.raw)
    hyperliquid_raw = tuple(raw for batch in stored[4:] for raw in batch.raw)
    bybit_responses = iter(bybit_raw)
    hyperliquid_responses = iter(hyperliquid_raw)

    def response(request: httpx.Request, raws: Any) -> httpx.Response:
        raw = next(raws)
        assert request.url.path == raw.endpoint
        return httpx.Response(
            200,
            content=raw.payload_json.encode(),
            headers={"content-type": "application/json"},
            request=request,
        )

    registry_store = DuckDBStore(tmp_path / "fixture-registry.duckdb")
    registry = InstrumentRegistry(registry_store)
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response(request, bybit_responses))
        ) as bybit_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response(request, hyperliquid_responses))
        ) as hyperliquid_client,
    ):
        bybit = BybitPublicAdapter(
            bybit_client,
            wall_clock=_Sequence([raw.observed_at for raw in bybit_raw]),
            monotonic_ns=_Sequence(list(range(100, 100 + 200 * len(bybit_raw), 100))),
            instrument_registry=registry,
        )
        hyperliquid = HyperliquidPublicAdapter(
            hyperliquid_client,
            wall_clock=_Sequence([raw.observed_at for raw in hyperliquid_raw]),
            monotonic_ns=_Sequence(list(range(10_000, 10_000 + 200 * len(hyperliquid_raw), 100))),
        )
        assets = frozenset({Asset.BTC, Asset.ETH, Asset.SOL})
        bybit_instruments = await bybit.fetch_instruments(assets, AS_OF)
        for instrument in bybit_instruments.normalized:
            registry.record(instrument)
        derived: list[AdapterBatch] = [bybit_instruments]
        for asset, expected in zip(Asset, stored[1:4], strict=True):
            observation = expected.normalized[0]
            derived.append(
                await bybit.fetch_funding_history(
                    asset,
                    observation.effective_at,
                    observation.effective_at,
                    AS_OF,
                )
            )
        derived.append(await hyperliquid.fetch_instruments(assets, AS_OF))
        for asset, expected in zip(Asset, stored[5:8], strict=True):
            observation = expected.normalized[0]
            derived.append(
                await hyperliquid.fetch_funding_history(
                    asset,
                    observation.effective_at,
                    observation.effective_at,
                    AS_OF,
                )
            )
    registry_store.close()
    return tuple(derived)


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
    funding_observed_at = {
        "BTC": "2026-08-12T11:59:30Z",
        "ETH": "2026-08-12T11:59:31Z",
        "SOL": "2026-08-12T11:59:32Z",
    }[asset]
    compatibility_reasons = [
        "collateral_mismatch",
        "pnl_asset_mismatch",
    ]
    if asset in {"BTC", "ETH"}:
        compatibility_reasons.append("funding_interval_mismatch")
    compatibility_reasons.extend(
        [
            "missing_metadata:index_family",
            "missing_metadata:oracle_family",
            "missing_metadata:mark_method",
            "missing_metadata:liquidation_method",
            "missing_metadata:funding_formula_id",
            "missing_metadata:funding_cap",
            "missing_metadata:funding_payment_offset_minutes",
        ]
    )
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
                "reasons": compatibility_reasons,
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
                "funding_effective_at": "2026-08-12T11:30:00Z",
                "funding_observed_at": funding_observed_at,
                "funding_source_hash": BYBIT_FUNDING_HASHES[asset],
                "hourly_rate": long_rate,
                "instrument_observed_at": "2026-08-12T11:00:00Z",
                "instrument_source_hash": BYBIT_INSTRUMENT_HASH,
                "schema_version": 1,
                "symbol": bybit_symbol,
                "venue": "bybit",
            },
            {
                "funding_effective_at": "2026-08-12T11:30:00Z",
                "funding_observed_at": funding_observed_at,
                "funding_source_hash": HYPERLIQUID_FUNDING_HASHES[asset],
                "hourly_rate": short_rate,
                "instrument_observed_at": "2026-08-12T11:00:00Z",
                "instrument_source_hash": HYPERLIQUID_INSTRUMENT_HASH,
                "schema_version": 1,
                "symbol": hyperliquid_symbol,
                "venue": "hyperliquid",
            },
        ],
        "funding_ready": True,
        "reason_codes": [*compatibility_reasons, "BOOK_EVIDENCE_MISSING"],
        "schema_version": 1,
        "status": "INELIGIBLE",
    }
