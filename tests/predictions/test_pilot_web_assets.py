from __future__ import annotations

import re
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path

import pytest

ASSET_ROOT = Path(str(resources.files("polytrading.predictions.pilot_web_assets")))
HTML = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
CSS = (ASSET_ROOT / "app.css").read_text(encoding="utf-8")
SCRIPTS = {
    name: (ASSET_ROOT / name).read_text(encoding="utf-8")
    for name in ("app.js", "api.js", "store.js", "views.js")
}


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, {name: value or "" for name, value in attrs}))


def elements() -> list[tuple[str, dict[str, str]]]:
    collector = _Collector()
    collector.feed(HTML)
    return collector.elements


def test_the_page_has_the_expected_landmarks_and_headings() -> None:
    tags = [tag for tag, _ in elements()]

    for landmark in ("header", "nav", "main", "footer", "h1"):
        assert landmark in tags
    assert HTML.count("<h1") == 4  # one per view
    assert 'href="#main"' in HTML


def test_the_four_primary_views_exist() -> None:
    identifiers = {attributes.get("id") for tag, attributes in elements() if tag == "section"}

    assert identifiers == {"readiness", "limits", "approval", "live"}


def test_status_regions_are_live_and_labelled() -> None:
    live_regions = [
        attributes for _tag, attributes in elements() if attributes.get("aria-live") == "polite"
    ]

    assert {attributes["id"] for attributes in live_regions} == {"posture", "heartbeat"}
    assert any(attributes.get("role") == "alert" for _tag, attributes in elements())


def test_no_inline_script_style_or_remote_asset_is_referenced() -> None:
    assert "<style" not in HTML
    assert not re.search(r"<script(?![^>]*\bsrc=)", HTML)
    assert not re.search(r"\son[a-z]+\s*=", HTML)  # no inline event handlers
    for source in (HTML, CSS, *SCRIPTS.values()):
        assert "http://" not in source.replace("http://localhost", "")
        assert "https://" not in source
        assert "//cdn" not in source
    assert "@import" not in CSS


def test_the_console_never_writes_markup_or_browser_storage() -> None:
    for name, source in SCRIPTS.items():
        code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))
        assert "innerHTML" not in code, name
        assert "outerHTML" not in code, name
        assert "document.write" not in code, name
        assert "localStorage" not in code, name
        assert "sessionStorage" not in code, name
        assert "indexedDB" not in code, name
        assert "eval(" not in code, name


def test_the_browser_never_builds_an_order_or_a_route() -> None:
    for name, source in SCRIPTS.items():
        for forbidden in (
            "makerAmount",
            "takerAmount",
            "signatureType",
            "tokenId",
            "clob.polymarket.com",
            "privateKey",
            "apiKey",
            "passphrase",
        ):
            assert forbidden not in source, f"{name} references {forbidden}"


def test_the_api_client_uses_only_same_origin_pilot_routes() -> None:
    api = SCRIPTS["api.js"]

    assert 'const BASE = "/api/v1/pilot"' in api
    assert 'credentials: "same-origin"' in api
    assert 'redirect: "error"' in api
    assert "X-Pilot-CSRF" in api
    for path in ("/readiness", "/policy", "/opportunities", "/live-session", "/stop", "/presence"):
        assert f'"{path}"' in api or f"`${{BASE}}{path}`" in api or path in api


def test_the_stop_control_is_prominent_and_described() -> None:
    stop = next(attributes for _tag, attributes in elements() if attributes.get("id") == "stop")

    assert stop["class"] == "danger"
    assert stop["aria-describedby"] == "stop-help"
    assert "Stop and kill" in HTML
    assert "button.danger" in CSS


def test_the_confirmation_ceremony_is_typed_and_exact() -> None:
    assert 'for="confirmation"' in HTML
    assert 'id="confirmation"' in HTML
    assert 'id="approve"' in HTML and "disabled" in HTML
    assert "state.confirmationInput !== state.confirmationText" in SCRIPTS["views.js"]


def test_immutable_ceilings_are_shown_beside_requested_limits() -> None:
    assert "Immutable ceilings are compiled" in HTML
    assert "A request may only lower one" in HTML
    assert "Ceiling" in HTML and "Requested" in HTML


def test_cross_venue_cards_are_visible_and_disabled() -> None:
    assert "Cross-venue opportunities are visible and disabled" in HTML
    assert "Cross-venue (disabled)" in SCRIPTS["views.js"]
    assert "select.disabled = Boolean(item.cross_venue)" in SCRIPTS["views.js"]
    assert '[data-disabled="true"]' in CSS


def test_success_is_never_marked_optimistically() -> None:
    app = SCRIPTS["app.js"]

    assert "await refresh();" in app
    assert "Never mark success optimistically" in app


def test_the_stylesheet_honours_focus_visibility_and_reduced_motion() -> None:
    assert ":focus-visible" in CSS
    assert "prefers-reduced-motion" in CSS
    assert "@media (max-width: 720px)" in CSS


@pytest.mark.parametrize("name", ["app.js", "api.js", "store.js", "views.js"])
def test_every_module_is_an_es_module_without_a_bundler(name: str) -> None:
    source = SCRIPTS[name]

    assert "require(" not in source
    if name != "store.js":
        assert "export " in source or "import " in source
