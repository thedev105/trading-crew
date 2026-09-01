# Task 8 report — reconciled live pilot launch

## Implemented

- `build_launch_runtime` now creates the launch issuer before the signer, passes only its public
  verification key into `live_pilot_signer_service`, describes the signer-owned identity, builds
  the typed signer link, performs startup reconciliation, and then composes the live environment
  and executor factory. A clean fake snapshot serves live services while every fresh launch still
  starts killed.
- `PilotRuntime` owns the issuer, signer channel, and venue port as one lifetime. Shutdown sends a
  signed `SIGNER_KILL` even when no capabilities have yet been issued, closes both IPC streams,
  destroys the issuer private key, and then closes the store. Bootstrap, identity, read, or
  reconciliation failures close acquired resources and emit only
  `pilot: signer unavailable; serving posture only`, without exception text.
- Added strict decoders for the fixed public account and position reads. Identity and startup
  reads remain proof-free and mutation-evidence-free; no secret-bearing or generic transport
  surface crossed into the parent.
- Production submission now performs `SIGN_ORDER` and then `SUBMIT_ORDER` over the same typed
  channel. Action-local signer proofs and evidence providers are installed only for the just-issued
  primary/recovery capability pair.
- The CLI surface is intentionally unchanged: `predictions pilot polymarket --db ... --port ...`
  gained no credentials, authority bypass, or activation flags.

## Trust-boundary correction

The initial Task 8 composition exposed an architectural gap: the live signer could verify the
capability signature but could not reconstruct current mutation authority without trusting a
parent-injected `AuthorityContext`. Leaving the production authority factory unavailable would
have kept every production mutation permanently disabled; passing the context directly would have
collapsed the signer boundary.

With the parent-approved correction, the launch issuer now signs a public, action-local mutation
evidence envelope with a maximum five-second lifetime. The envelope binds the exact manifest and
record hash, reconciliation hash and observation time, geoblock and account-scope evidence and
expiries, parent/child kill state, operator presence, plan digest, capability authority digest,
risk amounts, and issue/expiry timestamps. IPC validation binds that envelope to the request's
account, manifest, plan, and authority digests.

The signer verifies the envelope with its inherited launch public key, checks evidence and request
freshness, rechecks its own credential presence, secret-derived account identity, clock, kill
state, capability signature, protocol fixture, plan, mode, operation, and presence deadline, and
then builds `AuthorityContext` only from those verified values. Live production uses no injected
authority factory. Legacy injected contexts remain solely for focused compatibility tests.
Automation remains denied.

During final review, a distinct-hash regression exposed that the first envelope producer copied
the account-scope hash into `reconciliation_hash`. `ExecutionEvidence` now carries the
reconciliation hash explicitly, the live service populates it from the current reconciliation,
and the envelope preserves reconciliation and account-scope hashes independently.

## TDD evidence

- RED/GREEN cycles covered the signed evidence schema and issuer, IPC request binding, signer
  signature/freshness/context verification, signer-link evidence transport and deadline capping,
  live signer construction, launch sequencing, startup reconciliation, and reconciliation
  observation freshness.
- The Task 8 launch tests initially failed because launch still composed the unavailable account
  closure. They pass with identity → typed reads → reconciliation → environment composition.
- End-to-end mutation check: the new live-launch shutdown test passed, failed specifically when
  the runtime's signer-kill call was temporarily removed, and passed again after restoration.
- RED: constructing deliberately distinct reconciliation and account-scope hashes failed because
  `ExecutionEvidence` had no reconciliation hash. GREEN: the signed envelope now retains `8…8`
  and `a…a` independently.
- RED: the full predictions acceptance run rejected the pre-existing direct `httpx` import in the
  live signer composition test. GREEN: that test now replaces the typed transport/handler boundary
  with an in-memory fake and the structural no-network-import gate passes.

## Verification

- Final full predictions suite, run outside the sandbox so multiprocessing could bind its temporary
  local AF_UNIX resource-sharer sockets:
  `rtk .venv/bin/python -m pytest tests/predictions -q` passed (`3123 passed`, `13 warnings`). The
  warnings are existing Pydantic `BaseModel.copy` deprecations in dashboard-model tests.
- The complete signer IPC module separately passed outside the sandbox (`140 passed`). Its sandbox
  run completed all non-process behavior (`129 passed`) and failed only the eleven spawned cases at
  AF_UNIX `bind` with `PermissionError`; the same eleven passed outside the sandbox.
- Focused iteration gates passed for capability/signer behavior (`178 passed, 11 deselected`),
  execution/reconciliation (`146 passed`), launch/runtime/end-to-end/server (`98 passed`), and
  authority/secret scans (`71 passed`).
- Targeted Ruff checks and `git diff --check` passed. The final pre-commit rerun is recorded in the
  commit handoff.

## Scope

- Used only fake transports and in-memory framed channels; no real venue request was made.
- No real Keychain access, wallet secret, UI change, new CLI flag, authority bypass, or subagent
  was used.
