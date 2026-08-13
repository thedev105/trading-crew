# Semantic scout gold corpus

This directory is the local, append-only construction area for the reviewed semantic corpus.
All checked-in JSONL files intentionally contain zero production rows. Task 4 must populate them
only from public source evidence and genuine independent human review; synthetic examples live
under `tests/fixtures/ai/corpus` and cannot satisfy any production gate.

Run `polytrading ai corpus preregister --policy data/gold/policy.json --dir data/gold` in a fresh
construction directory to create deterministic pending work units. A frozen version is written to
`manifests/<dataset-id>.json`; a later correction creates a new content-addressed version and never
overwrites a frozen manifest.

The binding production minimum is 500 retained contracts across at least 20 rule templates, 250
positive/negative relationship pairs or sets, and 200 adversarial examples. Each contract and
relationship needs two independent human reviews tied to the exact immutable input hash. Any
disagreement needs a third adjudicator who is distinct from both reviewers. Source retrieval must
not occur after the registered information cutoff, and revisions, derivatives, raw duplicates, and
event families may not cross train/validation/test splits.

Use this check while constructing the corpus:

```bash
polytrading ai corpus validate \
  --dir data/gold \
  --require-contracts 500 \
  --require-templates 20 \
  --require-relationships 250 \
  --require-adversarial 200 \
  --require-two-reviews
```

The checked-in state is deliberately empty and unfrozen, so this command must fail with deficits.
That failure is an evidence result, not a setup problem. Do not run `freeze` with reduced review
requirements for production, and do not move any row from `tests/fixtures/ai/corpus` here.

After genuine completion, freeze and record the content-addressed dataset ID with the experiment
before tuning retrieval or extraction. Run train diagnostics, validation, and the untouched test in
that order. Even a fully reviewed semantic corpus cannot activate trading: the report remains
research-only until every separate dependency, including deterministic payoff compilation and graph
proof, is measurable and passing.
