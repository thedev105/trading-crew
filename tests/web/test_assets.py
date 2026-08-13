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
        "markets",
        "market-rows",
        "compatibility",
        "discovery-summary",
        "candidate-rows",
        "dossier-rows",
        "dossier-left-heading",
        "dossier-right-heading",
        "research",
        "operations",
        "refresh",
        "refresh-status",
    }.issubset(ids)
    assert any(
        tag == "a" and attrs.get("class") == "skip-link" and attrs.get("href") == "#main"
        for tag, attrs in tags
    )
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
    assert resource_urls == {"/assets/app.css", "/assets/app.js"}


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
    assert 'hasOwnProperty.call(snapshot, "venue_discovery")' in javascript
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
    ):
        assert forbidden not in combined


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
