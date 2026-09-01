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

## Fix round 1

- Mutation evidence now binds a one-use nonce, signer request ID, intent fingerprint, operation,
  plan digest, and capability authority digest. The signer validates those bindings and consumes
  the evidence nonce under its dispatch lock before `SIGN_ORDER`, `SUBMIT_ORDER`, or
  `CANCEL_ORDER`; failed dispatch cannot release it, fresh-request replay is rejected, and evidence
  prepared before signer kill cannot execute afterward.
- Launch fallback closes the write-capable composition store before opening the posture-only
  read-only runtime. Reconciliation-time exceptions now return killed posture without the nested
  DuckDB configuration/schema error, after closing the signer channel and issuer.
- Startup reconciliation now propagates signer transport/read exceptions to launch cleanup.
  Unknown venue state remains representable only when the signer returned a typed sanitized result,
  such as a sanitized `READ_FAILED` or a valid snapshot containing an open order.
- Execution evidence no longer derives geoblock authority from manifest status or substitutes the
  reconciliation hash. A narrowly typed current geoblock provider supplies the exact decision,
  evidence hash, and expiry; absence, malformed output, provider failure, a blocked decision, or
  expiry engages the mutation kill gate.

### Fix-round regressions and verification

- RED reproduced: evidence-schema fields were rejected as unknown; replay was accepted without
  nonce consumption; launch fallback raised nested `PILOT_DATABASE_SCHEMA_STALE`; signer read
  failure still composed live services; and `PilotEnvironment` had no current geoblock provider.
- GREEN regressions cover failed-dispatch nonce consumption, fresh-request replay, evidence held
  across signer kill, writer-before-reader fallback ordering, signer read cleanup (including issuer
  destruction), sanitized venue ambiguity, and exact geoblock provider binding/failure.
- Full predictions verification excluding the eleven sandbox-incompatible spawned-sidecar cases:
  `3118 passed, 11 deselected, 13 warnings`. The complete signer IPC module passed outside the
  sandbox (`143 passed`); the warnings remain the existing Pydantic dashboard-model deprecations.
- Targeted Ruff, `git diff --check`, authority/secret scans, and refreshed sealed hashes passed.
  Final reviewed hashes are `3f0b48457620138613c822e1dd6bf7bbd4e074169b55b909e99047c426dc1523`
  for `ipc.py` and `4dff9154f95dd69d93eeebf5bc340f530f642070e0577bb7c106f1f16c63a81d`
  for `signer.py`.

## Fix round 2

- Added one proof-free, fixed `READ_GEOBLOCK` signer IPC operation with an empty discriminated
  payload and a dedicated result containing only `allowed`, the raw-evidence SHA-256,
  `observed_at`, and `expires_at`. The result rejects extra fields, naive timestamps, and
  nonpositive evidence lifetimes; it cannot carry the geoblock country, region, raw IP, or body.
- The live signer read guard alone allows the operation. Offline composition retains an
  unreachable handler and refuses it with `EXECUTION_UNAVAILABLE`. The signer freshly validates
  the exact narrow handler result, rejects a generic authenticated-read result, and preserves
  stable failures for guard, handler, malformed IPC, and rejected-response paths.
- `SignerRestHandlers` calls the existing frozen `GEOBLOCK` route only through
  `execute_geoblock_restricted`. No CLOB credentials are passed to that call. The raw IP and exact
  response bytes stay in `RestrictedGeoblockEvidence` inside the sidecar; the handler projects
  only the decision, same-response evidence hash, transport observation time, and a five-minute
  expiry. Failed restricted reads produce no evidence, and a valid blocked response remains
  `allowed=False`.
- `SignerLinkVenuePort.geoblock_evidence()` sends only `ReadGeoblockPayload`, requires the exact
  `GeoblockEvidenceResult`, and converts it to the existing pilot `GeoblockEvidence` type.
  `VenueSubmissionPort` now declares the same method. `build_launch_runtime` installs that method
  as the production `geoblock_provider`, closing the launch-composition gap that previously left
  every live mutation without current geoblock evidence.

### Fix-round-2 regressions and verification

- RED/GREEN covered the missing operation and sanitized wire model, evidence lifetime validation,
  read-guarded exact handler dispatch, restricted no-auth transport projection, live read
  allowlisting, signer-link conversion, malformed IPC translation, and production launch wiring.
- The production launch regression calls the wired provider, builds a signed mutation-evidence
  envelope with the returned geoblock decision/hash/expiry, and asserts that no `SIGN_ORDER`,
  `SUBMIT_ORDER`, or `CANCEL_ORDER` request occurred.
- Explicit fail-closed regressions cover a failed restricted read, generic wrong-type handler
  result, malformed IPC result containing a raw IP, sanitized signer rejection, offline-service
  refusal, and a valid blocked response.
- Final sandbox-safe predictions suite:
  `rtk .venv/bin/python -m pytest tests/predictions -q -k 'not spawned_sidecar'` passed
  (`3135 passed, 11 deselected, 13 warnings`). The warnings remain the existing Pydantic
  dashboard-model deprecations.
- The complete signer IPC module passed outside the sandbox so its multiprocessing tests could
  create temporary local AF_UNIX resource-sharer sockets (`151 passed`). Authority and secret
  scans passed (`71 passed`); targeted Ruff and `git diff --check` passed.
- Refreshed reviewed hashes are
  `a74145dc0d7ae4c0bf59fbce153dedbac901a3450da90cf4669b1eaeb9dcae34` for
  `execution/models.py`, `568911250f12cdec4d7af9fecaf03b16a3d22a480d85b39f5601df59d100414d`
  for `ipc.py`, `0114d8285617c6aa862abd6e43bd54b02d9674e896b70b070ac8e5a4dfd912d1`
  for `rest.py`, and `900b0220da76bcb891df3ad38f9a8f29f80b81def8de0101d3d51fef03d09a2e`
  for `signer.py`.
- No real venue request, network access, Keychain access, browser transport, parent transport,
  credential/authority/CLI surface, or subagent was used.

## Fix round 3

- `GeoblockEvidenceResult` now rejects evidence whose positive lifetime exceeds the shared fixed
  five-minute maximum. UTC normalization and the existing nonpositive-lifetime rejection remain
  intact. `SignerRestHandlers` uses the same exported protocol constant when it derives expiry,
  removing the producer/validator drift risk.
- `SignerLinkVenuePort` validates and normalizes its clock before constructing requests. Before
  converting a typed geoblock result into pilot authority evidence, it rejects observations more
  than two seconds ahead of that clock with the stable `IPC_REQUEST_INVALID` code. Invalid link
  clocks fail with the same stable code.

### Fix-round-3 regressions and verification

- RED: a dedicated 3650-day evidence-lifetime regression failed because no
  `SignerProtocolError` was raised. GREEN: the new maximum-lifetime validation passed together
  with the existing UTC-normalization and nonpositive-lifetime cases (`5 passed`).
- RED: a signer result observed five seconds ahead of the link clock was accepted, and a naive
  link clock leaked `SignerProtocolError`. GREEN: current evidence still converts while both
  invalid cases fail closed with `IPC_REQUEST_INVALID` (`3 passed`).
- The geoblock-focused IPC, REST, signer-service, signer-link, and runtime slice passed
  (`33 passed, 278 deselected`). The complete affected-module selection excluding only the eleven
  known sandbox-incompatible spawned-sidecar cases passed (`300 passed, 11 deselected`).
- Authority and secret scans passed (`71 passed`); targeted Ruff and `git diff --check` passed.
  Refreshed reviewed hashes are
  `c707695f9ab72b45b4601becdea8ce22beccb333f8cc52112f815ccd4a31485b` for `ipc.py` and
  `8ea09d5ed144a1094f1d27b39e287cb9539d464c7a17e4372ba93b24284a80e5` for `rest.py`.
- No real venue request, network access, Keychain access, browser or parent transport, CLI flag,
  subagent, or real trade was used.
