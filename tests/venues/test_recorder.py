from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb
import pytest

from polytrading.domain.models import Asset, FundingObservation, RawEnvelope, Venue
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from polytrading.venues.public import AdapterBatch
from polytrading.venues.recorder import PublicRecorder, make_raw_envelope

NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)
SOURCE_HASH = "a" * 64


def raw_envelope(**overrides: object) -> RawEnvelope:
    values: dict[str, object] = {
        "schema_version": 1,
        "event_id": UUID("00000000-0000-0000-0000-000000000001"),
        "venue": Venue.BYBIT,
        "endpoint": "/v5/market/funding/history",
        "venue_timestamp": NOW - timedelta(milliseconds=25),
        "observed_at": NOW,
        "received_monotonic_ns": 12_345_678_901,
        "request_latency_ms": Decimal("1.234567"),
        "source_version": "v5",
        "payload_json": '{ "result": {"rate": "0.0001"} }\n',
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return RawEnvelope(**values)


def funding_observation(**overrides: object) -> FundingObservation:
    values: dict[str, object] = {
        "schema_version": 1,
        "venue": Venue.BYBIT,
        "symbol": "BTCUSDT",
        "asset": Asset.BTC,
        "rate": Decimal("0.0001"),
        "interval_hours": Decimal("8"),
        "effective_at": NOW - timedelta(hours=1),
        "observed_at": NOW,
        "source_hash": SOURCE_HASH,
    }
    values.update(overrides)
    return FundingObservation(**values)


class StoreSpy:
    def __init__(self, *, fail_raw: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_raw = fail_raw

    @contextmanager
    def transaction(self) -> Iterator[StoreSpy]:
        self.calls.append("begin")
        try:
            yield self
        except BaseException:
            self.calls.append("rollback")
            raise
        else:
            self.calls.append("commit")

    def append_raw(self, record: RawEnvelope) -> bool:
        self.calls.append("append_raw")
        if self.fail_raw:
            raise RuntimeError("raw insert failed")
        return True

    def append_funding(self, record: FundingObservation) -> bool:
        self.calls.append("append_funding")
        return True


class FakeFundingAdapter:
    venue = Venue.BYBIT

    def __init__(self, batch: AdapterBatch) -> None:
        self.batch = batch

    async def fetch_funding_history(
        self,
        asset: Asset,
        start: datetime,
        end: datetime,
        observed_at: datetime,
    ) -> AdapterBatch:
        return self.batch


@pytest.mark.parametrize("fail_raw", [False, True])
def test_recorder_appends_raw_before_normalized_and_stops_if_raw_fails(
    fail_raw: bool,
) -> None:
    raw = raw_envelope()
    funding = funding_observation()
    adapter = FakeFundingAdapter(AdapterBatch(raw=(raw,), normalized=(funding,)))
    store = StoreSpy(fail_raw=fail_raw)
    recorder = PublicRecorder(store)

    if fail_raw:
        with pytest.raises(RuntimeError, match="raw insert failed"):
            recorder.record(adapter.batch)
        assert store.calls == ["begin", "append_raw", "rollback"]
    else:
        recorder.record(adapter.batch)
        assert store.calls == ["begin", "append_raw", "append_funding", "commit"]


def test_make_raw_envelope_preserves_and_hashes_exact_utf8_bytes() -> None:
    payload = b'{ "result": [1, 2], "label": "\xe2\x98\x83" }\n'

    record = make_raw_envelope(
        venue=Venue.HYPERLIQUID,
        payload=payload,
        endpoint="/info",
        source_version="public-info-v1",
        venue_timestamp=None,
        monotonic_started_ns=9_000_000_001,
        monotonic_completed_ns=9_001_234_568,
        observed_at=NOW,
        event_id=UUID("00000000-0000-0000-0000-000000000099"),
    )

    assert record.payload_json == payload.decode("utf-8")
    assert record.source_hash == hashlib.sha256(payload).hexdigest()
    assert record.received_monotonic_ns == 9_001_234_568
    assert record.request_latency_ms == Decimal("1.234567")


def test_make_raw_envelope_rejects_invalid_utf8_and_negative_monotonic_duration() -> None:
    common: dict[str, Any] = {
        "venue": Venue.BYBIT,
        "endpoint": "/v5/market/orderbook",
        "source_version": "v5",
        "venue_timestamp": None,
        "monotonic_started_ns": 2,
        "monotonic_completed_ns": 3,
        "observed_at": NOW,
    }

    with pytest.raises(UnicodeDecodeError):
        make_raw_envelope(payload=b"\xff", **common)
    with pytest.raises(ValueError, match="monotonic completion"):
        make_raw_envelope(
            payload=b"{}",
            **{
                **common,
                "monotonic_started_ns": 4,
                "monotonic_completed_ns": 3,
            },
        )


def test_normalized_failure_rolls_back_raw_insert_as_one_unit(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)
    original = funding_observation()
    conflicting = funding_observation(rate=Decimal("0.0002"), source_hash="b" * 64)
    store.append_funding(original)

    with pytest.raises(ConflictingRecordError):
        PublicRecorder(store).record(
            AdapterBatch(raw=(raw_envelope(),), normalized=(conflicting,))
        )
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (1,)


def test_unknown_normalized_type_rolls_back_raw_insert(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    store = DuckDBStore(path)

    with pytest.raises(TypeError, match="unsupported normalized record type"):
        PublicRecorder(store).record(
            AdapterBatch(raw=(raw_envelope(),), normalized=(object(),))  # type: ignore[arg-type]
        )
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (0,)


def test_store_preserves_exact_raw_payload_text(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    payload = '{  "z": 1, "a": [true, null] }\n'
    store = DuckDBStore(path)
    store.append_raw(raw_envelope(payload_json=payload))
    store.close()

    with duckdb.connect(str(path), read_only=True) as connection:
        stored = connection.execute("SELECT payload_json FROM raw_envelopes").fetchone()
    assert stored == (payload,)
