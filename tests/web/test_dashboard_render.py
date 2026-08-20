from importlib import resources


def _asset(name: str) -> str:
    return resources.files("polytrading.web.assets").joinpath(name).read_text(encoding="utf-8")


def test_dashboard_html_declares_paper_positions_section() -> None:
    html = _asset("index.html")
    assert 'id="paper-positions"' in html
    assert 'id="paper-stat-tiles"' in html
    assert 'id="paper-position-cards"' in html
    assert 'href="#paper-positions"' in html


def test_dashboard_client_renders_paper_positions_with_status_badges_and_sparkline() -> None:
    javascript = _asset("app.js")

    assert "function renderPaperPositions(snapshot)" in javascript
    assert "renderPaperPositions(snapshot);" in javascript
    assert "nodes.paperStatTiles" in javascript
    assert "nodes.paperPositionCards" in javascript
    assert "snapshot.paper_position_rows" in javascript
    assert "!Array.isArray(snapshot.paper_position_rows)" in javascript
    assert 'OPEN: "paper_open"' in javascript
    assert 'CLOSED_REGIME_REVERSED: "paper_regime_reversed"' in javascript
    assert 'CLOSED_MAX_HORIZON_REACHED: "paper_max_horizon"' in javascript
    assert 'CLOSED_OPERATOR_CLOSED: "paper_operator_closed"' in javascript
    assert "function renderSparkline(points, currentValue)" in javascript
    assert '"baseline"' in javascript
    assert '"trend"' in javascript
    assert "endpoint-good" in javascript
    assert "endpoint-bad" in javascript
    assert "createElementNS" in javascript


def test_dashboard_paper_positions_css_maps_status_tones_and_sparkline_uses_tokens() -> None:
    css = _asset("app.css")

    assert '[data-tone="paper_open"] { --tone: var(--cyan); }' in css
    assert '[data-tone="paper_regime_reversed"] { --tone: var(--amber); }' in css
    assert '[data-tone="paper_max_horizon"] { --tone: var(--info); }' in css
    assert '[data-tone="paper_operator_closed"] { --tone: var(--muted); }' in css
    assert ".paper-sparkline" in css
