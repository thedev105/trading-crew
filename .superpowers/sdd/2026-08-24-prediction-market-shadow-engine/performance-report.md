# Full-suite performance report

Date: 2026-08-25

## Outcome

- Repository suite baseline: **1,852 passed in 525.67s** (8m45s).
- Repository suite after the changes: **2,356 passed in 167.99s** (2m48s; `real 168.57s`).
- Wall-clock reduction: **357.68s / 68.0%**, or about **3.13x faster**, even though the verified suite grew by 504 tests between the recorded baseline and the final branch.
- Focused storage/carry/trial suite: **562 passed in 88.00s** (`real 88.37s`). Before the bulk fixture layer, the same focused command took 230.37s.

## Root-cause evidence

The original profile of the complete economics test recorded about 203 million calls and 37,515 DuckDB executes. It showed row-at-a-time `book_levels` writes, per-cycle book and level reads, and a second complete eligible-book selection inside the assembler.

A separate 240-hour trial seed profile reproduced the remaining fixture hotspot after the first layers:

- 55,859,546 calls / 23.898s profiled.
- 9,138 DuckDB `execute` calls consumed 23.370s cumulative.
- 1,440 `append_book_snapshot` calls consumed 17.084s.
- DuckDB's scalar parameter conversion retried its absent optional `pandas` import 207,871 times, consuming 17.487s in import lookup.

This made fixture-only scalar binds, rather than the 2,160-hour data volume or health audit, the final dominant cost.

## Changes and measurements

1. Book depth persistence now sends every snapshot's depth rows in one parameterized multi-values DuckDB insert. Exact retry, conflict, transaction rollback, Decimal precision, side ordering, and round-trip tests cover the unchanged storage contract.

   - The isolated 2,160-hour health test moved from 128.44s to 114.39s after this layer.

2. `DuckDBStore.book_snapshots_for_cycles` bulk-loads cutoff-safe snapshot headers and all matching levels with two bounded queries. Trial hourly selection uses this bulk read. Economics derives hourly, dense, and latest books from one eligible-pair set instead of querying every cycle and selecting the window twice.

   - Complete economics assembly call: 9.40s before, 0.87-0.89s after (about 10.6x faster).
   - A query-count characterization requires one cycle-window read, one bulk book read, no per-cycle `books_for_cycle` reads, and no second completed-cycle selection.

3. Expensive tests use immutable module-scoped templates and private database copies. A test-only COPY helper loads already validated `BookCollectionCycle` and `Level2BookSnapshot` models into template databases without changing public production append APIs. Its characterization compares every stored cycle/snapshot/level column, canonical JSON, hashes, timestamps, ordering, and the complete health report against rowwise seeding.

   - Complete economics template setup: 33.74s before, 10.79s in the isolated after-run; final full-suite setups were 17.12s linked and 13.31s unlinked under concurrent suite load.
   - Complete 2,160-hour health test: 128.44s before, 26.74s setup + 2.77s audit in the isolated after-run (29.74s total, about 4.3x faster). In the final full suite it used 43.97s setup + 3.51s audit.
   - Two semantically identical 24-hour health tests share one template; linked and unlinked economics histories retain separate templates.

## Preserved invariants

- Full 90-day funding, 60-day book, and 2,160-hour trial datasets remain unchanged.
- No assertions, thresholds, validation, coverage windows, freshness/skew checks, source hashes, hourly/dense/latest outcomes, or point-in-time cutoffs were weakened.
- Bulk book reads exclude future-effective and future-observed snapshots and remain deterministic by cycle, venue, symbol, side, and level index.
- Unrelated assets, incomplete venue sets, duplicate books, malformed cycles, and future evidence remain ineligible.
- Each mutating expensive test receives a private copied DuckDB file; mutable connections/files are never shared.
- Production transaction, idempotency, conflict, and rollback behavior remains covered and unchanged.

## Verification commands

```text
PYTHONPATH=src /Volumes/WORK/poly-trading/.venv/bin/python -m pytest tests/storage tests/carry tests/trial --durations=25 -q
# 562 passed in 88.00s

PYTHONPATH=src /Volumes/WORK/poly-trading/.venv/bin/python -m pytest --durations=30 -q
# 2356 passed in 167.99s

/Volumes/WORK/poly-trading/.venv/bin/ruff check .
# All checks passed!

/Volumes/WORK/poly-trading/.venv/bin/ruff format --check .
# 256 files already formatted

git diff --check
# clean
```

## Remaining hotspots

The largest remaining costs are building the full 2,160-hour template (43.97s in the final suite), building the two distinct economics templates (30.43s combined), and a 4.24s venue funding-cycle property test. The health audit itself is only 3.51s, and complete economics assembly is about 1.2s under full-suite load, so further gains would primarily require a broader bulk persistence API for funding/cycle fixture records or supplying DuckDB's optional conversion dependency. Neither is necessary for the achieved reduction and both would broaden dependency or production API scope.
