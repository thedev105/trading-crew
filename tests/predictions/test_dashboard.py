import json
from datetime import UTC, datetime
from http import HTTPStatus
from importlib import resources
from pathlib import Path

from polytrading.predictions.candidates_models import CandidateDisposition
from polytrading.predictions.dashboard_server import PredictionDashboardApplication
from polytrading.predictions.storage.store import PredictionMarketStore
from tests.predictions.candidate_helpers import ai_provenance, candidate_relationship

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)

_FORBIDDEN_WORDS = ("risk-free", "guaranteed", "approved")


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
    assert '"AI-nominated — quarantined"' in javascript
    assert 'candidate.provenance_kind === "ai"' in javascript


def test_dashboard_candidates_panel_never_uses_forbidden_words() -> None:
    javascript = _asset("app.js").lower()
    html = _asset("index.html").lower()
    css = _asset("app.css").lower()

    for forbidden in _FORBIDDEN_WORDS:
        assert forbidden not in javascript
        assert forbidden not in html
        assert forbidden not in css


def test_dashboard_ai_provenance_candidate_shows_quarantined_badge_in_snapshot(
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
