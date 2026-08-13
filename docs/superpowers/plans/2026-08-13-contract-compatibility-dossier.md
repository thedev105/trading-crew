# Contract Compatibility Dossier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bundled, source-backed Hyperliquid/dYdX contract dossier whose deterministic CLI
and dashboard reports reject the current BTC/ETH/SOL pair because the approved initial strategy
excludes Hyperliquid's documented quanto structure.

**Architecture:** Strict Pydantic records validate short official-source excerpts, excerpt hashes,
canonical check coverage, and source references. A venue-agnostic evaluator applies fixed status
precedence, a package-resource loader supplies one immutable dossier, and the same report model feeds
the CLI and point-in-time dashboard. No runtime source fetching, database mutation, account access,
economic forecast, or order surface is introduced.

**Tech Stack:** Python 3.12-3.14, Pydantic 2.13.4, `importlib.resources`, argparse, vanilla
HTML/CSS/JavaScript, pytest 9.1.1, Ruff 0.15.22, setuptools package data.

## Global Constraints

- Scope remains read-only research evidence with no trading authority.
- Preserve only short exact excerpts; `excerpt_sha256` hashes the exact UTF-8 excerpt bytes and must
  never be represented as the full remote-page hash.
- Runtime CLI and dashboard code make no documentation network request.
- Status precedence is `blocking -> ineligible`, then `missing_evidence -> evidence_incomplete`,
  then `model_required -> model_required`, otherwise `compatible`.
- The canonical checks and their order are exactly those in Section 6.2 of the design.
- A dashboard `as_of` earlier than the dossier `observed_at` returns no dossier.
- JSON output is deterministic and the dashboard inserts dossier content with `textContent` only.
- No credentials, wallet code, account endpoints, balances, positions, transfers, signing, fills,
  orders, allocation, sizing, return forecast, or activation control.
- Use test-driven development: observe each focused test fail for the intended missing behavior
  before writing the implementation.

---

## File structure

- Create `src/polytrading/carry/dossier_models.py`: strict source, check, dossier, count, and report
  records plus the canonical enum/constants.
- Create `src/polytrading/carry/dossier.py`: package-resource loading and deterministic evaluation.
- Create `src/polytrading/carry/dossier_report.py`: stable text and JSON serialization.
- Create `src/polytrading/carry/dossiers/__init__.py`: package-resource namespace.
- Create `src/polytrading/carry/dossiers/hyperliquid-dydx-core-v1.json`: immutable official-source
  research artifact.
- Create `tests/carry/test_dossier_models.py`: strict model and evaluator behavior.
- Create `tests/carry/test_dossier_resource.py`: real artifact and package-resource checks.
- Create `tests/carry/test_dossier_report.py`: deterministic renderer checks.
- Modify `src/polytrading/cli.py` and `tests/test_cli.py`: expose `carry dossier`.
- Modify `src/polytrading/web/models.py`, `src/polytrading/web/dashboard.py`, and
  `tests/web/test_dashboard.py`: include the point-in-time dossier report.
- Modify `src/polytrading/web/assets/index.html`, `app.js`, `app.css`, and
  `tests/web/test_assets.py`: render the compatibility matrix safely.
- Modify `pyproject.toml` and `tests/test_package.py`: include and verify the JSON resource.
- Modify `README.md`: document the new research command, dashboard panel, rejection, and next gate.

---

### Task 1: Strict dossier model and deterministic evaluator

**Files:**

- Create: `src/polytrading/carry/dossier_models.py`
- Create: `src/polytrading/carry/dossier.py`
- Create: `tests/carry/test_dossier_models.py`

**Interfaces:**

- Produces `DossierCheckKind`, `DossierJudgment`, and `DossierStatus` string enums.
- Produces `CANONICAL_DOSSIER_CHECKS: tuple[DossierCheckKind, ...]`.
- Produces immutable `DossierSource`, `DossierCheck`, `ContractCompatibilityDossier`,
  `DossierJudgmentCounts`, and `ContractDossierReport` Pydantic records.
- Produces `evaluate_dossier(dossier: ContractCompatibilityDossier) -> ContractDossierReport`.

- [ ] **Step 1: Write failing source-integrity and model tests**

Create factories in `tests/carry/test_dossier_models.py` for a source, one check per canonical kind,
and a complete dossier. Add focused tests that assert:

```python
def test_source_hash_covers_the_exact_stored_excerpt() -> None:
    source = dossier_source(evidence_excerpt="short official statement")
    assert source.excerpt_sha256 == sha256(b"short official statement").hexdigest()


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"url": "http://example.test/source"}, "HTTPS"),
        ({"evidence_excerpt": "changed"}, "excerpt hash"),
        ({"observed_at": DOSSIER_AT + timedelta(seconds=1)}, "dossier observation"),
    ],
)
def test_dossier_rejects_invalid_source_evidence(change: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        contract_dossier(sources=(dossier_source(**change),))
```

Also test duplicate or noncanonical checks, an unknown source reference, an uncited source, same
left/right venue, duplicate assets, blank summaries/reasons, duplicate check source IDs, and a source
URL outside the current official Hyperliquid/dYdX prefixes.

- [ ] **Step 2: Run the model tests and verify the intended import failure**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_dossier_models.py -q
```

Expected: collection fails because `polytrading.carry.dossier_models` does not exist.

- [ ] **Step 3: Implement the strict records and validators**

Define the canonical check tuple in the design order. Use `StrictRecord`, `Venue`, `Asset`, and
`normalize_utc_timestamp`. Validate source IDs and reason codes with lowercase token regexes; require
HTTPS and these prefixes for the initial artifact:

```python
_OFFICIAL_SOURCE_PREFIXES = {
    Venue.HYPERLIQUID: ("https://hyperliquid.gitbook.io/hyperliquid-docs/",),
    Venue.DYDX: (
        "https://help.dydx.trade/",
        "https://github.com/dydxprotocol/",
        "https://docs.dydx.community/",
        "https://docs.dydx.xyz/",
    ),
}
```

In `ContractCompatibilityDossier` require distinct venues; unique assets in enum order; unique
source IDs; source timestamps no later than `observed_at`; exact check kind/order; all references
known; and every source cited. Hash `evidence_excerpt.encode("utf-8")` with SHA-256. The report
contains the warning literal `Research only — no trading authority.`, all sources and checks, and
`activation_status="not_authorized"`.

- [ ] **Step 4: Add failing evaluator precedence tests**

Parametrize complete dossiers whose judgment sequences yield:

```python
(
    ((DossierJudgment.MATCHED,) * 14, DossierStatus.COMPATIBLE, None),
    ((DossierJudgment.MODEL_REQUIRED,) + (DossierJudgment.MATCHED,) * 13,
     DossierStatus.MODEL_REQUIRED, "model_gap"),
    ((DossierJudgment.MISSING_EVIDENCE,) + (DossierJudgment.MODEL_REQUIRED,) * 13,
     DossierStatus.EVIDENCE_INCOMPLETE, "missing_gap"),
    ((DossierJudgment.MODEL_REQUIRED, DossierJudgment.BLOCKING)
     + (DossierJudgment.MISSING_EVIDENCE,) * 12,
     DossierStatus.INELIGIBLE, "blocking_gap"),
)
```

Assert that `primary_reason_code` comes from the first check with the winning judgment in canonical
order and that judgment counts total fourteen.

- [ ] **Step 5: Run the evaluator tests and verify they fail because evaluation is absent**

Run the same focused pytest command. Expected: model validation passes, but evaluator imports or
assertions fail.

- [ ] **Step 6: Implement minimal venue-agnostic evaluation**

Count each judgment, select status by the exact global precedence, select the first reason with the
winning judgment, and copy dossier facts into an immutable `ContractDossierReport`. Do not inspect a
venue name, asset, or reason-code string in the evaluator.

- [ ] **Step 7: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_dossier_models.py -q
.venv/bin/ruff check src/polytrading/carry/dossier_models.py src/polytrading/carry/dossier.py tests/carry/test_dossier_models.py
.venv/bin/ruff format --check src/polytrading/carry/dossier_models.py src/polytrading/carry/dossier.py tests/carry/test_dossier_models.py
```

Expected: all pass.

Commit:

```bash
git add src/polytrading/carry/dossier_models.py src/polytrading/carry/dossier.py tests/carry/test_dossier_models.py
git commit -m "feat(carry): evaluate contract dossiers"
```

---

### Task 2: Bundled Hyperliquid/dYdX evidence artifact

**Files:**

- Create: `src/polytrading/carry/dossiers/__init__.py`
- Create: `src/polytrading/carry/dossiers/hyperliquid-dydx-core-v1.json`
- Modify: `src/polytrading/carry/dossier.py`
- Create: `tests/carry/test_dossier_resource.py`

**Interfaces:**

- Produces `load_bundled_dossier() -> ContractCompatibilityDossier`.
- Produces the immutable dossier ID `hyperliquid-dydx-core-v1`, observed at
  `2026-08-13T12:00:00Z`, scoped to BTC, ETH, and SOL.

- [ ] **Step 1: Write failing loader and real-artifact tests**

Assert the loader returns the fixed ID, venues, assets, observation time, thirteen official-source
records, fourteen canonical checks, no orphan source, and a report with:

```python
assert report.status is DossierStatus.INELIGIBLE
assert report.primary_reason_code == "quanto_structure_excluded"
assert report.counts.blocking == 1
assert report.counts.missing_evidence >= 1
assert report.activation_status == "not_authorized"
```

Read the resource bytes twice and assert byte equality. Independently recompute every excerpt hash.

- [ ] **Step 2: Run the resource tests and verify missing-resource failure**

Run:

```bash
.venv/bin/python -m pytest tests/carry/test_dossier_resource.py -q
```

Expected: failure because the resource package/file and loader do not exist.

- [ ] **Step 3: Add the immutable JSON evidence**

Create thirteen source records from the official URLs named in the design, with a short exact
excerpt per URL and hashes computed from the stored UTF-8 excerpt. Create all fourteen checks with
these fixed judgments/reasons:

| Check | Judgment | Reason code |
|---|---|---|
| asset_and_quantity | matched | `base_quantity_semantics_match` |
| payoff_and_quote | blocking | `quanto_structure_excluded` |
| collateral_and_pnl | matched | `usdc_accounting_matches` |
| oracle_construction | model_required | `oracle_construction_differs` |
| mark_and_margin | model_required | `margin_reference_differs` |
| liquidation | model_required | `liquidation_mechanism_differs` |
| auto_deleveraging | model_required | `deleveraging_mechanism_differs` |
| funding_interval | matched | `hourly_funding_matches` |
| funding_formula | model_required | `funding_formula_differs` |
| funding_cap | model_required | `funding_caps_differ` |
| order_constraints | missing_evidence | `point_in_time_constraints_missing` |
| fee_schedule | missing_evidence | `effective_account_fees_missing` |
| venue_failure_domain | missing_evidence | `failure_domain_assessment_missing` |
| access_eligibility | missing_evidence | `eligibility_review_deferred` |

Summaries must distinguish documented mechanism facts from unresolved conclusions and must not
state that shared USDC makes the whole pair compatible.

- [ ] **Step 4: Implement package-resource loading**

Use:

```python
resource = files("polytrading.carry.dossiers").joinpath(
    "hyperliquid-dydx-core-v1.json"
)
return ContractCompatibilityDossier.model_validate_json(resource.read_text(encoding="utf-8"))
```

Wrap only `OSError` and Pydantic/JSON validation failures with a stable `ValueError` whose message
names the dossier ID but never suppresses the cause.

- [ ] **Step 5: Run focused tests and commit**

Run the model and resource test files plus Ruff for the touched Python. Expected: all pass.

Commit:

```bash
git add src/polytrading/carry/dossier.py src/polytrading/carry/dossiers tests/carry/test_dossier_resource.py
git commit -m "data(carry): add official contract dossier"
```

---

### Task 3: Deterministic CLI report

**Files:**

- Create: `src/polytrading/carry/dossier_report.py`
- Create: `tests/carry/test_dossier_report.py`
- Modify: `src/polytrading/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**

- Produces `render_dossier_json(report: ContractDossierReport) -> str`.
- Produces `render_dossier_text(report: ContractDossierReport) -> str`.
- Adds `.venv/bin/polytrading carry dossier --format {text,json}` with default `text`.

- [ ] **Step 1: Write failing renderer tests**

Assert JSON is byte-deterministic, sorted and indented; timestamps end in `Z`; enums are strings;
and `json.loads()` contains all source records and all fourteen checks. Assert text begins with:

```text
RESEARCH ONLY — NOT A TRADE RECOMMENDATION
status=ineligible | pair=hyperliquid->dydx | assets=BTC,ETH,SOL
primary_blocker=quanto_structure_excluded
```

Assert one stable row per canonical check and a final line
`No cost model or trading authority exists.`.

- [ ] **Step 2: Run report tests and verify the missing module failure**

Run the report test file. Expected: import failure for `dossier_report`.

- [ ] **Step 3: Implement deterministic renderers**

Follow the existing carry renderer's recursive JSON conversion, including UTC normalization and
enum conversion. In text, render fields with a fixed separator and use the stored left/right
summaries without wrapping-dependent semantics.

- [ ] **Step 4: Write failing CLI tests**

Test parser defaults, text and JSON output, no database creation or network client, and a monkeypatch
of `load_bundled_dossier` that raises `ValueError("invalid bundled dossier")`; assert exit two and
the existing `polytrading: error:` prefix. Ensure `carry audit` and `carry study` still dispatch.

- [ ] **Step 5: Run the focused CLI tests and observe dispatch failure**

Run only the new named tests in `tests/test_cli.py`. Expected: parser or dispatch assertions fail.

- [ ] **Step 6: Add parser and dispatch integration**

Add a third carry subcommand named `dossier`. Dispatch `audit`, `study`, and `dossier` explicitly;
do not retain a binary `audit else study` branch. `_carry_dossier()` loads, evaluates, renders, and
prints without opening a DuckDB store.

- [ ] **Step 7: Run focused tests and commit**

Run dossier model/resource/report tests and the full `tests/test_cli.py`; run Ruff check/format on
the touched files. Expected: all pass.

Commit:

```bash
git add src/polytrading/carry/dossier_report.py src/polytrading/cli.py tests/carry/test_dossier_report.py tests/test_cli.py
git commit -m "feat(cli): report contract compatibility dossier"
```

---

### Task 4: Point-in-time dashboard compatibility panel

**Files:**

- Modify: `src/polytrading/web/models.py`
- Modify: `src/polytrading/web/dashboard.py`
- Modify: `tests/web/test_dashboard.py`
- Modify: `src/polytrading/web/assets/index.html`
- Modify: `src/polytrading/web/assets/app.js`
- Modify: `src/polytrading/web/assets/app.css`
- Modify: `tests/web/test_assets.py`
- Modify: `tests/web/test_server.py`

**Interfaces:**

- Adds `compatibility_dossier: ContractDossierReport | None` to `DashboardSnapshot`.
- Adds `DashboardBuilder(..., dossier_loader=load_bundled_dossier)` as an injectable loader for
  deterministic boundary and error tests.
- Adds `renderDossier(snapshot)` in browser JavaScript.

- [ ] **Step 1: Write failing point-in-time dashboard model tests**

Inject a loader returning the real dossier. At `observed_at - 1 microsecond`, assert
`compatibility_dossier is None`; at exactly `observed_at`, assert `ineligible`, the primary blocker,
and fourteen checks. Assert JSON has the same boundary behavior. Add a model test rejecting a
dossier report observed after the dashboard `as_of`.

- [ ] **Step 2: Run dashboard tests and observe the missing field/injection failure**

Run `tests/web/test_dashboard.py`. Expected: constructor or snapshot-field assertions fail.

- [ ] **Step 3: Add report integration to model and builder**

Type the optional field directly as `ContractDossierReport | None`. The builder calls its injected
loader once, excludes a later dossier, and otherwise calls `evaluate_dossier`. Do not read or write
DuckDB for dossier content. Extend the point-in-time model validator to reject future dossier data.

- [ ] **Step 4: Write failing asset rendering tests**

Assert the HTML contains a new `#compatibility` section and nav item, `#dossier-summary`, and
`#dossier-rows`. Assert JavaScript references `snapshot.compatibility_dossier`, uses
`textContent`/the existing `element()` helper, and contains no `innerHTML`, `insertAdjacentHTML`,
`eval`, or new `fetch` target. Assert CSS defines tones for `blocking`, `model_required`,
`missing_evidence`, and `matched`, plus responsive table/card styles.

- [ ] **Step 5: Run asset tests and observe missing selectors**

Run `tests/web/test_assets.py`. Expected: assertions fail for the new section and renderer.

- [ ] **Step 6: Implement the compatibility UI**

Insert the section between markets and the legacy research gate, renumber subsequent indices, and
add a nav link. Render a status card with pair, assets, observation time, primary blocker, and four
counts. Render the fourteen checks in a horizontally scrollable table with judgment pills and the
two summaries. For `null`, render a single explicit `Unavailable at this snapshot cutoff` state.
Use only `element()`, `textContent`, and `replaceChildren()`.

- [ ] **Step 7: Extend server/API regression assertions**

At the current test clock, assert the API document includes the ineligible dossier. Keep all method,
host, CSP, no-store, and stable-error assertions unchanged.

- [ ] **Step 8: Run focused dashboard/server/assets tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/web -q
.venv/bin/ruff check src/polytrading/web tests/web
.venv/bin/ruff format --check src/polytrading/web tests/web
```

Expected: all pass.

Commit:

```bash
git add src/polytrading/web tests/web
git commit -m "feat(web): show contract compatibility dossier"
```

---

### Task 5: Package, documentation, and complete verification

**Files:**

- Modify: `pyproject.toml`
- Modify: `tests/test_package.py`
- Modify: `README.md`

**Interfaces:**

- Installed wheel contains `polytrading/carry/dossiers/hyperliquid-dydx-core-v1.json`.
- README documents CLI, dashboard interpretation, the current quanto rejection, and venue discovery
  as the next gate.

- [ ] **Step 1: Write failing wheel-content test**

Extend the packaging test to build a wheel and assert exactly one bundled dossier JSON path exists,
then open the zip member, validate it as `ContractCompatibilityDossier`, and evaluate it as
`ineligible` with `quanto_structure_excluded`.

- [ ] **Step 2: Run the packaging test and verify the dossier is absent**

Run `tests/test_package.py`. Expected: wheel member assertion fails.

- [ ] **Step 3: Include JSON package data**

Add:

```toml
"polytrading.carry.dossiers" = ["*.json"]
```

under `[tool.setuptools.package-data]`, preserving existing schema and web asset entries.

- [ ] **Step 4: Update operator documentation**

Document the two `carry dossier` commands, the dashboard section, why USDC/USDC does not overcome
the quanto blocker, why `ineligible` is a successful research outcome, dynamic fee limitations,
and the next venue-discovery milestone. Keep the existing no-profit and no-authority language.

- [ ] **Step 5: Run complete verification**

Run:

```bash
.venv/bin/python -m pytest --cov=polytrading --cov-report=term-missing --cov-fail-under=90
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir /tmp/polytrading-dossier-wheel
.venv/bin/python -c 'from polytrading.carry.dossier import evaluate_dossier, load_bundled_dossier; r=evaluate_dossier(load_bundled_dossier()); assert r.status.value == "ineligible" and r.primary_reason_code == "quanto_structure_excluded"'
```

Expected: all tests pass, total coverage is at least 90%, Ruff is clean, the wheel builds, and the
real artifact smoke assertion exits zero.

- [ ] **Step 6: Run a read-only HTTP smoke test**

Create a temporary current-schema DuckDB file, start the dashboard on an unused loopback port, GET
`/api/v1/dashboard`, and assert the JSON contains `compatibility_dossier.status == "ineligible"` and
`primary_reason_code == "quanto_structure_excluded"`. Stop the process and delete only the explicit
temporary directory.

- [ ] **Step 7: Perform final diff review**

Inspect every changed file and search for forbidden scope:

```bash
git diff --check
git diff --stat main...HEAD
rg -n "api[_-]?key|private[_-]?key|wallet|place[_-]?order|submit[_-]?order|withdraw|deposit|expected[_-]?return|position sizing" src tests README.md
```

Classify every match as pre-existing documentation/model language or a scope violation. Fix any
security, integrity, point-in-time, or misleading-profit issue and rerun the relevant focused and
full gates.

- [ ] **Step 8: Commit the packaged milestone**

```bash
git add pyproject.toml tests/test_package.py README.md
git commit -m "docs: explain contract compatibility gate"
```

Then confirm `git status --short` is empty.
