import json
from datetime import UTC, datetime
from http import HTTPStatus
from importlib import resources
from pathlib import Path

from polytrading.predictions.candidates_models import CandidateDisposition
from polytrading.predictions.dashboard_server import PredictionDashboardApplication
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.candidate_helpers import ai_provenance, candidate_relationship
from tests.predictions.proof_helpers import proof_artifact
from tests.predictions.scan_helpers import scan_report

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)

_FORBIDDEN_WORDS = ("risk-free", "guaranteed", "approved", "live eligible")


def _asset(name: str) -> str:
    return (
        resources.files("polytrading.predictions.web_assets")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def test_dashboard_serves_the_snapshot_at_the_json_endpoint(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")

    assert response.status == HTTPStatus.OK
    document = json.loads(response.body)
    assert document["as_of"] == "2026-08-16T12:00:00Z"
    assert {venue["venue"] for venue in document["health"]["venues"]} == {
        "polymarket",
        "kalshi",
        "limitless",
    }
    assert document["shadow"] == {
        "by_terminal_state": {},
        "experiments_by_family": {},
        "latest": [],
        "proposals_total": 0,
        "reconciled_count": 0,
        "reconciled_paper_pnl_usd": "0",
        "schema_version": 1,
        "unreconciled_count": 0,
    }


def test_dashboard_rejects_a_non_loopback_host(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "example.com")

    assert response.status == HTTPStatus.BAD_REQUEST
    assert json.loads(response.body)["error"]["code"] == "INVALID_HOST"


def test_dashboard_rejects_non_get_methods(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("POST", "/api/v1/predictions-dashboard", "127.0.0.1")

    assert response.status == HTTPStatus.METHOD_NOT_ALLOWED


def test_dashboard_serves_static_assets(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    index = application.respond("GET", "/", "127.0.0.1")
    css = application.respond("GET", "/assets/app.css", "127.0.0.1")
    js = application.respond("GET", "/assets/app.js", "127.0.0.1")

    assert index.status == HTTPStatus.OK
    assert b"predictions dashboard" in index.body
    assert css.status == HTTPStatus.OK
    assert js.status == HTTPStatus.OK


def test_dashboard_rejects_a_query_string_on_the_api_endpoint(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard?x=1", "127.0.0.1")
    assert response.status == HTTPStatus.BAD_REQUEST


def test_dashboard_returns_not_found_for_an_unknown_path(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/no-such-path", "127.0.0.1")
    assert response.status == HTTPStatus.NOT_FOUND


def test_dashboard_json_endpoint_includes_candidate_summary(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_candidate_relationship(candidate_relationship())
    store.close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")

    document = json.loads(response.body)
    assert document["candidates"]["total"] == 1
    assert document["candidates"]["by_disposition"] == {"quarantined": 1}
    assert document["candidates"]["latest"][0]["disposition"] == "quarantined"
    assert document["candidates"]["latest"][0]["provenance_kind"] == "deterministic"


def test_dashboard_json_endpoint_omits_candidates_when_none_seeded(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")

    document = json.loads(response.body)
    assert document["candidates"]["total"] == 0
    assert document["candidates"]["latest"] == []


def test_dashboard_html_declares_a_candidates_section() -> None:
    html = _asset("index.html")
    assert 'id="candidates"' in html
    assert 'id="candidates-summary"' in html
    assert 'id="candidates-empty"' in html
    assert "No candidate relationships observed" in html


def test_dashboard_client_renders_candidates_labeled_by_disposition_with_ai_badge() -> None:
    javascript = _asset("app.js")

    assert "function renderCandidates(snapshot)" in javascript
    assert "renderCandidates(snapshot);" in javascript
    assert "snapshot.candidates" in javascript
    assert "cell(candidate.disposition)" in javascript
    assert '"AI-nominated"' in javascript
    assert '"AI-nominated — quarantined"' not in javascript
    assert 'candidate.provenance_kind === "ai"' in javascript


def test_dashboard_candidates_panel_never_uses_forbidden_words() -> None:
    javascript = _asset("app.js").lower()
    html = _asset("index.html").lower()
    css = _asset("app.css").lower()

    for forbidden in _FORBIDDEN_WORDS:
        assert forbidden not in javascript
        assert forbidden not in html
        assert forbidden not in css


def test_dashboard_ai_provenance_candidate_carries_its_own_disposition_in_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_candidate_relationship(
        candidate_relationship(
            disposition=CandidateDisposition.QUARANTINED, provenance=ai_provenance()
        )
    )
    store.close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")

    document = json.loads(response.body)
    assert document["candidates"]["latest"][0]["provenance_kind"] == "ai"
    assert document["candidates"]["latest"][0]["disposition"] == "quarantined"


def test_dashboard_ai_provenance_candidate_disposition_is_not_forced_to_quarantined(
    tmp_path: Path,
) -> None:
    # An AI-provenance candidate may be rejected as well as quarantined; the JSON
    # snapshot's disposition field must reflect the candidate's actual disposition,
    # not the badge text the client happens to render alongside it.
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_candidate_relationship(
        candidate_relationship(
            disposition=CandidateDisposition.REJECTED, provenance=ai_provenance()
        )
    )
    store.close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")

    document = json.loads(response.body)
    assert document["candidates"]["latest"][0]["provenance_kind"] == "ai"
    assert document["candidates"]["latest"][0]["disposition"] == "rejected"


def test_dashboard_json_endpoint_includes_proof_summary(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_candidate_relationship(
        candidate_relationship(observed_at=NOW, information_cutoff=NOW)
    )
    store.append_proof_artifact(proof_artifact(observed_at=NOW, information_cutoff=NOW))
    store.close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")

    document = json.loads(response.body)
    assert document["proofs"]["total"] == 1
    assert document["proofs"]["by_status"] == {"proof_ready": 1}
    assert document["proofs"]["latest"][0]["status"] == "proof_ready"
    assert document["proofs"]["latest"][0]["template"] == "binary_complement@1"


def test_dashboard_json_endpoint_omits_proofs_when_none_seeded(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")

    document = json.loads(response.body)
    assert document["proofs"]["total"] == 0
    assert document["proofs"]["latest"] == []


def test_dashboard_json_endpoint_includes_scan_summary(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    store = PredictionMarketStore(database)
    store.append_candidate_relationship(
        candidate_relationship(observed_at=NOW, information_cutoff=NOW)
    )
    store.append_scan_report(
        scan_report(
            decision="REJECTED",
            reason="economics unfavorable",
            economics=None,
            proof_id=None,
            as_of=NOW,
            observed_at=NOW,
        )
    )
    store.close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")

    document = json.loads(response.body)
    assert document["scans"]["total"] == 1
    assert document["scans"]["by_decision"] == {"REJECTED": 1}
    assert document["scans"]["latest"][0]["decision"] == "REJECTED"
    assert document["scans"]["latest"][0]["surplus"] is None


def test_dashboard_json_endpoint_omits_scans_when_none_seeded(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")

    document = json.loads(response.body)
    assert document["scans"]["total"] == 0
    assert document["scans"]["latest"] == []


def test_dashboard_html_declares_proof_and_scan_sections() -> None:
    html = _asset("index.html")
    assert 'id="proofs"' in html
    assert 'id="proofs-summary"' in html
    assert 'id="proofs-empty"' in html
    assert "No proof artifacts observed" in html
    assert 'id="scans"' in html
    assert 'id="scans-summary"' in html
    assert 'id="scans-empty"' in html
    assert "No scan reports observed" in html


def test_dashboard_client_renders_proofs_and_scans() -> None:
    javascript = _asset("app.js")

    assert "function renderProofs(snapshot)" in javascript
    assert "renderProofs(snapshot);" in javascript
    assert "snapshot.proofs" in javascript
    assert "cell(proof.status)" in javascript

    assert "function renderScans(snapshot)" in javascript
    assert "renderScans(snapshot);" in javascript
    assert "snapshot.scans" in javascript
    assert "decisionCell.textContent = scan.decision;" in javascript


def test_dashboard_scan_panel_labels_shadow_candidate_as_research_only() -> None:
    javascript = _asset("app.js")
    assert '"research decision — not an instruction to trade"' in javascript
    assert 'scan.decision === "SHADOW_CANDIDATE"' in javascript


def test_dashboard_proof_and_scan_panels_never_use_forbidden_words() -> None:
    javascript = _asset("app.js").lower()
    html = _asset("index.html").lower()
    css = _asset("app.css").lower()

    for forbidden in _FORBIDDEN_WORDS:
        assert forbidden not in javascript
        assert forbidden not in html
        assert forbidden not in css


def test_dashboard_html_declares_an_accessible_shadow_results_section() -> None:
    html = _asset("index.html")

    assert 'id="shadow-summary"' in html
    assert 'id="shadow-proposals"' in html
    assert 'id="shadow-empty"' in html
    assert 'aria-live="polite"' in html
    assert "No shadow proposals observed" in html


def test_dashboard_client_renders_shadow_states_and_gates_paper_pnl() -> None:
    javascript = _asset("app.js")

    assert "function renderShadow(snapshot)" in javascript
    assert "renderShadow(snapshot);" in javascript
    assert "snapshot.shadow" in javascript
    assert 'shadow.current_state === "reconciled"' in javascript
    assert "shadow.paper_pnl !== null" in javascript
    assert "awaiting reconciliation — paper result invalid" in javascript
    assert "if (shadow.paper_pnl)" not in javascript


def test_dashboard_served_shadow_assets_never_use_forbidden_words(tmp_path: Path) -> None:
    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    application = PredictionDashboardApplication(database, clock=lambda: NOW)

    served = b"\n".join(
        application.respond("GET", path, "127.0.0.1").body
        for path in ("/", "/assets/app.css", "/assets/app.js")
    ).lower()

    for forbidden in _FORBIDDEN_WORDS:
        assert forbidden.encode() not in served


def test_database_snapshot_factory_captures_one_normalized_cutoff(tmp_path: Path) -> None:
    from datetime import timedelta, timezone

    import polytrading.predictions.dashboard as prediction_dashboard

    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    assert hasattr(prediction_dashboard, "build_prediction_dashboard_snapshot")

    eastern = NOW.astimezone(timezone(-timedelta(hours=4)))
    snapshot = prediction_dashboard.build_prediction_dashboard_snapshot(database, now=eastern)

    assert snapshot.as_of == NOW
    assert all(section.as_of == NOW for section in snapshot.cutoff_bound_sections())
    assert len(snapshot.revision_id) == 64


def test_database_snapshot_factory_rejects_a_naive_cutoff(tmp_path: Path) -> None:
    import pytest

    import polytrading.predictions.dashboard as prediction_dashboard

    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()

    with pytest.raises(ValueError, match="timezone-aware"):
        prediction_dashboard.build_prediction_dashboard_snapshot(
            database,
            now=NOW.replace(tzinfo=None),
        )


def test_database_snapshot_factory_remains_read_only_beside_concurrent_observers(
    tmp_path: Path,
) -> None:
    import polytrading.predictions.dashboard as prediction_dashboard

    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    observer = PredictionMarketStore(database, read_only=True)
    application = PredictionDashboardApplication(database, clock=lambda: NOW)
    try:
        for _ in range(3):
            snapshot = prediction_dashboard.build_prediction_dashboard_snapshot(database, now=NOW)
            response = application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787")
            assert snapshot.as_of == NOW
            assert response.status == HTTPStatus.OK
            assert all(count == 0 for count in observer.evidence_counts_as_of(NOW).values())
    finally:
        observer.close()


def test_actual_publisher_factory_polls_beside_read_only_get_and_store_observers(
    tmp_path: Path,
) -> None:
    import polytrading.predictions.dashboard as prediction_dashboard
    from polytrading.predictions.dashboard_live import (
        DashboardRevisionBuffer,
        DashboardRevisionPublisher,
    )

    database = tmp_path / "predictions.duckdb"
    PredictionMarketStore(database).close()
    observer = PredictionMarketStore(database, read_only=True)
    application = PredictionDashboardApplication(database, clock=lambda: NOW)
    publisher = DashboardRevisionPublisher(
        snapshot_factory=lambda: prediction_dashboard.build_prediction_dashboard_snapshot(
            database, now=NOW
        ),
        revision_buffer=DashboardRevisionBuffer(capacity=2, clock=lambda: NOW),
        interval_seconds=1,
        clock=lambda: NOW,
    )
    try:
        for _ in range(3):
            publisher.poll_once()
            assert (
                application.respond("GET", "/api/v1/predictions-dashboard", "127.0.0.1:8787").status
                == HTTPStatus.OK
            )
            assert all(count == 0 for count in observer.evidence_counts_as_of(NOW).values())
    finally:
        publisher.close()
        observer.close()
