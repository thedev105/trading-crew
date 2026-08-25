# Task 9 implementation report

## Outcome

Added a cutoff-safe shadow proposal and paper-result summary to the prediction-market dashboard,
including strict snapshot models, verified evidence aggregation, deterministic ordering, JSON
serialization, operator recipes, and an accessible browser panel. Paper P&L is unavailable until
the event-derived current state is `reconciled`; `unknown` rows are visibly marked as invalid
pending reconciliation.

## Production changes

- Added strict frozen `ShadowListing` and `ShadowSummary` records and the required `shadow` field on
  `PredictionDashboardSnapshot`. Counts are nonnegative and internally consistent, decimals are
  finite, latest rows are unique/newest-first/capped at 20, mappings are sorted, and paper P&L is
  present exactly on reconciled listings.
- Built the summary from immutable-hash-verified plans, experiments, and reconciliations at the
  requested cutoff. Each included plan uses its contiguous, chronological event prefix at or
  before the cutoff; current state is derived from that prefix rather than copied from another
  record.
- Failed closed on missing, gapped, nonchronological, cross-proposal, duplicate-identity, or
  mixed-scenario event evidence; duplicate experiments/reconciliations; orphan experiments; and
  inconsistent terminal, reconciliation, experiment, scenario, timestamp, or P&L evidence.
- Counted every cutoff-safe experiment by family, including failed, unknown, and losing rows.
  Aggregate P&L includes only proposals with a complete reconciliation, a trailing `reconciled`
  event, and one matching verified experiment. Zero and negative decimal results remain visible.
- Added deterministic newest-first ordering by `(observed_at, proposal_id)`, with the proposal's
  latest event timestamp and frozen plan quantity in each listing.
- Added copy-only `predictions shadow run` and `predictions shadow replay` recipes.
- Added an accessible shadow summary/table/empty state in the existing HTML/JS/CSS conventions.
  States render verbatim, unreconciled P&L displays as unavailable, and `unknown` rows show exactly
  `awaiting reconciliation — paper result invalid`.
- Preserved loopback-only serving and verified that served HTML, JavaScript, and CSS contain none
  of the forbidden promotional phrases.

## TDD evidence

Observed RED before production changes for:

- missing `ShadowListing` / `ShadowSummary` models;
- snapshots missing the `shadow` field and empty/cutoff-safe lifecycle aggregation;
- reconciled negative/zero P&L and experiment-family aggregation;
- missing shadow run/replay recipes;
- missing accessible HTML panel and client renderer;
- missing execution-scenario identity and chronological event-chain validation;
- aggregate/listing P&L inconsistency and unknown state-count keys.

The final tests cover strict/frozen/UTC/finite model behavior; nonnegative and coherent counts;
sorted mappings and tuples; more-than-20 ordering with timestamp ties; empty databases; plan,
event, reconciliation, and experiment cutoff boundaries; COMPLETE, UNWOUND, EXPIRED, UNKNOWN,
FIRST_LEG_SIMULATED, and RECONCILED current states; zero/negative/no P&L; all experiment outcomes;
duplicate, malformed, noncontiguous, nonchronological, and hash-invalid records; exact JSON decimal
serialization; recipes; accessible empty state; explicit null-safe browser rendering; and actual
served-asset vocabulary checks.

## Fresh verification

- Focused dashboard/model/server/store tests — `127 passed in 3.34s`.
- Final prediction-market suite — `893 passed in 18.60s`.
- `ruff check .` — clean.
- `ruff format --check .` — `255 files already formatted`.
- `git diff --check` — clean.

## Files changed

- `src/polytrading/predictions/dashboard.py`
- `src/polytrading/predictions/dashboard_models.py`
- `src/polytrading/predictions/web_assets/app.css`
- `src/polytrading/predictions/web_assets/app.js`
- `src/polytrading/predictions/web_assets/index.html`
- `tests/predictions/test_dashboard.py`
- `tests/predictions/test_dashboard_models.py`
- `.superpowers/sdd/2026-08-24-prediction-market-shadow-engine/task-9-report.md`
