from __future__ import annotations

import ast
import json
import shutil
import socket
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polytrading.cli import main
from polytrading.predictions.execution.models import ProtocolConformanceResult
from polytrading.predictions.polymarket_execution import conformance
from polytrading.predictions.polymarket_execution.conformance import run_conformance
from polytrading.predictions.polymarket_execution.protocol import bundled_fixture_path
from polytrading.predictions.storage.store import PredictionMarketStore

FIXED_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
EXPECTED_FIXTURE_NAMES = {
    "event_vectors_v1.json",
    "order_vectors_v1.json",
    "protocol_v1.json",
    "sources_v1.json",
}
FORBIDDEN_OUTPUT_FRAGMENTS = (
    "task-6-api-key",
    "task-6-passphrase",
    "cG9seW1hcmtldC10YXNrLTYtaG1hYy1rZXk=",
    "POLY_ADDRESS",
    "POLY_SIGNATURE",
)


def _assert_sensitive_absent(observed: str, *additional: str) -> None:
    for fragment in (*FORBIDDEN_OUTPUT_FRAGMENTS, *additional):
        if fragment in observed:
            raise AssertionError("SENSITIVE_OUTPUT_DETECTED") from None


def _copy_fixtures(tmp_path: Path) -> Path:
    target = tmp_path / "fixtures"
    shutil.copytree(bundled_fixture_path(), target)
    return target


def _copy_fixtures_with_duplicate_content(tmp_path: Path) -> Path:
    target = _copy_fixtures(tmp_path)
    shutil.copyfile(
        target / "order_vectors_v1.json",
        target / "event_vectors_v1.json",
    )
    return target


def _run_cli(
    database: Path,
    *,
    fixtures: Path | None = None,
    output_format: str = "json",
) -> int:
    argv = [
        "predictions",
        "execution",
        "conformance",
        "polymarket",
        "--db",
        str(database),
        "--format",
        output_format,
    ]
    if fixtures is not None:
        argv.extend(("--fixtures", str(fixtures)))
    return main(argv)


def _mutate_json(path: Path, mutate: object) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert callable(mutate)
    mutate(document)
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")


def test_bundled_fixture_run_is_conformant_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)

    first = run_conformance(bundled_fixture_path(), "test-revision")
    second = run_conformance(bundled_fixture_path(), "test-revision")

    assert isinstance(first, ProtocolConformanceResult)
    assert first == second
    assert first.result == "CONFORMANT"
    assert first.failure_fingerprints == ()
    assert first.implementation_revision == "test-revision"
    assert first.observed_at == FIXED_NOW
    assert first.executed_checks == tuple(sorted(first.executed_checks))
    assert {
        "amount_vectors",
        "event_vectors",
        "fixture_trust",
        "l1_auth_vector",
        "l2_auth_vector",
        "order_wire_vector",
        "route_catalog",
        "stable_fixture_view",
    }.issubset(first.executed_checks)
    assert len(first.fixture_hashes) == 4
    assert len(first.source_hashes) == 8


def test_valid_copied_fixture_override_is_conformant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)

    result = run_conformance(_copy_fixtures(tmp_path), "copied-fixtures")

    assert result.result == "CONFORMANT"
    assert result.failure_fingerprints == ()


def test_duplicate_allowlisted_fixture_content_is_a_strict_review_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)

    result = run_conformance(
        _copy_fixtures_with_duplicate_content(tmp_path),
        "duplicate-content",
    )

    assert result.result == "PROTOCOL_REVIEW_REQUIRED"
    assert result.failure_fingerprints
    assert result.fixture_hashes == tuple(sorted(set(result.fixture_hashes)))
    assert len(result.fixture_hashes) == 3
    _assert_sensitive_absent(result.model_dump_json())


@pytest.mark.parametrize(
    ("relative_path", "mutation"),
    [
        ("protocol_v1.json", lambda payload: payload.__setitem__("version", "changed")),
        (
            "order_vectors_v1.json",
            lambda payload: payload["amount_vectors"][0].__setitem__("makerAmount", "1"),
        ),
        (
            "order_vectors_v1.json",
            lambda payload: payload["wire_vector"]["request_payload"]["order"].__setitem__(
                "signature", "0x" + "00" * 65
            ),
        ),
        (
            "order_vectors_v1.json",
            lambda payload: payload["heartbeat_preimage_vector"].__setitem__("preimage", "changed"),
        ),
        (
            "event_vectors_v1.json",
            lambda payload: payload["trade_settlement_states"].append("UNKNOWN"),
        ),
        (
            "protocol_v1.json",
            lambda payload: payload["routes"]["heartbeat"].__setitem__("method", "GET"),
        ),
    ],
)
def test_protocol_and_vector_mutations_fail_closed_without_raw_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relative_path: str,
    mutation: object,
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)
    fixture_root = _copy_fixtures(tmp_path)
    _mutate_json(fixture_root / relative_path, mutation)

    result = run_conformance(fixture_root, "mutated")
    serialized = result.model_dump_json()

    assert result.result == "PROTOCOL_REVIEW_REQUIRED"
    assert result.failure_fingerprints
    assert len(result.failure_fingerprints) <= 32
    assert result.failure_fingerprints == tuple(sorted(set(result.failure_fingerprints)))
    assert relative_path not in serialized
    _assert_sensitive_absent(serialized)


@pytest.mark.parametrize("relative_path", sorted(EXPECTED_FIXTURE_NAMES))
def test_missing_fixture_member_requires_protocol_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, relative_path: str
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)
    fixture_root = _copy_fixtures(tmp_path)
    (fixture_root / relative_path).unlink()

    result = run_conformance(fixture_root, "missing-member")

    assert result.result == "PROTOCOL_REVIEW_REQUIRED"
    assert result.failure_fingerprints


def test_failure_fingerprints_are_stable_code_and_path_identities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)
    first_root = _copy_fixtures(tmp_path / "first")
    second_root = _copy_fixtures(tmp_path / "second")
    (first_root / "event_vectors_v1.json").write_bytes(b"first secret malformed payload")
    (second_root / "event_vectors_v1.json").write_bytes(b"different secret malformed payload")

    first = run_conformance(first_root, "same-revision")
    second = run_conformance(second_root, "same-revision")

    assert first.failure_fingerprints == second.failure_fingerprints


def test_fixture_root_rejects_unknown_symlink_nonregular_and_oversize_members(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)

    unknown_root = _copy_fixtures(tmp_path / "unknown")
    (unknown_root / "unexpected.json").write_text("{}", encoding="utf-8")
    assert run_conformance(unknown_root, "unknown").result == "PROTOCOL_REVIEW_REQUIRED"

    symlink_root = _copy_fixtures(tmp_path / "symlink")
    (symlink_root / "event_vectors_v1.json").unlink()
    (symlink_root / "event_vectors_v1.json").symlink_to(
        bundled_fixture_path() / "event_vectors_v1.json"
    )
    assert run_conformance(symlink_root, "symlink").result == "PROTOCOL_REVIEW_REQUIRED"

    nonregular_root = _copy_fixtures(tmp_path / "nonregular")
    (nonregular_root / "event_vectors_v1.json").unlink()
    (nonregular_root / "event_vectors_v1.json").mkdir()
    assert run_conformance(nonregular_root, "nonregular").result == "PROTOCOL_REVIEW_REQUIRED"

    oversize_root = _copy_fixtures(tmp_path / "oversize")
    (oversize_root / "event_vectors_v1.json").write_bytes(b"x" * (1_048_576 + 1))
    assert run_conformance(oversize_root, "oversize").result == "PROTOCOL_REVIEW_REQUIRED"


def test_traversal_declared_by_protocol_fixture_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)
    fixture_root = _copy_fixtures(tmp_path)
    _mutate_json(
        fixture_root / "protocol_v1.json",
        lambda payload: payload.__setitem__("source_manifest_path", "../outside.json"),
    )

    result = run_conformance(fixture_root, "traversal")

    assert result.result == "PROTOCOL_REVIEW_REQUIRED"
    _assert_sensitive_absent(result.model_dump_json(), "outside")


def test_changing_fixture_view_is_detected_after_semantic_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)
    fixture_root = _copy_fixtures(tmp_path)
    original = conformance._check_route_catalog

    def mutate_after_capture(*arguments: object) -> bool:
        passed = original(*arguments)
        (fixture_root / "event_vectors_v1.json").write_bytes(b"changed-after-capture")
        return passed

    monkeypatch.setattr(conformance, "_check_route_catalog", mutate_after_capture)

    result = run_conformance(fixture_root, "changing-view")

    assert result.result == "PROTOCOL_REVIEW_REQUIRED"
    assert result.failure_fingerprints


@pytest.mark.parametrize(
    "regression",
    ["amount", "order", "l1", "l2", "event", "route"],
)
def test_independent_api_regressions_require_review(
    monkeypatch: pytest.MonkeyPatch, regression: str
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)
    target_by_regression = {
        "amount": "_check_amount_vectors",
        "order": "_check_order_wire_vector",
        "l1": "_check_l1_auth_vector",
        "l2": "_check_l2_auth_vector",
        "event": "_check_event_vectors",
        "route": "_check_route_catalog",
    }
    monkeypatch.setattr(conformance, target_by_regression[regression], lambda *_args: False)

    result = run_conformance(bundled_fixture_path(), "api-regression")

    assert result.result == "PROTOCOL_REVIEW_REQUIRED"
    assert result.failure_fingerprints


def test_runner_translates_internal_exceptions_without_raw_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)

    def fail_with_secret(*_args: object) -> bool:
        raise ValueError("raw fixture secret task-6-passphrase")

    monkeypatch.setattr(conformance, "_check_l2_auth_vector", fail_with_secret)

    result = run_conformance(bundled_fixture_path(), "exception")

    assert result.result == "PROTOCOL_REVIEW_REQUIRED"
    _assert_sensitive_absent(result.model_dump_json())


def test_cli_defaults_to_bundle_never_constructs_socket_and_persists_exact_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)

    def reject_socket(*_args: object, **_kwargs: object) -> socket.socket:
        raise AssertionError("offline conformance must not construct a socket")

    monkeypatch.setattr(socket, "socket", reject_socket)
    database = tmp_path / "conformance.duckdb"

    exit_code = _run_cli(database)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert payload["result"] == "CONFORMANT"
    assert payload["network_used"] is False
    assert payload["protocol_version"] == "polymarket-clob-2026-08-25-v1"
    assert list(payload) == sorted(payload)
    _assert_sensitive_absent(captured.out)

    store = PredictionMarketStore(database, read_only=True)
    try:
        stored = store.verified_protocol_conformance_results(FIXED_NOW + timedelta(seconds=1))
    finally:
        store.close()
    assert len(stored) == 1
    assert stored[0].model_dump(mode="json") == {
        key: value
        for key, value in payload.items()
        if key not in {"network_used", "protocol_version"}
    }


def test_cli_copied_override_text_output_is_stable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)
    fixture_root = _copy_fixtures(tmp_path)

    assert _run_cli(tmp_path / "first.duckdb", fixtures=fixture_root, output_format="text") == 0
    first = capsys.readouterr()
    assert _run_cli(tmp_path / "second.duckdb", fixtures=fixture_root, output_format="text") == 0
    second = capsys.readouterr()

    assert first.err == second.err == ""
    assert first.out == second.out
    assert "network_used=false" in first.out
    assert "result=CONFORMANT" in first.out


def test_cli_changed_fixture_persists_review_result_and_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)
    fixture_root = _copy_fixtures(tmp_path)
    (fixture_root / "order_vectors_v1.json").write_bytes(b"raw-secret-invalid")
    database = tmp_path / "review.duckdb"

    exit_code = _run_cli(database, fixtures=fixture_root)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert payload["result"] == "PROTOCOL_REVIEW_REQUIRED"
    _assert_sensitive_absent(captured.out, "raw-secret-invalid")

    store = PredictionMarketStore(database, read_only=True)
    try:
        stored = store.verified_protocol_conformance_results(FIXED_NOW + timedelta(seconds=1))
    finally:
        store.close()
    assert len(stored) == 1
    assert stored[0].result == "PROTOCOL_REVIEW_REQUIRED"


def test_cli_duplicate_allowlisted_fixture_content_persists_review_result_and_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(conformance, "_utc_now", lambda: FIXED_NOW)
    fixture_root = _copy_fixtures_with_duplicate_content(tmp_path)
    database = tmp_path / "duplicate-content.duckdb"

    exit_code = _run_cli(database, fixtures=fixture_root)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert captured.err == ""
    assert payload["result"] == "PROTOCOL_REVIEW_REQUIRED"
    assert payload["fixture_hashes"] == sorted(set(payload["fixture_hashes"]))
    assert len(payload["fixture_hashes"]) == 3
    _assert_sensitive_absent(captured.out)

    store = PredictionMarketStore(database, read_only=True)
    try:
        stored = store.verified_protocol_conformance_results(FIXED_NOW + timedelta(seconds=1))
    finally:
        store.close()
    assert len(stored) == 1
    assert stored[0].result == "PROTOCOL_REVIEW_REQUIRED"


def test_cli_invalid_fixture_root_is_exit_64_without_database_or_raw_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture_file = tmp_path / "not-a-directory-secret"
    fixture_file.write_text("private", encoding="utf-8")
    database = tmp_path / "must-not-exist.duckdb"

    exit_code = _run_cli(database, fixtures=fixture_file)
    captured = capsys.readouterr()

    assert exit_code == 64
    assert captured.out == ""
    _assert_sensitive_absent(captured.err, "not-a-directory-secret")
    assert not database.exists()


def test_cli_database_or_writer_lease_failure_is_sanitized_exit_64(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    @contextmanager
    def reject_lease(*_args: object, **_kwargs: object):
        raise OSError("secret lease path")
        yield

    monkeypatch.setattr("polytrading.predictions.cli.database_writer_lease", reject_lease)

    exit_code = _run_cli(tmp_path / "blocked.duckdb")
    captured = capsys.readouterr()

    assert exit_code == 64
    assert captured.out == ""
    _assert_sensitive_absent(captured.err, "secret lease path")


def test_cli_read_only_database_is_exit_64_without_partial_persistence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "read-only.duckdb"
    PredictionMarketStore(database).close()
    database.chmod(0o444)
    try:
        exit_code = _run_cli(database)
        captured = capsys.readouterr()
    finally:
        database.chmod(0o644)

    assert exit_code == 64
    assert captured.out == ""
    _assert_sensitive_absent(captured.err, str(database))
    store = PredictionMarketStore(database, read_only=True)
    try:
        assert store.verified_protocol_conformance_results(FIXED_NOW + timedelta(days=1)) == ()
    finally:
        store.close()


def test_conformance_runtime_has_no_transport_signer_or_activation_import_surface() -> None:
    source_paths = (
        Path("src/polytrading/predictions/polymarket_execution/conformance.py"),
        Path("src/polytrading/predictions/cli.py"),
    )
    forbidden_modules = {
        "httpx",
        "socket",
        "subprocess",
        "websockets",
        "polytrading.predictions.polymarket_execution.rest",
        "polytrading.predictions.execution.signer",
        "polytrading.predictions.execution.activation",
    }
    conformance_tree = ast.parse(source_paths[0].read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(conformance_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module
        for node in ast.walk(conformance_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert imported.isdisjoint(forbidden_modules)
    cli_source = source_paths[1].read_text(encoding="utf-8")
    execution_slice = cli_source[cli_source.index("def _run_polymarket_conformance") :]
    assert not any(
        token in execution_slice
        for token in ("private_key", "credential", "signer", "activate", "clear_kill", "httpx")
    )
