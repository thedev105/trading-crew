from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import UUID

from polytrading.domain.models import Asset, Venue
from polytrading.venues.funding_cycle_models import (
    FUNDING_CYCLE_PROTOCOL_VERSION,
    FUNDING_CYCLE_WARNINGS,
    FundingCaptureOutcome,
    FundingCollectionCycle,
    FundingCycleItem,
    FundingCycleStatus,
    InstrumentCaptureOutcome,
)
from polytrading.venues.funding_cycle_report import (
    render_funding_cycle_json,
    render_funding_cycle_text,
)
from tests.venues.funding_cycle_helpers import CYCLE_END


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _complete_cycle() -> FundingCollectionCycle:
    items: list[FundingCycleItem] = []
    for venue in (Venue.BYBIT, Venue.HYPERLIQUID):
        for asset in (Asset.BTC, Asset.ETH, Asset.SOL):
            instrument_hash = _hash(f"{venue.value}:{asset.value}:instrument")
            funding_hash = _hash(f"{venue.value}:{asset.value}:funding")
            bybit = venue is Venue.BYBIT
            items.append(
                FundingCycleItem(
                    schema_version=1,
                    venue=venue,
                    asset=asset,
                    symbol=f"{asset.value}USDT" if bybit else asset.value,
                    instrument_outcome=InstrumentCaptureOutcome.CAPTURED,
                    funding_outcome=(
                        FundingCaptureOutcome.NO_SETTLEMENT
                        if bybit
                        else FundingCaptureOutcome.CAPTURED
                    ),
                    instrument_observed_at=CYCLE_END + timedelta(minutes=1),
                    funding_effective_at=None if bybit else CYCLE_END,
                    funding_observed_at=CYCLE_END + timedelta(minutes=2),
                    instrument_source_hashes=(instrument_hash,),
                    funding_source_hashes=(funding_hash,),
                    reason_codes=(),
                )
            )
    hashes = tuple(
        sorted(
            source_hash
            for item in items
            for source_hash in (*item.instrument_source_hashes, *item.funding_source_hashes)
        )
    )
    return FundingCollectionCycle(
        schema_version=1,
        protocol_version=FUNDING_CYCLE_PROTOCOL_VERSION,
        cycle_id=UUID("00000000-0000-0000-0000-000000000904"),
        cycle_end=CYCLE_END,
        assets=(Asset.BTC, Asset.ETH, Asset.SOL),
        venues=(Venue.BYBIT, Venue.HYPERLIQUID),
        request_started_at=CYCLE_END + timedelta(seconds=30),
        request_completed_at=CYCLE_END + timedelta(minutes=2),
        items=tuple(items),
        status=FundingCycleStatus.COMPLETE,
        source_hashes=hashes,
        warnings=FUNDING_CYCLE_WARNINGS,
    )


def _late_cycle() -> FundingCollectionCycle:
    items = tuple(
        FundingCycleItem(
            schema_version=1,
            venue=venue,
            asset=asset,
            symbol=f"{asset.value}USDT" if venue is Venue.BYBIT else asset.value,
            instrument_outcome=InstrumentCaptureOutcome.LATE_NOT_COLLECTED,
            funding_outcome=FundingCaptureOutcome.LATE_NOT_COLLECTED,
            instrument_observed_at=None,
            funding_effective_at=None,
            funding_observed_at=None,
            instrument_source_hashes=(),
            funding_source_hashes=(),
            reason_codes=("COLLECTION_WINDOW_MISSED",),
        )
        for venue in (Venue.BYBIT, Venue.HYPERLIQUID)
        for asset in (Asset.BTC, Asset.ETH, Asset.SOL)
    )
    return FundingCollectionCycle(
        schema_version=1,
        protocol_version=FUNDING_CYCLE_PROTOCOL_VERSION,
        cycle_id=UUID("00000000-0000-0000-0000-000000000905"),
        cycle_end=CYCLE_END,
        assets=(Asset.BTC, Asset.ETH, Asset.SOL),
        venues=(Venue.BYBIT, Venue.HYPERLIQUID),
        request_started_at=CYCLE_END + timedelta(minutes=6),
        request_completed_at=CYCLE_END + timedelta(minutes=6),
        items=items,
        status=FundingCycleStatus.LATE,
        source_hashes=(),
        warnings=FUNDING_CYCLE_WARNINGS,
    )


def test_json_is_byte_stable_canonical_and_preserves_exact_types() -> None:
    cycle = _complete_cycle()

    rendered = render_funding_cycle_json(cycle)
    payload = json.loads(rendered)

    assert rendered == render_funding_cycle_json(cycle)
    assert rendered == json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    assert payload["cycle_end"] == "2026-08-13T17:00:00Z"
    assert payload["status"] == "complete"
    assert payload["warnings"] == list(FUNDING_CYCLE_WARNINGS)
    assert [
        (item["venue"], item["asset"], item["funding_outcome"])
        for item in payload["items"]
    ] == [
        ("bybit", "BTC", "no_settlement"),
        ("bybit", "ETH", "no_settlement"),
        ("bybit", "SOL", "no_settlement"),
        ("hyperliquid", "BTC", "captured"),
        ("hyperliquid", "ETH", "captured"),
        ("hyperliquid", "SOL", "captured"),
    ]


def test_text_is_stable_explicit_and_ends_with_exact_warnings() -> None:
    rendered = render_funding_cycle_text(_complete_cycle())

    assert rendered == render_funding_cycle_text(_complete_cycle())
    assert rendered.splitlines()[:2] == [
        "Point-in-time funding cycle v1 | 2026-08-13T17:00:00Z | complete",
        "Window cutoff: 2026-08-13T17:05:00Z",
    ]
    assert [line.split(" | ", 1)[0] for line in rendered.splitlines()[2:8]] == [
        "bybit BTC",
        "bybit ETH",
        "bybit SOL",
        "hyperliquid BTC",
        "hyperliquid ETH",
        "hyperliquid SOL",
    ]
    assert all("instrument=captured" in line for line in rendered.splitlines()[2:8])
    assert all("reasons=none" in line for line in rendered.splitlines()[2:8])
    assert rendered.endswith("\n".join(FUNDING_CYCLE_WARNINGS))


def test_late_text_makes_every_missed_component_and_reason_explicit() -> None:
    rendered = render_funding_cycle_text(_late_cycle())

    assert rendered.splitlines()[0].endswith(" | late")
    assert rendered.count("instrument=late_not_collected") == 6
    assert rendered.count("funding=late_not_collected") == 6
    assert rendered.count("reasons=COLLECTION_WINDOW_MISSED") == 6


def test_outputs_do_not_claim_authority_or_executable_economics() -> None:
    outputs = (
        render_funding_cycle_json(_complete_cycle()),
        render_funding_cycle_text(_complete_cycle()),
    )

    for rendered in outputs:
        lowered = rendered.casefold()
        for forbidden in (
            "trade",
            "approved",
            "live_eligible",
            "expected profit",
            "recommended",
            "api key",
            "private key",
        ):
            assert forbidden not in lowered
