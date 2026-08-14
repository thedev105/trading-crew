from html.parser import HTMLParser
from importlib import resources


class DashboardMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _asset(name: str) -> str:
    return resources.files("polytrading.web.assets").joinpath(name).read_text(encoding="utf-8")


def test_dashboard_document_has_semantic_landmarks_and_local_resources() -> None:
    parser = DashboardMarkupParser()
    parser.feed(_asset("index.html"))
    tags = parser.tags
    ids = {attrs["id"] for _tag, attrs in tags if attrs.get("id")}

    assert {"header", "main", "section", "table", "button", "noscript"}.issubset(
        {tag for tag, _attrs in tags}
    )
    assert {
        "main",
        "overview",
        "trial",
        "trial-summary",
        "trial-asset-rows",
        "trial-boundary-rows",
        "trial-gap-reasons",
        "trial-economics",
        "trial-fees",
        "markets",
        "market-rows",
        "compatibility",
        "discovery-summary",
        "candidate-rows",
        "dossier-rows",
        "dossier-left-heading",
        "dossier-right-heading",
        "research",
        "economics",
        "economics-rows",
        "operations",
        "refresh",
        "refresh-status",
    }.issubset(ids)
    assert any(
        tag == "a" and attrs.get("class") == "skip-link" and attrs.get("href") == "#main"
        for tag, attrs in tags
    )
    assert any(tag == "a" and attrs.get("href") == "#trial" for tag, attrs in tags)
    assert any(tag == "a" and attrs.get("href") == "#economics" for tag, attrs in tags)
    favicon_urls = {
        attrs["href"]
        for tag, attrs in tags
        if tag == "link"
        and attrs.get("rel") == "icon"
        and (attrs.get("href") or "").startswith("data:image/svg+xml,")
    }
    assert len(favicon_urls) == 1
    assert any(
        tag == "script" and attrs.get("type") == "module" and attrs.get("src") == "/assets/app.js"
        for tag, attrs in tags
    )
    assert any(tag == "link" and attrs.get("href") == "/assets/app.css" for tag, attrs in tags)
    resource_urls = {
        value
        for _tag, attrs in tags
        for name, value in attrs.items()
        if name in {"src", "href"} and value is not None and not value.startswith("#")
    }
    assert resource_urls == {
        "/assets/app.css",
        "/assets/app.js",
        *favicon_urls,
    }


def test_dashboard_assets_use_safe_dom_rendering_without_remote_or_mutation_surfaces() -> None:
    html = _asset("index.html")
    css = _asset("app.css")
    javascript = _asset("app.js")
    combined = "\n".join((html, css, javascript)).lower()

    assert "textContent" in javascript
    assert "AbortController" in javascript
    assert "replaceChildren" in javascript
    assert "navigator.clipboard.writeText" in javascript
    assert "snapshot.compatibility_dossier" in javascript
    assert "snapshot.venue_discovery" in javascript
    assert "renderDiscovery(snapshot)" in javascript
    assert "nodes.candidateRows.replaceChildren" in javascript
    assert "nodes.dossierLeftHeading.textContent" in javascript
    assert "nodes.dossierRightHeading.textContent" in javascript
    assert "renderEconomics(snapshot)" in javascript
    assert "nodes.economicsRows.replaceChildren" in javascript
    assert 'hasOwnProperty.call(snapshot, "venue_discovery")' in javascript
    assert "Lighter-dYdX prospective trial" in html
    assert "collection-only projection" in html
    assert "Operator policy not assessed" in html
    assert "Research only" in html
    assert "No trading authority" in html
    for forbidden in (
        "innerhtml",
        "outerhtml",
        "insertadjacenthtml",
        "document.write",
        "http://",
        "https://",
        "<form",
        "password",
        "api-key",
        "place-order",
        "execute-trade",
        "websocket",
        "eventsource",
        'method: "post"',
        'method: "put"',
        'method: "patch"',
        'method: "delete"',
    ):
        assert forbidden not in combined


def test_dashboard_client_accepts_the_canonical_twelve_market_rows() -> None:
    javascript = _asset("app.js")

    assert "snapshot.markets.length !== 12" in javascript
    assert "snapshot.markets.length !== 9" not in javascript
    assert "snapshot.economics_rows.length !== 3" in javascript


def test_trial_client_validates_and_renders_the_complete_trial_contract() -> None:
    javascript = _asset("app.js")

    assert "snapshot.trial_health" in javascript
    assert "snapshot.trial_health.assets.length !== 3" in javascript
    assert '.map((item) => item.asset).join(",") !== "BTC,ETH,SOL"' in javascript
    assert "!Array.isArray(snapshot.trial_health.recent_boundaries)" in javascript
    assert "function renderTrial(snapshot)" in javascript
    assert "function renderTrialAssets(trial)" in javascript
    assert "function renderTrialBoundaries(trial)" in javascript
    assert "function renderTrialEconomics(trial)" in javascript
    assert "renderTrial(snapshot);" in javascript
    assert 'NOT_STARTED: "missing"' in javascript
    assert 'COLLECTING: "collecting"' in javascript
    assert 'DEGRADED: "degraded"' in javascript
    assert 'READY_FOR_ECONOMICS_EVALUATION: "ready"' in javascript
    for required_field in (
        "paired_training_funding_hours",
        "training_funding_coverage",
        "paired_evaluation_funding_hours",
        "evaluation_funding_coverage",
        "paired_total_funding_hours",
        "total_funding_coverage",
        "paired_book_hours",
        "book_coverage",
        "current_funding_paired_hours",
        "current_funding_consecutive",
        "dense_book_pair_count",
        "consecutive_dense_sample_count",
        "latest_funding_boundary",
        "latest_book_completed_at",
        "latest_book_age_seconds",
        "latest_book_skew_ms",
        "fresh_book_ready",
        "projected_earliest_evaluation_end",
        "dossier_available",
        "reviewed_fees",
        "effective_from",
        "observed_at",
        "source_hash",
        "funding_status",
        "book_status",
        "attempt_count",
        "complete_attempt_count",
        "degraded_attempt_count",
        "late_attempt_count",
        "failed_book_attempt_count",
        "skewed_book_attempt_count",
        "evaluation_schema_version",
        "evaluation_id",
        "policy_hash",
        "reason_codes",
    ):
        assert required_field in javascript
    assert "Unavailable" in javascript


def test_trial_recipes_are_copy_only_and_complete() -> None:
    javascript = _asset("app.js")

    for recipe_key in (
        "collect_trial_funding",
        "collect_trial_books_burst",
        "collect_trial_books_once",
        "inspect_trial_health",
        "import_trial_fees",
        "evaluate_trial_btc",
        "trial_scheduler_example",
    ):
        assert recipe_key in javascript
    assert 'copy.type = "button"' in javascript
    assert "navigator.clipboard.writeText(recipe)" in javascript


def test_dashboard_retries_only_database_busy_with_one_abort_budget() -> None:
    javascript = _asset("app.js")

    assert "const databaseBusyRetryMs = [250, 500, 1000];" in javascript
    assert 'code !== "DATABASE_BUSY"' in javascript
    assert "async function fetchSnapshot(signal)" in javascript
    assert javascript.count("new AbortController()") == 1
    assert "const snapshot = await fetchSnapshot(controller.signal);" in javascript
    assert "state.lastSnapshot = snapshot;" in javascript
    assert "state.lastSnapshot = null" not in javascript
    assert "Stale" in javascript
    assert "Unavailable" in javascript
    assert javascript.count("state.timer = window.setTimeout(refreshSnapshot, 15_000);") == 1


def test_economics_section_is_research_only_and_has_explicit_unavailable_values() -> None:
    html = _asset("index.html")
    javascript = _asset("app.js")

    assert "Conservative shadow economics" in html
    assert (
        "Research only — shadow candidate, not a fill, recommendation, or trading authorization."
        in html
    )
    assert "Unavailable" in javascript
    assert "SHADOW_CANDIDATE" in javascript
    assert "INSUFFICIENT_EVIDENCE" in javascript
    assert "REJECTED" in javascript
    assert "<form" not in html.lower()


def test_discovery_copy_is_neutral_and_keeps_activation_closed() -> None:
    html = _asset("index.html")
    javascript = _asset("app.js")

    assert "Venue discovery" in html
    assert "Ranked candidates" in html
    assert "Selected candidate checks" in html
    assert "Public evidence + economic modeling" in javascript
    assert "Not authorized" in javascript
    assert "No advanceable candidate" in javascript
    assert "candidate-selected" in javascript


def test_dashboard_styles_cover_focus_mobile_and_reduced_motion() -> None:
    css = _asset("app.css")

    assert ":focus-visible" in css
    assert "@media (max-width: 720px)" in css
    assert "prefers-reduced-motion" in css
    assert '[data-tone="blocking"]' in css
    assert '[data-tone="model_required"]' in css
    assert '[data-tone="missing_evidence"]' in css
    assert '[data-tone="matched"]' in css
    assert '[data-tone="shadow_candidate"]' in css
    assert '[data-tone="rejected"]' in css
    assert '[data-tone="insufficient_evidence"]' in css
    assert '[data-tone="collecting"]' in css
    assert '[data-tone="ready"]' in css
    assert '#refresh-status[data-state="stale"]' in css
    assert ".trial-summary" in css
    assert ".trial-table-shell" in css
    assert ".trial-matrix-shell" in css
    assert ".trial-detail-grid" in css
    assert ".trial-summary { grid-template-columns: 1fr; }" in css
