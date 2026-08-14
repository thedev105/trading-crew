import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from polytrading.carry.fee_import import (
    ReviewedFeeDocument,
    parse_reviewed_fee_document,
    record_reviewed_fees,
)
from polytrading.domain.models import FeeSchedule, Venue
from polytrading.storage.store import ConflictingRecordError, DuckDBStore

REVIEWED_AT = datetime(2026, 8, 13, 17, tzinfo=UTC)


def fee_row(venue: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "venue": venue,
        "tier_name": "reviewed-tier",
        "maker_rate": "0",
        "taker_rate": "0.0005" if venue == "dydx" else "0",
        "effective_from": "2026-08-13T00:00:00Z",
        "observed_at": "2026-08-13T16:00:00Z",
        "source_url": (
            "https://help.dydx.trade/en/articles/166995-trading-fees-on-dydx"
            if venue == "dydx"
            else "https://docs.lighter.xyz/trading/trading-fees"
        ),
        "source_hash": ("a" if venue == "dydx" else "b") * 64,
    }


def payload(**overrides: object) -> bytes:
    values: dict[str, object] = {
        "schema_version": 1,
        "reviewed_at": "2026-08-13T17:00:00Z",
        "fees": [fee_row("dydx"), fee_row("lighter")],
    }
    values.update(overrides)
    return json.dumps(values, separators=(",", ":")).encode()


def test_parse_preserves_exact_decimal_utc_lineage_and_canonical_order() -> None:
    document = parse_reviewed_fee_document(payload())

    assert isinstance(document, ReviewedFeeDocument)
    assert document.reviewed_at == REVIEWED_AT
    assert tuple(item.venue for item in document.fees) == (Venue.DYDX, Venue.LIGHTER)
    assert document.fees[0].taker_rate == Decimal("0.0005")
    assert document.fees[1].taker_rate == Decimal("0")
    assert document.fees[0].effective_from == datetime(2026, 8, 13, tzinfo=UTC)


@pytest.mark.parametrize(
    ("bad_payload", "match"),
    [
        (b"\xff", "UTF-8"),
        (b"{", "JSON"),
        (
            b'{"schema_version":1,"schema_version":1,"reviewed_at":"2026-08-13T17:00:00Z","fees":[]}',
            "duplicate JSON key",
        ),
        (payload(unexpected=True), "invalid reviewed fee document"),
        (payload(reviewed_at="2026-08-13 17:00:00"), "invalid reviewed fee document"),
    ],
)
def test_parse_fails_closed_on_malformed_or_ambiguous_documents(
    bad_payload: bytes, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        parse_reviewed_fee_document(bad_payload)


def test_parse_rejects_json_numbers_instead_of_decimal_strings() -> None:
    fees = [fee_row("dydx"), fee_row("lighter")]
    fees[0]["taker_rate"] = 0.0005

    with pytest.raises(ValueError, match="invalid reviewed fee document"):
        parse_reviewed_fee_document(payload(fees=fees))


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        ({"venue": "bybit"}, "dYdX and Lighter"),
        ({"source_url": "https://help.dydx.trade.evil.test/fees"}, "official source"),
        ({"taker_rate": "-0.0001"}, "nonnegative"),
        ({"tier_name": "   "}, "tier name"),
        ({"observed_at": "2026-08-13T17:00:00.000001Z"}, "review time"),
        ({"source_hash": "A" * 64}, "invalid reviewed fee document"),
    ],
)
def test_parse_rejects_unreviewed_fee_evidence(mutate: dict[str, object], match: str) -> None:
    fees = [fee_row("dydx"), fee_row("lighter")]
    fees[0].update(mutate)

    with pytest.raises(ValueError, match=match):
        parse_reviewed_fee_document(payload(fees=fees))


def test_parse_requires_exact_two_venues_in_canonical_order() -> None:
    fees = [fee_row("lighter"), fee_row("dydx")]
    with pytest.raises(ValueError, match="canonical dYdX/Lighter order"):
        parse_reviewed_fee_document(payload(fees=fees))
    with pytest.raises(ValueError, match="exactly one dYdX and one Lighter"):
        parse_reviewed_fee_document(payload(fees=[fee_row("dydx")]))


def test_errors_do_not_echo_untrusted_document_content() -> None:
    sentinel = "SECRET-FEE-DOCUMENT-CONTENT"
    fees = [fee_row("dydx"), fee_row("lighter")]
    fees[0]["unexpected"] = sentinel

    with pytest.raises(ValueError) as caught:
        parse_reviewed_fee_document(payload(fees=fees))

    assert sentinel not in str(caught.value)


def test_record_reviewed_fees_is_transactional_and_idempotent(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    document = parse_reviewed_fee_document(payload())

    assert record_reviewed_fees(store, document) == 2
    assert record_reviewed_fees(store, document) == 0
    assert store.latest_fee_as_of(Venue.DYDX, "reviewed-tier", REVIEWED_AT) == document.fees[0]
    assert store.latest_fee_as_of(Venue.LIGHTER, "reviewed-tier", REVIEWED_AT) == document.fees[1]
    store.close()


def test_conflicting_middle_record_rolls_back_the_whole_import(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "research.duckdb")
    document = parse_reviewed_fee_document(payload())
    lighter = document.fees[1]
    conflict = FeeSchedule(
        **{
            **lighter.model_dump(),
            "taker_rate": Decimal("0.001"),
            "source_hash": "c" * 64,
        }
    )
    store.append_fee_schedule(conflict)

    with pytest.raises(ConflictingRecordError, match="conflicting fee schedule"):
        record_reviewed_fees(store, document)

    assert store.latest_fee_as_of(Venue.DYDX, "reviewed-tier", REVIEWED_AT) is None
    assert store.latest_fee_as_of(Venue.LIGHTER, "reviewed-tier", REVIEWED_AT) == conflict
    store.close()


def test_review_time_can_follow_observation_but_not_precede_it() -> None:
    document = parse_reviewed_fee_document(payload(reviewed_at="2026-08-13T16:00:00Z"))
    assert all(item.observed_at <= document.reviewed_at for item in document.fees)

    fees = [fee_row("dydx"), fee_row("lighter")]
    fees[1]["observed_at"] = (REVIEWED_AT + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    with pytest.raises(ValueError, match="review time"):
        parse_reviewed_fee_document(payload(fees=fees))
