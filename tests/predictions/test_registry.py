from datetime import timedelta
from pathlib import Path
from uuid import UUID

from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.registry import PredictionRegistry
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.domain_helpers import NOW, market_record, rule_version

FIRST_RULE = UUID("00000000-0000-0000-0000-000000004001")
SECOND_RULE = UUID("00000000-0000-0000-0000-000000004002")


def seeded_store(tmp_path: Path) -> PredictionMarketStore:
    store = PredictionMarketStore(tmp_path / "predictions.duckdb")
    first_market = market_record(rule_version_id=FIRST_RULE, retrieved_at=NOW - timedelta(hours=2))
    second_market = market_record(rule_version_id=SECOND_RULE, retrieved_at=NOW)
    store.append_market(first_market)
    store.append_market(second_market)
    store.append_rule_version(
        rule_version(rule_version_id=FIRST_RULE, effective_at=NOW - timedelta(hours=2))
    )
    store.append_rule_version(
        rule_version(
            rule_version_id=SECOND_RULE,
            effective_at=NOW,
            superseded_rule_version_id=FIRST_RULE,
        )
    )
    return store


def test_market_as_of_returns_none_for_an_unknown_market(tmp_path: Path) -> None:
    registry = PredictionRegistry(seeded_store(tmp_path))
    assert registry.market_as_of(PredictionVenue.POLYMARKET, "no-such-market", NOW) is None


def test_market_as_of_reflects_the_point_in_time_rule_version(tmp_path: Path) -> None:
    registry = PredictionRegistry(seeded_store(tmp_path))
    market_id = market_record().market_id

    early = registry.market_as_of(PredictionVenue.POLYMARKET, market_id, NOW - timedelta(hours=1))
    late = registry.market_as_of(PredictionVenue.POLYMARKET, market_id, NOW)

    assert early is not None and early.rule_version_id == FIRST_RULE
    assert late is not None and late.rule_version_id == SECOND_RULE


def test_rule_history_never_includes_a_version_effective_after_the_cutoff(
    tmp_path: Path,
) -> None:
    registry = PredictionRegistry(seeded_store(tmp_path))
    market_id = market_record().market_id

    early_history = registry.rule_history(
        PredictionVenue.POLYMARKET, market_id, NOW - timedelta(hours=1)
    )
    full_history = registry.rule_history(PredictionVenue.POLYMARKET, market_id, NOW)

    assert [item.rule_version_id for item in early_history] == [FIRST_RULE]
    assert [item.rule_version_id for item in full_history] == [FIRST_RULE, SECOND_RULE]


def test_rule_history_excludes_a_different_venues_version(tmp_path: Path) -> None:
    registry = PredictionRegistry(seeded_store(tmp_path))
    market_id = market_record().market_id
    assert registry.rule_history(PredictionVenue.KALSHI, market_id, NOW) == ()


def test_markets_by_venue_as_of_returns_only_the_latest_version(tmp_path: Path) -> None:
    registry = PredictionRegistry(seeded_store(tmp_path))
    markets = registry.markets_by_venue_as_of(PredictionVenue.POLYMARKET, NOW)
    assert len(markets) == 1
    assert markets[0].rule_version_id == SECOND_RULE


def test_has_rule_changed_since_is_true_only_for_a_genuinely_new_version(
    tmp_path: Path,
) -> None:
    registry = PredictionRegistry(seeded_store(tmp_path))
    market_id = market_record().market_id

    assert (
        registry.has_rule_changed_since(PredictionVenue.POLYMARKET, market_id, FIRST_RULE, NOW)
        is True
    )
    assert (
        registry.has_rule_changed_since(PredictionVenue.POLYMARKET, market_id, SECOND_RULE, NOW)
        is False
    )


def test_has_rule_changed_since_is_false_with_no_history(tmp_path: Path) -> None:
    registry = PredictionRegistry(seeded_store(tmp_path))
    assert (
        registry.has_rule_changed_since(
            PredictionVenue.POLYMARKET, "no-such-market", FIRST_RULE, NOW
        )
        is False
    )
