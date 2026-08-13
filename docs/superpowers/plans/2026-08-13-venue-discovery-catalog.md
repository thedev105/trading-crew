# Venue Discovery Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic multi-dossier catalog that selects the source-backed Lighter/dYdX core-perpetual pair for further research while preserving the rejected Hyperliquid/dYdX result and all no-trading boundaries.

**Architecture:** Keep research-only venue identities in the dossier domain instead of widening the market-data adapter enum. Load two explicitly named package resources, evaluate each with the existing fail-closed logic, then rank their immutable reports in a separate discovery model. Expose that report through offline CLI renderers and the point-in-time dashboard without adding network, account, or execution behavior.

**Tech Stack:** Python 3.12–3.14, Pydantic v2 strict/frozen models, `importlib.resources`, argparse, pytest, vanilla JavaScript/CSS, Hatchling wheel packaging, Ruff.

## Global Constraints

- Runtime and development dependencies remain exactly pinned in `pyproject.toml`; add no dependency.
- All source URLs use HTTPS official domains and every stored excerpt hash is SHA-256 over its exact UTF-8 bytes.
- Runtime dossier and discovery commands make no network request and require no database.
- Preserve `carry dossier` default output for `hyperliquid-dydx-core-v1`.
- Lighter/dYdX must evaluate to exactly 4 matched, 10 model-required, 0 blocking, and 0 missing-evidence checks.
- Selection never authorizes paper or live trading; every report keeps `activation_status="not_authorized"`.
- The dashboard remains loopback-only, GET/HEAD-only, read-only, and inserts external evidence with `textContent`.
- Do not add credentials, keys, wallets, accounts, balances, positions, signing, transfers, deposits, withdrawals, order placement, cancellation, allocation, or execution.
- Do not estimate profit, annualized return, slippage, fee-adjusted carry, or funding persistence in this increment.

---

## File structure

- Modify `src/polytrading/carry/dossier_models.py`: own research-venue identity and existing dossier/report validation.
- Modify `src/polytrading/carry/dossier.py`: own explicit package-resource catalog loading and individual dossier evaluation.
- Create `src/polytrading/carry/discovery_models.py`: own discovery counts and immutable ranked-report schema.
- Create `src/polytrading/carry/discovery.py`: own deterministic report ranking and selection only.
- Create `src/polytrading/carry/discovery_report.py`: own text/JSON discovery serialization.
- Create `src/polytrading/carry/dossiers/lighter-dydx-core-v1.json`: own the immutable official-source artifact.
- Modify `src/polytrading/cli.py`: parse and dispatch `carry dossier --id` and `carry discovery`.
- Modify `src/polytrading/web/models.py`: add the optional typed discovery report and point-in-time invariants.
- Modify `src/polytrading/web/dashboard.py`: build discovery from the historical artifact subset.
- Modify `src/polytrading/web/assets/index.html`, `app.js`, and `app.css`: render ranked candidates and the selected check matrix.
- Modify `README.md`: document commands, interpretation, evidence gaps, and next gate.
- Modify focused tests under `tests/carry`, `tests/web`, `tests/test_cli.py`, and `tests/test_package.py`.

---

### Task 1: Isolate research venues and load a two-artifact catalog

**Files:**
- Modify: `src/polytrading/carry/dossier_models.py`
- Modify: `src/polytrading/carry/dossier.py`
- Create: `src/polytrading/carry/dossiers/lighter-dydx-core-v1.json`
- Modify: `tests/carry/test_dossier_models.py`
- Modify: `tests/carry/test_dossier_resource.py`
- Modify: `tests/test_package.py`

**Interfaces:**
- Produces: `ResearchVenue(StrEnum)` with `HYPERLIQUID`, `DYDX`, and `LIGHTER`.
- Produces: `BUNDLED_DOSSIER_IDS: tuple[str, ...]`.
- Produces: `load_bundled_dossiers() -> tuple[ContractCompatibilityDossier, ...]`.
- Preserves: `load_bundled_dossier(dossier_id: str = "hyperliquid-dydx-core-v1") -> ContractCompatibilityDossier`.
- Later tasks consume evaluated reports whose venue fields are `ResearchVenue`.

- [ ] **Step 1: Write failing research-venue and catalog tests**

Update test factories to use `ResearchVenue`, assert `Venue` still has exactly the three adapter-backed members, accept only the documented Lighter domains, and add these resource assertions:

```python
def test_bundled_catalog_preserves_rejected_pair_and_adds_complete_candidate() -> None:
    dossiers = load_bundled_dossiers()
    assert tuple(item.dossier_id for item in dossiers) == (
        "hyperliquid-dydx-core-v1",
        "lighter-dydx-core-v1",
    )

    legacy, candidate = dossiers
    assert evaluate_dossier(legacy).status is DossierStatus.INELIGIBLE
    report = evaluate_dossier(candidate)
    assert candidate.left_venue is ResearchVenue.LIGHTER
    assert candidate.right_venue is ResearchVenue.DYDX
    assert report.status is DossierStatus.MODEL_REQUIRED
    assert report.counts.model_dump() == {
        "matched": 4,
        "blocking": 0,
        "model_required": 10,
        "missing_evidence": 0,
    }
```

Also test unknown IDs, duplicate dossier IDs, duplicate directed pairs, invalid UTF-8 naming the failing stable ID, complete excerpt hashes, complete citations, and wheel members equal exactly the two expected JSON paths.

- [ ] **Step 2: Run focused tests and confirm they fail for missing types/resources/functions**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_dossier_models.py tests/carry/test_dossier_resource.py tests/test_package.py -q
```

Expected: collection or assertion failures because `ResearchVenue`, catalog loading, and the Lighter artifact do not yet exist.

- [ ] **Step 3: Add `ResearchVenue` without widening adapter support**

In `dossier_models.py`, replace dossier-only uses of `Venue` with:

```python
class ResearchVenue(StrEnum):
    HYPERLIQUID = "hyperliquid"
    DYDX = "dydx"
    LIGHTER = "lighter"
```

Define `_OFFICIAL_SOURCE_PREFIXES` exhaustively for these members. Lighter accepts
`https://docs.lighter.xyz/`, `https://apidocs.lighter.xyz/`, `https://lighter.xyz/`, and
`https://assets.lighter.xyz/`. Update `DossierSource.venue`, dossier venue fields, and report venue
fields to `ResearchVenue`. Do not alter `polytrading.domain.models.Venue`.

- [ ] **Step 4: Implement explicit catalog loading and validation**

In `dossier.py`, use:

```python
BUNDLED_DOSSIER_IDS = (
    "hyperliquid-dydx-core-v1",
    "lighter-dydx-core-v1",
)

def load_bundled_dossier(
    dossier_id: str = "hyperliquid-dydx-core-v1",
) -> ContractCompatibilityDossier:
    if dossier_id not in BUNDLED_DOSSIER_IDS:
        raise ValueError(f"unknown bundled dossier: {dossier_id}")
    try:
        payload = (
            files("polytrading.carry.dossiers")
            .joinpath(f"{dossier_id}.json")
            .read_text(encoding="utf-8")
        )
        dossier = ContractCompatibilityDossier.model_validate_json(payload)
    except (OSError, UnicodeError, ValidationError) as error:
        raise ValueError(f"invalid bundled dossier: {dossier_id}") from error
    if dossier.dossier_id != dossier_id:
        raise ValueError(f"invalid bundled dossier: {dossier_id}")
    return dossier

def load_bundled_dossiers() -> tuple[ContractCompatibilityDossier, ...]:
    dossiers = tuple(load_bundled_dossier(item) for item in BUNDLED_DOSSIER_IDS)
    ids = tuple(item.dossier_id for item in dossiers)
    pairs = tuple((item.left_venue, item.right_venue) for item in dossiers)
    if len(set(ids)) != len(ids):
        raise ValueError("invalid bundled dossier catalog: duplicate dossier ID")
    if len(set(pairs)) != len(pairs):
        raise ValueError("invalid bundled dossier catalog: duplicate venue pair")
    return dossiers
```

Keep catalog IDs explicit; do not glob arbitrary package JSON.

- [ ] **Step 5: Add the immutable Lighter/dYdX artifact**

Use `observed_at="2026-08-13T16:23:08Z"`, core assets in BTC/ETH/SOL order, and 16 source
records: the eight Lighter and eight dYdX URLs listed in the design. Keep every excerpt under 25
words. Required Lighter excerpts include these exact page strings:

```text
Currently each deployed market has a funding period of 1 hour.
It is calculated based on the difference in USDC value between your average entry price and exit price.
Lighter uses a combination of oracles (Chainlink, Stork, Pyth) to determine the index price.
liquidation engine first cancels all of the open orders of the user.
the exchange initiates auto-deleveraging (ADL) for the bankrupt account’s positions.
Funding payments occur at each hour mark.
Standard Account (Default) — Trade for free.
The Company may suspend or terminate your access, without prior notice
```

Add the sequence-continuity source as a ninth Lighter record if needed so both order constraints
and failure domains cite direct evidence. Reuse the seven current dYdX technical excerpts from the
legacy artifact and add the geo-restriction excerpt from the official help page. Generate each
`excerpt_sha256` from the exact JSON string bytes; never hand-edit a hash.

Use these judgments in canonical order:

```python
(
    DossierJudgment.MATCHED,
    DossierJudgment.MATCHED,
    DossierJudgment.MATCHED,
    DossierJudgment.MODEL_REQUIRED,
    DossierJudgment.MODEL_REQUIRED,
    DossierJudgment.MODEL_REQUIRED,
    DossierJudgment.MODEL_REQUIRED,
    DossierJudgment.MATCHED,
    DossierJudgment.MODEL_REQUIRED,
    DossierJudgment.MODEL_REQUIRED,
    DossierJudgment.MODEL_REQUIRED,
    DossierJudgment.MODEL_REQUIRED,
    DossierJudgment.MODEL_REQUIRED,
    DossierJudgment.MODEL_REQUIRED,
)
```

- [ ] **Step 6: Run focused tests, format, and commit**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_dossier_models.py tests/carry/test_dossier_resource.py tests/test_package.py -q
.venv/bin/ruff check src/polytrading/carry tests/carry tests/test_package.py
.venv/bin/ruff format --check src/polytrading/carry tests/carry tests/test_package.py
```

Expected: all selected tests and checks pass.

Commit:

```bash
git add src/polytrading/carry/dossier_models.py src/polytrading/carry/dossier.py src/polytrading/carry/dossiers tests/carry/test_dossier_models.py tests/carry/test_dossier_resource.py tests/test_package.py
git commit -m "data(carry): add Lighter dYdX dossier catalog"
```

---

### Task 2: Evaluate and render deterministic venue discovery

**Files:**
- Create: `src/polytrading/carry/discovery_models.py`
- Create: `src/polytrading/carry/discovery.py`
- Create: `src/polytrading/carry/discovery_report.py`
- Create: `tests/carry/test_discovery.py`
- Create: `tests/carry/test_discovery_report.py`

**Interfaces:**
- Consumes: `ContractDossierReport`, `DossierStatus`, `evaluate_dossier()`.
- Produces: `DiscoveryStatusCounts`, `VenueDiscoveryReport`.
- Produces: `evaluate_discovery(reports: tuple[ContractDossierReport, ...]) -> VenueDiscoveryReport`.
- Produces: `render_discovery_text(report: VenueDiscoveryReport) -> str` and `render_discovery_json(report: VenueDiscoveryReport) -> str`.

- [ ] **Step 1: Write failing ranking, validation, and renderer tests**

Build reports from dossier test factories and verify this order:

```python
assert tuple(item.status for item in report.candidates) == (
    DossierStatus.COMPATIBLE,
    DossierStatus.MODEL_REQUIRED,
    DossierStatus.EVIDENCE_INCOMPLETE,
    DossierStatus.INELIGIBLE,
)
assert report.selected_dossier_id == compatible.dossier_id
assert report.selection_reason_code == "best_nonblocking_complete_evidence"
assert report.activation_status == "not_authorized"
```

Add tests for dossier-ID tie-breaking, duplicate IDs/pairs, all-rejected selection returning null
with `selection_reason_code="no_advanceable_candidate"`, selected reports never having blocking or
missing counts, observed time equal to the newest candidate, exact status counts, deterministic
JSON, and stable text rows for both bundled candidates.

- [ ] **Step 2: Run the new tests and confirm import failures**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_discovery.py tests/carry/test_discovery_report.py -q
```

Expected: test collection fails because discovery modules do not exist.

- [ ] **Step 3: Implement strict discovery models**

Define:

```python
class DiscoveryStatusCounts(StrictRecord):
    compatible: NonnegativeInt
    model_required: NonnegativeInt
    evidence_incomplete: NonnegativeInt
    ineligible: NonnegativeInt

class VenueDiscoveryReport(StrictRecord):
    schema_version: Literal[1]
    observed_at: datetime
    warning: Literal["Research only — no trading authority."]
    candidates: tuple[ContractDossierReport, ...]
    counts: DiscoveryStatusCounts
    selected_dossier_id: str | None
    selection_reason_code: Literal[
        "best_nonblocking_complete_evidence",
        "no_advanceable_candidate",
    ]
    activation_status: Literal["not_authorized"]
```

Validators require a nonempty unique catalog, newest observation equality, count equality, exact
status rank order with dossier-ID tie-break, and a selected ID resolving to a compatible or
model-required report with zero blocking and missing checks. A null selection requires no
advanceable report and the no-candidate reason.

- [ ] **Step 4: Implement venue-neutral ranking**

Use the fixed map:

```python
_STATUS_RANK = {
    DossierStatus.COMPATIBLE: 0,
    DossierStatus.MODEL_REQUIRED: 1,
    DossierStatus.EVIDENCE_INCOMPLETE: 2,
    DossierStatus.INELIGIBLE: 3,
}
```

Sort on `(_STATUS_RANK[item.status], item.dossier_id)`, count every status, and select the first
candidate at rank zero or one. Do not inspect source summaries, reason codes, venue names, or asset
symbols in the evaluator.

- [ ] **Step 5: Implement stable discovery renderers**

Share the existing JSON value conversion by moving it to a private focused helper only if this
avoids duplication without changing output. Text begins and ends exactly with:

```text
RESEARCH ONLY — NOT A TRADE RECOMMENDATION
selected=lighter-dydx-core-v1 | reason=best_nonblocking_complete_evidence | activation=not_authorized
...
Next gate: collect public Lighter evidence and model costs; no trading authority exists.
```

Each candidate row includes rank, dossier ID, pair, status, assets, all four judgment counts, and
primary reason or `none`.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_discovery.py tests/carry/test_discovery_report.py -q
.venv/bin/ruff check src/polytrading/carry tests/carry
.venv/bin/ruff format --check src/polytrading/carry tests/carry
```

Commit:

```bash
git add src/polytrading/carry/discovery.py src/polytrading/carry/discovery_models.py src/polytrading/carry/discovery_report.py tests/carry/test_discovery.py tests/carry/test_discovery_report.py
git commit -m "feat(carry): rank venue discovery dossiers"
```

---

### Task 3: Add database-free discovery CLI commands

**Files:**
- Modify: `src/polytrading/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: catalog loader, dossier evaluator, discovery evaluator, and all four renderers.
- Produces CLI: `carry dossier [--id ID] [--format text|json]`.
- Produces CLI: `carry discovery [--format text|json]`.

- [ ] **Step 1: Write failing parser and command tests**

Assert default dossier ID, explicit ID, new subcommand parsing, deterministic repeated output,
JSON selected ID and counts, text selected line, no `DuckDBStore`, no public HTTP client, unknown ID
exit two, and malformed catalog exit two with one sanitized stderr line.

Use a network guard:

```python
monkeypatch.setattr(
    cli,
    "make_public_http_client",
    lambda *args, **kwargs: pytest.fail("discovery CLI must stay offline"),
)
```

- [ ] **Step 2: Run focused CLI tests and confirm failures**

Run:

```bash
.venv/bin/python -m pytest tests/test_cli.py -k 'dossier or discovery' -q
```

Expected: parser and dispatch failures for missing `--id` and `discovery`.

- [ ] **Step 3: Implement explicit dispatch**

Add `dossier.add_argument("--id", default="hyperliquid-dydx-core-v1")` and a sibling `discovery`
parser with `--format`. Replace the carry fallthrough with explicit branches, then add:

```python
def _carry_dossier(arguments: argparse.Namespace) -> int:
    report = evaluate_dossier(load_bundled_dossier(arguments.id))
    renderer = render_dossier_json if arguments.format == "json" else render_dossier_text
    print(renderer(report))
    return 0

def _carry_discovery(arguments: argparse.Namespace) -> int:
    reports = tuple(evaluate_dossier(item) for item in load_bundled_dossiers())
    report = evaluate_discovery(reports)
    renderer = render_discovery_json if arguments.format == "json" else render_discovery_text
    print(renderer(report))
    return 0
```

- [ ] **Step 4: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_cli.py -k 'dossier or discovery' -q
.venv/bin/ruff check src/polytrading/cli.py tests/test_cli.py
.venv/bin/ruff format --check src/polytrading/cli.py tests/test_cli.py
```

Commit:

```bash
git add src/polytrading/cli.py tests/test_cli.py
git commit -m "feat(cli): report venue discovery catalog"
```

---

### Task 4: Add point-in-time discovery to the dashboard model and builder

**Files:**
- Modify: `src/polytrading/web/models.py`
- Modify: `src/polytrading/web/dashboard.py`
- Modify: `tests/web/test_models.py`
- Modify: `tests/web/test_dashboard.py`
- Modify: `tests/web/test_server.py`

**Interfaces:**
- Consumes: `load_bundled_dossiers()` and `evaluate_discovery()`.
- Produces: `DashboardSnapshot.venue_discovery: VenueDiscoveryReport | None`.
- Preserves: `DashboardSnapshot.compatibility_dossier` as legacy Hyperliquid/dYdX.

- [ ] **Step 1: Write failing snapshot and cutoff tests**

Test three exact cutoffs: before `2026-08-13T15:58:12Z`, between that timestamp and the new
artifact timestamp, and at/after `2026-08-13T16:23:08Z`. Expected behavior:

```python
assert before.compatibility_dossier is None
assert before.venue_discovery is None
assert between.compatibility_dossier.status is DossierStatus.INELIGIBLE
assert between.venue_discovery.selected_dossier_id is None
assert after.venue_discovery.selected_dossier_id == "lighter-dydx-core-v1"
```

Add model tests rejecting a discovery observation after `as_of`, mismatched legacy report, and a
selected dossier absent from candidates. Update server JSON assertions for both fields.

- [ ] **Step 2: Run focused web model/builder tests and confirm failures**

Run:

```bash
.venv/bin/python -m pytest tests/web/test_models.py tests/web/test_dashboard.py tests/web/test_server.py -q
```

Expected: failures for the absent snapshot field and single-resource loader.

- [ ] **Step 3: Implement historical catalog building**

Change `DashboardBuilder` injection to:

```python
dossier_catalog_loader: Callable[
    [], tuple[ContractCompatibilityDossier, ...]
] = load_bundled_dossiers
```

At build time, load once, filter `observed_at <= normalized_as_of`, evaluate the known subset, find
the legacy ID for `compatibility_dossier`, and set `venue_discovery` to null only when the subset is
empty. Never select or expose a future artifact.

- [ ] **Step 4: Add model invariants and serialization**

Add the optional field and require its observation not to follow `as_of`. When both fields exist,
require the legacy dossier ID to appear exactly once in `venue_discovery.candidates` and equal the
legacy field. Existing `_json_value` handles nested strict models without special cases.

- [ ] **Step 5: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/web/test_models.py tests/web/test_dashboard.py tests/web/test_server.py -q
.venv/bin/ruff check src/polytrading/web tests/web
.venv/bin/ruff format --check src/polytrading/web tests/web
```

Commit:

```bash
git add src/polytrading/web/models.py src/polytrading/web/dashboard.py tests/web/test_models.py tests/web/test_dashboard.py tests/web/test_server.py
git commit -m "feat(web): expose point-in-time venue discovery"
```

---

### Task 5: Render the selected candidate and ranked alternatives in the web UI

**Files:**
- Modify: `src/polytrading/web/assets/index.html`
- Modify: `src/polytrading/web/assets/app.js`
- Modify: `src/polytrading/web/assets/app.css`
- Modify: `tests/web/test_assets.py`

**Interfaces:**
- Consumes: dashboard JSON `venue_discovery` and retained `compatibility_dossier`.
- Produces DOM IDs: `discovery-summary`, `candidate-rows`, `dossier-rows`, `dossier-left-heading`, and `dossier-right-heading`.

- [ ] **Step 1: Write failing static asset tests**

Assert the HTML includes the new IDs and neutral copy, JavaScript reads `venue_discovery`, renders
candidate rows, finds the selected report, changes the two evidence column headings with
`textContent`, and validates both dashboard fields. Retain the security assertions that no
`innerHTML`, POST, WebSocket, EventSource, credential, account, or order control exists.

- [ ] **Step 2: Run asset tests and confirm failures**

Run:

```bash
.venv/bin/python -m pytest tests/web/test_assets.py -q
```

Expected: failures for absent discovery DOM and renderer text.

- [ ] **Step 3: Replace the single-pair section with discovery markup**

Use a selected-candidate summary grid, a compact candidate table with pair/status/counts/reason,
and a separate complete-check table. Keep the section number and navigation anchor stable. Change
pair-specific headings to spans whose text is populated from the selected report.

- [ ] **Step 4: Implement safe discovery rendering**

Use only the existing `element()` and `tableCell()` helpers. `renderDiscovery(snapshot)` must:

1. render unavailable state when `venue_discovery` is null;
2. render all ranked candidates and mark only `selected_dossier_id` as selected;
3. show an explicit no-advanceable-candidate state when selection is null;
4. find the selected full report and render all 14 checks;
5. write both venue headings through `textContent`; and
6. show `not authorized` and the public-evidence next gate.

- [ ] **Step 5: Add responsive candidate styles and run tests**

Reuse existing tones. Add `.candidate-table`, `.candidate-selected`, and selected-summary rules
without animation or color-only meaning. Run:

```bash
.venv/bin/python -m pytest tests/web/test_assets.py -q
```

- [ ] **Step 6: Commit**

```bash
git add src/polytrading/web/assets tests/web/test_assets.py
git commit -m "feat(web): render venue discovery ranking"
```

---

### Task 6: Document, verify, self-review, and integrate

**Files:**
- Modify: `README.md`
- Modify only if verification exposes a scoped defect: files already named above

**Interfaces:**
- Documents the stable CLI commands, exact interpretation, current candidate, and next gate.

- [ ] **Step 1: Update README and write documentation assertions if needed**

Replace the single-dossier section with a discovery section while preserving the Hyperliquid
rejection explanation. Include all three commands, the 4/10/0/0 result, Lighter Standard Account
fee/latency caution, jurisdiction-review boundary, and next Lighter public-adapter gate. State that
`model_required` is not a profit or activation decision.

- [ ] **Step 2: Run the full automated quality suite**

Run:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Expected: all tests pass, repository coverage remains at least the configured 90%, and Ruff emits
no diagnostics.

- [ ] **Step 3: Build and inspect the installed artifact**

Run:

```bash
.venv/bin/python -m build --wheel
.venv/bin/python -m pytest tests/test_package.py -q
```

Expected: the wheel contains exactly the two expected dossier JSON resources and each validates.

- [ ] **Step 4: Run offline CLI smokes**

Run:

```bash
.venv/bin/polytrading carry dossier --format text
.venv/bin/polytrading carry dossier --id lighter-dydx-core-v1 --format json
.venv/bin/polytrading carry discovery --format text
.venv/bin/polytrading carry discovery --format json
```

Expected: default remains Hyperliquid/dYdX `ineligible`; explicit Lighter/dYdX is
`model_required`; discovery selects `lighter-dydx-core-v1`; every command says no trading
authority and exits zero.

- [ ] **Step 5: Inspect the dashboard with a current-schema fixture database**

Start the loopback server only long enough to fetch `/api/v1/dashboard` and the root assets, or use
the existing server tests if a local port is unavailable. Confirm the JSON and rendered code expose
both candidates, 14 selected checks, the legacy blocker, and no mutation control.

- [ ] **Step 6: Perform a fresh diff and security-boundary review**

Run:

```bash
git diff --check
git status --short
git diff --stat main...HEAD
git diff main...HEAD -- src/polytrading tests README.md
```

Review for source/hash mistakes, future-evidence leakage, unsafe HTML insertion, accidental
`Venue.LIGHTER`, network-at-runtime behavior, account/trading surfaces, return claims, and unrelated
worktree changes. Fix scoped findings test-first and rerun affected plus full verification.

- [ ] **Step 7: Commit documentation and verification fixes**

```bash
git add README.md
git commit -m "docs: explain Lighter dYdX discovery gate"
```

- [ ] **Step 8: Finish the branch under the standing autonomous integration instruction**

Invoke `superpowers:verification-before-completion`, then `superpowers:finishing-a-development-branch`.
Because the operator has already instructed autonomous completion and review, choose local merge
after confirming the base branch and worktree state. Merge non-destructively, rerun the full
verification suite on `main`, and remove only the feature worktree created for this plan. Do not
touch the unrelated `feat/offline-ai-semantic-scout` worktree.
