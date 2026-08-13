from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from polytrading.carry.audit import AuditStatus
from polytrading.carry.discovery import evaluate_discovery
from polytrading.carry.dossier import (
    evaluate_dossier,
    load_bundled_dossier,
    load_bundled_dossiers,
)
from polytrading.domain.models import Asset, Venue
from polytrading.venues.funding_health import FundingCollectionHealthAuditor
from polytrading.web.models import (
    RESEARCH_WARNING,
    CarryEvidenceRow,
    DashboardSnapshot,
    EvidenceCounts,
    MarketEvidenceRow,
    OperationRecipes,
)

AS_OF = datetime(2026, 8, 13, 16, 6, tzinfo=UTC)
VENUES = (Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX, Venue.LIGHTER)
ASSETS = (Asset.BTC, Asset.ETH, Asset.SOL)
EXPECTED_PAIRS = tuple((venue, asset) for venue in VENUES for asset in ASSETS)


class EmptyFundingHistory:
    def funding_collection_cycles_between(self, start, end):
        return ()


def _symbol(venue: Venue, asset: Asset) -> str:
    if venue is Venue.BYBIT:
        return f"{asset.value}USDT"
    if venue is Venue.DYDX:
        return f"{asset.value}-USD"
    return asset.value


def _market_rows() -> tuple[MarketEvidenceRow, ...]:
    return tuple(
        MarketEvidenceRow(
            schema_version=1,
            venue=venue,
            asset=asset,
            symbol=_symbol(venue, asset),
            instrument_observed_at=None,
            funding_rate=None,
            funding_interval_hours=None,
            funding_effective_at=None,
            funding_observed_at=None,
            best_bid=None,
            best_ask=None,
            spread_bps=None,
            book_effective_at=None,
            book_observed_at=None,
        )
        for venue, asset in EXPECTED_PAIRS
    )


def _carry_rows() -> tuple[CarryEvidenceRow, ...]:
    return tuple(
        CarryEvidenceRow(
            schema_version=1,
            asset=asset,
            status=AuditStatus.INSUFFICIENT_DATA,
            funding_ready=False,
            book_ready=False,
            hourly_spread=None,
            reason_codes=("BOOK_EVIDENCE_MISSING",),
        )
        for asset in ASSETS
    )


def _snapshot_values() -> dict[str, object]:
    return {
        "schema_version": 1,
        "as_of": AS_OF,
        "database_name": "research.duckdb",
        "warning": RESEARCH_WARNING,
        "funding_health": FundingCollectionHealthAuditor(EmptyFundingHistory()).audit(AS_OF, 24),
        "latest_funding_cycle": None,
        "latest_book_cycle": None,
        "compatibility_dossier": None,
        "venue_discovery": None,
        "markets": _market_rows(),
        "carry_rows": _carry_rows(),
        "evidence_counts": EvidenceCounts(
            raw_envelopes=0,
            instrument_specs=0,
            funding_observations=0,
            market_snapshots=0,
            book_snapshots=0,
            book_collection_cycles=0,
            funding_collection_cycles=0,
        ),
        "operation_recipes": OperationRecipes(
            collect_public="polytrading collect public",
            collect_books_once="polytrading collect books --once",
            collect_current_funding="polytrading collect funding-cycle --current",
            inspect_funding_health="polytrading funding health",
        ),
    }


def test_snapshot_requires_every_market_pair_and_carry_asset_in_canonical_order() -> None:
    values = _snapshot_values()
    snapshot = DashboardSnapshot(**values)

    assert tuple((row.venue, row.asset) for row in snapshot.markets) == EXPECTED_PAIRS
    assert tuple(row.asset for row in snapshot.carry_rows) == ASSETS

    values["markets"] = tuple(reversed(_market_rows()))
    with pytest.raises(ValidationError, match="markets must cover"):
        DashboardSnapshot(**values)

    values = _snapshot_values()
    values["carry_rows"] = tuple(reversed(_carry_rows()))
    with pytest.raises(ValidationError, match="carry rows must cover"):
        DashboardSnapshot(**values)


def test_market_row_requires_complete_funding_and_book_groups() -> None:
    values = _market_rows()[0].model_dump()
    values.update(
        {
            "best_bid": Decimal("100"),
            "best_ask": Decimal("101"),
            "spread_bps": Decimal("99.502487562189054726"),
            "book_effective_at": AS_OF,
            "book_observed_at": AS_OF,
        }
    )
    row = MarketEvidenceRow(**values)
    assert row.best_bid == Decimal("100")

    values["best_ask"] = None
    with pytest.raises(ValidationError, match="book fields must be present together"):
        MarketEvidenceRow(**values)

    values = _market_rows()[0].model_dump()
    values.update(
        {
            "funding_rate": Decimal("0.0001"),
            "funding_interval_hours": Decimal("1"),
        }
    )
    with pytest.raises(ValidationError, match="funding fields must be present together"):
        MarketEvidenceRow(**values)


@pytest.mark.parametrize(
    ("bid", "ask", "message"),
    [
        (Decimal("0"), Decimal("101"), "book prices must be positive"),
        (Decimal("101"), Decimal("101"), "best bid must be less than best ask"),
    ],
)
def test_market_row_rejects_nonpositive_or_locked_books(
    bid: Decimal, ask: Decimal, message: str
) -> None:
    values = _market_rows()[0].model_dump()
    values.update(
        {
            "best_bid": bid,
            "best_ask": ask,
            "spread_bps": Decimal("0"),
            "book_effective_at": AS_OF,
            "book_observed_at": AS_OF,
        }
    )

    with pytest.raises(ValidationError, match=message):
        MarketEvidenceRow(**values)


def test_snapshot_rejects_blank_or_path_database_names_and_unsorted_reasons() -> None:
    for name in ("", "../research.duckdb", "/tmp/research.duckdb"):
        values = _snapshot_values()
        values["database_name"] = name
        with pytest.raises(ValidationError, match="database name"):
            DashboardSnapshot(**values)

    row = _carry_rows()[0].model_dump()
    row["reason_codes"] = ("Z_REASON", "A_REASON")
    with pytest.raises(ValidationError, match="reason codes"):
        CarryEvidenceRow(**row)


def test_snapshot_rejects_dossier_observed_after_snapshot_cutoff() -> None:
    values = _snapshot_values()
    report = evaluate_dossier(load_bundled_dossier())
    values["as_of"] = report.observed_at - datetime.resolution
    values["funding_health"] = FundingCollectionHealthAuditor(EmptyFundingHistory()).audit(
        values["as_of"], 24
    )
    values["compatibility_dossier"] = report

    with pytest.raises(ValidationError, match="dossier must not follow dashboard as-of"):
        DashboardSnapshot(**values)


def test_snapshot_rejects_discovery_observed_after_snapshot_cutoff() -> None:
    values = _snapshot_values()
    discovery = evaluate_discovery(
        tuple(evaluate_dossier(dossier) for dossier in load_bundled_dossiers())
    )
    values["as_of"] = discovery.observed_at - datetime.resolution
    values["funding_health"] = FundingCollectionHealthAuditor(EmptyFundingHistory()).audit(
        values["as_of"], 24
    )
    values["venue_discovery"] = discovery

    with pytest.raises(ValidationError, match="discovery must not follow dashboard as-of"):
        DashboardSnapshot(**values)


def test_snapshot_requires_legacy_dossier_to_match_discovery_candidate() -> None:
    values = _snapshot_values()
    legacy = evaluate_dossier(load_bundled_dossier())
    discovery = evaluate_discovery(
        tuple(evaluate_dossier(dossier) for dossier in load_bundled_dossiers())
    )
    values["as_of"] = discovery.observed_at
    values["funding_health"] = FundingCollectionHealthAuditor(EmptyFundingHistory()).audit(
        values["as_of"], 24
    )
    values["compatibility_dossier"] = legacy.model_copy(
        update={"primary_reason_code": "different_reason"}
    )
    values["venue_discovery"] = discovery

    with pytest.raises(ValidationError, match="legacy dossier must match discovery candidate"):
        DashboardSnapshot(**values)
