from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import duckdb
import httpx
import pytest

import polytrading.cli as cli
from polytrading.carry.economics_models import EconomicsDecision
from polytrading.cli import RetryingTransport, collect_book_cycles, main
from polytrading.corpus_intake.evidence import (
    POLYMARKET_EVIDENCE_TARGETS,
    verify_source_use_run,
)
from polytrading.corpus_intake.models import (
    AcquisitionDiagnostics,
    AcquisitionResult,
    CorpusIntakeError,
)
from polytrading.corpus_intake.polymarket import parse_page
from polytrading.corpus_intake.review_queue import verify_review_queue_run
from polytrading.corpus_intake.source_policy import SourceEvidence, canonical_sha256
from polytrading.domain.models import (
    Asset,
    BookLevel,
    Level2BookSnapshot,
    MarketSnapshot,
    RawEnvelope,
    Venue,
)
from polytrading.replay import replay_file
from polytrading.storage.store import ConflictingRecordError, DuckDBStore
from polytrading.trial.books import TrialBookRunSummary
from polytrading.trial.funding_models import TrialFundingCycleStatus
from polytrading.trial.writer_lease import WriterLeaseUnavailable
from polytrading.venues.funding_cycle_models import FundingCycleStatus
from polytrading.venues.public import AdapterBatch, AdapterWarning
from tests.carry.study_helpers import at, complete_block
from tests.carry.test_economics import EVALUATION_ID, evaluate_bundle, passing_bundle
from tests.carry.test_economics_models import KNOWN_AS_OF, policy
from tests.carry.test_fee_import import payload as fee_payload
from tests.domain.factories import funding_observation, instrument_spec
from tests.trial.funding_helpers import CYCLE_END as TRIAL_CYCLE_END
from tests.venues.funding_cycle_helpers import (
    CYCLE_END as FUNDING_CYCLE_END,
)
from tests.venues.funding_cycle_helpers import (
    FakeFundingAdapter,
    funding_batch,
    instrument_batch,
)
from tests.venues.funding_cycle_helpers import (
    instrument_spec as cycle_instrument_spec,
)
from tests.venues.funding_health_helpers import HEALTH_AS_OF, LATEST_BOUNDARY, funding_cycle

FIXTURE = Path("tests/fixtures/replay/public_snapshot.jsonl")
NOW = datetime(2026, 8, 12, 12, tzinfo=UTC)


def policy_payload(**updates: object) -> bytes:
    document = policy().model_dump(mode="json")
    document.update(updates)
    return json.dumps(document, separators=(",", ":")).encode()


def test_shadow_economics_and_fee_import_parsers_are_explicit() -> None:
    fees = cli.build_parser().parse_args(
        ["fees", "import", "--input", "reviewed.json", "--db", "research.duckdb"]
    )
    economics = cli.build_parser().parse_args(
        [
            "carry",
            "economics",
            "--policy",
            "policy.json",
            "--db",
            "research.duckdb",
            "--evaluated-at",
            "2026-08-13T17:00:07Z",
            "--evaluation-id",
            str(EVALUATION_ID),
            "--format",
            "json",
        ]
    )

    assert (fees.command, fees.fees_command) == ("fees", "import")
    assert fees.input == Path("reviewed.json")
    assert economics.carry_command == "economics"
    assert economics.format == "json"


def test_fee_import_cli_records_one_atomic_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_path = tmp_path / "reviewed.json"
    input_path.write_bytes(fee_payload())
    database = tmp_path / "research.duckdb"

    assert main(["fees", "import", "--input", str(input_path), "--db", str(database)]) == 0

    assert capsys.readouterr().out == "imported 2 reviewed fee schedules\n"
    store = DuckDBStore(database, read_only=True)
    assert store.latest_fee_as_of(Venue.DYDX, "reviewed-tier", KNOWN_AS_OF) is not None
    store.close()


@pytest.mark.parametrize(
    "bad_payload",
    [
        b'{"schema_version":1,"schema_version":1}',
        policy_payload(account_equity_usd=8000),
    ],
)
def test_economics_policy_cli_rejects_duplicate_keys_and_json_numbers(
    bad_payload: bytes,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(bad_payload)
    database = tmp_path / "research.duckdb"
    database.touch()

    result = main(
        [
            "carry",
            "economics",
            "--policy",
            str(policy_path),
            "--db",
            str(database),
            "--evaluated-at",
            "2026-08-13T17:00:07Z",
            "--evaluation-id",
            str(EVALUATION_ID),
        ]
    )

    assert result == 2
    assert "policy" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    "decision",
    [
        EconomicsDecision.INSUFFICIENT_EVIDENCE,
        EconomicsDecision.REJECTED,
        EconomicsDecision.SHADOW_CANDIDATE,
    ],
)
def test_economics_cli_is_offline_persists_once_and_returns_decisions_as_success(
    decision: EconomicsDecision,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(policy_payload())
    database = tmp_path / "research.duckdb"
    database.touch()
    baseline = evaluate_bundle(passing_bundle())
    if decision is EconomicsDecision.INSUFFICIENT_EVIDENCE:
        selected = baseline.model_copy(
            update={
                "decision": decision,
                "reason_codes": ("BOOK_COVERAGE_INSUFFICIENT",),
                "direction": None,
                "short_venue": None,
                "long_venue": None,
                "economics": None,
            }
        )
    elif decision is EconomicsDecision.REJECTED:
        selected = baseline.model_copy(
            update={"decision": decision, "reason_codes": ("COMPATIBILITY_BLOCKING",)}
        )
    else:
        selected = baseline
    calls: list[object] = []

    class FakeStore:
        def __init__(self, path: Path) -> None:
            calls.append(("store", path))

        def append_economic_evaluation(self, report: object) -> bool:
            calls.append(("append", report))
            return True

        def close(self) -> None:
            calls.append("close")

    class FakeAssembler:
        def __init__(self, store: object) -> None:
            calls.append(("assembler", store))

        def assemble(self, loaded_policy: object) -> object:
            calls.append(("assemble", loaded_policy))
            return object()

    class FakeEvaluator:
        def evaluate(self, result: object, *, evaluated_at: datetime, evaluation_id: UUID):
            calls.append(("evaluate", result, evaluated_at, evaluation_id))
            return selected

    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("economics command must remain offline")

    monkeypatch.setattr(cli, "DuckDBStore", FakeStore)
    monkeypatch.setattr(cli, "EconomicsEvidenceAssembler", FakeAssembler)
    monkeypatch.setattr(cli, "CandidateEconomicsEvaluator", FakeEvaluator)
    monkeypatch.setattr(cli, "make_public_http_client", reject_network)

    exit_code = main(
        [
            "carry",
            "economics",
            "--policy",
            str(policy_path),
            "--db",
            str(database),
            "--evaluated-at",
            "2026-08-13T17:00:07Z",
            "--evaluation-id",
            str(EVALUATION_ID),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["decision"] == decision.value
    assert sum(isinstance(call, tuple) and call[0] == "append" for call in calls) == 1
    assert calls[-1] == "close"


def test_dashboard_parser_has_local_read_only_arguments_only() -> None:
    parsed = cli.build_parser().parse_args(["dashboard", "--db", "var/forward.duckdb"])

    assert parsed.command == "dashboard"
    assert parsed.db == Path("var/forward.duckdb")
    assert parsed.port == 8787
    assert {
        "host",
        "token",
        "user",
        "password",
        "account",
        "order",
        "execution",
    }.isdisjoint(vars(parsed))


def test_carry_dossier_parser_defaults_to_text_without_database_argument() -> None:
    parsed = cli.build_parser().parse_args(["carry", "dossier"])

    assert parsed.command == "carry"
    assert parsed.carry_command == "dossier"
    assert parsed.id == "hyperliquid-dydx-core-v1"
    assert parsed.format == "text"
    assert "db" not in vars(parsed)


def test_carry_dossier_accepts_explicit_catalog_id() -> None:
    parsed = cli.build_parser().parse_args(
        ["carry", "dossier", "--id", "lighter-dydx-core-v1", "--format", "json"]
    )

    assert parsed.id == "lighter-dydx-core-v1"
    assert parsed.format == "json"


def test_carry_discovery_parser_is_database_free() -> None:
    parsed = cli.build_parser().parse_args(["carry", "discovery"])

    assert parsed.command == "carry"
    assert parsed.carry_command == "discovery"
    assert parsed.format == "text"
    assert "db" not in vars(parsed)


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_carry_dossier_is_deterministic_offline_and_database_free(
    output_format: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dossier command must not create a public network client")

    monkeypatch.setattr(cli, "make_public_http_client", reject_network)

    arguments = ["carry", "dossier", "--format", output_format]
    assert main(arguments) == 0
    first = capsys.readouterr().out
    assert main(arguments) == 0
    second = capsys.readouterr().out

    assert first == second
    assert not tuple(tmp_path.iterdir())
    if output_format == "json":
        assert json.loads(first)["primary_reason_code"] == "quanto_structure_excluded"
    else:
        assert "status=ineligible" in first
        assert "primary_blocker=quanto_structure_excluded" in first


def test_carry_dossier_validation_failure_uses_sanitized_exit_two(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_dossier(_dossier_id: str = "hyperliquid-dydx-core-v1") -> None:
        raise ValueError("invalid bundled dossier")

    monkeypatch.setattr(cli, "load_bundled_dossier", reject_dossier)

    assert main(["carry", "dossier"]) == 2
    assert capsys.readouterr().err == "polytrading: error: invalid bundled dossier\n"


def test_carry_dossier_explicit_candidate_reports_model_required(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["carry", "dossier", "--id", "lighter-dydx-core-v1", "--format", "json"]) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["dossier_id"] == "lighter-dydx-core-v1"
    assert document["status"] == "model_required"
    assert document["counts"] == {
        "blocking": 0,
        "matched": 4,
        "missing_evidence": 0,
        "model_required": 10,
    }
    assert document["activation_status"] == "not_authorized"


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_carry_discovery_is_deterministic_offline_and_database_free(
    output_format: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("discovery command must not create a public network client")

    monkeypatch.setattr(cli, "make_public_http_client", reject_network)
    arguments = ["carry", "discovery", "--format", output_format]

    assert main(arguments) == 0
    first = capsys.readouterr().out
    assert main(arguments) == 0
    second = capsys.readouterr().out

    assert first == second
    assert not tuple(tmp_path.iterdir())
    if output_format == "json":
        document = json.loads(first)
        assert document["selected_dossier_id"] == "lighter-dydx-core-v1"
        assert document["counts"]["model_required"] == 1
        assert document["activation_status"] == "not_authorized"
    else:
        assert "selected=lighter-dydx-core-v1" in first
        assert "rank=2 | dossier=hyperliquid-dydx-core-v1" in first
        assert "no trading authority exists" in first


def test_carry_discovery_catalog_failure_uses_sanitized_exit_two(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_catalog() -> None:
        raise ValueError("invalid bundled dossier catalog")

    monkeypatch.setattr(cli, "load_bundled_dossiers", reject_catalog, raising=False)

    assert main(["carry", "discovery"]) == 2
    assert capsys.readouterr().err == "polytrading: error: invalid bundled dossier catalog\n"


def test_carry_dossier_unknown_id_uses_sanitized_exit_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["carry", "dossier", "--id", "unknown-pair-v1"]) == 2
    assert (
        capsys.readouterr().err == "polytrading: error: unknown bundled dossier: unknown-pair-v1\n"
    )


@pytest.mark.parametrize("port", ["0", "65536", "1.5", "true", ""])
def test_dashboard_rejects_invalid_ports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], port: str
) -> None:
    assert main(["dashboard", "--db", str(tmp_path / "db.duckdb"), "--port", port]) == 2
    assert "port must be an integer between 1 and 65535" in capsys.readouterr().err


@pytest.mark.parametrize("port", [1, 65_535])
def test_dashboard_accepts_port_boundaries(port: int) -> None:
    parsed = cli.build_parser().parse_args(
        ["dashboard", "--db", "var/forward.duckdb", "--port", str(port)]
    )
    assert parsed.port == port


def test_dashboard_validates_then_serves_selected_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "research data.duckdb"
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        cli,
        "validate_dashboard_database",
        lambda value: calls.append(("validate", value)),
    )
    monkeypatch.setattr(
        cli,
        "serve_dashboard",
        lambda value, port: calls.append(("serve", value, port)),
    )

    assert main(["dashboard", "--db", str(path), "--port", "9000"]) == 0
    assert calls == [("validate", path), ("serve", path, 9000)]


def test_replay_and_audit_are_deterministic_and_preserve_lineage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "replay.duckdb"

    assert main(["replay", "--input", str(FIXTURE), "--db", str(database)]) == 0
    capsys.readouterr()
    audit_args = [
        "carry",
        "audit",
        "--db",
        str(database),
        "--as-of",
        "2026-08-12T12:00:00Z",
        "--format",
        "json",
    ]
    assert main(audit_args) == 0
    first = capsys.readouterr().out
    assert main(audit_args) == 0
    second = capsys.readouterr().out

    assert first == second
    report = json.loads(first)
    assert [row["asset"] for row in report["assets"]] == ["BTC", "ETH", "SOL"]
    assert all(row["status"] == "INELIGIBLE" for row in report["assets"])
    with duckdb.connect(str(database), read_only=True) as connection:
        raw_hashes = {
            row[0] for row in connection.execute("SELECT source_hash FROM raw_envelopes").fetchall()
        }
        normalized_hashes = {
            row[0]
            for table in ("instrument_specs", "funding_observations")
            for row in connection.execute(f"SELECT source_hash FROM {table}").fetchall()
        }
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (8,)
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (6,)
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (6,)
    assert normalized_hashes <= raw_hashes


def test_replay_aborts_the_entire_file_on_a_malformed_later_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    input_path = tmp_path / "malformed.jsonl"
    first_line = FIXTURE.read_text().splitlines()[0]
    input_path.write_text(f"{first_line}\n{{not-json}}\n")
    database = tmp_path / "replay.duckdb"

    assert main(["replay", "--input", str(input_path), "--db", str(database)]) == 2

    assert "line 2" in capsys.readouterr().err
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (0,)


def test_replay_rejects_normalized_lineage_outside_its_raw_batch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    row = json.loads(FIXTURE.read_text().splitlines()[0])
    row["normalized"][0]["source_hash"] = "f" * 64
    input_path = tmp_path / "bad-lineage.jsonl"
    input_path.write_text(json.dumps(row) + "\n")

    assert main(["replay", "--input", str(input_path), "--db", str(tmp_path / "db.duckdb")]) == 2
    assert "lineage" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("corruption", ["payload", "venue"])
def test_replay_rejects_corrupt_raw_hash_and_cross_venue_lineage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], corruption: str
) -> None:
    row = json.loads(FIXTURE.read_text().splitlines()[0])
    if corruption == "payload":
        row["raw"][0]["payload_json"] += " "
    else:
        row["raw"][0]["venue"] = "hyperliquid"
    input_path = tmp_path / f"bad-{corruption}.jsonl"
    input_path.write_text(json.dumps(row) + "\n")

    assert main(["replay", "--input", str(input_path), "--db", str(tmp_path / "db.duckdb")]) == 2
    message = capsys.readouterr().err.lower()
    assert "hash" in message or "lineage" in message


def test_cli_validation_errors_exit_two_without_tracebacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "carry",
                "audit",
                "--db",
                str(tmp_path / "audit.duckdb"),
                "--as-of",
                "invalid",
            ]
        )
        == 2
    )
    assert "traceback" not in capsys.readouterr().err.lower()


def test_carry_study_requires_explicit_research_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "carry",
                "study",
                "--db",
                str(tmp_path / "study.duckdb"),
                "--asset",
                "BTC",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-01T08:00:00Z",
            ]
        )
        == 2
    )
    assert "known-as-of" in capsys.readouterr().err


def test_carry_study_is_deterministic_read_only_and_offline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "study.duckdb"
    start = at("2026-01-01T00:00:00Z")
    store = DuckDBStore(database)
    for row in complete_block(start):
        store.append_funding(row)
    store.close()
    before_hash = sha256(database.read_bytes()).hexdigest()
    before_rows = _stored_funding_rows(database)

    def reject_network(**_kwargs: object) -> None:
        raise AssertionError("carry study must not create a network client")

    monkeypatch.setattr(cli, "make_public_http_client", reject_network)
    arguments = [
        "carry",
        "study",
        "--db",
        str(database),
        "--asset",
        "BTC",
        "--start",
        "2026-01-01T00:00:00Z",
        "--end",
        "2026-01-01T08:00:00Z",
        "--known-as-of",
        "2026-01-01T08:05:00Z",
        "--format",
        "json",
    ]

    assert main(arguments) == 0
    first = capsys.readouterr().out
    assert main(arguments) == 0
    second = capsys.readouterr().out

    assert first == second
    assert json.loads(first)["decision"] == "INSUFFICIENT_DATA"
    assert before_rows == _stored_funding_rows(database)
    assert before_hash == sha256(database.read_bytes()).hexdigest()


def test_carry_study_validation_is_sanitized_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "carry",
                "study",
                "--db",
                str(tmp_path / "study.duckdb"),
                "--asset",
                "BTC",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-01T08:00:00Z",
                "--known-as-of",
                "2026-01-01T07:59:59Z",
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "polytrading: error:" in error
    assert "known_as_of must not precede end" in error
    assert "traceback" not in error.lower()


def test_carry_study_missing_database_fails_without_creating_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "missing.duckdb"

    assert (
        main(
            [
                "carry",
                "study",
                "--db",
                str(database),
                "--asset",
                "BTC",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-01T08:00:00Z",
                "--known-as-of",
                "2026-01-01T08:05:00Z",
            ]
        )
        == 1
    )
    assert not database.exists()
    assert "collection failed" in capsys.readouterr().err


def _stored_funding_rows(database: Path) -> list[tuple[object, ...]]:
    with duckdb.connect(str(database), read_only=True) as connection:
        return connection.execute(
            """
            SELECT venue, symbol, asset, rate, interval_hours, epoch_us(effective_at),
                   epoch_us(observed_at), source_hash, schema_version, record_hash
            FROM funding_observations
            ORDER BY venue, symbol, effective_at, observed_at
            """
        ).fetchall()


def test_collect_corpus_requires_explicit_bounded_quarantine_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    base = [
        "collect",
        "corpus",
        "--source",
        "polymarket",
        "--output",
        str(tmp_path / "var" / "corpus-intake" / "run"),
        "--retrieved-at",
        "2026-08-12T16:00:00Z",
        "--information-cutoff",
        "2026-08-12T15:00:00Z",
        "--max-candidates",
        "500",
    ]

    assert main(base[:-2]) == 2
    assert "required" in capsys.readouterr().err.lower()
    assert main([*base, "--page-size", "101"]) == 2
    assert "page size" in capsys.readouterr().err.lower()
    assert main([*base, "--max-pages", "0"]) == 2
    assert "max pages" in capsys.readouterr().err.lower()
    assert main([*base, "--information-cutoff", "2026-08-12T17:00:00Z"]) == 2
    assert "cutoff" in capsys.readouterr().err.lower()

    outside = [*base]
    outside[outside.index("--output") + 1] = str(tmp_path / "outside")
    assert main(outside) == 2
    assert "var/corpus-intake" in capsys.readouterr().err


def test_collect_corpus_writes_verified_public_only_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "polymarket" / "markets_keyset_page_1.json"
    observed = {}

    async def acquire(client, request, on_raw_page):
        observed["request"] = request
        page = parse_page(
            body=fixture.read_bytes(),
            request_url="https://gamma-api.polymarket.com/markets/keyset?limit=100",
            requested_cursor=None,
            page_ordinal=1,
            retrieved_at=request.retrieved_at,
            information_cutoff=request.information_cutoff,
            status_code=200,
            headers={"content-type": "application/json"},
        )
        on_raw_page(page.raw)
        return AcquisitionResult(
            candidates=page.candidates,
            diagnostics=AcquisitionDiagnostics(
                page_count=1,
                received_market_count=2,
                exact_duplicate_count=0,
                canonical_duplicate_count=0,
                truncated_at_candidate_limit=False,
                truncated_at_page_limit=False,
            ),
        )

    monkeypatch.setattr(cli, "acquire_polymarket", acquire)
    output = tmp_path / "var" / "corpus-intake" / "run"

    assert (
        main(
            [
                "collect",
                "corpus",
                "--source",
                "polymarket",
                "--output",
                str(output),
                "--retrieved-at",
                "2026-08-12T18:00:00+02:00",
                "--information-cutoff",
                "2026-08-12T15:00:00Z",
                "--max-candidates",
                "500",
                "--market-state",
                "closed",
            ]
        )
        == 0
    )

    assert observed["request"].retrieved_at == NOW.replace(hour=16)
    assert observed["request"].market_state == "closed"
    assert (output / "manifest.json").exists()
    output_text = capsys.readouterr().out
    assert "2 review candidates" in output_text
    assert "Bitcoin" not in output_text


def test_collect_corpus_source_failure_is_exit_one_and_has_no_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    async def fail(client, request, on_raw_page):
        raise CorpusIntakeError("public source returned HTTP 503")

    monkeypatch.setattr(cli, "acquire_polymarket", fail)
    output = tmp_path / "var" / "corpus-intake" / "failed"
    status = main(
        [
            "collect",
            "corpus",
            "--source",
            "polymarket",
            "--output",
            str(output),
            "--retrieved-at",
            "2026-08-12T16:00:00Z",
            "--information-cutoff",
            "2026-08-12T15:00:00Z",
            "--max-candidates",
            "500",
        ]
    )

    assert status == 1
    assert "HTTP 503" in capsys.readouterr().err
    assert not (output / "manifest.json").exists()


def test_collect_source_use_writes_unresolved_hash_only_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    async def capture(client, target, *, retrieved_at, max_response_bytes):
        del client
        assert max_response_bytes == 2_097_152
        index = POLYMARKET_EVIDENCE_TARGETS.index(target) + 1
        return SourceEvidence(
            schema_version=1,
            source="polymarket",
            url=target.url,
            retrieved_at=retrieved_at,
            status_code=200,
            content_type="text/html",
            body_byte_count=index,
            body_sha256=str(index) * 64,
            etag=None,
            last_modified=None,
            locator=target.locator,
            excerpt=target.excerpt,
            excerpt_sha256=canonical_sha256(target.excerpt),
            full_body_retained=False,
        )

    monkeypatch.setattr(cli, "capture_evidence", capture)
    output = tmp_path / "var/source-use/run"

    exit_code = main(
        [
            "collect",
            "source-use",
            "--source",
            "polymarket",
            "--output",
            str(output),
            "--retrieved-at",
            "2026-08-12T16:00:00Z",
            "--max-response-bytes",
            "2097152",
        ]
    )
    verified = verify_source_use_run(output)

    assert exit_code == 0
    assert verified.evidence_count == 2
    assert verified.assessment.status == "requires_external_confirmation"
    assert "external confirmation" in capsys.readouterr().out.lower()


def test_collect_review_queue_reports_valid_blocked_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parent / "fixtures/polymarket/markets_keyset_page_1.json"

    async def acquire(client, request, on_raw_page):
        del client
        page = parse_page(
            body=fixture.read_bytes(),
            request_url="https://gamma-api.polymarket.com/markets/keyset?limit=2",
            requested_cursor=None,
            page_ordinal=1,
            retrieved_at=request.retrieved_at,
            information_cutoff=request.information_cutoff,
            status_code=200,
            headers={"content-type": "application/json"},
        )
        on_raw_page(page.raw)
        return AcquisitionResult(
            candidates=page.candidates,
            diagnostics=AcquisitionDiagnostics(
                page_count=1,
                received_market_count=2,
                exact_duplicate_count=0,
                canonical_duplicate_count=0,
                truncated_at_candidate_limit=True,
                truncated_at_page_limit=False,
            ),
        )

    async def capture(client, target, *, retrieved_at, max_response_bytes):
        del client, max_response_bytes
        index = POLYMARKET_EVIDENCE_TARGETS.index(target) + 1
        return SourceEvidence(
            schema_version=1,
            source="polymarket",
            url=target.url,
            retrieved_at=retrieved_at,
            status_code=200,
            content_type="text/html",
            body_byte_count=index,
            body_sha256=str(index) * 64,
            etag=None,
            last_modified=None,
            locator=target.locator,
            excerpt=target.excerpt,
            excerpt_sha256=canonical_sha256(target.excerpt),
            full_body_retained=False,
        )

    monkeypatch.setattr(cli, "acquire_polymarket", acquire)
    monkeypatch.setattr(cli, "capture_evidence", capture)
    intake = tmp_path / "var/corpus-intake/run"
    source_use = tmp_path / "var/source-use/run"
    queue = tmp_path / "var/review-queue/run"
    assert (
        main(
            [
                "collect",
                "corpus",
                "--source",
                "polymarket",
                "--output",
                str(intake),
                "--retrieved-at",
                "2026-08-12T16:00:00Z",
                "--information-cutoff",
                "2026-08-12T15:00:00Z",
                "--max-candidates",
                "2",
                "--page-size",
                "2",
                "--max-pages",
                "1",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "collect",
                "source-use",
                "--source",
                "polymarket",
                "--output",
                str(source_use),
                "--retrieved-at",
                "2026-08-12T16:00:00Z",
            ]
        )
        == 0
    )
    capsys.readouterr()

    exit_code = main(
        [
            "collect",
            "review-queue",
            "--intake",
            str(intake),
            "--source-use",
            str(source_use),
            "--output",
            str(queue),
            "--as-of",
            "2026-08-12T16:00:00Z",
            "--ontology-version",
            "candidate-triage-v1",
        ]
    )
    verified = verify_review_queue_run(queue)

    assert exit_code == 0
    assert verified.allowed is False
    assert verified.blocked_item_count == 2
    assert verified.reviewer_packet_count == 0
    assert "external_confirmation_required" in capsys.readouterr().out
    assert not list(queue.glob("reviewer-*"))


def test_source_use_cli_requires_explicit_timestamp_and_quarantine_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_time = main(
        [
            "collect",
            "source-use",
            "--source",
            "polymarket",
            "--output",
            str(tmp_path / "var/source-use/run"),
        ]
    )
    outside = main(
        [
            "collect",
            "source-use",
            "--source",
            "polymarket",
            "--output",
            str(tmp_path / "outside"),
            "--retrieved-at",
            "2026-08-12T16:00:00Z",
        ]
    )

    assert missing_time == 2
    assert outside == 2
    assert "var/source-use" in capsys.readouterr().err


def test_public_collection_cli_validation_errors_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "all",
                "--start",
                "2026-08-01T12:00:00Z",
                "--end",
                "2026-08-12T12:00:00Z",
                "--db",
                str(tmp_path / "too-much.duckdb"),
            ]
        )
        == 2
    )
    assert "seven days" in capsys.readouterr().err.lower()

    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "all",
                "--start",
                "2026-08-12T12:00:01Z",
                "--end",
                "2026-08-12T12:00:00Z",
                "--db",
                str(tmp_path / "reversed.duckdb"),
            ]
        )
        == 2
    )
    assert "must not follow" in capsys.readouterr().err.lower()

    assert (
        main(
            [
                "collect",
                "books",
                "--venue",
                "all",
                "--assets",
                "DOGE",
                "--once",
                "--db",
                str(tmp_path / "bad-asset.duckdb"),
            ]
        )
        == 2
    )
    assert "traceback" not in capsys.readouterr().err.lower()

    for field, value in (("--interval-seconds", "nan"), ("--duration-seconds", "inf")):
        args = [
            "collect",
            "books",
            "--venue",
            "all",
            "--db",
            str(tmp_path / f"nonfinite-{field[2:]}.duckdb"),
        ]
        if field == "--interval-seconds":
            args.extend(("--once", field, value))
        else:
            args.extend((field, value))
        assert main(args) == 2
        assert "finite positive" in capsys.readouterr().err.lower()
    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "unknown",
                "--db",
                str(tmp_path / "public.duckdb"),
            ]
        )
        == 2
    )
    assert "traceback" not in capsys.readouterr().err.lower()


def test_trial_funding_parser_requires_exactly_one_boundary_mode_without_scope_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parsed = cli.build_parser().parse_args(
        [
            "trial",
            "funding",
            "--current",
            "--db",
            "var/trial.duckdb",
            "--format",
            "json",
        ]
    )
    assert parsed.command == "trial"
    assert parsed.trial_command == "funding"
    assert parsed.current is True
    assert not hasattr(parsed, "venue")
    assert not hasattr(parsed, "assets")

    assert main(["trial", "funding", "--db", str(tmp_path / "neither.duckdb")]) == 2
    assert "--cycle-end" in capsys.readouterr().err
    assert (
        main(
            [
                "trial",
                "funding",
                "--db",
                str(tmp_path / "both.duckdb"),
                "--current",
                "--cycle-end",
                TRIAL_CYCLE_END.isoformat(),
            ]
        )
        == 2
    )
    assert "not allowed with argument" in capsys.readouterr().err


def test_trial_books_parser_requires_exactly_one_mode_without_scope_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parsed = cli.build_parser().parse_args(["trial", "books", "--once", "--db", "var/trial.duckdb"])

    assert parsed.command == "trial"
    assert parsed.trial_command == "books"
    assert parsed.once is True
    assert parsed.interval_seconds == 5.0
    assert not hasattr(parsed, "venue")
    assert not hasattr(parsed, "assets")

    assert main(["trial", "books", "--db", str(tmp_path / "neither.duckdb")]) == 2
    assert "--once" in capsys.readouterr().err
    assert (
        main(
            [
                "trial",
                "books",
                "--db",
                str(tmp_path / "both.duckdb"),
                "--once",
                "--duration-seconds",
                "60",
            ]
        )
        == 2
    )
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("option", "mode", "value"),
    [
        ("--duration-seconds", (), "0"),
        ("--duration-seconds", (), "-1"),
        ("--duration-seconds", (), "nan"),
        ("--duration-seconds", (), "inf"),
        ("--interval-seconds", ("--once",), "0"),
        ("--interval-seconds", ("--once",), "-1"),
        ("--interval-seconds", ("--once",), "nan"),
        ("--interval-seconds", ("--once",), "inf"),
    ],
)
def test_trial_books_rejects_non_positive_or_non_finite_timing_before_clients(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    mode: tuple[str, ...],
    value: str,
) -> None:
    @asynccontextmanager
    async def reject_session() -> Iterator[tuple[object, ...]]:
        raise AssertionError("invalid timing opened public clients")
        yield ()

    monkeypatch.setattr(cli, "_lighter_dydx_adapter_session", reject_session)

    assert (
        main(
            [
                "trial",
                "books",
                "--db",
                str(tmp_path / "invalid.duckdb"),
                *mode,
                option,
                value,
            ]
        )
        == 2
    )
    assert "finite positive" in capsys.readouterr().err


def test_trial_books_keeps_exact_pair_clients_alive_for_whole_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    adapters = (object(), object())

    @asynccontextmanager
    async def session() -> Iterator[tuple[object, object]]:
        events.append("clients-open")
        yield adapters
        events.append("clients-close")

    async def run(received: object, database: Path, **kwargs: object) -> TrialBookRunSummary:
        assert received is adapters
        assert database == tmp_path / "trial.duckdb"
        assert kwargs["duration_seconds"] is None
        assert kwargs["interval_seconds"] == 5.0
        assert kwargs["store_factory"] is cli.DuckDBStore
        events.append("run")
        return TrialBookRunSummary(1, 1, 0, 0, 0)

    monkeypatch.setattr(cli, "_lighter_dydx_adapter_session", session)
    monkeypatch.setattr(cli, "run_trial_book_session", run, raising=False)

    assert (
        main(
            [
                "trial",
                "books",
                "--once",
                "--db",
                str(tmp_path / "trial.duckdb"),
            ]
        )
        == 0
    )
    assert events == ["clients-open", "run", "clients-close"]
    assert capsys.readouterr().out == (
        "trial books: attempted_cycles=1 persisted_cycles=1 failed_cycles=0 "
        "skewed_cycles=0 lease_skipped_cycles=0; "
        "Research only — no trading authority.\n"
    )


@pytest.mark.parametrize(
    ("summary", "expected_exit"),
    [
        (TrialBookRunSummary(1, 0, 0, 0, 1), 1),
        (TrialBookRunSummary(2, 2, 1, 1, 0), 0),
    ],
)
def test_trial_books_exit_and_summary_follow_durable_diagnostic_cycles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    summary: TrialBookRunSummary,
    expected_exit: int,
) -> None:
    @asynccontextmanager
    async def session() -> Iterator[tuple[object, object]]:
        yield object(), object()

    async def run(*_args: object, **_kwargs: object) -> TrialBookRunSummary:
        return summary

    monkeypatch.setattr(cli, "_lighter_dydx_adapter_session", session)
    monkeypatch.setattr(cli, "run_trial_book_session", run, raising=False)

    assert (
        main(
            [
                "trial",
                "books",
                "--once",
                "--db",
                str(tmp_path / "trial.duckdb"),
            ]
        )
        == expected_exit
    )
    assert capsys.readouterr().out == (
        f"trial books: attempted_cycles={summary.attempted_cycles} "
        f"persisted_cycles={summary.persisted_cycles} failed_cycles={summary.failed_cycles} "
        f"skewed_cycles={summary.skewed_cycles} "
        f"lease_skipped_cycles={summary.lease_skipped_cycles}; "
        "Research only — no trading authority.\n"
    )


def test_trial_funding_current_uses_one_clock_read_for_boundary_and_late_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def now() -> datetime:
        nonlocal calls
        calls += 1
        return TRIAL_CYCLE_END + timedelta(minutes=5, microseconds=1)

    monkeypatch.setattr(cli, "_utc_now", now)
    assert (
        main(
            [
                "trial",
                "funding",
                "--current",
                "--db",
                str(tmp_path / "current.duckdb"),
            ]
        )
        == 0
    )
    assert calls == 1


def test_trial_funding_early_input_returns_two_before_lease_store_or_client(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "early-trial.duckdb"

    def reject(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("early input touched an external resource")

    monkeypatch.setattr(cli, "database_writer_lease", reject, raising=False)
    monkeypatch.setattr(cli, "DuckDBStore", reject)
    monkeypatch.setattr(cli, "make_public_http_client", reject)
    monkeypatch.setattr(cli, "_utc_now", lambda: TRIAL_CYCLE_END - timedelta(microseconds=1))

    assert (
        main(
            [
                "trial",
                "funding",
                "--cycle-end",
                TRIAL_CYCLE_END.isoformat(),
                "--db",
                str(database),
            ]
        )
        == 2
    )
    assert "precedes cycle end" in capsys.readouterr().err
    assert not database.exists()


def test_trial_funding_late_mode_uses_no_client_and_persists_diagnostic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "late-trial.duckdb"

    def reject_client(**_kwargs: object) -> None:
        raise AssertionError("late mode created a public client")

    monkeypatch.setattr(cli, "make_public_http_client", reject_client)
    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: TRIAL_CYCLE_END + timedelta(minutes=5, microseconds=1),
    )

    assert (
        main(
            [
                "trial",
                "funding",
                "--current",
                "--db",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "late"
    store = DuckDBStore(database, read_only=True)
    cycles = store.lighter_dydx_funding_cycles_between(
        TRIAL_CYCLE_END,
        TRIAL_CYCLE_END,
        TRIAL_CYCLE_END + timedelta(minutes=5, microseconds=1),
    )
    store.close()
    assert len(cycles) == 1
    assert cycles[0].status is TrialFundingCycleStatus.LATE


def test_trial_funding_acquires_bounded_lease_before_clients_and_store(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.trial.test_funding import complete_prepared_cycle

    database = tmp_path / "complete-trial.duckdb"
    prepared = complete_prepared_cycle()
    events: list[str] = []
    lease_timeouts: list[float] = []

    @contextmanager
    def lease(path: Path, *, timeout_seconds: float) -> Iterator[None]:
        assert path == database
        lease_timeouts.append(timeout_seconds)
        events.append("lease")
        yield

    @asynccontextmanager
    async def session() -> Iterator[tuple[object, ...]]:
        events.append("clients")
        assert not database.exists()
        yield (object(), object())

    class Collector:
        def __init__(self, *, clock: object) -> None:
            del clock

        async def prepare_once(self, adapters: object, assets: object, cycle_end: object) -> object:
            del adapters, assets, cycle_end
            events.append("prepared")
            assert not database.exists()
            return prepared

    real_store = cli.DuckDBStore

    def open_store(path: Path) -> DuckDBStore:
        events.append("store")
        return real_store(path)

    times = iter(
        [
            TRIAL_CYCLE_END + timedelta(seconds=10),
            TRIAL_CYCLE_END + timedelta(seconds=12),
            TRIAL_CYCLE_END + timedelta(seconds=13),
        ]
    )
    monkeypatch.setattr(cli, "database_writer_lease", lease, raising=False)
    monkeypatch.setattr(cli, "_lighter_dydx_adapter_session", session, raising=False)
    monkeypatch.setattr(cli, "LighterDydxFundingCollector", Collector, raising=False)
    monkeypatch.setattr(cli, "DuckDBStore", open_store)
    monkeypatch.setattr(cli, "_utc_now", lambda: next(times))

    assert (
        main(
            [
                "trial",
                "funding",
                "--cycle-end",
                TRIAL_CYCLE_END.isoformat(),
                "--db",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "complete"
    assert lease_timeouts == [290.0]
    assert events == ["lease", "clients", "prepared", "store"]


def test_trial_funding_adapter_session_uses_independent_clients_and_closes_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[object] = []

    class Client:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class Adapter:
        def __init__(self, client: object, *args: object) -> None:
            del args
            self.client = client

    def client_factory() -> object:
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr(cli, "make_public_http_client", client_factory)
    monkeypatch.setattr(cli, "DydxPublicAdapter", Adapter)
    monkeypatch.setattr(cli, "LighterPublicAdapter", Adapter)

    async def exercise() -> tuple[object, object]:
        async with cli._lighter_dydx_adapter_session() as adapters:
            return adapters[0].client, adapters[1].client  # type: ignore[attr-defined]

    adapter_clients = asyncio.run(exercise())

    assert adapter_clients == tuple(clients)
    assert adapter_clients[0] is not adapter_clients[1]
    assert all(client.closed for client in clients)  # type: ignore[attr-defined]


def test_trial_funding_degraded_persisted_cycle_returns_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.trial.test_funding import FakeAdapter, _collect, _funding_batch

    database = tmp_path / "degraded-trial.duckdb"
    lighter = FakeAdapter(Venue.LIGHTER)
    lighter.funding_results[Asset.BTC] = _funding_batch(
        Venue.LIGHTER,
        Asset.BTC,
        TRIAL_CYCLE_END + timedelta(seconds=12),
        777,
        include_record=False,
    )
    prepared = _collect((FakeAdapter(Venue.DYDX), lighter))

    @asynccontextmanager
    async def session() -> Iterator[tuple[object, ...]]:
        yield (object(), object())

    class Collector:
        def __init__(self, *, clock: object) -> None:
            del clock

        async def prepare_once(self, adapters: object, assets: object, cycle_end: object) -> object:
            del adapters, assets, cycle_end
            return prepared

    times = iter(
        [
            TRIAL_CYCLE_END + timedelta(seconds=10),
            TRIAL_CYCLE_END + timedelta(seconds=20),
            TRIAL_CYCLE_END + timedelta(seconds=21),
        ]
    )
    monkeypatch.setattr(cli, "_lighter_dydx_adapter_session", session)
    monkeypatch.setattr(cli, "LighterDydxFundingCollector", Collector)
    monkeypatch.setattr(cli, "_utc_now", lambda: next(times))

    assert (
        main(
            [
                "trial",
                "funding",
                "--cycle-end",
                TRIAL_CYCLE_END.isoformat(),
                "--db",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "degraded"


def test_trial_funding_window_closing_during_lease_wait_records_late_without_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "lease-late.duckdb"
    entered = False

    @asynccontextmanager
    async def session() -> Iterator[tuple[object, ...]]:
        nonlocal entered
        entered = True
        yield ()

    times = iter(
        [
            TRIAL_CYCLE_END + timedelta(seconds=10),
            TRIAL_CYCLE_END + timedelta(minutes=5, microseconds=1),
        ]
    )
    monkeypatch.setattr(cli, "_lighter_dydx_adapter_session", session, raising=False)
    monkeypatch.setattr(cli, "_utc_now", lambda: next(times))

    assert (
        main(
            [
                "trial",
                "funding",
                "--cycle-end",
                TRIAL_CYCLE_END.isoformat(),
                "--db",
                str(database),
            ]
        )
        == 0
    )
    assert entered is False
    store = DuckDBStore(database, read_only=True)
    cycle = store.latest_lighter_dydx_funding_cycle_as_of(
        TRIAL_CYCLE_END + timedelta(minutes=5, microseconds=1)
    )
    store.close()
    assert cycle is not None
    assert cycle.status is TrialFundingCycleStatus.LATE


def test_trial_funding_persists_honest_late_response_started_on_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.trial.test_funding import FakeAdapter, SequenceClock, _collect, _funding_batch

    database = tmp_path / "response-late.duckdb"
    late = TRIAL_CYCLE_END + timedelta(minutes=5, microseconds=1)
    lighter = FakeAdapter(Venue.LIGHTER)
    lighter.funding_results[Asset.SOL] = _funding_batch(Venue.LIGHTER, Asset.SOL, late, 991)
    prepared = _collect(
        (FakeAdapter(Venue.DYDX), lighter),
        clock=SequenceClock(TRIAL_CYCLE_END + timedelta(seconds=10), late),
    )

    @asynccontextmanager
    async def session() -> Iterator[tuple[object, ...]]:
        yield (object(), object())

    class Collector:
        def __init__(self, *, clock: object) -> None:
            del clock

        async def prepare_once(self, adapters: object, assets: object, cycle_end: object) -> object:
            del adapters, assets, cycle_end
            return prepared

    times = iter(
        [
            TRIAL_CYCLE_END + timedelta(seconds=10),
            TRIAL_CYCLE_END + timedelta(seconds=11),
        ]
    )
    monkeypatch.setattr(cli, "_lighter_dydx_adapter_session", session)
    monkeypatch.setattr(cli, "LighterDydxFundingCollector", Collector)
    monkeypatch.setattr(cli, "_utc_now", lambda: next(times))

    assert (
        main(
            [
                "trial",
                "funding",
                "--cycle-end",
                TRIAL_CYCLE_END.isoformat(),
                "--db",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "late"
    store = DuckDBStore(database, read_only=True)
    cycle = store.latest_lighter_dydx_funding_cycle_as_of(late)
    store.close()
    assert cycle == prepared.cycle


def test_trial_funding_lease_failure_preventing_persistence_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "busy.duckdb"
    entered = False

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise WriterLeaseUnavailable("busy")

    @asynccontextmanager
    async def session() -> Iterator[tuple[object, ...]]:
        nonlocal entered
        entered = True
        yield ()

    monkeypatch.setattr(cli, "database_writer_lease", unavailable, raising=False)
    monkeypatch.setattr(cli, "_lighter_dydx_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: TRIAL_CYCLE_END + timedelta(seconds=10))

    assert (
        main(
            [
                "trial",
                "funding",
                "--cycle-end",
                TRIAL_CYCLE_END.isoformat(),
                "--db",
                str(database),
            ]
        )
        == 1
    )
    assert "collection failed" in capsys.readouterr().err
    assert entered is False
    assert not database.exists()


def test_trial_funding_malformed_timestamp_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "trial",
                "funding",
                "--cycle-end",
                "not-a-time",
                "--db",
                str(tmp_path / "malformed.duckdb"),
            ]
        )
        == 2
    )
    assert "invalid timestamp" in capsys.readouterr().err


def test_funding_cycle_cli_requires_db_and_exactly_one_boundary_mode_without_venue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["collect", "funding-cycle", "--cycle-end", FUNDING_CYCLE_END.isoformat()]) == 2
    assert "--db" in capsys.readouterr().err
    assert main(["collect", "funding-cycle", "--db", str(tmp_path / "missing.duckdb")]) == 2
    missing_mode = capsys.readouterr().err
    assert "--cycle-end" in missing_mode
    assert "--current" in missing_mode
    assert (
        main(
            [
                "collect",
                "funding-cycle",
                "--db",
                str(tmp_path / "both.duckdb"),
                "--cycle-end",
                FUNDING_CYCLE_END.isoformat(),
                "--current",
            ]
        )
        == 2
    )
    assert "not allowed with argument" in capsys.readouterr().err
    explicit = cli.build_parser().parse_args(
        [
            "collect",
            "funding-cycle",
            "--db",
            str(tmp_path / "explicit.duckdb"),
            "--cycle-end",
            FUNDING_CYCLE_END.isoformat(),
        ]
    )
    current = cli.build_parser().parse_args(
        [
            "collect",
            "funding-cycle",
            "--db",
            str(tmp_path / "current.duckdb"),
            "--current",
        ]
    )
    assert explicit.cycle_end == FUNDING_CYCLE_END.isoformat()
    assert explicit.current is False
    assert current.cycle_end is None
    assert current.current is True
    assert (
        main(
            [
                "collect",
                "funding-cycle",
                "--db",
                str(tmp_path / "venue.duckdb"),
                "--cycle-end",
                FUNDING_CYCLE_END.isoformat(),
                "--venue",
                "all",
            ]
        )
        == 2
    )
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("cycle_end", ["invalid", "2026-08-13T17:00:01Z"])
def test_funding_cycle_cli_rejects_invalid_or_non_hour_boundary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    cycle_end: str,
) -> None:
    monkeypatch.setattr(cli, "_utc_now", lambda: FUNDING_CYCLE_END)

    assert (
        main(
            [
                "collect",
                "funding-cycle",
                "--db",
                str(tmp_path / "invalid.duckdb"),
                "--cycle-end",
                cycle_end,
            ]
        )
        == 2
    )
    assert "polytrading: error:" in capsys.readouterr().err


def test_funding_cycle_cli_rejects_early_clock_before_db_or_session(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "early.duckdb"
    entered = False

    @asynccontextmanager
    async def session(store: object, venues: object):
        nonlocal entered
        entered = True
        yield ()

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: FUNDING_CYCLE_END - timedelta(microseconds=1))

    assert (
        main(
            [
                "collect",
                "funding-cycle",
                "--db",
                str(database),
                "--cycle-end",
                FUNDING_CYCLE_END.isoformat(),
            ]
        )
        == 2
    )
    assert "precedes cycle end" in capsys.readouterr().err
    assert entered is False
    assert not database.exists()


def test_funding_cycle_cli_records_late_attempt_without_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "late.duckdb"
    entered = False

    @asynccontextmanager
    async def session(store: object, venues: object):
        nonlocal entered
        entered = True
        yield ()

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(
        cli,
        "_utc_now",
        lambda: FUNDING_CYCLE_END + timedelta(minutes=5, microseconds=1),
    )

    assert (
        main(
            [
                "collect",
                "funding-cycle",
                "--db",
                str(database),
                "--current",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert output.startswith("Point-in-time funding cycle v1")
    assert "late_not_collected" in output
    assert entered is False
    store = DuckDBStore(database, read_only=True)
    cycles = store.funding_collection_cycles_between(FUNDING_CYCLE_END, FUNDING_CYCLE_END)
    previous_cycles = store.funding_collection_cycles_between(
        FUNDING_CYCLE_END - timedelta(hours=1),
        FUNDING_CYCLE_END - timedelta(hours=1),
    )
    store.close()
    assert len(cycles) == 1
    assert cycles[0].status == "late"
    assert len(cycles[0].items) == 6
    assert previous_cycles == ()


def test_funding_cycle_cli_collects_complete_exact_boundary_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "complete.duckdb"
    assets = frozenset({Asset.BTC, Asset.ETH, Asset.SOL})
    instrument_observed_at = FUNDING_CYCLE_END + timedelta(minutes=1)
    funding_observed_at = FUNDING_CYCLE_END + timedelta(minutes=2)
    bybit = FakeFundingAdapter(
        Venue.BYBIT,
        instrument_batch(
            Venue.BYBIT,
            assets,
            observed_at=instrument_observed_at,
            event_int=4100,
        ),
        {
            asset: funding_batch(
                Venue.BYBIT,
                asset,
                effective_at=FUNDING_CYCLE_END,
                observed_at=funding_observed_at,
                event_int=4200 + index,
            )
            for index, asset in enumerate(sorted(assets, key=lambda item: item.value))
        },
    )
    hyperliquid = FakeFundingAdapter(
        Venue.HYPERLIQUID,
        instrument_batch(
            Venue.HYPERLIQUID,
            assets,
            observed_at=instrument_observed_at,
            event_int=4300,
        ),
        {
            asset: funding_batch(
                Venue.HYPERLIQUID,
                asset,
                effective_at=FUNDING_CYCLE_END,
                observed_at=funding_observed_at,
                event_int=4400 + index,
            )
            for index, asset in enumerate(sorted(assets, key=lambda item: item.value))
        },
    )
    for adapter in (bybit, hyperliquid):
        adapter.fetch_positions = lambda: pytest.fail("private positions were accessed")  # type: ignore[attr-defined]
        adapter.place_order = lambda: pytest.fail("an order method was accessed")  # type: ignore[attr-defined]

    seed = DuckDBStore(database)
    for asset in assets:
        seed.append_instrument(
            cycle_instrument_spec(
                Venue.BYBIT,
                asset,
                observed_at=FUNDING_CYCLE_END - timedelta(hours=1),
                source_hash="a" * 64,
            )
        )
    seed.close()

    @asynccontextmanager
    async def session(store: object, venues: object):
        assert tuple(venues) == (Venue.BYBIT, Venue.HYPERLIQUID)
        yield (hyperliquid, bybit)

    times = iter(
        [
            FUNDING_CYCLE_END + timedelta(seconds=30),
            FUNDING_CYCLE_END + timedelta(seconds=30),
            FUNDING_CYCLE_END + timedelta(hours=1, seconds=1),
        ]
    )
    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: next(times))

    assert (
        main(
            [
                "collect",
                "funding-cycle",
                "--db",
                str(database),
                "--current",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "complete"
    assert len(payload["items"]) == 6
    assert all(call[1] == call[2] == FUNDING_CYCLE_END for call in bybit.funding_calls)
    assert all(call[1] == call[2] == FUNDING_CYCLE_END for call in hyperliquid.funding_calls)
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (8,)
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (9,)
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (6,)
        assert connection.execute("SELECT count(*) FROM funding_collection_cycles").fetchone() == (
            1,
        )


def test_funding_cycle_cli_classifies_persistence_conflict_as_collection_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RejectingCollector:
        def __init__(self, store: object, *, clock: object) -> None:
            del store, clock

        async def collect_once(self, adapters: object, assets: object, cycle_end: object) -> object:
            del adapters, assets, cycle_end
            raise ConflictingRecordError("conflicting funding cycle persistence")

    @asynccontextmanager
    async def session(store: object, venues: object):
        del store, venues
        yield ()

    times = iter(
        [
            FUNDING_CYCLE_END + timedelta(seconds=30),
            FUNDING_CYCLE_END + timedelta(seconds=30),
        ]
    )
    monkeypatch.setattr(cli, "PointInTimeFundingCollector", RejectingCollector)
    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: next(times))

    assert (
        main(
            [
                "collect",
                "funding-cycle",
                "--db",
                str(tmp_path / "conflict.duckdb"),
                "--cycle-end",
                FUNDING_CYCLE_END.isoformat(),
            ]
        )
        == 1
    )
    message = capsys.readouterr().err
    assert "collection failed" in message
    assert "conflicting funding cycle persistence" in message


def _seed_health_database(
    database: Path, status: FundingCycleStatus = FundingCycleStatus.COMPLETE
) -> None:
    store = DuckDBStore(database)
    store.append_funding_collection_cycle(
        funding_cycle(
            LATEST_BOUNDARY, status, cycle_int=900 + list(FundingCycleStatus).index(status)
        )
    )
    store.close()


def _database_table_counts(database: Path) -> tuple[tuple[str, int], ...]:
    with duckdb.connect(str(database), read_only=True) as connection:
        tables = tuple(
            row[0]
            for row in connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'main'
                ORDER BY table_name
                """
            ).fetchall()
        )
        return tuple(
            (table, connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
            for table in tables
        )


def test_funding_health_cli_is_deterministic_read_only_and_offline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "healthy.duckdb"
    _seed_health_database(database)
    before_bytes = database.read_bytes()
    before_counts = _database_table_counts(database)

    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("health audit touched the network")

    monkeypatch.setattr(cli, "make_public_http_client", reject_network)
    monkeypatch.setattr(cli, "public_adapter_session", reject_network)
    arguments = [
        "funding",
        "health",
        "--db",
        str(database),
        "--hours",
        "1",
        "--as-of",
        HEALTH_AS_OF.isoformat(),
        "--format",
        "json",
    ]

    assert main(arguments) == 0
    first = capsys.readouterr().out
    assert main(arguments) == 0
    second = capsys.readouterr().out

    assert first == second
    payload = json.loads(first)
    assert payload["status"] == "healthy"
    assert payload["boundaries"][0]["status"] == "complete"
    assert database.read_bytes() == before_bytes
    assert _database_table_counts(database) == before_counts


@pytest.mark.parametrize(
    ("cycle_status", "health_status"),
    [
        (FundingCycleStatus.DEGRADED, "degraded"),
        (FundingCycleStatus.LATE, "critical"),
    ],
)
def test_funding_health_cli_returns_one_for_actionable_health(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cycle_status: FundingCycleStatus,
    health_status: str,
) -> None:
    database = tmp_path / f"{cycle_status.value}.duckdb"
    _seed_health_database(database, cycle_status)

    assert (
        main(
            [
                "funding",
                "health",
                "--db",
                str(database),
                "--hours",
                "1",
                "--as-of",
                HEALTH_AS_OF.isoformat(),
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert output.splitlines()[0].endswith(f" | {health_status}")


def test_funding_health_cli_captures_omitted_as_of_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "clock.duckdb"
    _seed_health_database(database)
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return HEALTH_AS_OF

    monkeypatch.setattr(cli, "_utc_now", clock)

    assert (
        main(
            [
                "funding",
                "health",
                "--db",
                str(database),
                "--hours",
                "1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert calls == 1
    assert json.loads(capsys.readouterr().out)["as_of"] == "2026-08-14T17:06:00Z"


def test_funding_health_cli_rejects_invalid_hours_before_opening_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "must-not-open.duckdb"

    assert (
        main(
            [
                "funding",
                "health",
                "--db",
                str(missing),
                "--hours",
                "0",
                "--as-of",
                HEALTH_AS_OF.isoformat(),
            ]
        )
        == 2
    )
    message = capsys.readouterr().err
    assert "between 1 and 2160" in message
    assert "database" not in message
    assert not missing.exists()


def test_funding_health_cli_sanitizes_missing_and_old_schema_databases(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.duckdb"
    old = tmp_path / "old.duckdb"
    duckdb.connect(str(old)).close()

    for database in (missing, old):
        assert (
            main(
                [
                    "funding",
                    "health",
                    "--db",
                    str(database),
                    "--as-of",
                    HEALTH_AS_OF.isoformat(),
                ]
            )
            == 2
        )
        message = capsys.readouterr().err
        assert message.startswith("polytrading: error:")
        assert "traceback" not in message.casefold()


class _ReplayOrderStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @contextmanager
    def transaction(self) -> Iterator[_ReplayOrderStore]:
        self.calls.append("begin")
        yield self
        self.calls.append("commit")

    def append_raw(self, record: object) -> bool:
        self.calls.append("raw")
        return True

    def append_instrument(self, record: object) -> bool:
        self.calls.append("instrument")
        return True

    def append_funding(self, record: object) -> bool:
        self.calls.append("funding")
        return True


def test_replay_is_batch_raw_first_and_exact_retries_are_idempotent(tmp_path: Path) -> None:
    one_batch = tmp_path / "one.jsonl"
    one_batch.write_text(FIXTURE.read_text().splitlines()[0] + "\n")
    spy = _ReplayOrderStore()

    assert replay_file(one_batch, spy) == 1  # type: ignore[arg-type]
    assert spy.calls == ["begin", "raw", "instrument", "instrument", "instrument", "commit"]

    database = tmp_path / "retry.duckdb"
    store = DuckDBStore(database)
    assert replay_file(FIXTURE, store) == 8
    assert replay_file(FIXTURE, store) == 8
    store.close()
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM raw_envelopes").fetchone() == (8,)
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (6,)
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (6,)


class _SequenceTransport(httpx.AsyncBaseTransport):
    def __init__(self, statuses: list[int], body: bytes = b"{}") -> None:
        self.statuses = iter(statuses)
        self.body = body
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(next(self.statuses), content=self.body, request=request)


def test_http_retry_policy_is_bounded_and_never_retries_parse_errors() -> None:
    async def exercise() -> None:
        delays: list[float] = []
        sequence = _SequenceTransport([429, 503, 200])
        transport = RetryingTransport(
            sequence, max_attempts=3, sleep=lambda delay: _record_delay(delays, delay)
        )
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://example.test/public")
        assert response.status_code == 200
        assert sequence.calls == 3
        assert delays == [0.25, 0.5]

        malformed = _SequenceTransport([200, 200], body=b"not-json")
        async with httpx.AsyncClient(
            transport=RetryingTransport(malformed, max_attempts=3)
        ) as client:
            response = await client.get("https://example.test/public")
            with pytest.raises(json.JSONDecodeError):
                response.json()
        assert malformed.calls == 1

        non_transient = _SequenceTransport([404, 200])
        async with httpx.AsyncClient(
            transport=RetryingTransport(non_transient, max_attempts=3)
        ) as client:
            response = await client.get("https://example.test/public")
        assert response.status_code == 404
        assert non_transient.calls == 1

        exhausted_delays: list[float] = []
        exhausted = _SequenceTransport([429, 500, 503, 200])
        async with httpx.AsyncClient(
            transport=RetryingTransport(
                exhausted,
                max_attempts=3,
                sleep=lambda delay: _record_delay(exhausted_delays, delay),
            )
        ) as client:
            response = await client.get("https://example.test/public")
        assert response.status_code == 503
        assert exhausted.calls == 3
        assert exhausted_delays == [0.25, 0.5]

    asyncio.run(exercise())


def test_public_http_client_has_explicit_identity_and_timeouts() -> None:
    client = cli.make_public_http_client(transport=_SequenceTransport([200]))

    assert client.headers["user-agent"] == "polytrading/0.1 public-market-research"
    assert client.headers["accept-encoding"] == "identity"
    assert client.timeout.connect == 10
    assert client.timeout.read == 30
    asyncio.run(client.aclose())


def test_public_adapter_session_routes_every_venue_to_its_concrete_adapter(
    tmp_path: Path,
) -> None:
    # Catches the old non-Bybit fallback silently routing dYdX requests to Hyperliquid.
    store = DuckDBStore(tmp_path / "routing.duckdb")

    async def exercise() -> tuple[tuple[str, Venue], ...]:
        async with cli.public_adapter_session(
            store, (Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX, Venue.LIGHTER)
        ) as adapters:
            return tuple((type(adapter).__name__, adapter.venue) for adapter in adapters)

    assert asyncio.run(exercise()) == (
        ("BybitPublicAdapter", Venue.BYBIT),
        ("HyperliquidPublicAdapter", Venue.HYPERLIQUID),
        ("DydxPublicAdapter", Venue.DYDX),
        ("LighterPublicAdapter", Venue.LIGHTER),
    )
    store.close()


@pytest.mark.parametrize("command", ["public", "books"])
def test_generic_collection_parsers_accept_dydx(command: str, tmp_path: Path) -> None:
    # Catches a venue implemented in code but unreachable through the public CLI contract.
    arguments = ["collect", command, "--venue", "dydx", "--db", str(tmp_path / "x.duckdb")]
    if command == "books":
        arguments.append("--once")

    parsed = cli.build_parser().parse_args(arguments)

    assert parsed.venue == "dydx"


@pytest.mark.parametrize("command", ["public", "books"])
def test_generic_collection_parsers_accept_lighter(command: str, tmp_path: Path) -> None:
    # Catches a public adapter that is implemented but unreachable through the CLI.
    arguments = ["collect", command, "--venue", "lighter", "--db", str(tmp_path / "x.duckdb")]
    if command == "books":
        arguments.append("--once")

    parsed = cli.build_parser().parse_args(arguments)

    assert parsed.venue == "lighter"


async def _record_delay(delays: list[float], delay: float) -> None:
    delays.append(delay)


class _AdvancingClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.value += delay


class _BookAdapter:
    def __init__(self, venue: Venue, starts: list[Venue], *, fail_first: bool = False) -> None:
        self.venue = venue
        self.starts = starts
        self.calls = 0
        self.fail_first = fail_first

    async def fetch_order_books(
        self, assets: frozenset[Asset], observed_at: datetime, cycle_id: UUID
    ) -> AdapterBatch:
        self.calls += 1
        self.starts.append(self.venue)
        await asyncio.sleep(0)
        if self.fail_first and self.calls == 1:
            raise TimeoutError("first cycle failed")
        payload_json = "{}"
        source_hash = sha256(payload_json.encode()).hexdigest()
        raw = RawEnvelope(
            schema_version=1,
            event_id=UUID(
                int=self.calls * 10
                + {
                    Venue.BYBIT: 1,
                    Venue.HYPERLIQUID: 2,
                    Venue.DYDX: 3,
                    Venue.LIGHTER: 4,
                }[self.venue]
            ),
            venue=self.venue,
            endpoint="/public/book",
            venue_timestamp=observed_at,
            observed_at=observed_at,
            received_monotonic_ns=self.calls,
            request_latency_ms=Decimal("1"),
            source_version="test-public-v1",
            payload_json=payload_json,
            source_hash=source_hash,
        )
        books = tuple(
            _book(self.venue, asset, cycle_id, observed_at, source_hash) for asset in assets
        )
        return AdapterBatch(raw=(raw,), normalized=books)


class _WarningBookAdapter(_BookAdapter):
    async def fetch_order_books(
        self, assets: frozenset[Asset], observed_at: datetime, cycle_id: UUID
    ) -> AdapterBatch:
        batch = await super().fetch_order_books(assets, observed_at, cycle_id)
        return AdapterBatch(
            raw=batch.raw,
            normalized=batch.normalized,
            warnings=(
                AdapterWarning(
                    code="DYDX_REST_BOOK_LOCAL_TIMESTAMP",
                    venue=Venue.DYDX,
                    endpoint="/v4/orderbooks/perpetualMarket/BTC-USD",
                    symbol="BTC-USD",
                    message=(
                        "dYdX REST book has no venue timestamp or sequence; "
                        "local receipt time was used"
                    ),
                ),
            ),
        )


def _book(
    venue: Venue, asset: Asset, cycle_id: UUID, observed_at: datetime, source_hash: str
) -> Level2BookSnapshot:
    base = {
        Asset.BTC: Decimal("65000"),
        Asset.ETH: Decimal("3500"),
        Asset.SOL: Decimal("150"),
    }[asset]
    return Level2BookSnapshot(
        schema_version=1,
        cycle_id=cycle_id,
        venue=venue,
        symbol=(
            f"{asset.value}USDT"
            if venue is Venue.BYBIT
            else f"{asset.value}-USD"
            if venue is Venue.DYDX
            else asset.value
        ),
        asset=asset,
        bids=(BookLevel(price=base - 1, quantity=Decimal("1"), order_count=1),),
        asks=(BookLevel(price=base + 1, quantity=Decimal("1"), order_count=1),),
        depth_limit=20,
        sequence="1" if venue is Venue.BYBIT else None,
        effective_at=observed_at,
        observed_at=observed_at,
        source_hash=source_hash,
    )


def test_book_loop_runs_both_venues_concurrently_and_continues_after_failure(
    tmp_path: Path,
) -> None:
    starts: list[Venue] = []
    bybit = _BookAdapter(Venue.BYBIT, starts)
    hyperliquid = _BookAdapter(Venue.HYPERLIQUID, starts, fail_first=True)
    times = iter([0.0, 0.0, 0.5, 0.5, 1.1])
    wall_times = iter(
        [
            NOW,
            NOW,
            NOW + timedelta(milliseconds=10),
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=1, milliseconds=10),
        ]
    )
    delays: list[float] = []
    store = DuckDBStore(tmp_path / "books.duckdb")

    async def exercise() -> None:
        await collect_book_cycles(
            (bybit, hyperliquid),
            frozenset({Asset.BTC, Asset.ETH, Asset.SOL}),
            store,
            duration_seconds=1,
            interval_seconds=0.5,
            monotonic=lambda: next(times),
            wall_clock=lambda: next(wall_times),
            sleep=lambda delay: _record_delay(delays, delay),
        )

    asyncio.run(exercise())
    assert starts == [Venue.BYBIT, Venue.HYPERLIQUID, Venue.BYBIT, Venue.HYPERLIQUID]
    assert bybit.calls == hyperliquid.calls == 2
    assert delays == [0.5]
    store.close()
    with duckdb.connect(str(tmp_path / "books.duckdb"), read_only=True) as connection:
        cycles = connection.execute(
            "SELECT cycle_id, status FROM book_collection_cycles ORDER BY request_completed_at"
        ).fetchall()
        assert [row[1] for row in cycles] == ["failed", "complete"]
        assert connection.execute(
            "SELECT count(*) FROM book_snapshots WHERE cycle_id = ?", [cycles[0][0]]
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (6,)


@pytest.mark.parametrize(
    ("failed", "interval_seconds", "max_failure_backoff_seconds"),
    [(False, 5, 30), (True, 60, 30)],
)
def test_book_loop_caps_normal_interval_and_failure_backoff_at_remaining_duration(
    tmp_path: Path,
    failed: bool,
    interval_seconds: float,
    max_failure_backoff_seconds: float,
) -> None:
    starts: list[Venue] = []
    adapter = _BookAdapter(Venue.HYPERLIQUID, starts, fail_first=failed)
    clock = _AdvancingClock()
    store = DuckDBStore(tmp_path / f"deadline-{failed}.duckdb")

    asyncio.run(
        collect_book_cycles(
            (adapter,),
            frozenset({Asset.BTC}),
            store,
            duration_seconds=1,
            interval_seconds=interval_seconds,
            monotonic=clock.monotonic,
            wall_clock=lambda: NOW,
            sleep=clock.sleep,
            max_failure_backoff_seconds=max_failure_backoff_seconds,
        )
    )

    assert clock.delays == [1]
    assert clock.value == 1
    assert adapter.calls == 1
    store.close()


class _PublicAdapter:
    def __init__(self, venue: Venue, calls: list[tuple[object, ...]]) -> None:
        self.venue = venue
        self.calls = calls

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "instruments", assets, observed_at))
        return AdapterBatch(raw=(), normalized=())

    async def fetch_market_snapshots(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "markets", assets, observed_at))
        return AdapterBatch(raw=(), normalized=())

    async def fetch_funding_history(
        self, asset: Asset, start: datetime, end: datetime, observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "funding", asset, start, end, observed_at))
        return AdapterBatch(raw=(), normalized=())


class _WarningPublicAdapter(_PublicAdapter):
    async def fetch_market_snapshots(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "markets", assets, observed_at))
        return AdapterBatch(
            raw=(),
            normalized=(),
            warnings=(
                AdapterWarning(
                    code="DYDX_MARK_PRICE_UNAVAILABLE",
                    venue=Venue.DYDX,
                    endpoint="/v4/perpetualMarkets",
                    symbol="BTC-USD",
                    message="dYdX public market evidence has no documented mark-price field",
                ),
            ),
        )


class _PersistingPublicAdapter(_PublicAdapter):
    def _raw(self, suffix: int) -> RawEnvelope:
        payload = f'{{"venue":"{self.venue.value}","suffix":{suffix}}}'
        return RawEnvelope(
            schema_version=1,
            event_id=UUID(
                int={
                    Venue.BYBIT: 1000,
                    Venue.HYPERLIQUID: 2000,
                    Venue.DYDX: 3000,
                    Venue.LIGHTER: 4000,
                }[self.venue]
                + suffix
            ),
            venue=self.venue,
            endpoint="/public/test",
            venue_timestamp=NOW,
            observed_at=NOW,
            received_monotonic_ns=suffix,
            request_latency_ms=Decimal("1"),
            source_version="test-public-v1",
            payload_json=payload,
            source_hash=sha256(payload.encode()).hexdigest(),
        )

    async def fetch_instruments(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "instruments", assets, observed_at))
        raw = self._raw(1)
        symbol = "BTCUSDT" if self.venue is Venue.BYBIT else "BTC"
        return AdapterBatch(
            raw=(raw,),
            normalized=(
                instrument_spec(
                    venue=self.venue,
                    symbol=symbol,
                    instrument_id=f"{self.venue.value}:{symbol}",
                    observed_at=NOW,
                    source_hash=raw.source_hash,
                ),
            ),
        )

    async def fetch_market_snapshots(
        self, assets: frozenset[Asset], observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "markets", assets, observed_at))
        raw = self._raw(2)
        symbol = "BTCUSDT" if self.venue is Venue.BYBIT else "BTC"
        return AdapterBatch(
            raw=(raw,),
            normalized=(
                MarketSnapshot(
                    schema_version=1,
                    venue=self.venue,
                    symbol=symbol,
                    asset=Asset.BTC,
                    bid=Decimal("99"),
                    ask=Decimal("101"),
                    mark=Decimal("100"),
                    index=Decimal("100"),
                    open_interest=Decimal("10"),
                    effective_at=NOW,
                    observed_at=NOW,
                    source_hash=raw.source_hash,
                ),
            ),
        )

    async def fetch_funding_history(
        self, asset: Asset, start: datetime, end: datetime, observed_at: datetime
    ) -> AdapterBatch:
        self.calls.append((self.venue, "funding", asset, start, end, observed_at))
        raw = self._raw(3)
        symbol = "BTCUSDT" if self.venue is Venue.BYBIT else "BTC"
        return AdapterBatch(
            raw=(raw,),
            normalized=(
                funding_observation(
                    venue=self.venue,
                    symbol=symbol,
                    effective_at=end,
                    observed_at=NOW,
                    source_hash=raw.source_hash,
                ),
            ),
        )


def test_collect_public_cli_uses_all_public_adapters_and_seven_day_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, ...]] = []
    adapters = (
        _PublicAdapter(Venue.BYBIT, calls),
        _PublicAdapter(Venue.HYPERLIQUID, calls),
        _PublicAdapter(Venue.DYDX, calls),
        _PublicAdapter(Venue.LIGHTER, calls),
    )

    @asynccontextmanager
    async def session(store: object, venues: object):
        assert tuple(venues) == (
            Venue.BYBIT,
            Venue.HYPERLIQUID,
            Venue.DYDX,
            Venue.LIGHTER,
        )
        yield adapters

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    monkeypatch.setattr(cli, "_has_bybit_history_basis", lambda store, asset, start: True)

    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "all",
                "--assets",
                "BTC,ETH,SOL",
                "--db",
                str(tmp_path / "public.duckdb"),
            ]
        )
        == 0
    )
    assert [(call[0], call[1]) for call in calls[:4]] == [
        (Venue.BYBIT, "instruments"),
        (Venue.HYPERLIQUID, "instruments"),
        (Venue.DYDX, "instruments"),
        (Venue.LIGHTER, "instruments"),
    ]
    funding_calls = [call for call in calls if call[1] == "funding"]
    assert len(funding_calls) == 12
    assert all(call[3] == NOW - timedelta(days=7) and call[4] == NOW for call in funding_calls)


def test_collect_public_cli_prints_structured_adapter_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Catches valid adapter limitations being persisted but hidden from the operator.
    calls: list[tuple[object, ...]] = []
    adapter = _WarningPublicAdapter(Venue.DYDX, calls)

    @asynccontextmanager
    async def session(store: object, venues: object):
        assert tuple(venues) == (Venue.DYDX,)
        yield (adapter,)

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "dydx",
                "--assets",
                "BTC",
                "--db",
                str(tmp_path / "warning.duckdb"),
            ]
        )
        == 0
    )
    assert capsys.readouterr().err == (
        "polytrading: warning: dydx DYDX_MARK_PRICE_UNAVAILABLE BTC-USD "
        "/v4/perpetualMarkets: dYdX public market evidence has no documented "
        "mark-price field\n"
    )


def test_collect_public_skips_bybit_history_without_point_in_time_instrument_basis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[object, ...]] = []
    adapters = (
        _PersistingPublicAdapter(Venue.BYBIT, calls),
        _PersistingPublicAdapter(Venue.HYPERLIQUID, calls),
    )

    @asynccontextmanager
    async def session(store: object, venues: object):
        yield adapters

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "all",
                "--assets",
                "BTC",
                "--db",
                str(tmp_path / "fresh.duckdb"),
            ]
        )
        == 0
    )
    assert [(call[0], call[1]) for call in calls] == [
        (Venue.BYBIT, "instruments"),
        (Venue.HYPERLIQUID, "instruments"),
        (Venue.BYBIT, "markets"),
        (Venue.HYPERLIQUID, "markets"),
        (Venue.HYPERLIQUID, "funding"),
    ]
    message = capsys.readouterr().err.lower()
    assert "bybit btc funding was not collected" in message
    assert "2026-08-05t12:00:00+00:00..2026-08-12t12:00:00+00:00" in message
    assert "no bybit instrument specification was known at the range start" in message
    with duckdb.connect(str(tmp_path / "fresh.duckdb"), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM instrument_specs").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM market_snapshots").fetchone() == (2,)
        assert connection.execute("SELECT venue FROM funding_observations").fetchall() == [
            (Venue.HYPERLIQUID.value,)
        ]


def test_preseeded_historical_bybit_instrument_enables_funding_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "seeded.duckdb"
    start = NOW - timedelta(days=7)
    store = DuckDBStore(database)
    store.append_instrument(
        instrument_spec(
            venue=Venue.BYBIT,
            symbol="BTCUSDT",
            instrument_id="bybit:BTCUSDT",
            observed_at=start,
        )
    )
    store.close()
    calls: list[tuple[object, ...]] = []
    adapters = (_PersistingPublicAdapter(Venue.BYBIT, calls),)

    @asynccontextmanager
    async def session(store: object, venues: object):
        yield adapters

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    assert (
        main(
            [
                "collect",
                "public",
                "--venue",
                "bybit",
                "--assets",
                "BTC",
                "--start",
                start.isoformat(),
                "--end",
                NOW.isoformat(),
                "--db",
                str(database),
            ]
        )
        == 0
    )
    assert [call[1] for call in calls] == ["instruments", "markets", "funding"]
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM funding_observations").fetchone() == (1,)


def test_bybit_history_basis_requires_an_instrument_known_at_range_start(
    tmp_path: Path,
) -> None:
    store = DuckDBStore(tmp_path / "basis.duckdb")
    start = NOW - timedelta(days=7)
    assert not cli._has_bybit_history_basis(store, Asset.BTC, start)
    store.append_instrument(
        instrument_spec(
            venue=Venue.BYBIT,
            symbol="BTCUSDT",
            instrument_id="bybit:BTCUSDT",
            observed_at=start,
        )
    )
    assert cli._has_bybit_history_basis(store, Asset.BTC, start)
    assert not cli._has_bybit_history_basis(store, Asset.ETH, start)
    store.close()


def test_collect_books_once_cli_launches_all_venues_in_one_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts: list[Venue] = []
    adapters = (
        _BookAdapter(Venue.BYBIT, starts),
        _BookAdapter(Venue.HYPERLIQUID, starts),
        _BookAdapter(Venue.DYDX, starts),
        _BookAdapter(Venue.LIGHTER, starts),
    )

    @asynccontextmanager
    async def session(store: object, venues: object):
        assert tuple(venues) == (
            Venue.BYBIT,
            Venue.HYPERLIQUID,
            Venue.DYDX,
            Venue.LIGHTER,
        )
        yield adapters

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)
    database = tmp_path / "books-once.duckdb"

    assert (
        main(
            [
                "collect",
                "books",
                "--venue",
                "all",
                "--assets",
                "BTC,ETH,SOL",
                "--once",
                "--db",
                str(database),
            ]
        )
        == 0
    )
    assert starts == [Venue.BYBIT, Venue.DYDX, Venue.HYPERLIQUID, Venue.LIGHTER]
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM book_collection_cycles").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM book_snapshots").fetchone() == (12,)


def test_collect_books_cli_prints_validated_adapter_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Catches synchronized-book warnings being dropped between validation and the CLI.
    starts: list[Venue] = []
    adapter = _WarningBookAdapter(Venue.DYDX, starts)

    @asynccontextmanager
    async def session(store: object, venues: object):
        assert tuple(venues) == (Venue.DYDX,)
        yield (adapter,)

    monkeypatch.setattr(cli, "public_adapter_session", session)
    monkeypatch.setattr(cli, "_utc_now", lambda: NOW)

    assert (
        main(
            [
                "collect",
                "books",
                "--venue",
                "dydx",
                "--assets",
                "BTC",
                "--once",
                "--db",
                str(tmp_path / "book-warning.duckdb"),
            ]
        )
        == 0
    )
    assert capsys.readouterr().err == (
        "polytrading: warning: dydx DYDX_REST_BOOK_LOCAL_TIMESTAMP BTC-USD "
        "/v4/orderbooks/perpetualMarket/BTC-USD: dYdX REST book has no venue timestamp "
        "or sequence; local receipt time was used\n"
    )


def test_venue_modules_define_no_private_or_trading_method_names() -> None:
    prohibited = {
        "place_order",
        "cancel_order",
        "withdraw",
        "transfer",
        "authenticate",
        "sign",
    }
    definitions: set[str] = set()
    for path in Path("src/polytrading/venues").rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        definitions.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )

    assert definitions.isdisjoint(prohibited)
