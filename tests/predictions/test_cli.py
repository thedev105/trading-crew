import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest

import polytrading.cli as cli
import polytrading.predictions.cli as predictions_cli
from polytrading.cli import build_parser, main
from polytrading.predictions.candidates_models import RelationshipType
from polytrading.predictions.domain import PredictionVenue
from polytrading.predictions.economics_models import ScanReport, deterministic_scan_report_id
from polytrading.predictions.experiments import TrialFamily
from polytrading.predictions.manifest import AdapterImplementationState
from polytrading.predictions.risk import PredictionRiskPolicy
from polytrading.predictions.shadow_models import ShadowState
from polytrading.predictions.storage.store import ConflictingRecordError, PredictionMarketStore
from tests.predictions.attestation_helpers import rule_attestation
from tests.predictions.candidate_helpers import (
    CANDIDATE_ID,
    RULE_VERSION_ID,
    candidate_relationship,
)
from tests.predictions.domain_helpers import (
    NOW,
    fee_rate,
    level,
    market_record,
    prediction_book_snapshot,
    rule_version,
)
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
    # Sanitized per §14: a stable code plus venue and market id only -- never the raw
    # exception text, which may carry a URL or response-body fragment (the transport's
    # "server error" body here) that shouldn't reach the console.
    assert (
        f"polytrading: warning: polymarket BOOK_FEE_COLLECTION_FAILED {_HIGHER_MARKET_ID}"
        in captured.err
    )
    assert "server error" not in captured.err

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

    exit_code = main(["predictions", "attest", "--db", str(database), "--input", str(input_path)])
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

    first_exit = main(["predictions", "attest", "--db", str(database), "--input", str(input_path)])
    capsys.readouterr()
    second_exit = main(["predictions", "attest", "--db", str(database), "--input", str(input_path)])
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

    exit_code = main(["predictions", "attest", "--db", str(database), "--input", str(input_path)])
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


def test_attest_command_rejects_a_venue_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    attestation = rule_attestation(venue=PredictionVenue.POLYMARKET)
    store.append_rule_version(
        rule_version(
            rule_version_id=attestation.rule_version_id,
            source_hash=attestation.rule_source_hash,
            venue=PredictionVenue.KALSHI,
            market_id=attestation.market_id,
        )
    )
    store.close()

    input_path = tmp_path / "attestations.json"
    _write_attestations(input_path, attestation)

    exit_code = main(["predictions", "attest", "--db", str(database), "--input", str(input_path)])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert str(attestation.attestation_id) in captured.err
    assert "venue" in captured.err

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        stored = verify_store.latest_attestation_for_rule_version(
            attestation.rule_version_id, NOW + timedelta(days=1)
        )
    finally:
        verify_store.close()
    assert stored is None


def test_attest_command_rejects_a_market_id_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    attestation = rule_attestation()
    store.append_rule_version(
        rule_version(
            rule_version_id=attestation.rule_version_id,
            source_hash=attestation.rule_source_hash,
            market_id="a-different-market",
        )
    )
    store.close()

    input_path = tmp_path / "attestations.json"
    _write_attestations(input_path, attestation)

    exit_code = main(["predictions", "attest", "--db", str(database), "--input", str(input_path)])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert str(attestation.attestation_id) in captured.err
    assert "market_id" in captured.err

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

    exit_code = main(["predictions", "attest", "--db", str(database), "--input", str(input_path)])
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

    exit_code = main(["predictions", "attest", "--db", str(database), "--input", str(input_path)])
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

    exit_code = main(["predictions", "attest", "--db", str(database), "--input", str(input_path)])
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

    exit_code = main(["predictions", "attest", "--db", str(database), "--input", str(input_path)])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "rule attestation" in captured.err
    assert "conflicting rule attestation" not in captured.err


def _seed_candidate_with_rule_and_attestation(database: Path) -> None:
    store = PredictionMarketStore(database)
    store.append_market(market_record(rule_version_id=RULE_VERSION_ID))
    store.append_rule_version(rule_version(rule_version_id=RULE_VERSION_ID, effective_at=NOW))
    store.append_rule_attestation(
        rule_attestation(rule_version_id=RULE_VERSION_ID, reviewed_at=NOW)
    )
    store.append_candidate_relationship(candidate_relationship())
    store.close()


def test_prove_command_compiles_and_persists_a_proof_ready_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    _seed_candidate_with_rule_and_attestation(database)

    exit_code = main(
        [
            "predictions",
            "prove",
            "--db",
            str(database),
            "--candidate-id",
            str(CANDIDATE_ID),
            "--as-of",
            "2026-08-15T12:00:00Z",
            "--review-identity",
            "tester@example.test",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["candidate_id"] == str(CANDIDATE_ID)
    assert output["status"] == "proof_ready"
    assert output["rejection_reason"] is None
    assert output["minimum_basket_payout"] == "1"
    assert output["persisted"] is True

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        proof = verify_store.latest_proof_for_candidate(
            CANDIDATE_ID, datetime(2026, 8, 15, 12, tzinfo=UTC)
        )
    finally:
        verify_store.close()
    assert proof is not None
    assert proof.status == "proof_ready"
    assert str(proof.proof_id) == output["proof_id"]


def test_prove_command_rejects_an_unknown_candidate_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    exit_code = main(
        ["predictions", "prove", "--db", str(database), "--candidate-id", str(CANDIDATE_ID)]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert str(CANDIDATE_ID) in captured.err


def test_prove_command_rejects_a_malformed_candidate_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    exit_code = main(
        ["predictions", "prove", "--db", str(database), "--candidate-id", "not-a-uuid"]
    )
    assert exit_code == 2


def test_prove_command_is_idempotent_across_reruns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    _seed_candidate_with_rule_and_attestation(database)

    args = [
        "predictions",
        "prove",
        "--db",
        str(database),
        "--candidate-id",
        str(CANDIDATE_ID),
        "--as-of",
        "2026-08-15T12:00:00Z",
        "--format",
        "json",
    ]
    first_exit = main(args)
    first_output = json.loads(capsys.readouterr().out)
    second_exit = main(args)
    second_output = json.loads(capsys.readouterr().out)

    assert first_exit == 0
    assert second_exit == 0
    assert first_output["persisted"] is True
    assert second_output["persisted"] is False
    assert first_output["proof_id"] == second_output["proof_id"]

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        proofs = verify_store.proof_artifacts_for_candidate(
            CANDIDATE_ID, datetime(2026, 8, 15, 12, tzinfo=UTC)
        )
    finally:
        verify_store.close()
    assert len(proofs) == 1


def test_prove_command_sanitizes_a_persistence_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    _seed_candidate_with_rule_and_attestation(database)

    def raise_conflict(self: PredictionMarketStore, record: object) -> bool:
        raise ConflictingRecordError("conflicting proof artifact for immutable identity")

    monkeypatch.setattr(PredictionMarketStore, "append_proof_artifact", raise_conflict)

    exit_code = main(
        [
            "predictions",
            "prove",
            "--db",
            str(database),
            "--candidate-id",
            str(CANDIDATE_ID),
            "--as-of",
            "2026-08-15T12:00:00Z",
        ]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "proof compilation failed to persist durably" in captured.err
    assert "conflicting proof artifact" not in captured.err


def test_scan_command_persists_a_shadow_candidate_with_hand_checked_surplus(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    _seed_candidate_with_rule_and_attestation(database)
    store = PredictionMarketStore(database)
    store.append_book_snapshot(
        prediction_book_snapshot(
            outcome_token_id="111",
            bids=(level("0.20", "10"),),
            asks=(level("0.30", "100"),),
            observed_at=NOW,
        )
    )
    store.append_book_snapshot(
        prediction_book_snapshot(
            outcome_token_id="222",
            bids=(level("0.30", "10"),),
            asks=(level("0.35", "80"),),
            observed_at=NOW,
        )
    )
    store.append_fee_rate(fee_rate(market_id="0xcondition", taker_rate=Decimal("0.01")))
    store.close()

    as_of = "2026-08-15T12:00:00Z"
    prove_exit = main(
        [
            "predictions",
            "prove",
            "--db",
            str(database),
            "--candidate-id",
            str(CANDIDATE_ID),
            "--as-of",
            as_of,
        ]
    )
    assert prove_exit == 0
    capsys.readouterr()

    scan_exit = main(
        ["predictions", "scan", "--db", str(database), "--as-of", as_of, "--format", "json"]
    )
    assert scan_exit == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tally"] == {
        "SHADOW_CANDIDATE": 1,
        "REJECTED": 0,
        "INSUFFICIENT_EVIDENCE": 0,
    }
    assert len(output["shadow_candidates"]) == 1
    shadow = output["shadow_candidates"][0]
    assert shadow["candidate_id"] == str(CANDIDATE_ID)
    # Hand-checked against DEFAULT_RESEARCH_POLICY (research-v1):
    #   bottleneck quantity q = min(leg0 ask depth 100, leg1 ask depth 80) = 80
    #   acquisition0 = 0.30 * 80 = 24.00; acquisition1 = 0.35 * 80 = 28.00
    #   acquisition_total = 52.00
    #   fee_total = 52.00 * 0.01 taker (applied per leg) = 0.24 + 0.28 = 0.52
    #   currency_basis_reserve = 52.00 * 0.0025 = 0.13
    #   capital_lockup_reserve = 52.00 * 0.0002 * 3 = 0.0312
    #   all_in_cost = 52.00 + 0.52 + 2.00(gas) + 0.13 + 1.00(transfer) + 0.0312 + 0.50(ops)
    #               = 56.1812
    #   failure_reserve = 52.00 * (0.01+0.005+0.005+0.0025) = 52.00 * 0.0225 = 1.17
    #   proven_floor = q * minimum_basket_payout(1) = 80.00
    #   surplus = 80.00 - 56.1812 - 1.17 = 22.6488
    assert Decimal(shadow["conservative_surplus_usd"]) == Decimal("22.6488")
    assert Decimal(shadow["capacity_usd_at_current_depth"]) == Decimal("52.00")

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        reports = verify_store.scan_reports_as_of(datetime(2026, 8, 15, 12, tzinfo=UTC))
    finally:
        verify_store.close()
    assert len(reports) == 1
    assert reports[0].decision == "SHADOW_CANDIDATE"
    assert reports[0].economics is not None
    assert reports[0].economics.conservative_surplus_usd == Decimal("22.6488")


def test_scan_command_reports_insufficient_evidence_without_books(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    _seed_candidate_with_rule_and_attestation(database)

    as_of = "2026-08-15T12:00:00Z"
    main(
        [
            "predictions",
            "prove",
            "--db",
            str(database),
            "--candidate-id",
            str(CANDIDATE_ID),
            "--as-of",
            as_of,
        ]
    )
    capsys.readouterr()

    exit_code = main(
        ["predictions", "scan", "--db", str(database), "--as-of", as_of, "--format", "json"]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tally"] == {
        "SHADOW_CANDIDATE": 0,
        "REJECTED": 0,
        "INSUFFICIENT_EVIDENCE": 1,
    }
    assert output["shadow_candidates"] == []

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        reports = verify_store.scan_reports_as_of(datetime(2026, 8, 15, 12, tzinfo=UTC))
    finally:
        verify_store.close()
    assert len(reports) == 1
    assert reports[0].decision == "INSUFFICIENT_EVIDENCE"
    assert reports[0].reason == "MISSING_BOOK"


def test_scan_command_reports_insufficient_evidence_when_no_proof_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No ``predictions prove`` run has ever happened for this candidate: the store
    has no proof_artifacts row at all, so ``latest_proof_for_candidate`` returns
    ``None`` and the scan must fall back to the fixed "no proof compiled" reason
    (distinct from an actually-compiled ``insufficient_evidence``/``rejected``
    proof, which instead passes through that proof's own ``rejection_reason``).
    """
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_candidate_relationship(candidate_relationship())
    store.close()

    as_of = "2026-08-15T12:00:00Z"
    exit_code = main(
        ["predictions", "scan", "--db", str(database), "--as-of", as_of, "--format", "json"]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tally"] == {
        "SHADOW_CANDIDATE": 0,
        "REJECTED": 0,
        "INSUFFICIENT_EVIDENCE": 1,
    }
    assert output["shadow_candidates"] == []

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        reports = verify_store.scan_reports_as_of(datetime(2026, 8, 15, 12, tzinfo=UTC))
    finally:
        verify_store.close()
    assert len(reports) == 1
    assert reports[0].decision == "INSUFFICIENT_EVIDENCE"
    assert reports[0].proof_id is None
    assert reports[0].reason == "no proof compiled"


def test_scan_command_rejects_a_candidate_with_a_rejected_proof(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An attestation with ``tie_possible=True`` makes ``compile_proof`` emit a
    ``rejected`` (not ``insufficient_evidence``) ``binary_complement@1`` artifact
    with ``rejection_reason="TIE_UNMODELED"``. The scan must classify this as
    ``REJECTED`` and pass that exact reason through verbatim -- proving the
    "non-ready proof -> REJECTED with the proof's own rejection_reason" branch is
    distinct from the "no proof at all" and "insufficient_evidence proof" branches.
    """
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_market(market_record(rule_version_id=RULE_VERSION_ID))
    store.append_rule_version(rule_version(rule_version_id=RULE_VERSION_ID, effective_at=NOW))
    store.append_rule_attestation(
        rule_attestation(
            rule_version_id=RULE_VERSION_ID,
            reviewed_at=NOW,
            tie_possible=True,
            tie_behavior="split_at_par",
        )
    )
    store.append_candidate_relationship(candidate_relationship())
    store.close()

    as_of = "2026-08-15T12:00:00Z"
    prove_exit = main(
        [
            "predictions",
            "prove",
            "--db",
            str(database),
            "--candidate-id",
            str(CANDIDATE_ID),
            "--as-of",
            as_of,
            "--format",
            "json",
        ]
    )
    assert prove_exit == 0
    prove_output = json.loads(capsys.readouterr().out)
    assert prove_output["status"] == "rejected"
    assert prove_output["rejection_reason"] == "TIE_UNMODELED"

    exit_code = main(
        ["predictions", "scan", "--db", str(database), "--as-of", as_of, "--format", "json"]
    )
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tally"] == {
        "SHADOW_CANDIDATE": 0,
        "REJECTED": 1,
        "INSUFFICIENT_EVIDENCE": 0,
    }
    assert output["shadow_candidates"] == []

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        reports = verify_store.scan_reports_as_of(datetime(2026, 8, 15, 12, tzinfo=UTC))
    finally:
        verify_store.close()
    assert len(reports) == 1
    assert reports[0].decision == "REJECTED"
    assert reports[0].reason == "TIE_UNMODELED"
    assert reports[0].proof_id is not None
    assert reports[0].economics is None


def test_scan_command_rejects_a_proof_ready_candidate_with_non_positive_surplus(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    _seed_candidate_with_rule_and_attestation(database)
    store = PredictionMarketStore(database)
    store.append_book_snapshot(
        prediction_book_snapshot(
            outcome_token_id="111",
            bids=(level("0.40", "10"),),
            asks=(level("0.49", "100"),),
            observed_at=NOW,
        )
    )
    store.append_book_snapshot(
        prediction_book_snapshot(
            outcome_token_id="222",
            bids=(level("0.40", "10"),),
            asks=(level("0.49", "80"),),
            observed_at=NOW,
        )
    )
    store.append_fee_rate(fee_rate(market_id="0xcondition", taker_rate=Decimal("0.01")))
    store.close()

    as_of = "2026-08-15T12:00:00Z"
    prove_exit = main(
        [
            "predictions",
            "prove",
            "--db",
            str(database),
            "--candidate-id",
            str(CANDIDATE_ID),
            "--as-of",
            as_of,
        ]
    )
    assert prove_exit == 0
    capsys.readouterr()

    scan_exit = main(
        ["predictions", "scan", "--db", str(database), "--as-of", as_of, "--format", "json"]
    )
    assert scan_exit == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tally"] == {
        "SHADOW_CANDIDATE": 0,
        "REJECTED": 1,
        "INSUFFICIENT_EVIDENCE": 0,
    }
    assert output["shadow_candidates"] == []

    # Hand-checked against DEFAULT_RESEARCH_POLICY (research-v1):
    #   bottleneck quantity q = min(leg0 ask depth 100, leg1 ask depth 80) = 80
    #   acquisition0 = 0.49 * 80 = 39.20; acquisition1 = 0.49 * 80 = 39.20
    #   acquisition_total = 78.40
    #   fee_total = 39.20*0.01 + 39.20*0.01 = 0.392 + 0.392 = 0.784
    #   currency_basis_reserve = 78.40 * 0.0025 = 0.196
    #   capital_lockup_reserve = 78.40 * 0.0002 * 3 = 0.04704
    #   all_in_cost = 78.40 + 0.784 + 2.00(gas) + 0.196 + 1.00(transfer) + 0.04704 + 0.50(ops)
    #               = 82.92704
    #   failure_reserve = 78.40 * (0.01+0.005+0.005+0.0025) = 78.40 * 0.0225 = 1.764
    #   proven_floor = q * minimum_basket_payout(1) = 80.00
    #   surplus = 80.00 - 82.92704 - 1.764 = -4.69104  (negative -> REJECTED)
    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        reports = verify_store.scan_reports_as_of(datetime(2026, 8, 15, 12, tzinfo=UTC))
    finally:
        verify_store.close()
    assert len(reports) == 1
    report = reports[0]
    assert report.decision == "REJECTED"
    assert report.reason == "conservative surplus not positive"
    assert report.proof_id is not None
    assert report.economics is not None
    assert report.economics.status == "evaluated"
    assert report.economics.conservative_surplus_usd == Decimal("-4.69104")
    assert report.economics.capacity_usd_at_current_depth == Decimal("78.40")


def test_scan_command_rejects_a_proof_compiled_against_a_superseded_rule_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A proof compiled while ``RULE_VERSION_ID`` was current must not silently survive
    into a persisted ``SHADOW_CANDIDATE`` once a newer rule version supersedes it --
    spec §14 and the proof's own ``invalidation_conditions`` both name this exact
    case. The scan must re-check currency itself (never trust the proof's own
    now-possibly-stale ``rule_version_ids``) and reject before running economics, even
    though books/fees exist and would otherwise price out to a positive surplus.
    """
    database = tmp_path / "predictions.duckdb"
    _seed_candidate_with_rule_and_attestation(database)
    store = PredictionMarketStore(database)
    store.append_book_snapshot(
        prediction_book_snapshot(
            outcome_token_id="111",
            bids=(level("0.20", "10"),),
            asks=(level("0.30", "100"),),
            observed_at=NOW,
        )
    )
    store.append_book_snapshot(
        prediction_book_snapshot(
            outcome_token_id="222",
            bids=(level("0.30", "10"),),
            asks=(level("0.35", "80"),),
            observed_at=NOW,
        )
    )
    store.append_fee_rate(fee_rate(market_id="0xcondition", taker_rate=Decimal("0.01")))
    store.close()

    prove_as_of = "2026-08-15T12:00:00Z"
    prove_exit = main(
        [
            "predictions",
            "prove",
            "--db",
            str(database),
            "--candidate-id",
            str(CANDIDATE_ID),
            "--as-of",
            prove_as_of,
        ]
    )
    assert prove_exit == 0
    capsys.readouterr()

    # A newer rule version for the same market supersedes RULE_VERSION_ID, effective
    # before the scan's as_of -- the candidate's legs (and the persisted proof) still
    # carry the now-superseded RULE_VERSION_ID, since candidates/proofs are append-only.
    newer_rule_version_id = UUID("00000000-0000-0000-0000-000000002099")
    store = PredictionMarketStore(database)
    store.append_rule_version(
        rule_version(
            rule_version_id=newer_rule_version_id,
            market_id="0xcondition",
            effective_at=NOW + timedelta(minutes=30),
            superseded_rule_version_id=RULE_VERSION_ID,
        )
    )
    store.close()

    scan_as_of = "2026-08-15T13:00:00Z"
    scan_exit = main(
        ["predictions", "scan", "--db", str(database), "--as-of", scan_as_of, "--format", "json"]
    )
    assert scan_exit == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tally"] == {
        "SHADOW_CANDIDATE": 0,
        "REJECTED": 1,
        "INSUFFICIENT_EVIDENCE": 0,
    }
    assert output["shadow_candidates"] == []

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        reports = verify_store.scan_reports_as_of(datetime(2026, 8, 15, 13, tzinfo=UTC))
    finally:
        verify_store.close()
    assert len(reports) == 1
    report = reports[0]
    assert report.decision == "REJECTED"
    assert report.reason == "RULE_VERSION_CHANGED"
    assert report.proof_id is not None
    assert report.economics is None


def test_scan_command_reports_insufficient_evidence_for_an_insufficient_evidence_proof(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A proof compiled with no attestation available is itself ``insufficient_evidence``
    (``rejection_reason="MISSING_ATTESTATION"``), distinct from a ``rejected`` proof and
    from no proof existing at all. The scan must pass that proof's own
    ``rejection_reason`` through verbatim as the ``INSUFFICIENT_EVIDENCE`` reason.
    """
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_market(market_record(rule_version_id=RULE_VERSION_ID))
    store.append_rule_version(rule_version(rule_version_id=RULE_VERSION_ID, effective_at=NOW))
    # Deliberately no rule attestation appended.
    store.append_candidate_relationship(candidate_relationship())
    store.close()

    as_of = "2026-08-15T12:00:00Z"
    prove_exit = main(
        [
            "predictions",
            "prove",
            "--db",
            str(database),
            "--candidate-id",
            str(CANDIDATE_ID),
            "--as-of",
            as_of,
        ]
    )
    assert prove_exit == 0
    capsys.readouterr()

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        proof = verify_store.latest_proof_for_candidate(
            CANDIDATE_ID, datetime(2026, 8, 15, 12, tzinfo=UTC)
        )
    finally:
        verify_store.close()
    assert proof is not None
    assert proof.status == "insufficient_evidence"
    assert proof.rejection_reason == "MISSING_ATTESTATION"

    scan_exit = main(
        ["predictions", "scan", "--db", str(database), "--as-of", as_of, "--format", "json"]
    )
    assert scan_exit == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tally"] == {
        "SHADOW_CANDIDATE": 0,
        "REJECTED": 0,
        "INSUFFICIENT_EVIDENCE": 1,
    }
    assert output["shadow_candidates"] == []

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        reports = verify_store.scan_reports_as_of(datetime(2026, 8, 15, 12, tzinfo=UTC))
    finally:
        verify_store.close()
    assert len(reports) == 1
    report = reports[0]
    assert report.decision == "INSUFFICIENT_EVIDENCE"
    assert report.reason == "MISSING_ATTESTATION"
    assert report.proof_id == proof.proof_id
    assert report.economics is None


def test_scan_command_is_idempotent_across_reruns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    _seed_candidate_with_rule_and_attestation(database)

    as_of = "2026-08-15T12:00:00Z"
    main(
        [
            "predictions",
            "prove",
            "--db",
            str(database),
            "--candidate-id",
            str(CANDIDATE_ID),
            "--as-of",
            as_of,
        ]
    )
    capsys.readouterr()

    first_exit = main(["predictions", "scan", "--db", str(database), "--as-of", as_of])
    capsys.readouterr()
    second_exit = main(["predictions", "scan", "--db", str(database), "--as-of", as_of])
    capsys.readouterr()

    assert first_exit == 0
    assert second_exit == 0

    verify_store = PredictionMarketStore(database, read_only=True)
    try:
        reports = verify_store.scan_reports_as_of(datetime(2026, 8, 15, 12, tzinfo=UTC))
    finally:
        verify_store.close()
    assert len(reports) == 1


def test_scan_command_sanitizes_a_persistence_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_candidate_relationship(candidate_relationship())
    store.close()

    def raise_conflict(self: PredictionMarketStore, record: object) -> bool:
        raise ConflictingRecordError("conflicting scan report for immutable identity")

    monkeypatch.setattr(PredictionMarketStore, "append_scan_report", raise_conflict)

    exit_code = main(
        ["predictions", "scan", "--db", str(database), "--as-of", "2026-08-15T12:00:00Z"]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "scan failed to persist durably" in captured.err
    assert "conflicting scan report" not in captured.err


def test_scan_command_never_prints_a_forbidden_promotional_word(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    _seed_candidate_with_rule_and_attestation(database)
    store = PredictionMarketStore(database)
    store.append_book_snapshot(
        prediction_book_snapshot(
            outcome_token_id="111",
            bids=(level("0.20", "10"),),
            asks=(level("0.30", "100"),),
            observed_at=NOW,
        )
    )
    store.append_book_snapshot(
        prediction_book_snapshot(
            outcome_token_id="222",
            bids=(level("0.30", "10"),),
            asks=(level("0.35", "80"),),
            observed_at=NOW,
        )
    )
    store.append_fee_rate(fee_rate(market_id="0xcondition", taker_rate=Decimal("0.01")))
    store.close()

    as_of = "2026-08-15T12:00:00Z"
    main(
        [
            "predictions",
            "prove",
            "--db",
            str(database),
            "--candidate-id",
            str(CANDIDATE_ID),
            "--as-of",
            as_of,
        ]
    )
    capsys.readouterr()

    main(["predictions", "scan", "--db", str(database), "--as-of", as_of])
    output = capsys.readouterr().out.lower()
    for forbidden in ("risk-free", "guaranteed", "approved", "live eligible"):
        assert forbidden not in output


def _seed_runnable_shadow_candidate(database: Path) -> None:
    _seed_candidate_with_rule_and_attestation(database)
    store = PredictionMarketStore(database)
    store.append_book_snapshot(
        prediction_book_snapshot(
            outcome_token_id="111",
            bids=(level("0.19", "10"),),
            asks=(level("0.20", "10"),),
            observed_at=NOW,
        )
    )
    store.append_book_snapshot(
        prediction_book_snapshot(
            outcome_token_id="222",
            bids=(level("0.29", "8"),),
            asks=(level("0.30", "8"),),
            observed_at=NOW,
        )
    )
    store.append_fee_rate(
        fee_rate(market_id="0xcondition", taker_rate=Decimal("0.01"), source_hash="f" * 64)
    )
    store.append_trial_family(
        TrialFamily(
            family_id="shadow-cli-v1",
            hypothesis="Positive proof-floor surplus persists in shadow replay.",
            preregistered_at=NOW,
            thresholds_json='{"version":1}',
            venues=(PredictionVenue.POLYMARKET,),
            registered_by="cli-test",
        )
    )
    store.close()
    assert (
        main(
            [
                "predictions",
                "prove",
                "--db",
                str(database),
                "--candidate-id",
                str(CANDIDATE_ID),
                "--as-of",
                "2026-08-15T12:00:00Z",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "predictions",
                "scan",
                "--db",
                str(database),
                "--as-of",
                "2026-08-15T12:00:00Z",
            ]
        )
        == 0
    )


def test_shadow_command_tree_parses_run_and_replay() -> None:
    run = build_parser().parse_args(
        [
            "predictions",
            "shadow",
            "run",
            "--db",
            "predictions.duckdb",
            "--trial-family",
            "shadow-cli-v1",
        ]
    )
    replay = build_parser().parse_args(
        [
            "predictions",
            "shadow",
            "replay",
            "--db",
            "predictions.duckdb",
            "--proposal-id",
            "00000000-0000-0000-0000-000000000001",
        ]
    )

    assert (run.predictions_command, run.predictions_shadow_command) == ("shadow", "run")
    assert (replay.predictions_command, replay.predictions_shadow_command) == (
        "shadow",
        "replay",
    )


def test_shadow_run_persists_complete_reconciled_lifecycle_with_hand_pnl(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A changed fill, fee, payout, or transaction boundary breaks the USD 3.96 result."""
    database = tmp_path / "predictions.duckdb"
    _seed_runnable_shadow_candidate(database)
    capsys.readouterr()

    exit_code = main(
        [
            "predictions",
            "shadow",
            "run",
            "--db",
            str(database),
            "--trial-family",
            "shadow-cli-v1",
            "--as-of",
            "2026-08-15T12:00:00Z",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["planned"] == 1
    assert output["existing"] == 0
    assert output["refused"] == {}
    assert output["terminal_states"] == {"complete": 1}
    assert Decimal(output["reconciled_paper_pnl_usd"]) == Decimal("3.960")

    store = PredictionMarketStore(database, read_only=True)
    try:
        plans = store.shadow_plans_as_of(NOW)
        assert len(plans) == 1
        events = store.shadow_events_for_proposal(plans[0].proposal_id, NOW)
        postings = store.ledger_postings_for_proposal(plans[0].proposal_id, NOW)
        reconciliation = store.latest_reconciliation_for_proposal(plans[0].proposal_id, NOW)
        experiments = store.shadow_experiments_as_of(NOW)
    finally:
        store.close()
    assert [event.to_state for event in events][-2:] == [
        ShadowState.COMPLETE,
        ShadowState.RECONCILED,
    ]
    assert postings
    assert reconciliation is not None and reconciliation.complete
    assert len(experiments) == 1
    assert experiments[0].paper_pnl_usd == Decimal("3.960")


def test_shadow_run_requires_family_positive_expiry_and_current_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(database),
                "--trial-family",
                "missing",
            ]
        )
        == 2
    )
    assert "trial family is not preregistered" in capsys.readouterr().err
    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(database),
                "--trial-family",
                "missing",
                "--expiry-seconds",
                "0",
            ]
        )
        == 2
    )
    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(tmp_path / "absent.duckdb"),
                "--trial-family",
                "missing",
            ]
        )
        == 2
    )


def test_shadow_run_tallies_risk_and_current_rule_refusals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    risk_database = tmp_path / "risk.duckdb"
    _seed_runnable_shadow_candidate(risk_database)
    capsys.readouterr()
    monkeypatch.setattr(
        predictions_cli,
        "DEFAULT_RISK_POLICY",
        PredictionRiskPolicy(policy_version="shadow-risk-v1", starting_equity_usd=Decimal("50")),
    )
    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(risk_database),
                "--trial-family",
                "shadow-cli-v1",
                "--as-of",
                "2026-08-15T12:00:00Z",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["refused"] == {"RISK_REFUSED": 1}

    monkeypatch.setattr(
        predictions_cli,
        "DEFAULT_RISK_POLICY",
        PredictionRiskPolicy(policy_version="shadow-risk-v1"),
    )
    rule_database = tmp_path / "rule.duckdb"
    _seed_runnable_shadow_candidate(rule_database)
    capsys.readouterr()
    store = PredictionMarketStore(rule_database)
    store.append_rule_version(
        rule_version(
            rule_version_id=UUID("00000000-0000-0000-0000-00000000f001"),
            effective_at=NOW + timedelta(seconds=1),
            superseded_rule_version_id=RULE_VERSION_ID,
        )
    )
    store.close()
    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(rule_database),
                "--trial-family",
                "shadow-cli-v1",
                "--as-of",
                "2026-08-15T12:00:01Z",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["refused"] == {"PROOF_NOT_CURRENT": 1}


def test_shadow_run_runtime_gap_stays_unknown_without_pnl_or_reconciled_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    _seed_runnable_shadow_candidate(database)
    capsys.readouterr()

    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(database),
                "--trial-family",
                "shadow-cli-v1",
                "--as-of",
                "2026-08-15T12:00:00Z",
                "--scenario",
                "latency_5s",
                "--format",
                "json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["terminal_states"] == {"unknown": 1}
    assert Decimal(output["reconciled_paper_pnl_usd"]) == 0

    cutoff = NOW + timedelta(minutes=1)
    store = PredictionMarketStore(database, read_only=True)
    try:
        plan = store.shadow_plans_as_of(cutoff)[0]
        events = store.shadow_events_for_proposal(plan.proposal_id, cutoff)
        experiment = store.shadow_experiments_as_of(cutoff)[0]
    finally:
        store.close()
    assert events[-1].to_state is ShadowState.UNKNOWN
    assert all(event.to_state is not ShadowState.RECONCILED for event in events)
    assert experiment.reconciled is False
    assert experiment.paper_pnl_usd is None


def test_shadow_run_persists_failed_losing_experiment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    _seed_runnable_shadow_candidate(database)
    capsys.readouterr()

    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(database),
                "--trial-family",
                "shadow-cli-v1",
                "--as-of",
                "2026-08-15T12:00:00Z",
                "--scenario",
                "second_leg_reject",
                "--format",
                "json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["terminal_states"] == {"unwound": 1}
    assert Decimal(output["reconciled_paper_pnl_usd"]) < 0

    store = PredictionMarketStore(database, read_only=True)
    try:
        experiment = store.shadow_experiments_as_of(NOW)[0]
    finally:
        store.close()
    assert experiment.terminal_state is ShadowState.RECONCILED
    assert experiment.reconciled is True
    assert experiment.paper_pnl_usd is not None and experiment.paper_pnl_usd < 0


def test_shadow_run_is_idempotent_and_rolls_back_every_row_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "idempotent.duckdb"
    _seed_runnable_shadow_candidate(database)
    capsys.readouterr()
    command = [
        "predictions",
        "shadow",
        "run",
        "--db",
        str(database),
        "--trial-family",
        "shadow-cli-v1",
        "--as-of",
        "2026-08-15T12:00:00Z",
        "--format",
        "json",
    ]
    assert main(command) == 0
    capsys.readouterr()
    assert main(command) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["planned"] == 0
    assert second["existing"] == 1
    assert second["terminal_states"] == {}
    assert Decimal(second["reconciled_paper_pnl_usd"]) == 0

    unknown_database = tmp_path / "unknown-idempotent.duckdb"
    _seed_runnable_shadow_candidate(unknown_database)
    capsys.readouterr()
    unknown_command = [*command]
    unknown_command[unknown_command.index(str(database))] = str(unknown_database)
    unknown_command.extend(["--scenario", "latency_5s"])
    assert main(unknown_command) == 0
    capsys.readouterr()
    assert main(unknown_command) == 0
    unknown_second = json.loads(capsys.readouterr().out)
    assert unknown_second["planned"] == 0
    assert unknown_second["existing"] == 1

    rollback_database = tmp_path / "rollback.duckdb"
    _seed_runnable_shadow_candidate(rollback_database)
    capsys.readouterr()

    def fail_experiment(self: PredictionMarketStore, record: object) -> bool:
        raise RuntimeError("private database detail")

    monkeypatch.setattr(PredictionMarketStore, "append_shadow_experiment", fail_experiment)
    rollback_command = [*command]
    rollback_command[rollback_command.index(str(database))] = str(rollback_database)
    assert main(rollback_command) == 1
    captured = capsys.readouterr()
    assert "shadow run failed to persist atomically" in captured.err
    assert "private database detail" not in captured.err
    store = PredictionMarketStore(rollback_database, read_only=True)
    try:
        assert store.shadow_plans_as_of(NOW) == ()
    finally:
        store.close()


def test_shadow_run_text_and_json_outputs_never_use_promotional_vocabulary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for output_format in ("text", "json"):
        database = tmp_path / f"{output_format}.duckdb"
        _seed_runnable_shadow_candidate(database)
        capsys.readouterr()
        assert (
            main(
                [
                    "predictions",
                    "shadow",
                    "run",
                    "--db",
                    str(database),
                    "--trial-family",
                    "shadow-cli-v1",
                    "--as-of",
                    "2026-08-15T12:00:00Z",
                    "--format",
                    output_format,
                ]
            )
            == 0
        )
        output = capsys.readouterr().out.lower()
        for forbidden in ("risk-free", "guaranteed", "approved", "live eligible"):
            assert forbidden not in output


def _seed_completed_shadow_run(database: Path, capsys: pytest.CaptureFixture[str]) -> UUID:
    _seed_runnable_shadow_candidate(database)
    capsys.readouterr()
    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(database),
                "--trial-family",
                "shadow-cli-v1",
                "--as-of",
                "2026-08-15T12:00:00Z",
            ]
        )
        == 0
    )
    capsys.readouterr()
    store = PredictionMarketStore(database, read_only=True)
    try:
        proposal_id = store.shadow_plans_as_of(NOW)[0].proposal_id
    finally:
        store.close()
    return proposal_id


def test_shadow_replay_exact_match_and_later_evidence_noninterference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    proposal_id = _seed_completed_shadow_run(database, capsys)
    store = PredictionMarketStore(database)
    store.append_book_snapshot(
        prediction_book_snapshot(
            cycle_id=UUID("00000000-0000-0000-0000-00000000b999"),
            outcome_token_id="111",
            bids=(level("0.10", "1"),),
            asks=(level("0.90", "1"),),
            observed_at=NOW + timedelta(minutes=1),
            effective_at=NOW + timedelta(minutes=1),
            source_hash="9" * 64,
        )
    )
    store.append_fee_rate(
        fee_rate(
            market_id="0xcondition",
            observed_at=NOW + timedelta(minutes=1),
            taker_rate=Decimal("0.5"),
            source_hash="8" * 64,
        )
    )
    store.close()

    assert (
        main(
            [
                "predictions",
                "shadow",
                "replay",
                "--db",
                str(database),
                "--proposal-id",
                str(proposal_id),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert output.rstrip().endswith("replay MATCHES stored events")
    assert "sequence=0" in output
    assert "sequence=6" in output


def test_shadow_replay_detects_direct_stored_event_tamper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    proposal_id = _seed_completed_shadow_run(database, capsys)
    store = PredictionMarketStore(database)
    terminal = store.shadow_events_for_proposal(proposal_id, NOW)[5]
    tampered = terminal.model_copy(update={"detail": "tampered but structurally valid"})
    store._connection.execute(
        "UPDATE shadow_events SET record_json = ? WHERE proposal_id = ? AND sequence = 5",
        [tampered.model_dump_json(), proposal_id],
    )
    store.close()

    assert (
        main(
            [
                "predictions",
                "shadow",
                "replay",
                "--db",
                str(database),
                "--proposal-id",
                str(proposal_id),
            ]
        )
        == 1
    )
    assert capsys.readouterr().out.rstrip().endswith("replay DIVERGES at sequence 5")


def test_shadow_replay_what_if_is_read_only_and_not_a_corruption_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "predictions.duckdb"
    proposal_id = _seed_completed_shadow_run(database, capsys)
    before = database.read_bytes()

    def reject_writer_lease(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("replay must not acquire a writer lease")

    monkeypatch.setattr(predictions_cli, "database_writer_lease", reject_writer_lease)

    assert (
        main(
            [
                "predictions",
                "shadow",
                "replay",
                "--db",
                str(database),
                "--proposal-id",
                str(proposal_id),
                "--scenario",
                "second_leg_reject",
                "--format",
                "json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "what_if"
    assert output["persisted"] is False
    assert output["scenario"] == "second_leg_reject"
    assert "verdict" not in output
    assert database.read_bytes() == before


def test_shadow_replay_rejects_invalid_uuid_and_sanitizes_missing_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    assert (
        main(
            [
                "predictions",
                "shadow",
                "replay",
                "--db",
                str(database),
                "--proposal-id",
                "not-a-uuid",
            ]
        )
        == 2
    )
    capsys.readouterr()
    assert (
        main(
            [
                "predictions",
                "shadow",
                "replay",
                "--db",
                str(database),
                "--proposal-id",
                "00000000-0000-0000-0000-000000000001",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "shadow replay evidence is unavailable or inconsistent" in captured.err
    assert str(database) not in captured.err


@pytest.mark.parametrize(
    ("table", "key_column", "field", "value"),
    [
        ("scan_reports", "report_id", "reason", "tampered scan metadata"),
        (
            "candidate_relationships",
            "candidate_id",
            "provenance",
            {
                "kind": "deterministic",
                "generator": "tampered",
                "generator_version": "1",
                "code_revision": "tampered",
            },
        ),
        ("proof_artifacts", "proof_id", "review_identity", "tampered-reviewer"),
        ("rule_versions", "rule_version_id", "description", "tampered rules"),
        ("prediction_books", "cycle_id", "sequence", "tampered-sequence"),
        ("prediction_fee_rates", "venue", "maker_rate", "0.123"),
        ("shadow_plans", "proposal_id", "completion_path", "tampered path"),
    ],
)
def test_shadow_replay_rejects_exact_lineage_record_json_tamper(
    table: str,
    key_column: str,
    field: str,
    value: object,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / f"{table}.duckdb"
    proposal_id = _seed_completed_shadow_run(database, capsys)
    store = PredictionMarketStore(database)
    if table == "shadow_plans":
        key: object = proposal_id
    elif table == "candidate_relationships":
        key = CANDIDATE_ID
    elif table == "proof_artifacts":
        key = store.shadow_plan_by_proposal(proposal_id).proof_id
    elif table == "scan_reports":
        key = store.shadow_plan_by_proposal(proposal_id).scan_report_id
    elif table == "rule_versions":
        key = RULE_VERSION_ID
    elif table == "prediction_books":
        key = store.latest_book_as_of(
            PredictionVenue.POLYMARKET, "0xcondition", "111", NOW
        ).cycle_id
    else:
        key = PredictionVenue.POLYMARKET.value
    row = store._connection.execute(
        f"SELECT record_json FROM {table} WHERE {key_column} = ? LIMIT 1", [key]
    ).fetchone()
    payload = json.loads(row[0])
    payload[field] = value
    store._connection.execute(
        f"UPDATE {table} SET record_json = ? WHERE {key_column} = ?",
        [json.dumps(payload, sort_keys=True), key],
    )
    store.close()

    assert (
        main(
            [
                "predictions",
                "shadow",
                "replay",
                "--db",
                str(database),
                "--proposal-id",
                str(proposal_id),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "MATCHES" not in captured.out
    assert "shadow replay evidence is unavailable or inconsistent" in captured.err


@pytest.mark.parametrize("sequence", [0, 6])
def test_shadow_replay_reports_provenance_and_reconciliation_event_tamper_as_divergence(
    sequence: int,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / f"event-{sequence}.duckdb"
    proposal_id = _seed_completed_shadow_run(database, capsys)
    store = PredictionMarketStore(database)
    event = store.shadow_events_for_proposal(proposal_id, NOW)[sequence]
    tampered = event.model_copy(update={"detail": f"tampered sequence {sequence}"})
    store._connection.execute(
        "UPDATE shadow_events SET record_json = ? WHERE proposal_id = ? AND sequence = ?",
        [tampered.model_dump_json(), proposal_id, sequence],
    )
    store.close()

    assert (
        main(
            [
                "predictions",
                "shadow",
                "replay",
                "--db",
                str(database),
                "--proposal-id",
                str(proposal_id),
            ]
        )
        == 1
    )
    assert capsys.readouterr().out.rstrip().endswith(f"replay DIVERGES at sequence {sequence}")


def _append_scan_revision(
    store: PredictionMarketStore,
    source: ScanReport,
    *,
    as_of: datetime,
    decision: str = "SHADOW_CANDIDATE",
) -> ScanReport:
    economics = source.economics if decision == "SHADOW_CANDIDATE" else None
    proof_id = source.proof_id
    reason = "later positive" if decision == "SHADOW_CANDIDATE" else "later rejection"
    report_id = deterministic_scan_report_id(
        candidate_id=source.candidate_id,
        proof_id=proof_id,
        decision=decision,
        reason=reason,
        economics=economics,
        policy_id=source.policy_id,
        policy_version=source.policy_version,
        as_of=as_of,
    )
    report = ScanReport(
        report_id=report_id,
        candidate_id=source.candidate_id,
        proof_id=proof_id,
        decision=decision,
        reason=reason,
        economics=economics,
        policy_id=source.policy_id,
        policy_version=source.policy_version,
        as_of=as_of,
        observed_at=as_of,
    )
    store.append_scan_report(report)
    return report


@pytest.mark.parametrize(
    ("latest_decision", "planned", "pnl"),
    [("SHADOW_CANDIDATE", 1, Decimal("3.960")), ("REJECTED", 0, Decimal("0"))],
)
def test_shadow_run_uses_only_each_candidates_latest_effective_scan(
    latest_decision: str,
    planned: int,
    pnl: Decimal,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / f"{latest_decision}.duckdb"
    _seed_runnable_shadow_candidate(database)
    capsys.readouterr()
    store = PredictionMarketStore(database)
    source = store.scan_reports_as_of(NOW)[0]
    _append_scan_revision(
        store,
        source,
        as_of=NOW + timedelta(seconds=1),
        decision=latest_decision,
    )
    store.close()

    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(database),
                "--trial-family",
                "shadow-cli-v1",
                "--as-of",
                "2026-08-15T12:00:01Z",
                "--format",
                "json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["planned"] == planned
    assert Decimal(output["reconciled_paper_pnl_usd"]) == pnl


def test_shadow_run_applies_first_batch_loss_before_risk_gating_second_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "sequential-risk.duckdb"
    _seed_runnable_shadow_candidate(database)
    capsys.readouterr()
    store = PredictionMarketStore(database)
    first_candidate = store.candidate_relationship_by_id(CANDIDATE_ID, NOW)
    first_proof = store.latest_proof_for_candidate(CANDIDATE_ID, NOW)
    first_report = store.scan_reports_as_of(NOW)[0]
    second_candidate_id = UUID("00000000-0000-0000-0000-000000003002")
    second_proof_id = UUID("00000000-0000-0000-0000-000000006002")
    second_candidate = first_candidate.model_copy(update={"candidate_id": second_candidate_id})
    second_proof = first_proof.model_copy(
        update={"proof_id": second_proof_id, "candidate_id": second_candidate_id}
    )
    second_report_id = deterministic_scan_report_id(
        candidate_id=second_candidate_id,
        proof_id=second_proof_id,
        decision="SHADOW_CANDIDATE",
        reason="second candidate",
        economics=first_report.economics,
        policy_id=first_report.policy_id,
        policy_version=first_report.policy_version,
        as_of=NOW,
    )
    second_report = ScanReport(
        report_id=second_report_id,
        candidate_id=second_candidate_id,
        proof_id=second_proof_id,
        decision="SHADOW_CANDIDATE",
        reason="second candidate",
        economics=first_report.economics,
        policy_id=first_report.policy_id,
        policy_version=first_report.policy_version,
        as_of=NOW,
        observed_at=NOW,
    )
    store.append_candidate_relationship(second_candidate)
    store.append_proof_artifact(second_proof)
    store.append_scan_report(second_report)
    store.close()
    monkeypatch.setattr(
        predictions_cli,
        "DEFAULT_RISK_POLICY",
        PredictionRiskPolicy(policy_version="shadow-risk-v1", starting_equity_usd=Decimal("151.1")),
    )

    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(database),
                "--trial-family",
                "shadow-cli-v1",
                "--as-of",
                "2026-08-15T12:00:00Z",
                "--scenario",
                "second_leg_reject",
                "--format",
                "json",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["planned"] == 1
    assert output["refused"] == {"RISK_REFUSED": 1}
    assert output["terminal_states"] == {"unwound": 1}
    assert Decimal(output["reconciled_paper_pnl_usd"]) == Decimal("-0.1272")


@pytest.mark.parametrize(
    "missing_phase",
    ["plan_only", "events", "postings", "reconciliation", "experiment"],
)
def test_shadow_run_rejects_partial_existing_lifecycle_without_mutation(
    missing_phase: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / f"{missing_phase}.duckdb"
    _seed_completed_shadow_run(database, capsys)
    store = PredictionMarketStore(database)
    if missing_phase in {"plan_only", "events"}:
        store._connection.execute("DELETE FROM shadow_events")
    if missing_phase in {"plan_only", "postings"}:
        store._connection.execute("DELETE FROM shadow_ledger_postings")
    if missing_phase in {"plan_only", "reconciliation"}:
        store._connection.execute("DELETE FROM shadow_reconciliations")
    if missing_phase in {"plan_only", "experiment"}:
        store._connection.execute("DELETE FROM shadow_experiments")
    tables = (
        "shadow_plans",
        "shadow_events",
        "shadow_ledger_postings",
        "shadow_reconciliations",
        "shadow_experiments",
    )
    before = {
        table: tuple(
            row[0]
            for row in store._connection.execute(
                f"SELECT record_json FROM {table} ORDER BY record_json"
            ).fetchall()
        )
        for table in tables
    }
    store.close()

    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(database),
                "--trial-family",
                "shadow-cli-v1",
                "--as-of",
                "2026-08-15T12:00:00Z",
                "--format",
                "json",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "shadow run failed to persist atomically" in captured.err
    assert "existing" not in captured.out

    store = PredictionMarketStore(database, read_only=True)
    try:
        after = {
            table: tuple(
                row[0]
                for row in store._connection.execute(
                    f"SELECT record_json FROM {table} ORDER BY record_json"
                ).fetchall()
            )
            for table in tables
        }
    finally:
        store.close()
    assert after == before


@pytest.mark.parametrize(
    "forbidden_phrase",
    ["risk-free", "guaranteed", "approved", "live eligible"],
)
def test_shadow_run_never_echoes_registered_or_missing_trial_family_ids(
    forbidden_phrase: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / f"family-{forbidden_phrase.replace(' ', '-')}.duckdb"
    _seed_runnable_shadow_candidate(database)
    family_id = f"adversarial {forbidden_phrase} family"
    store = PredictionMarketStore(database)
    store.append_trial_family(
        TrialFamily(
            family_id=family_id,
            hypothesis="A deliberately adversarial identifier is never rendered.",
            preregistered_at=NOW,
            thresholds_json='{"version":1}',
            venues=(PredictionVenue.POLYMARKET,),
            registered_by="cli-test",
        )
    )
    store.close()
    capsys.readouterr()

    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(database),
                "--trial-family",
                family_id,
                "--as-of",
                "2026-08-15T12:00:00Z",
                "--format",
                "json",
            ]
        )
        == 0
    )
    registered_output = capsys.readouterr()
    assert forbidden_phrase not in registered_output.out.lower()
    assert forbidden_phrase not in registered_output.err.lower()

    missing_family_id = f"missing {forbidden_phrase} family"
    assert (
        main(
            [
                "predictions",
                "shadow",
                "run",
                "--db",
                str(database),
                "--trial-family",
                missing_family_id,
                "--as-of",
                "2026-08-15T12:00:00Z",
            ]
        )
        == 2
    )
    missing_output = capsys.readouterr()
    assert forbidden_phrase not in missing_output.out.lower()
    assert forbidden_phrase not in missing_output.err.lower()
