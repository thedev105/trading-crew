# Task 5 report — proof-bound parent IPC

## Implemented

- `SignerLinkVenuePort` now receives `proof_for` and `kill_directive` callbacks.
- Submit and cancel resolve the exact capability ID before IPC, attach that public proof, and derive
  `capability_digest` and `plan_digest` from the returned grant. Missing or mismatched mappings
  fail locally before any frame is written.
- Identity, reads, and the dedicated signer-kill operation remain proof-free.
- `engage_kill` creates only `SignerKillPayload(SIGNER_KILL, signed_directive)` and returns no
  control response data; a signer rejection becomes `SignerLinkError` with its sanitized code.

## Tests

- RED: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_link.py -q -k "proof or engage_kill"`
  failed because the port did not accept proof callbacks or expose `engage_kill`.
- GREEN: the same selection passed (`3 passed`).
- Required suite: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_link.py tests/predictions/test_polymarket_signer_ipc.py -q`
  passed (`150 passed`) when run with permission for its local temporary Unix-domain sidecar socket.
- `rtk .venv/bin/ruff check src/polytrading/predictions/pilot/signer_link.py tests/predictions/test_pilot_signer_link.py`
  and `rtk git diff --check` passed.

## Scope

- No network, Keychain, executor, or runtime wiring was added.
