# Local Operator Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a loopback-only, read-only web console that presents one point-in-time view of collection health, stored market evidence, carry readiness, evidence counts, and safe CLI recipes.

**Architecture:** A strict dashboard response model and builder compose existing point-in-time store methods and auditors at one captured UTC `as_of`. A standard-library HTTP server opens DuckDB read-only for each API refresh and serves packaged first-party HTML, CSS, and JavaScript. The server is GET-only, has no command or mutation surface, and binds only to `127.0.0.1`.

**Tech Stack:** Python 3.12–3.14, Pydantic 2.13.4, DuckDB 1.5.4, `http.server`, vanilla HTML/CSS/JavaScript, pytest 9.1.1, Ruff 0.15.22.

## Global Constraints

- Add no runtime dependency and no external browser resource.
- Capture one aware UTC `as_of` for every snapshot and use it for every lookup and audit.
- Open DuckDB with `read_only=True` for each API refresh; never migrate or mutate it.
- Bind only to `127.0.0.1`; expose no `--host` option.
- Serve only GET `/`, `/assets/app.css`, `/assets/app.js`, `/api/v1/dashboard`, and `/healthz`.
- Reject every non-GET method, unknown route, nonempty API query string, and non-loopback Host.
- Import no adapter, HTTP client, credential, wallet, signer, subprocess, scheduler, or execution module from `polytrading.web`.
- Present research evidence, never profit, recommendation, order, or allocation advice.
- Preserve Decimal values as JSON strings and UTC timestamps ending in `Z`.
- Render database-derived content with `textContent`, never `innerHTML`.
- Keep total repository coverage at or above 90%.

---

### Task 1: Point-in-time storage reads

**Files:**
- Modify: `src/polytrading/storage/store.py`
- Modify: `tests/storage/test_store.py`

**Interfaces:**
- Consumes: `FundingCollectionCycle`, `normalize_utc_timestamp`, and the seven evidence tables.
- Produces: `latest_funding_collection_cycle_as_of(as_of: datetime) -> FundingCollectionCycle | None`.
- Produces: `evidence_counts_as_of(as_of: datetime) -> dict[str, int]` with seven fixed keys.

- [ ] **Step 1: Write failing latest-cycle and count tests**

Persist one eligible and one future funding cycle plus pre/post-cutoff instrument records:

```python
def test_latest_funding_cycle_and_counts_are_point_in_time(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "dashboard.duckdb")
    old = funding_cycle(HOUR, FundingCycleStatus.COMPLETE, cycle_int=201)
    future = funding_cycle(HOUR + timedelta(hours=1), FundingCycleStatus.DEGRADED, cycle_int=202)
    store.append_funding_collection_cycle(old)
    store.append_funding_collection_cycle(future)
    store.append_instrument(instrument_spec(observed_at=NOW - timedelta(minutes=1)))
    store.append_instrument(instrument_spec(
        instrument_id="bybit:ETHUSDT", symbol="ETHUSDT", asset=Asset.ETH,
        observed_at=NOW + timedelta(minutes=1),
    ))

    assert store.latest_funding_collection_cycle_as_of(NOW) == old
    assert store.evidence_counts_as_of(NOW)["instrument_specs"] == 1
    assert store.evidence_counts_as_of(NOW)["funding_collection_cycles"] == 1
    store.close()
```

Add an empty-store test for `None`, the exact seven keys, and zero values. Add naive timestamp tests.

- [ ] **Step 2: Run tests and observe missing methods**

```bash
.venv/bin/python -m pytest tests/storage/test_store.py -q
```

Expected: FAIL because both methods are absent.

- [ ] **Step 3: Implement fixed parameterized reads**

```python
def latest_funding_collection_cycle_as_of(
    self, as_of: datetime
) -> FundingCollectionCycle | None:
    normalized = normalize_utc_timestamp(as_of)
    row = self._connection.execute(
        """
        SELECT CAST(record_json AS VARCHAR)
        FROM funding_collection_cycles
        WHERE request_completed_at <= ?
        ORDER BY request_completed_at DESC, cycle_end DESC, cycle_id
        LIMIT 1
        """,
        [normalized],
    ).fetchone()
    return None if row is None else FundingCollectionCycle.model_validate_json(row[0])
```

```python
def evidence_counts_as_of(self, as_of: datetime) -> dict[str, int]:
    normalized = normalize_utc_timestamp(as_of)
    queries = (
        ("raw_envelopes", "SELECT count(*) FROM raw_envelopes WHERE observed_at <= ?"),
        ("instrument_specs", "SELECT count(*) FROM instrument_specs WHERE observed_at <= ?"),
        ("funding_observations", "SELECT count(*) FROM funding_observations WHERE observed_at <= ?"),
        ("market_snapshots", "SELECT count(*) FROM market_snapshots WHERE observed_at <= ?"),
        ("book_snapshots", "SELECT count(*) FROM book_snapshots WHERE observed_at <= ?"),
        ("book_collection_cycles", "SELECT count(*) FROM book_collection_cycles WHERE request_completed_at <= ?"),
        ("funding_collection_cycles", "SELECT count(*) FROM funding_collection_cycles WHERE request_completed_at <= ?"),
    )
    return {
        name: int(self._connection.execute(sql, [normalized]).fetchone()[0])
        for name, sql in queries
    }
```

- [ ] **Step 4: Run storage and regression tests**

```bash
.venv/bin/python -m pytest tests/storage/test_store.py -q
.venv/bin/python -m pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/polytrading/storage/store.py tests/storage/test_store.py
git commit -m "feat(web): expose point-in-time dashboard reads"
```

---

### Task 2: Snapshot models and builder

**Files:**
- Create: `src/polytrading/web/__init__.py`
- Create: `src/polytrading/web/models.py`
- Create: `src/polytrading/web/dashboard.py`
- Create: `tests/web/__init__.py`
- Create: `tests/web/test_models.py`
- Create: `tests/web/test_dashboard.py`

**Interfaces:**
- Consumes: Task 1 reads, existing latest-record reads, `FundingCollectionHealthAuditor`, `CarryAuditor`.
- Produces: strict `DashboardSnapshot` and nested models.
- Produces: `DashboardBuilder(store: DuckDBStore, database_path: Path).build(as_of: datetime) -> DashboardSnapshot`.
- Produces: `render_dashboard_json(snapshot: DashboardSnapshot) -> bytes`.

- [ ] **Step 1: Write failing model tests**

Require canonical venue/asset coverage and coherent nullable evidence groups:

```python
EXPECTED_PAIRS = tuple(
    (venue, asset)
    for venue in (Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX)
    for asset in (Asset.BTC, Asset.ETH, Asset.SOL)
)

def test_snapshot_requires_every_market_pair_in_canonical_order() -> None:
    values = empty_snapshot_values()
    assert tuple((row.venue, row.asset) for row in DashboardSnapshot(**values).markets) == EXPECTED_PAIRS
    values["markets"] = tuple(reversed(values["markets"]))
    with pytest.raises(ValidationError, match="markets must cover"):
        DashboardSnapshot(**values)
```

Also prove available books require positive `bid < ask`, nonnegative spread, and both timestamps;
unavailable books require every book field to be `None`. Require exact evidence-count fields.

- [ ] **Step 2: Run tests and observe import failure**

```bash
.venv/bin/python -m pytest tests/web/test_models.py -q
```

- [ ] **Step 3: Implement strict models**

Define:

```python
RESEARCH_WARNING = "Research only — no trading authority."

class MarketEvidenceRow(StrictRecord):
    schema_version: Literal[1]
    venue: Venue
    asset: Asset
    symbol: str
    instrument_observed_at: datetime | None
    funding_rate: Decimal | None
    funding_interval_hours: Decimal | None
    funding_effective_at: datetime | None
    funding_observed_at: datetime | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread_bps: Decimal | None
    book_effective_at: datetime | None
    book_observed_at: datetime | None

class CarryEvidenceRow(StrictRecord):
    schema_version: Literal[1]
    asset: Asset
    status: AuditStatus
    funding_ready: bool
    book_ready: bool
    hourly_spread: Decimal | None
    reason_codes: tuple[str, ...]

class FundingCycleSummary(StrictRecord):
    schema_version: Literal[1]
    cycle_id: UUID
    cycle_end: datetime
    request_completed_at: datetime
    status: FundingCycleStatus

class BookCycleSummary(StrictRecord):
    schema_version: Literal[1]
    cycle_id: UUID
    request_completed_at: datetime
    status: Literal["complete", "failed", "skew_exceeds_research_target"]
    max_effective_skew_ms: Decimal

class EvidenceCounts(StrictRecord):
    raw_envelopes: int = Field(ge=0)
    instrument_specs: int = Field(ge=0)
    funding_observations: int = Field(ge=0)
    market_snapshots: int = Field(ge=0)
    book_snapshots: int = Field(ge=0)
    book_collection_cycles: int = Field(ge=0)
    funding_collection_cycles: int = Field(ge=0)

class OperationRecipes(StrictRecord):
    collect_public: str
    collect_books_once: str
    collect_current_funding: str
    inspect_funding_health: str

class DashboardSnapshot(StrictRecord):
    schema_version: Literal[1]
    as_of: datetime
    database_name: str
    warning: Literal["Research only — no trading authority."]
    funding_health: FundingCollectionHealthReport
    latest_funding_cycle: FundingCycleSummary | None
    latest_book_cycle: BookCycleSummary | None
    markets: tuple[MarketEvidenceRow, ...]
    carry_rows: tuple[CarryEvidenceRow, ...]
    evidence_counts: EvidenceCounts
    operation_recipes: OperationRecipes
```

Add UTC, canonical-order, sorted-unique reasons, basename-only name, and nullable-group validators.

- [ ] **Step 4: Run model tests until green**

```bash
.venv/bin/python -m pytest tests/web/test_models.py -q
```

- [ ] **Step 5: Write failing builder tests**

For an empty migrated DB assert 24 missing health boundaries, nine unavailable market rows, three
`INSUFFICIENT_DATA` carry rows, nullable cycles, and zero counts. For a populated DB seed pre/post
cutoff evidence and assert future records are excluded, dYdX uses `BTC-USD`, the native funding
rate is unchanged, and spread basis points equal:

```python
(ask - bid) / ((ask + bid) / Decimal(2)) * Decimal(10_000)
```

Use a spy store to assert every lookup and audit receives the one identical normalized `as_of`.
Assert recipes contain `shlex.quote(str(database_path))` for a path with spaces and a single quote.

- [ ] **Step 6: Run builder tests and observe failure**

```bash
.venv/bin/python -m pytest tests/web/test_dashboard.py -q
```

- [ ] **Step 7: Implement composition and rendering**

Use fixed order and symbols:

```python
_VENUES = (Venue.BYBIT, Venue.HYPERLIQUID, Venue.DYDX)
_ASSETS = (Asset.BTC, Asset.ETH, Asset.SOL)

def _symbol(venue: Venue, asset: Asset) -> str:
    if venue is Venue.BYBIT:
        return f"{asset.value}USDT"
    if venue is Venue.DYDX:
        return f"{asset.value}-USD"
    return asset.value
```

Health uses 24 hours. Carry limits match CLI: seven-day instrument/funding age, 30-second book age,
one-second book-cycle skew. Build shell recipes with `shlex.quote`. Render datetimes, Decimals,
UUIDs, and Enums recursively and encode canonical UTF-8 JSON.

- [ ] **Step 8: Run focused and regression tests**

```bash
.venv/bin/python -m pytest tests/web/test_models.py tests/web/test_dashboard.py -q
.venv/bin/python -m pytest -q
```

- [ ] **Step 9: Commit**

```bash
git add src/polytrading/web tests/web
git commit -m "feat(web): build read-only evidence snapshots"
```

---

### Task 3: GET-only local server and packaged interface

**Files:**
- Create: `src/polytrading/web/server.py`
- Create: `src/polytrading/web/assets/__init__.py`
- Create: `src/polytrading/web/assets/index.html`
- Create: `src/polytrading/web/assets/app.css`
- Create: `src/polytrading/web/assets/app.js`
- Modify: `pyproject.toml`
- Create: `tests/web/test_server.py`
- Create: `tests/web/test_assets.py`

**Interfaces:**
- Consumes: `DashboardBuilder`, `render_dashboard_json`, `DuckDBStore(read_only=True)`, `importlib.resources.files`.
- Produces: `validate_dashboard_database(path: Path) -> None`.
- Produces: `DashboardApplication(database_path: Path, clock: Callable[[], datetime]).respond(method: str, target: str, host: str) -> WebResponse`.
- Produces: `serve_dashboard(database_path: Path, port: int, *, clock: Callable[[], datetime] = utc_now) -> None`.

- [ ] **Step 1: Write failing routing and security tests**

Test the pure application without a socket:

```python
response = app.respond("GET", "/api/v1/dashboard", "127.0.0.1:8787")
assert response.status == HTTPStatus.OK
assert response.content_type == "application/json; charset=utf-8"
assert json.loads(response.body)["as_of"] == "2026-08-13T12:00:00Z"
assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
assert response.headers["X-Content-Type-Options"] == "nosniff"
assert response.headers["Referrer-Policy"] == "no-referrer"
assert response.headers["Cache-Control"] == "no-store"
```

Cover all five routes; `POST`/`PUT`/`DELETE` returning 405 with `Allow: GET`; unknown routes; API
query rejection; and bad Hosts `evil.example`, `127.0.0.2`, and empty. Accept `localhost` and
`127.0.0.1`, with optional decimal ports.

- [ ] **Step 2: Run and observe import failure**

```bash
.venv/bin/python -m pytest tests/web/test_server.py -q
```

- [ ] **Step 3: Implement the pure app and HTTP adapter**

Use an immutable response and fixed asset map:

```python
@dataclass(frozen=True)
class WebResponse:
    status: HTTPStatus
    content_type: str
    body: bytes
    headers: Mapping[str, str]

_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
```

For the API, capture one normalized clock value, open the store read-only, build and serialize, and
close in `finally`. Map expected database availability errors to stable 503
`DATABASE_UNAVAILABLE`; log unexpected exceptions server-side and return 500 `INTERNAL_ERROR`
without details.

Adapt with a private `BaseHTTPRequestHandler`. `serve_dashboard` uses
`HTTPServer(("127.0.0.1", port), handler)`, prints the exact local URL, runs `serve_forever`, closes
on exit, and treats `KeyboardInterrupt` as normal shutdown. Emit `Content-Length` on every response.

- [ ] **Step 4: Run server tests until green**

```bash
.venv/bin/python -m pytest tests/web/test_server.py -q
```

- [ ] **Step 5: Write failing asset and packaging tests**

Use `importlib.resources.files("polytrading.web.assets")` and assert all assets exist. Parse HTML and
require a skip link, header, main, overview, markets table, research section, operations section,
refresh button, live status, and module script. Assert JavaScript contains `textContent` and
`AbortController`, while combined assets exclude:

```python
FORBIDDEN = (
    "innerHTML", "http://", "https://", "<form", "password", "api-key",
    "place-order", "execute-trade",
)
```

- [ ] **Step 6: Run and observe missing resources**

```bash
.venv/bin/python -m pytest tests/web/test_assets.py -q
```

- [ ] **Step 7: Build the semantic document**

Use stable empty targets; never interpolate database content into HTML:

```html
<a class="skip-link" href="#main">Skip to dashboard</a>
<header class="topbar">
  <p class="eyebrow">POLYTRADING // EVIDENCE CONSOLE</p>
  <h1>Market research, without hidden authority.</h1>
  <span class="badge">READ ONLY</span>
  <p id="refresh-status" role="status" aria-live="polite">Loading evidence…</p>
  <button id="refresh" type="button">Refresh snapshot</button>
</header>
<main id="main">
  <section id="overview" aria-labelledby="overview-title"></section>
  <section id="markets" aria-labelledby="markets-title"><table><tbody id="market-rows"></tbody></table></section>
  <section id="research" aria-labelledby="research-title"></section>
  <section id="operations" aria-labelledby="operations-title"></section>
</main>
<script type="module" src="/assets/app.js"></script>
```

Include fixed warning copy and `noscript` guidance.

- [ ] **Step 8: Implement responsive CSS and safe JavaScript**

Use a restrained dark operations aesthetic: system fonts, off-black/navy surfaces, warm-white text,
cyan data, amber degraded, and coral critical. Use CSS grid for cards, an overflow wrapper for the
table, a 24-cell boundary strip, visible focus rings, a `max-width: 720px` stacked layout, and
`prefers-reduced-motion`.

JavaScript holds only `lastSnapshot`, `refreshing`, and one timer. Fetch the API with a ten-second
`AbortController`, validate the top-level shape, create elements, assign only `.textContent`, and
use `replaceChildren`. On failure retain the current DOM and mark the refresh state stale. Schedule
the next 15-second poll only after completion. Copy buttons call `navigator.clipboard.writeText`
only from a click handler.

- [ ] **Step 9: Package assets and run web/package tests**

Add beside the schema package-data entry:

```toml
"polytrading.web.assets" = ["*.html", "*.css", "*.js"]
```

Run:

```bash
.venv/bin/python -m pytest tests/web -q
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
unzip -l dist/polytrading-0.1.0-py3-none-any.whl | rg 'web/assets/(index.html|app.css|app.js)'
```

Expected: tests pass and the wheel lists the three assets.

- [ ] **Step 10: Commit**

```bash
git add pyproject.toml src/polytrading/web tests/web
git commit -m "feat(web): serve the local evidence console"
```

---

### Task 4: CLI integration and documentation

**Files:**
- Modify: `src/polytrading/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 3 database validation and server runner.
- Produces: `polytrading dashboard --db PATH [--port 8787]`.

- [ ] **Step 1: Write failing parser and handoff tests**

Require `--db`, default port 8787, range 1–65535, and no host or authority arguments:

```python
def test_dashboard_validates_then_serves_selected_database(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "research data.duckdb"
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(cli, "validate_dashboard_database", lambda value: calls.append(("validate", value)))
    monkeypatch.setattr(cli, "serve_dashboard", lambda value, port: calls.append(("serve", value, port)))

    assert cli.main(["dashboard", "--db", str(path), "--port", "9000"]) == 0
    assert calls == [("validate", path), ("serve", path, 9000)]
```

Reject 0, 65536, and nonintegers. Assert no host, token, user, password, account, order, or execution
argument exists.

- [ ] **Step 2: Run and observe unknown command**

```bash
.venv/bin/python -m pytest tests/test_cli.py -q
```

- [ ] **Step 3: Add parser and dispatch**

```python
dashboard = commands.add_parser("dashboard", help="serve the local read-only evidence console")
dashboard.add_argument("--db", required=True, type=Path)
dashboard.add_argument("--port", type=_dashboard_port, default=8787)
```

Dispatch before branches that assume `collect_command` exists:

```python
if arguments.command == "dashboard":
    validate_dashboard_database(arguments.db)
    serve_dashboard(arguments.db, arguments.port)
    return 0
```

`_dashboard_port` parses base-ten input using `int`, raises `argparse.ArgumentTypeError` for invalid
syntax, and requires `1 <= port <= 65535`.

- [ ] **Step 4: Run CLI and regression tests**

```bash
.venv/bin/python -m pytest tests/test_cli.py -q
.venv/bin/python -m pytest -q
```

- [ ] **Step 5: Document operation and boundary**

Add:

```bash
.venv/bin/polytrading dashboard \
  --db var/forward.duckdb \
  --port 8787
```

Document `http://127.0.0.1:8787`, existing/current database requirement, read-only refreshes,
temporary stale state during lock conflicts, copy-only recipes, and no remote/authenticated mode or
trading authority.

- [ ] **Step 6: Commit**

```bash
git add src/polytrading/cli.py tests/test_cli.py README.md
git commit -m "feat(cli): expose the local evidence dashboard"
```

---

### Task 5: Browser, authority, and release verification

**Files:**
- Modify only files implicated by verified defects.

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: a verified local UI milestone with no added authority.

- [ ] **Step 1: Run formatting, lint, tests, and coverage**

```bash
.venv/bin/ruff format .
.venv/bin/ruff check .
git diff --check
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing --cov-fail-under=90 -q
```

- [ ] **Step 2: Scan for forbidden authority and resources**

```bash
rg -n -i 'api.?key|private.?key|wallet|balance|position|place.?order|submit.?order|cancel.?order|withdraw|transfer|authenticate|sign|subprocess|os\.system|https?://' src/polytrading/web
rg -n 'innerHTML|outerHTML|insertAdjacentHTML|document\.write' src/polytrading/web/assets
```

Expected: no matches. Confirm server source contains `127.0.0.1`, `read_only=True`, and only the five
designed paths.

- [ ] **Step 3: Create a temporary representative database**

```bash
tmp_dir=$(mktemp -d /private/tmp/polytrading-dashboard.XXXXXX)
.venv/bin/polytrading replay --input tests/fixtures/replay/public_snapshot.jsonl --db "$tmp_dir/dashboard.duckdb"
```

Add a deterministic funding-cycle history through project APIs; never add the generated DB to Git.

- [ ] **Step 4: Run loopback HTTP smoke**

Start on an available local port; request all five routes and the API; assert status, content type,
security headers, schema 1, nine markets, and no mutation routes; stop cleanly.

- [ ] **Step 5: Verify desktop and mobile browser layouts**

At 1440×1000 and approximately 390×844 verify no page overflow; all sections render; status is not
color-only; keyboard focus is visible; refresh advances `as_of`; stale errors retain last data; and
the successful path has no console errors. Capture uncommitted screenshots for self-review.

- [ ] **Step 6: Self-review and fix verified defects test-first**

Review the full diff against the design, focusing on point-in-time filters, close paths, Host parsing,
query rejection, content length, caching, escaping, accessible labels, timer duplication, and honest
wording. Add a failing reproduction before every correction.

- [ ] **Step 7: Re-run final verification**

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
git diff --check
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest --cov=polytrading --cov-report=term --cov-fail-under=90 -q
git status --short --branch
```

- [ ] **Step 8: Integrate locally and verify merged tree**

Fast-forward the verified feature branch into local `main`, run the full suite against merged source,
remove the worktree, delete the merged branch, and confirm clean `main`. Do not push, deploy, expose a
remote listener, or modify a scheduler.
