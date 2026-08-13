# Semantic scout gold corpus

This directory is the local, append-only construction area for the reviewed semantic corpus.
All checked-in JSONL files intentionally contain zero production rows. Task 4 must populate them
only from public source evidence and genuine independent human review; synthetic examples live
under `tests/fixtures/ai/corpus` and cannot satisfy any production gate.

Run `polytrading ai corpus preregister --policy data/gold/policy.json --dir data/gold` in a fresh
construction directory to create deterministic pending work units. A frozen version is written to
`manifests/<dataset-id>.json`; a later correction creates a new content-addressed version and never
overwrites a frozen manifest.
