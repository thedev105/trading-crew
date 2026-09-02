# Credential command final-fix report

## Summary

Commit `d5fae19` hardens the fixed `credentials create --confirm` path. The command parent now
forks its dedicated ceremony child before it reads any Keychain item or secret buffer. The child
holds a public, non-secret, advisory `flock` for its complete process lifetime while it classifies
CLOB state, validates the wallet, makes the one fixed CREATE call, persists the three values, and
rolls back failed writes.

The child returns only canonical public results. Concurrent attempts return
`CREDENTIALS_CREATE_IN_PROGRESS`; post-lock existing and partial states return
`CREDENTIALS_ALREADY_PRESENT` and `CREDENTIALS_PARTIAL`; incomplete cleanup remains the
fail-closed `CREDENTIAL_ROLLBACK_FAILED` result. `check` now validates the exact secp256k1 scalar
interval, so zero and scalars at or above the order are `wallet_ready=false`.

No live Polymarket request, real Keychain access/write, or server operation was performed.

## Root-cause confirmation

The prior create path reused the descriptor-transfer ceremony. That required the command parent
to call `_read_secrets` before it forked, temporarily owning the wallet and CLOB buffers. Its
preflight was also outside the ceremony lifetime, leaving no cross-process exclusion around the
state check, remote CREATE, persistence, and rollback.

The replacement create-only path transfers no secret descriptors. It creates only a public
response pipe in the parent, forks, and performs all credential-store reads in the child after
that child has acquired the advisory lock.

## TDD red evidence

Before production changes, the focused regression run reported six expected failures:

- zero and order-or-larger 32-byte wallets were reported ready;
- rollback integrity failure was collapsed to `SIGNER_BOOTSTRAP_FAILED`;
- the parent read secrets before the child launch;
- a post-launch partial state was reported as a store failure after a remote attempt;
- two concurrent ceremonies did not have a bounded, public in-progress outcome.

Command: `.venv/bin/pytest -q tests/predictions/test_pilot_credential_commands.py tests/predictions/test_pilot_signer_bootstrap.py -k 'secp256k1 or forks_before or post_lock or concurrent_create or rollback_integrity'`

Observed: `6 failed, 35 deselected`.

## Tests and verification

- Focused red-to-green regressions: `6 passed, 35 deselected`.
- Credential/bootstrap/keychain unit slice: `87 passed`.
- Authority and secret static scans with `PYTHONPATH=.`: `72 passed`.
- Full requested targeted proof with `PYTHONPATH=.`: `334 passed`.
- `ruff check .`: passed.
- Touched-file `ruff format --check`: passed.
- `git diff --check`: passed before the source commit.

Repository-wide `ruff format --check .` still lists ten pre-existing unrelated files outside this
fix; none were reformatted or included in the commit.

## Commit

- `d5fae19 fix(predictions): harden CLOB credential command`

## Self-review

- The create parent has no Keychain state preflight and no descriptor secret handoff.
- The child lock path and lock contents contain no secret; production keeps its descriptor open
  until `os._exit`, while inline fake children release it for test isolation.
- CLOB state is classified only after lock acquisition, before wallet read or remote CREATE.
- The only child operation remains literal `CREATE`; no derive, generic RPC, transport option,
  secret label, or trading path was added.
- The response remains public-only and the updated source digests keep the static manifest
  fail-closed.

## Residual impossibilities

The advisory lock coordinates application command children only. An arbitrary non-cooperating
external Keychain writer cannot be made transactionally atomic with the macOS Keychain API. If it
replaces a ceremony-owned item during rollback, the opaque creation ticket refuses to delete that
replacement; the code retains the replacement and returns `CREDENTIAL_ROLLBACK_FAILED`, never
claiming all-or-none success. The regression test covers that fail-closed behavior.
