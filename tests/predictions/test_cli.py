import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import polytrading.cli as cli
import polytrading.predictions.cli as predictions_cli
from polytrading.cli import build_parser, main
from polytrading.predictions.candidates_models import RelationshipType
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.storage.store import ConflictingRecordError, PredictionMarketStore
from tests.predictions.attestation_helpers import rule_attestation
from tests.predictions.domain_helpers import NOW, market_record, rule_version
from tests.predictions.manifest_helpers import venue_manifest

MARKETS_FIXTURE = Path("tests/fixtures/predictions/polymarket/gamma_markets_page_1.json")
LIMITLESS_MARKETS_FIXTURE = Path("tests/fixtures/predictions/limitless/markets_active_page_1.json")
CLOB_BOOK_FIXTURE = Path("tests/fixtures/predictions/polymarket/clob_book.json")
FEE_RATE_FIXTURE = Path("tests/fixtures/predictions/polymarket/fee_rate.json")
KALSHI_MARKETS_FIXTURE = Path("tests/fixtures/predictions/kalshi/markets_page_1.json")
KALSHI_ORDERBOOK_FIXTURE = Path("tests/fixtures/predictions/kalshi/orderbook.json")

# Sorted ascending by market_id -- "KXHIGHNY-26AUG16-T78" < "KXHIGHNY-26AUG16-T85".
_KALSHI_LOWER_MARKET_ID = "KXHIGHNY-26AUG16-T78"
_KALSHI_HIGHER_MARKET_ID = "KXHIGHNY-26AUG16-T85"

# Sorted ascending by market_id (condition_id) -- "0x0f49..." < "0xa467...".
_LOWER_MARKET_ID = "0x0f49db97f71c68b1e42a6d16e3de93d85dbf7d4148e3f018eb79e88554be9f75"
_LOWER_MARKET_TOKEN_IDS = (
    "54533043819946592547517511176940999955633860128497669742211153063842200957669",
    "87854174148074652060467921081181402357467303721471806610111179101805869578687",
)
_HIGHER_MARKET_ID = "0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7"
_HIGHER_MARKET_TOKEN_IDS = (
    "32338220190071351435772801779725302244575775216413325951443816017994629993401",
    "25659310674993675562345759665114759892400026242514633218387667107987341231962",
)


def _polymarket_client_factory(handler):
    def fake_client(**_kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return fake_client


def test_predictions_collect_is_a_subcommand_tree_not_a_venue_flag() -> None:
    parsed = build_parser().parse_args(
        ["predictions", "collect", "polymarket", "--db", "var/predictions.duckdb"]
    )
    assert parsed.command == "predictions"
    assert parsed.predictions_command == "collect"
    assert parsed.predictions_collect_command == "polymarket"
    assert not hasattr(parsed, "venue")


def test_predictions_command_does_not_collide_with_existing_top_level_names() -> None:
    existing = {"replay", "dashboard", "carry", "fees", "funding", "trial", "collect", "ai"}
    parsed = build_parser().parse_args(["predictions", "venues", "status", "--db", "x.duckdb"])
    assert parsed.command == "predictions"
    assert "predictions" not in existing


def test_predictions_venues_status_reports_missing_manifests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    exit_code = main(["predictions", "venues", "status", "--db", str(database), "--format", "json"])
    assert exit_code == 0

    output = json.loads(capsys.readouterr().out)
    limitless_row = next(row for row in output["venues"] if row["venue"] == "limitless")
    assert limitless_row["collection_allowed"] is False
    assert limitless_row["reason"] == "MANIFEST_NOT_FOUND"


def test_predictions_venues_status_rejects_a_missing_database(tmp_path: Path) -> None:
    exit_code = main(["predictions", "venues", "status", "--db", str(tmp_path / "missing.duckdb")])
    assert exit_code == 2


def test_collect_polymarket_exits_two_before_any_network_call_when_watchlisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def reject_network(*_a: object, **_k: object) -> httpx.AsyncClient:
        raise AssertionError("collect must not open a network client when gate-rejected")

    monkeypatch.setattr(cli, "make_public_http_client", reject_network)
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.WATCHLIST,
            reviewed_at=NOW - timedelta(hours=1),
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "polymarket", "--db", str(database)])
    assert exit_code == 2


def test_collect_limitless_fails_closed_without_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def reject_network(*_a: object, **_k: object) -> httpx.AsyncClient:
        raise AssertionError("collect must not open a network client when gate-rejected")

    monkeypatch.setattr(predictions_cli, "make_public_http_client", reject_network)
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()  # migrated, empty

    exit_code = main(["predictions", "collect", "limitless", "--db", str(database)])
    assert exit_code == 2
    assert "MANIFEST_NOT_FOUND" in capsys.readouterr().err


def test_collect_limitless_with_permitting_manifest_stores_markets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets/active":
            return httpx.Response(
                200,
                content=LIMITLESS_MARKETS_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    def fake_client(**_kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(predictions_cli, "make_public_http_client", fake_client)

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.LIMITLESS,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "limitless", "--db", str(database)])
    assert exit_code == 0
    assert "collected 3 limitless markets" in capsys.readouterr().out

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        markets = verify_store.markets_as_of(
            PredictionVenue.LIMITLESS, datetime.now(UTC) + timedelta(days=1)
        )
    finally:
        verify_store.close()
    assert len(markets) == 3


def test_predictions_health_exits_zero_when_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.KALSHI,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(
        [
            "predictions",
            "health",
            "--db",
            str(database),
            "--as-of",
            "2026-08-15T12:00:00Z",
            "--format",
            "json",
        ]
    )
    # No book evidence collected yet, so both venues are NOT_COLLECTED -> exit 1.
    assert exit_code == 1


def test_predictions_health_rejects_a_missing_database(tmp_path: Path) -> None:
    exit_code = main(["predictions", "health", "--db", str(tmp_path / "missing.duckdb")])
    assert exit_code == 2


def test_predictions_health_rejects_invalid_as_of(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    exit_code = main(["predictions", "health", "--db", str(database), "--as-of", "not-a-timestamp"])
    assert exit_code == 2


def test_predictions_venues_status_text_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    assert main(["predictions", "venues", "status", "--db", str(database)]) == 0
    output = capsys.readouterr().out
    assert "polymarket" in output
    assert "kalshi" in output


def test_predictions_health_rejects_a_naive_as_of(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    exit_code = main(
        ["predictions", "health", "--db", str(database), "--as-of", "2026-08-15T12:00:00"]
    )
    assert exit_code == 2


def test_collect_polymarket_persists_markets_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets":
            return httpx.Response(
                200,
                content=MARKETS_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    def fake_client(**_kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(predictions_cli, "make_public_http_client", fake_client)

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "polymarket", "--db", str(database)])
    assert exit_code == 0
    assert "collected 2 polymarket markets" in capsys.readouterr().out

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        markets = verify_store.markets_as_of(
            PredictionVenue.POLYMARKET, datetime.now(UTC) + timedelta(days=1)
        )
    finally:
        verify_store.close()
    assert len(markets) == 2


def test_collect_polymarket_exits_one_on_a_network_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def raise_network_error(**_kwargs: object) -> httpx.AsyncClient:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(predictions_cli, "make_public_http_client", raise_network_error)

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "polymarket", "--db", str(database)])
    assert exit_code == 1


def test_predictions_dashboard_dispatches_to_validate_and_serve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        predictions_cli,
        "validate_prediction_dashboard_database",
        lambda db: calls.append(f"validate:{db}"),
    )
    monkeypatch.setattr(
        predictions_cli,
        "serve_prediction_dashboard",
        lambda db, port: calls.append(f"serve:{db}:{port}"),
    )

    database = tmp_path / "predictions.duckdb"
    exit_code = main(["predictions", "dashboard", "--db", str(database), "--port", "8787"])

    assert exit_code == 0
    assert calls == [f"validate:{database}", f"serve:{database}:8787"]


def test_candidates_command_persists_deterministic_candidates_idempotently(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_market(market_record())
    store.append_rule_version(rule_version())
    store.close()

    exit_code = main(
        [
            "predictions",
            "candidates",
            "--db",
            str(database),
            "--venues",
            "polymarket",
            "--as-of",
            "2026-08-15T12:00:00Z",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    polymarket_counts = output["venues"]["polymarket"]
    assert polymarket_counts[RelationshipType.BINARY_COMPLEMENT.value] == {
        "newly_appended": 1,
        "already_known": 0,
    }

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        candidates = verify_store.candidate_relationships_as_of(
            datetime(2026, 8, 15, 12, tzinfo=UTC)
        )
    finally:
        verify_store.close()
    assert len(candidates) == 1

    exit_code = main(
        [
            "predictions",
            "candidates",
            "--db",
            str(database),
            "--venues",
            "polymarket",
            "--as-of",
            "2026-08-15T12:00:00Z",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    polymarket_counts = output["venues"]["polymarket"]
    assert polymarket_counts[RelationshipType.BINARY_COMPLEMENT.value] == {
        "newly_appended": 0,
        "already_known": 1,
    }


def test_candidates_command_is_idempotent_across_different_as_of_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_market(market_record())
    store.append_rule_version(rule_version())
    store.close()

    first_as_of = "2026-08-15T12:00:00Z"
    exit_code = main(
        [
            "predictions",
            "candidates",
            "--db",
            str(database),
            "--venues",
            "polymarket",
            "--as-of",
            first_as_of,
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    polymarket_counts = output["venues"]["polymarket"]
    assert polymarket_counts[RelationshipType.BINARY_COMPLEMENT.value] == {
        "newly_appended": 1,
        "already_known": 0,
    }

    # A second run at a LATER --as-of regenerates the same deterministic candidate_id
    # (same legs, same relationship type) but with a different observed_at/
    # information_cutoff. Before the fix this raised ConflictingRecordError and rolled
    # back the whole transaction, hard-failing the command; it must instead report the
    # regenerated candidate as already_known and exit 0.
    second_as_of = "2026-08-16T12:00:00Z"
    exit_code = main(
        [
            "predictions",
            "candidates",
            "--db",
            str(database),
            "--venues",
            "polymarket",
            "--as-of",
            second_as_of,
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    polymarket_counts = output["venues"]["polymarket"]
    assert polymarket_counts[RelationshipType.BINARY_COMPLEMENT.value] == {
        "newly_appended": 0,
        "already_known": 1,
    }

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        candidates = verify_store.candidate_relationships_as_of(datetime(2026, 8, 17, tzinfo=UTC))
    finally:
        verify_store.close()
    assert len(candidates) == 1
    assert candidates[0].observed_at == datetime(2026, 8, 15, 12, tzinfo=UTC)
    assert candidates[0].information_cutoff == datetime(2026, 8, 15, 12, tzinfo=UTC)


def test_candidates_command_reports_cross_venue_abstention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    exit_code = main(
        [
            "predictions",
            "candidates",
            "--db",
            str(database),
            "--venues",
            "polymarket",
            "--as-of",
            "2026-08-15T12:00:00Z",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "SCOUT_GATE_UNMET" in captured.out


def test_candidates_rejects_unknown_venue_name(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    exit_code = main(
        [
            "predictions",
            "candidates",
            "--db",
            str(database),
            "--venues",
            "polymarket,not-a-venue",
        ]
    )
    assert exit_code == 2


def test_candidates_command_defaults_trial_family_and_as_of(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_market(market_record())
    store.append_rule_version(rule_version())
    store.close()

    exit_code = main(["predictions", "candidates", "--db", str(database), "--venues", "polymarket"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "trial_family=increment-2-structural" in output

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        candidates = verify_store.candidate_relationships_as_of(
            datetime.now(UTC) + timedelta(days=1)
        )
    finally:
        verify_store.close()
    assert len(candidates) == 1
    assert candidates[0].trial_family_id == "increment-2-structural"


def test_candidates_command_sanitizes_a_persistence_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_market(market_record())
    store.append_rule_version(rule_version())
    store.close()

    def raise_conflict(self: PredictionMarketStore, record: object) -> bool:
        raise ConflictingRecordError("conflicting candidate relationship for immutable identity")

    monkeypatch.setattr(PredictionMarketStore, "append_candidate_relationship", raise_conflict)

    exit_code = main(
        [
            "predictions",
            "candidates",
            "--db",
            str(database),
            "--venues",
            "polymarket",
            "--as-of",
            "2026-08-15T12:00:00Z",
        ]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "candidate discovery failed to persist durably" in captured.err
    assert "conflicting candidate relationship" not in captured.err


def test_collect_polymarket_default_books_fetches_no_book_or_fee_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets":
            return httpx.Response(
                200,
                content=MARKETS_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        raise AssertionError(f"unexpected request to {request.url.path} with --books 0")

    monkeypatch.setattr(
        predictions_cli, "make_public_http_client", _polymarket_client_factory(handler)
    )

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "polymarket", "--db", str(database)])
    assert exit_code == 0

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        far_future = datetime.now(UTC) + timedelta(days=1)
        assert (
            verify_store.latest_book_as_of(
                PredictionVenue.POLYMARKET, _LOWER_MARKET_ID, _LOWER_MARKET_TOKEN_IDS[0], far_future
            )
            is None
        )
        assert (
            verify_store.latest_fee_rate_as_of(
                PredictionVenue.POLYMARKET, _LOWER_MARKET_ID, far_future
            )
            is None
        )
    finally:
        verify_store.close()


def test_collect_polymarket_with_books_persists_book_and_fee_evidence_for_selected_market(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets":
            return httpx.Response(
                200,
                content=MARKETS_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        if request.url.path == "/book":
            return httpx.Response(
                200,
                content=CLOB_BOOK_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        if request.url.path == "/fee-rate":
            return httpx.Response(
                200,
                content=FEE_RATE_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    monkeypatch.setattr(
        predictions_cli, "make_public_http_client", _polymarket_client_factory(handler)
    )

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(
        ["predictions", "collect", "polymarket", "--db", str(database), "--books", "1"]
    )
    assert exit_code == 0

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        far_future = datetime.now(UTC) + timedelta(days=1)
        # --books 1 selects the deterministically-lowest market_id.
        for token_id in _LOWER_MARKET_TOKEN_IDS:
            book = verify_store.latest_book_as_of(
                PredictionVenue.POLYMARKET, _LOWER_MARKET_ID, token_id, far_future
            )
            assert book is not None
            assert book.outcome_token_id == token_id
        fee = verify_store.latest_fee_rate_as_of(
            PredictionVenue.POLYMARKET, _LOWER_MARKET_ID, far_future
        )
        assert fee is not None

        # The higher-sorted market was not selected -- no evidence collected for it.
        assert (
            verify_store.latest_book_as_of(
                PredictionVenue.POLYMARKET,
                _HIGHER_MARKET_ID,
                _HIGHER_MARKET_TOKEN_IDS[0],
                far_future,
            )
            is None
        )
        assert (
            verify_store.latest_fee_rate_as_of(
                PredictionVenue.POLYMARKET, _HIGHER_MARKET_ID, far_future
            )
            is None
        )
    finally:
        verify_store.close()


def test_collect_polymarket_with_books_isolates_a_single_market_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets":
            return httpx.Response(
                200,
                content=MARKETS_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        if request.url.path == "/book":
            token_id = request.url.params.get("token_id")
            if token_id in _HIGHER_MARKET_TOKEN_IDS:
                return httpx.Response(500, content=b"server error")
            return httpx.Response(
                200,
                content=CLOB_BOOK_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        if request.url.path == "/fee-rate":
            return httpx.Response(
                200,
                content=FEE_RATE_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    monkeypatch.setattr(
        predictions_cli, "make_public_http_client", _polymarket_client_factory(handler)
    )

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.POLYMARKET,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(
        ["predictions", "collect", "polymarket", "--db", str(database), "--books", "2"]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "polymarket" in captured.err
    assert _HIGHER_MARKET_ID in captured.err

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        far_future = datetime.now(UTC) + timedelta(days=1)
        # The healthy market still collected successfully.
        for token_id in _LOWER_MARKET_TOKEN_IDS:
            assert (
                verify_store.latest_book_as_of(
                    PredictionVenue.POLYMARKET, _LOWER_MARKET_ID, token_id, far_future
                )
                is not None
            )
        assert (
            verify_store.latest_fee_rate_as_of(
                PredictionVenue.POLYMARKET, _LOWER_MARKET_ID, far_future
            )
            is not None
        )
        # The failing market has no book or fee evidence persisted.
        assert (
            verify_store.latest_book_as_of(
                PredictionVenue.POLYMARKET,
                _HIGHER_MARKET_ID,
                _HIGHER_MARKET_TOKEN_IDS[0],
                far_future,
            )
            is None
        )
        assert (
            verify_store.latest_fee_rate_as_of(
                PredictionVenue.POLYMARKET, _HIGHER_MARKET_ID, far_future
            )
            is None
        )
    finally:
        verify_store.close()


def test_collect_limitless_rejects_books_greater_than_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def reject_network(*_a: object, **_k: object) -> httpx.AsyncClient:
        raise AssertionError("collect must not open a network client on a --books usage error")

    monkeypatch.setattr(predictions_cli, "make_public_http_client", reject_network)

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.LIMITLESS,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "limitless", "--db", str(database), "--books", "1"])
    assert exit_code == 2
    assert "limitless_endpoint_not_collected" in capsys.readouterr().err


def test_collect_limitless_default_books_is_unaffected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/markets/active":
            return httpx.Response(
                200,
                content=LIMITLESS_MARKETS_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    monkeypatch.setattr(
        predictions_cli, "make_public_http_client", _polymarket_client_factory(handler)
    )

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.LIMITLESS,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "limitless", "--db", str(database)])
    assert exit_code == 0
    assert "collected 3 limitless markets" in capsys.readouterr().out


def test_collect_kalshi_with_books_falls_back_to_yes_no_outcome_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Kalshi markets always have outcome_token_ids=None; --books must fall back to the
    market's own outcomes ("yes"/"no") as the per-outcome token identifiers passed to
    fetch_book_snapshot, since that adapter rejects any other outcome_token_id value.
    This exercises `cli.py`'s `outcome_token_ids is None -> market.outcomes` branch
    end-to-end, which otherwise has no coverage anywhere and fires on every real Kalshi
    --books>0 run.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/markets"):
            return httpx.Response(
                200,
                content=KALSHI_MARKETS_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(
                200,
                content=KALSHI_ORDERBOOK_FIXTURE.read_bytes(),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    monkeypatch.setattr(
        predictions_cli, "make_public_http_client", _polymarket_client_factory(handler)
    )

    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_venue_manifest(
        venue_manifest(
            venue=PredictionVenue.KALSHI,
            implementation_state=AdapterImplementationState.READ_ONLY,
        )
    )
    store.close()

    exit_code = main(["predictions", "collect", "kalshi", "--db", str(database), "--books", "1"])
    assert exit_code == 0

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        far_future = datetime.now(UTC) + timedelta(days=1)
        # --books 1 selects the deterministically-lowest market_id.
        for outcome_token_id in ("yes", "no"):
            book = verify_store.latest_book_as_of(
                PredictionVenue.KALSHI, _KALSHI_LOWER_MARKET_ID, outcome_token_id, far_future
            )
            assert book is not None
            assert book.outcome_token_id == outcome_token_id

        # Kalshi's fetch_fee_rate never returns a normalized fee record -- it documents a
        # published per-category schedule instead of a live public fee-rate endpoint -- so
        # no fee evidence is ever persisted for Kalshi, but the fee fetch was still attempted
        # (and its structured warning surfaced) rather than silently skipped.
        assert (
            verify_store.latest_fee_rate_as_of(
                PredictionVenue.KALSHI, _KALSHI_LOWER_MARKET_ID, far_future
            )
            is None
        )
        assert "KALSHI_FEE_RATE_ENDPOINT_UNAVAILABLE" in capsys.readouterr().err

        # The higher-sorted market was not selected -- no evidence collected for it.
        assert (
            verify_store.latest_book_as_of(
                PredictionVenue.KALSHI, _KALSHI_HIGHER_MARKET_ID, "yes", far_future
            )
            is None
        )
    finally:
        verify_store.close()


def _write_attestations(path: Path, *attestations: object) -> None:
    payload = [
        attestation.model_dump(mode="json") if hasattr(attestation, "model_dump") else attestation
        for attestation in attestations
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_attest_command_appends_and_reports_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    attestation = rule_attestation()
    store.append_rule_version(
        rule_version(
            rule_version_id=attestation.rule_version_id,
            source_hash=attestation.rule_source_hash,
        )
    )
    store.close()

    input_path = tmp_path / "attestations.json"
    _write_attestations(input_path, attestation)

    exit_code = main(
        ["predictions", "attest", "--db", str(database), "--input", str(input_path)]
    )
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "appended 1" in output
    assert "already_known=0" in output or "0 already known" in output

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        stored = verify_store.latest_attestation_for_rule_version(
            attestation.rule_version_id, NOW + timedelta(days=1)
        )
    finally:
        verify_store.close()
    assert stored == attestation


def test_attest_command_is_idempotent_across_reimport(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    attestation = rule_attestation()
    store.append_rule_version(
        rule_version(
            rule_version_id=attestation.rule_version_id,
            source_hash=attestation.rule_source_hash,
        )
    )
    store.close()

    input_path = tmp_path / "attestations.json"
    _write_attestations(input_path, attestation)

    first_exit = main(
        ["predictions", "attest", "--db", str(database), "--input", str(input_path)]
    )
    capsys.readouterr()
    second_exit = main(
        ["predictions", "attest", "--db", str(database), "--input", str(input_path)]
    )
    assert first_exit == 0
    assert second_exit == 0
    output = capsys.readouterr().out
    assert "appended 0" in output


def test_attest_command_rejects_a_rule_source_hash_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    attestation = rule_attestation()
    store.append_rule_version(
        rule_version(rule_version_id=attestation.rule_version_id, source_hash="b" * 64)
    )
    store.close()

    input_path = tmp_path / "attestations.json"
    _write_attestations(input_path, attestation)

    exit_code = main(
        ["predictions", "attest", "--db", str(database), "--input", str(input_path)]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert str(attestation.rule_version_id) in captured.err

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        stored = verify_store.latest_attestation_for_rule_version(
            attestation.rule_version_id, NOW + timedelta(days=1)
        )
    finally:
        verify_store.close()
    assert stored is None


def test_attest_command_rejects_an_unknown_rule_version_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    attestation = rule_attestation()
    input_path = tmp_path / "attestations.json"
    _write_attestations(input_path, attestation)

    exit_code = main(
        ["predictions", "attest", "--db", str(database), "--input", str(input_path)]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert str(attestation.rule_version_id) in captured.err


def test_attest_command_rejects_a_non_array_json_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    input_path = tmp_path / "attestations.json"
    input_path.write_text(json.dumps({"not": "an array"}), encoding="utf-8")

    exit_code = main(
        ["predictions", "attest", "--db", str(database), "--input", str(input_path)]
    )
    assert exit_code == 2


def test_attest_command_rejects_a_strictly_invalid_attestation_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    attestation = rule_attestation()
    payload = attestation.model_dump(mode="json")
    payload["extra_unexpected_field"] = "nope"
    input_path = tmp_path / "attestations.json"
    input_path.write_text(json.dumps([payload]), encoding="utf-8")

    exit_code = main(
        ["predictions", "attest", "--db", str(database), "--input", str(input_path)]
    )
    assert exit_code == 2


def test_attest_command_sanitizes_a_persistence_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    attestation = rule_attestation()
    store.append_rule_version(
        rule_version(
            rule_version_id=attestation.rule_version_id,
            source_hash=attestation.rule_source_hash,
        )
    )
    store.close()

    input_path = tmp_path / "attestations.json"
    _write_attestations(input_path, attestation)

    def raise_conflict(self: PredictionMarketStore, record: object) -> bool:
        raise ConflictingRecordError("conflicting rule attestation for immutable identity")

    monkeypatch.setattr(PredictionMarketStore, "append_rule_attestation", raise_conflict)

    exit_code = main(
        ["predictions", "attest", "--db", str(database), "--input", str(input_path)]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "rule attestation" in captured.err
    assert "conflicting rule attestation" not in captured.err
