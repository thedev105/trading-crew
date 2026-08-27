# Polymarket Local Live Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a loopback-only, passkey-approved Polymarket pilot that lets one locally present
operator authorize tightly bounded deterministic execution while secrets remain inside the macOS
Keychain and signer sidecar.

**Architecture:** Keep the existing Market Atlas server observation-only. Add a separate Pilot
package owning immutable policy/evidence records, WebAuthn ceremonies, local capability issuance,
presence, and an exact-origin control server; extend the existing coordinator and signer through
typed ports rather than bypasses. Every live mutation remains FAK/FOK-only, capability-bound,
persist-before-submit, fail-closed, and operator-triggered only after offline qualification.

**Tech Stack:** Python 3.12–3.14, Pydantic 2.13.4, DuckDB 1.5.4, httpx 0.28.1,
websockets 17.0.1, eth-account 0.13.7, webauthn 3.0.0, cryptography 50.0.1, vanilla ES
modules, pytest 9.1.1, Hypothesis 6.160.0, Ruff 0.15.22, macOS Security framework.

**Spec:** docs/superpowers/specs/2026-08-27-polymarket-local-live-pilot-design.md

## Global Constraints

- Implement Polymarket only for one individual operator, one account, and one dedicated wallet.
- Never make an authenticated live request, derive real credentials, unlock the operator keychain,
  or submit/cancel a real order during implementation, tests, CI, review, or agent work.
- Stage 4 is an operator-only UI action after implementation acceptance; no CLI exposes live
  credential, order, cancel, activation, capability, or kill-clear operations.
- The existing Market Atlas process stays GET/HEAD-only and has no signer, secret, capability
  issuer, control route, or authenticated transport dependency.
- The Pilot process binds to `http://localhost:<configured-port>` only, uses RP ID `localhost`,
  rejects every other Host/Origin, emits no CORS permission, and loads no third-party asset.
- Browser requests contain stable IDs, lower requested limits, confirmation text, and WebAuthn
  responses only. They never contain secrets, signed order bodies, arbitrary routes, hosts, token
  IDs, proof decisions, or client-computed economics.
- Immutable ceilings are exactly: wallet trading equity USD 250; order notional USD 10; strategy
  gross notional USD 25; session duration 15 minutes; session deployed capital USD 50; one active
  strategy; session loss USD 5; UTC-day realized plus unrealized loss USD 10.
- Requested UI limits may only lower ceilings; reject attempted increases rather than clamping.
- All money and quantities use finite `Decimal`; all timestamps are timezone-aware UTC; persisted
  records are canonical JSON plus SHA-256 and append-only.
- All strategy and recovery order types are FAK/FOK. GTC, GTD, post-only, passive quoting, and
  resting strategies are structurally rejected at every boundary.
- AI may nominate research relationships but cannot prove, rank, size, approve, or execute.
- Cross-venue opportunities remain visible and disabled; no Kalshi or Limitless live authority.
- Funding, withdrawal, transfer, allowance mutation, approval, split/merge, conversion, redemption,
  bridge, relayer, and wallet generation remain outside the application.
- Wallet/CLOB secrets exist only in macOS Keychain, inherited signer descriptors, signer memory,
  and the Keychain API child boundary. Never place them in arguments, environment, HTTP, ordinary
  IPC, DuckDB, logs, exceptions, telemetry, screenshots, fixtures, or support output.
- Unknown outcomes stop normal execution. Never blindly retry an order POST. P&L remains
  unavailable until venue, settlement, balance, allowance, position, and ledger reconcile exactly.
- Primary authority dies on kill. Recovery is separately signed, frozen, risk-reducing, uses no
  extra capital allowance, and lives for at most 120 seconds.
- Every launch starts killed. Kill clearance requires exact reconciliation, explicit review, an
  exact phrase, and a new passkey assertion; clearance creates no trading capability.
- The current 45-day Class G thresholds and 30 additional shadow days are recomputed from persisted
  evidence per proof family and are never represented as editable checkboxes.
- Use `.venv/bin/python -m pytest`, not bare `pytest`. Preserve `.claude/settings.json`,
  `.playwright-mcp/`, and existing screenshots; stage only task files.
- Full-suite p50 target is at most 181 seconds on the reference machine; focused changed-area tests
  target under 30 seconds. Report every new test slower than two seconds.

---

## File and Responsibility Map

### New pilot package

- `src/polytrading/predictions/pilot/models.py` — immutable policy, eligibility, challenge,
  capability-event, session, presence, activation, and clearance records.
- `src/polytrading/predictions/pilot/policy.py` — compiled ceilings, requested-limit validation,
  conservative equity/loss accounting, and effective bounds.
- `src/polytrading/predictions/pilot/qualification.py` — Class G plus shadow evidence evaluator and
  manifest-promotion input.
- `src/polytrading/predictions/pilot/passkeys.py` — WebAuthn 3.0.0 registration/authentication port,
  exact origin/RP validation, challenge binding, and fake verifier.
- `src/polytrading/predictions/pilot/capabilities.py` — ephemeral Ed25519 issuer, canonical bundles,
  primary/recovery grants, and verifier adapter.
- `src/polytrading/predictions/pilot/selector.py` — deterministic eligible-strategy ranking and
  frozen live/recovery plan compilation.
- `src/polytrading/predictions/pilot/sessions.py` — exact-order, strategy, and automation-session
  orchestration, loss/deployment clocks, stop, and state derivation.
- `src/polytrading/predictions/pilot/presence.py` — two-second browser heartbeat, five-second loss,
  native sleep/lock signal port, and kill events.
- `src/polytrading/predictions/pilot/presence_macos.py` — macOS screen-lock notification and
  monotonic sleep-gap adapter behind the presence port.
- `src/polytrading/predictions/pilot/read_models.py` — coherent readiness, limits, opportunity,
  session, activation, and audit projections.
- `src/polytrading/predictions/pilot/server.py` — exact-origin HTTP application, sessions, CSRF,
  schemas, fixed routes, headers, rate/size bounds, and loopback lifecycle.
- `src/polytrading/predictions/pilot/runtime.py` — dependency composition; production objects are
  constructed only after all gates and operator ceremonies.
- `src/polytrading/predictions/pilot_web_assets/` — Pilot HTML, CSS, ES modules, icons, views, and
  same-origin API client.

### Existing packages extended

- `src/polytrading/predictions/execution/authority.py` — mode/session/recovery capability fields and
  independent ceiling/presence checks.
- `src/polytrading/predictions/execution/coordinator.py` — frozen plan continuation, recovery grant,
  session claims, and authoritative post-leg callback.
- `src/polytrading/predictions/execution/kill_switch.py` — passkey/reconciliation-gated append-only
  production clearance.
- `src/polytrading/predictions/polymarket_execution/protocol.py` and `fixtures/` — fresh official
  source checkpoint, wallet/funder/signature model, and credential route fixtures.
- `src/polytrading/predictions/polymarket_execution/routes.py` — closed credential-provisioning
  request available only under its distinct grant.
- `src/polytrading/predictions/polymarket_execution/keychain_macos.py` — signer-side macOS Security
  framework adapter with a fake test port.
- `src/polytrading/predictions/polymarket_execution/credentials.py` — one-time create-or-derive
  credential flow that writes directly to the secret store and returns fingerprints only.
- `src/polytrading/predictions/polymarket_execution/signer.py` — ephemeral transport lifecycle,
  credential-grant handling, and independent primary/recovery checks.
- `src/polytrading/predictions/storage/schema/011_polymarket_live_pilot.sql` and `store.py` — new
  append-only records and atomic nonce/session transactions.
- `src/polytrading/predictions/dashboard*.py` — sanitized pilot posture only; observer authority is
  unchanged.
- `src/polytrading/predictions/cli.py` — safe `predictions pilot polymarket --db --port` server
  launcher only.
- `pyproject.toml` — exact dependencies and Pilot package data.
- `README.md` and `docs/predictions/polymarket-live-pilot.md` — setup, concepts, modes, activation,
  recovery, checklists, and secret safety.

---

### Task 1: Pilot records and append-only migration

**Files:**
- Create: `src/polytrading/predictions/pilot/__init__.py`
- Create: `src/polytrading/predictions/pilot/models.py`
- Create: `src/polytrading/predictions/storage/schema/011_polymarket_live_pilot.sql`
- Modify: `src/polytrading/predictions/storage/store.py`
- Test: `tests/predictions/test_pilot_models.py`
- Test: `tests/predictions/test_pilot_store.py`

**Interfaces:**
- Produces `AuthorizationMode`, `GrantKind`, `PilotProofFamily`, `PilotLimits`, `PilotLossState`,
  `EligibilityAttestationRef`,
  `PilotPolicyProfile`, `PilotActivationCeremony`, `CredentialProvisioningEvent`,
  `AuthorizationChallenge`, `PilotCapabilityEvent`, `PilotNonceClaim`, `PilotExecutionSession`,
  `PilotPresenceEvent`, and `PilotKillClearanceEvent`.
- Produces `PredictionMarketStore.append_pilot_*()`, `claim_pilot_nonce()`, and verified read methods.

- [ ] **Step 1: Write failing strict-model tests**

```python
def test_requested_policy_cannot_encode_a_limit_above_compiled_ceiling() -> None:
    with pytest.raises(ValidationError, match="order_notional"):
        PilotPolicyProfile.model_validate(policy_fields(order_notional="10.01"), strict=True)

def test_eligibility_reference_contains_no_document_body() -> None:
    fields = eligibility_fields()
    assert "document" not in EligibilityAttestationRef.model_validate(fields).model_dump()
```

- [ ] **Step 2: Run the focused model test and confirm imports fail**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_models.py -q`

Expected: FAIL because `polytrading.predictions.pilot.models` does not exist.

- [ ] **Step 3: Implement frozen schema-versioned records**

```python
class AuthorizationMode(StrEnum):
    EXACT_ORDER = "EXACT_ORDER"
    COMPLETE_STRATEGY = "COMPLETE_STRATEGY"
    AUTOMATION_SESSION = "AUTOMATION_SESSION"

class GrantKind(StrEnum):
    PRIMARY = "PRIMARY"
    RECOVERY = "RECOVERY"
    CREDENTIAL_PROVISIONING = "CREDENTIAL_PROVISIONING"
```

Use the existing `PredictionRecord`, UTC normalization, `Sha256`, sorted-unique hash validation,
and canonical record conventions. Reject secret-looking fields, non-Philippines pilot jurisdiction,
non-individual account type, mutable/expired challenge states, or mismatched wallet/account hashes.

- [ ] **Step 4: Write migration/store tests for upgrade, reopen, tamper, and atomic nonce claim**

```python
def test_pilot_nonce_claim_is_atomic_and_replay_safe(store: PredictionMarketStore) -> None:
    claim = nonce_claim()
    assert store.claim_pilot_nonce(claim) is True
    assert store.claim_pilot_nonce(claim) is False
    with pytest.raises(ConflictingRecordError):
        store.claim_pilot_nonce(claim.model_copy(update={"payload_hash": "f" * 64}))
```

- [ ] **Step 5: Add migration 011 and typed store methods**

Create the ten tables named in spec section 8 with primary keys, account/time indexes, canonical
`record_json`, and `record_hash`. Use one transaction for nonce claim plus session/capability event
append. Never use `INSERT OR REPLACE`.

- [ ] **Step 6: Run focused tests and commit**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_models.py tests/predictions/test_pilot_store.py -q`

Expected: PASS.

```bash
git add src/polytrading/predictions/pilot src/polytrading/predictions/storage/schema/011_polymarket_live_pilot.sql src/polytrading/predictions/storage/store.py tests/predictions/test_pilot_models.py tests/predictions/test_pilot_store.py
git commit -m "feat(predictions): add live pilot records"
```

### Task 2: Immutable limits, loss accounting, and evidence qualification

**Files:**
- Create: `src/polytrading/predictions/pilot/policy.py`
- Create: `src/polytrading/predictions/pilot/qualification.py`
- Test: `tests/predictions/test_pilot_policy.py`
- Test: `tests/predictions/test_pilot_qualification.py`

**Interfaces:**
- Produces `COMPILED_PILOT_CEILINGS`, `effective_limits(requested) -> PilotLimits`,
  `mark_trading_equity(snapshot) -> Decimal`, `loss_state(window) -> PilotLossState`, and
  `evaluate_pilot_qualification(store, proof_family, as_of) -> QualificationReport`.

- [ ] **Step 1: Write failing ceiling and conservative-mark tests**

```python
def test_effective_limits_rejects_increase_instead_of_clamping() -> None:
    requested = limits(order_notional=Decimal("10.01"))
    with pytest.raises(PilotPolicyError, match="ORDER_NOTIONAL_CEILING"):
        effective_limits(requested)

def test_unknown_position_mark_makes_loss_unknown() -> None:
    assert loss_state(loss_window(position_mark=None)).status == "UNKNOWN"
```

- [ ] **Step 2: Run tests and confirm the missing implementation**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_policy.py -q`

Expected: FAIL on missing imports.

- [ ] **Step 3: Implement ceilings and deterministic accounting**

```python
COMPILED_PILOT_CEILINGS = PilotLimits(
    wallet_equity=Decimal("250"), order_notional=Decimal("10"),
    strategy_gross_notional=Decimal("25"), session_seconds=900,
    session_deployed_capital=Decimal("50"), concurrent_strategies=1,
    session_loss=Decimal("5"), utc_day_loss=Decimal("10"),
)
```

Use executable unwind bids/asks, confirmed cash flows, and reconciled start equity. Return UNKNOWN
for stale or missing marks, flows, balances, or positions. Recovery gross notional consumes the
same order/strategy budgets and may not increase deployed capital.

- [ ] **Step 4: Write qualification tests for every inherited threshold**

Generate persisted evidence with one threshold changed at a time: 44 days, 24/9 opportunities,
0.74% median surplus, USD 99 capacity, false proof, 0.25%-or-greater incomplete loss, 8%-or-greater
drawdown, 29 shadow days, reward-dependent profit, and unreconciled shadow state. Each fixture must
fail with one stable reason code.

- [ ] **Step 5: Implement qualification from verified store reads**

```python
def evaluate_pilot_qualification(
    store: PredictionMarketStore, proof_family: PilotProofFamily, as_of: datetime
) -> QualificationReport:
    """Recompute all Class G and additional-shadow gates; never accept caller booleans."""
```

Bind rule/proof/economics/policy/protocol/source hashes and keep each proof family independent.

- [ ] **Step 6: Run focused tests and commit**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_policy.py tests/predictions/test_pilot_qualification.py -q`

Expected: PASS.

```bash
git add src/polytrading/predictions/pilot/policy.py src/polytrading/predictions/pilot/qualification.py tests/predictions/test_pilot_policy.py tests/predictions/test_pilot_qualification.py
git commit -m "feat(predictions): enforce pilot qualification"
```

### Task 3: Fresh protocol checkpoint and credential-only route

**Files:**
- Modify: `src/polytrading/predictions/polymarket_execution/protocol.py`
- Modify: `src/polytrading/predictions/polymarket_execution/routes.py`
- Modify: `src/polytrading/predictions/polymarket_execution/conformance.py`
- Create: `src/polytrading/predictions/polymarket_execution/fixtures/protocol_v2.json`
- Create: `src/polytrading/predictions/polymarket_execution/fixtures/sources_v2.json`
- Create: `src/polytrading/predictions/polymarket_execution/fixtures/order_vectors_v2.json`
- Create: `src/polytrading/predictions/polymarket_execution/fixtures/event_vectors_v2.json`
- Create: `src/polytrading/predictions/polymarket_execution/fixtures/credential_vectors_v2.json`
- Test: `tests/predictions/test_polymarket_pilot_protocol.py`

**Interfaces:**
- Produces a reviewed protocol version/hash, explicit wallet/funder/signature model, and
  `CredentialProvisioningRequest`; keeps this request outside the execution mutation route set.

- [ ] **Step 1: Freeze current official source bytes and hashes**

Retrieve only the official URLs from spec section 18. Store canonical URL, retrieval time, SHA-256,
and reviewed interpretation. Do not make an authenticated request. If current docs conflict with
the implementation, make conformance fail before changing behavior.

Preserve every v1 fixture byte-for-byte. Add a complete v2 fixture set and make snapshot selection
explicit; never rewrite historical conformance evidence under the old version string.

- [ ] **Step 2: Write failing protocol-drift and account-model tests**

```python
def test_new_wallet_requires_explicit_account_signature_model() -> None:
    with pytest.raises(ProtocolSnapshotError, match="ACCOUNT_SIGNATURE_MODEL_REQUIRED"):
        snapshot_fields(account_signature_model=None)

def test_credential_route_is_not_an_execution_route() -> None:
    assert RouteKey.CREATE_OR_DERIVE_CREDENTIALS not in execution_route_keys()
```

- [ ] **Step 3: Implement explicit account/signature binding and credential request schema**

The model must bind signer address, funder address, signature type, chain ID, exchange/domain, and
credential route hash. Unsupported or ambiguous combinations return
`ACCOUNT_SIGNATURE_MODEL_UNSUPPORTED`; never default to signature type zero.

- [ ] **Step 4: Add official-example-derived credential fixtures and mutation tests**

Mutate address, timestamp, nonce, chain ID, signature type, host, method, and path independently;
each must change the signed bytes or fail validation. Keep response secrets inside a redacted
test-only object and assert serialization is forbidden.

- [ ] **Step 5: Run conformance and commit**

Run: `.venv/bin/python -m pytest tests/predictions/test_polymarket_conformance.py tests/predictions/test_polymarket_pilot_protocol.py -q`

Expected: PASS without network access.

```bash
git add src/polytrading/predictions/polymarket_execution tests/predictions/test_polymarket_pilot_protocol.py
git commit -m "feat(predictions): refresh pilot protocol checkpoint"
```

### Task 4: WebAuthn challenges and ephemeral capability issuer

**Files:**
- Create: `src/polytrading/predictions/pilot/passkeys.py`
- Create: `src/polytrading/predictions/pilot/capabilities.py`
- Modify: `src/polytrading/predictions/execution/authority.py`
- Modify: `pyproject.toml`
- Test: `tests/predictions/test_pilot_passkeys.py`
- Test: `tests/predictions/test_pilot_capabilities.py`
- Test: `tests/predictions/test_execution_authority.py`

**Interfaces:**
- Produces `PasskeyService`, `PyWebAuthnPasskeyService`, `VerifiedOperatorAssertion`,
  `CapabilityRequest`, `PilotCapabilityIssuer`, `IssuedGrantPair`, and the extended
  `VerifiedExecutionCapability`.

- [ ] **Step 1: Add exact dependencies and install in the isolated execution worktree**

Add `webauthn==3.0.0` and direct `cryptography==50.0.1` pins, then run:

`.venv/bin/python -m pip install -e '.[dev]'`

- [ ] **Step 2: Write failing origin, replay, and action-binding tests**

```python
def test_assertion_rejects_127_origin_for_localhost_rp(fake_passkey: FakePasskey) -> None:
    with pytest.raises(PasskeyError, match="ORIGIN_MISMATCH"):
        fake_passkey.verify(origin="http://127.0.0.1:8788", rp_id="localhost")

def test_strategy_assertion_cannot_issue_a_session_capability(issuer) -> None:
    with pytest.raises(CapabilityIssueError, match="MODE_MISMATCH"):
        issuer.issue(assertion=strategy_assertion(), request=session_request())
```

- [ ] **Step 3: Implement WebAuthn wrapper and canonical action challenge**

Use `generate_registration_options`, `verify_registration_response`,
`generate_authentication_options`, and `verify_authentication_response`. Require user verification,
exact `http://localhost:<port>`, RP ID `localhost`, one browser session, one CSRF token, one action
digest, and one unused challenge. First registration requires an already unlocked dedicated-wallet
fingerprint and an empty credential registry; adding/replacing a credential requires an assertion
from the existing credential. Persist only credential public data and assertion digest.

- [ ] **Step 4: Implement per-launch Ed25519 primary/recovery issuance**

```python
class PilotCapabilityIssuer:
    def issue(self, request: CapabilityRequest,
              assertion: VerifiedOperatorAssertion) -> IssuedGrantPair:
        self._require_open()
        self._verify_assertion_binding(request, assertion)
        return self._sign_primary_and_recovery(request, assertion)

    def close(self) -> None:
        self._private_key = None
        self._closed = True
```

Generate the Ed25519 key in memory at launch, pass only its public bytes through inherited startup
state, zero/drop private references on close, bind every approved hash/limit/nonce, and make exact
order/strategy grants single-use. Recovery expires no later than primary expiry plus 120 seconds.

- [ ] **Step 5: Extend dual authority verification**

Add mode, grant kind, action/session, ceiling, requested policy, presence, and recovery-policy
comparisons to both coordinator and signer contexts. A primary grant cannot validate a recovery
operation or credential route.

- [ ] **Step 6: Run focused tests and commit**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_passkeys.py tests/predictions/test_pilot_capabilities.py tests/predictions/test_execution_authority.py -q`

Expected: PASS.

```bash
git add pyproject.toml src/polytrading/predictions/pilot/passkeys.py src/polytrading/predictions/pilot/capabilities.py src/polytrading/predictions/execution/authority.py tests/predictions/test_pilot_passkeys.py tests/predictions/test_pilot_capabilities.py tests/predictions/test_execution_authority.py
git commit -m "feat(predictions): issue passkey-bound pilot grants"
```

### Task 5: macOS Keychain, signer bootstrap, and credential provisioning

**Files:**
- Create: `src/polytrading/predictions/polymarket_execution/keychain_macos.py`
- Create: `src/polytrading/predictions/polymarket_execution/credentials.py`
- Modify: `src/polytrading/predictions/polymarket_execution/secrets.py`
- Modify: `src/polytrading/predictions/polymarket_execution/ipc.py`
- Modify: `src/polytrading/predictions/polymarket_execution/signer.py`
- Test: `tests/predictions/test_polymarket_keychain.py`
- Test: `tests/predictions/test_polymarket_credentials.py`
- Test: `tests/predictions/test_polymarket_secret_boundary.py`

**Interfaces:**
- Produces `SecretBuffer`, `SecretStore`, `MacOSKeychainSecretStore`, `CredentialFingerprint`,
  `CredentialProvisioner`, `provision_credentials(grant, account_model) -> CredentialFingerprint`, and a signer bootstrap
  accepting inherited descriptors/public verifier state only.

- [ ] **Step 1: Write fake-store tests before touching macOS APIs**

```python
def test_provisioner_writes_secrets_without_returning_them(fake_store, fake_client) -> None:
    result = provisioner(fake_store, fake_client).provision(valid_credential_grant())
    assert result.credential_fingerprint
    assert fake_store.read("clob-api-secret") == TEST_SECRET
    assert TEST_SECRET not in repr(result)
```

- [ ] **Step 2: Implement the secret-store protocol and fake**

```python
class SecretStore(Protocol):
    def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
        raise SecretStoreError("ABSTRACT_SECRET_STORE")

    def write_protected(self, service: str, account: str, value: SecretBuffer) -> None:
        raise SecretStoreError("ABSTRACT_SECRET_STORE")
```

No method returns `str`; use mutable bounded buffers with redacted repr and existing close logic.

- [ ] **Step 3: Implement macOS Security-framework adapter**

Use Security framework calls through a narrow `ctypes` wrapper inside the signer process. Require
an interactive operation prompt on each launch read, reject non-Darwin platforms, enforce exact
service/account labels, cap returned bytes, and translate OS errors to stable codes. Never invoke
`security -w`, shell commands, environment variables, or stdout-bearing secret helpers.

- [ ] **Step 4: Implement credential-only signer operation**

Verify `CredentialProvisioningGrant`, call only the frozen create-or-derive operation, write all
returned secret fields directly to `SecretStore`, close response buffers, and return fingerprints.
Reject any execution grant, second use, wrong wallet, arbitrary host/path, or credential response
that cannot be fully stored atomically.

- [ ] **Step 5: Add lifecycle and canary tests**

Test launch unlock, denial, missing item, partial-write rollback, signer crash, transport close,
descriptor close, buffer close, and scans of logs/exceptions/IPC/database/JSON for seeded canaries.

- [ ] **Step 6: Run focused tests and commit**

Run: `.venv/bin/python -m pytest tests/predictions/test_polymarket_keychain.py tests/predictions/test_polymarket_credentials.py tests/predictions/test_polymarket_secret_boundary.py -q`

Expected: PASS; macOS API tests use an injected fake and never touch the real Keychain.

```bash
git add src/polytrading/predictions/polymarket_execution tests/predictions/test_polymarket_keychain.py tests/predictions/test_polymarket_credentials.py tests/predictions/test_polymarket_secret_boundary.py
git commit -m "feat(predictions): isolate pilot credentials in keychain"
```

### Task 6: Exact-origin Pilot control server

**Files:**
- Create: `src/polytrading/predictions/pilot/server.py`
- Create: `src/polytrading/predictions/pilot/runtime.py`
- Modify: `src/polytrading/predictions/cli.py`
- Test: `tests/predictions/test_pilot_server.py`
- Test: `tests/predictions/test_pilot_runtime.py`
- Test: `tests/predictions/test_execution_authority_scan.py`

**Interfaces:**
- Produces `PilotRequest`, `PilotResponse`, `PilotApplication.respond(PilotRequest) -> PilotResponse`,
  `serve_polymarket_pilot(database_path, port)`, and a safe server-launch CLI only.

- [ ] **Step 1: Write request-boundary tests**

Cover exact Host/Origin, absolute targets, oversized target/body/header, invalid JSON, duplicate
headers, missing/mismatched CSRF, absent session, reused challenge, OPTIONS/TRACE/CONNECT, CORS
absence, SameSite/HttpOnly cookie, CSP, frame/MIME/referrer/permissions headers, rate limits, and
every unregistered path/method.

- [ ] **Step 2: Run tests and confirm the server is absent**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_server.py -q`

Expected: FAIL on missing server imports.

- [ ] **Step 3: Implement a fixed route table**

```python
PILOT_ROUTES = {
    ("POST", "/api/v1/pilot/session"): "create_browser_session",
    ("GET", "/api/v1/pilot/readiness"): "readiness",
    ("GET", "/api/v1/pilot/policy"): "policy",
    ("GET", "/api/v1/pilot/opportunities"): "opportunities",
    ("GET", "/api/v1/pilot/live-session"): "live_session",
    ("GET", "/api/v1/pilot/audit"): "audit",
    ("POST", "/api/v1/pilot/policy"): "update_policy",
    ("POST", "/api/v1/pilot/passkeys/register/options"): "register_options",
    ("POST", "/api/v1/pilot/passkeys/register/verify"): "register_verify",
    ("POST", "/api/v1/pilot/passkeys/authenticate/options"): "auth_options",
    ("POST", "/api/v1/pilot/credentials/provision"): "provision_credentials",
    ("POST", "/api/v1/pilot/activation"): "activate",
    ("POST", "/api/v1/pilot/authorizations"): "authorize",
    ("POST", "/api/v1/pilot/presence"): "presence",
    ("POST", "/api/v1/pilot/stop"): "stop",
    ("POST", "/api/v1/pilot/kill/clear"): "clear_kill",
}
```

Use strict request DTOs; server handlers resolve stable IDs and recompute all action material.

- [ ] **Step 4: Compose fail-closed runtime and safe CLI launcher**

Runtime construction starts killed, validates migration/origin/RP/policy/source/passkey state, and
does not load secrets or transport until an operator request reaches the exact gated ceremony. The
CLI accepts only `--db` and `--port`; no credential or execution values.

- [ ] **Step 5: Prove observer isolation and run tests**

Extend authority scans so `dashboard_server.py` and existing web assets cannot import Pilot,
signer, credential, capability-issuer, or authenticated transport modules. Confirm the existing
dashboard still rejects POST.

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_server.py tests/predictions/test_pilot_runtime.py tests/predictions/test_dashboard_server.py tests/predictions/test_execution_authority_scan.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/polytrading/predictions/pilot/server.py src/polytrading/predictions/pilot/runtime.py src/polytrading/predictions/cli.py tests/predictions/test_pilot_server.py tests/predictions/test_pilot_runtime.py tests/predictions/test_execution_authority_scan.py
git commit -m "feat(predictions): add local pilot control plane"
```

### Task 7: Deterministic selector and frozen recovery compiler

**Files:**
- Create: `src/polytrading/predictions/pilot/selector.py`
- Modify: `src/polytrading/predictions/execution/models.py`
- Test: `tests/predictions/test_pilot_selector.py`
- Test: `tests/predictions/test_execution_models.py`

**Interfaces:**
- Produces `PilotOpportunity`, `FrozenRecoveryBranch`, `FrozenPilotPlan`,
  `eligible_opportunities(store, account, as_of)`, `rank_pilot_opportunities(opportunities)`, and
  `compile_frozen_pilot_plan(opportunity, limits, account_state)`.

- [ ] **Step 1: Write one failing test per proof family and rejection gate**

Use binary complement, exhaustive multi-outcome/negative-risk, implication, and within-Polymarket
equivalence fixtures. Reject missing current attestation, stale depth/fee, nonpositive current or
five-second surplus, incomplete loss at/above threshold, absent shadow replay, wrong wallet,
insufficient balance/allowance, kill, and cross-venue legs.

- [ ] **Step 2: Write deterministic ranking tests**

```python
def test_ranking_is_incomplete_loss_then_stressed_surplus_then_capacity_then_id() -> None:
    ranked = rank_pilot_opportunities(permuted_fixture())
    assert [item.proof_id for item in ranked] == EXPECTED_STABLE_IDS
```

- [ ] **Step 3: Implement eligibility and ranking from server-side artifacts**

Reuse `compile_proof`, existing economics/risk models, verified store reads, and Task 2
qualification. Do not add a composite score. Persist/display the first tie-break field.

- [ ] **Step 4: Implement frozen normal and recovery plans**

Compile exact tokens, FAK/FOK type, leg order, sizes, prices, deadlines, fees, gross notional,
deployed capital, incomplete exposure, recovery directions, and evidence hashes. Prove each recovery
branch reduces worst-case incomplete exposure and never increases deployed capital.

- [ ] **Step 5: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_selector.py tests/predictions/test_execution_models.py -q`

Expected: PASS.

```bash
git add src/polytrading/predictions/pilot/selector.py src/polytrading/predictions/execution/models.py tests/predictions/test_pilot_selector.py tests/predictions/test_execution_models.py
git commit -m "feat(predictions): compile deterministic pilot plans"
```

### Task 8: Exact order, complete strategy, and bounded recovery execution

**Files:**
- Modify: `src/polytrading/predictions/execution/coordinator.py`
- Modify: `src/polytrading/predictions/polymarket_execution/signer.py`
- Create: `src/polytrading/predictions/pilot/sessions.py`
- Test: `tests/predictions/test_pilot_execution.py`
- Modify: `tests/predictions/test_execution_coordinator.py`
- Modify: `tests/predictions/test_execution_recovery.py`

**Interfaces:**
- Produces `ExecutionResult`, `StopReason`, `StopResult`, `ContinuationDecision`,
  `PilotExecutor.execute_exact_order()`, `execute_complete_strategy()`, and `stop()`; consumes
  Tasks 4 and 7 grant/plan types.

- [ ] **Step 1: Write exact-order scope tests**

Prove exact order rejects a position-increasing side/size, unknown position, stale account read,
more than USD 10, non-FAK/FOK, second use, or an order absent from frozen recovery authority.

- [ ] **Step 2: Write multi-leg lifecycle tests**

Cover full fills, partial FAK, FOK rejection, delayed/live/unknown ack, response lost before/after
acceptance, post-fill stale book/fee/account, next-leg ineligibility, recovery fill/rejection/expiry,
and exact reconciliation. Assert persist-before-sign/submit and no blind POST retry.

- [ ] **Step 3: Implement coordinator entry points and authoritative continuation**

```python
class PilotExecutor:
    def execute_exact_order(self, plan: FrozenPilotPlan,
                            grants: IssuedGrantPair) -> ExecutionResult:
        self._require_risk_reducing_exact_order(plan)
        return self._run_frozen_plan(plan, grants, maximum_legs=1)

    def execute_complete_strategy(self, plan: FrozenPilotPlan,
                                  grants: IssuedGrantPair) -> ExecutionResult:
        return self._run_frozen_plan(plan, grants, maximum_legs=len(plan.normal_intents))

    def stop(self, reason: StopReason) -> StopResult:
        return self._revoke_primary_and_engage_kill(reason)
```

After each venue result, persist, fetch authoritative order/trade/balance/allowance state, post
eligible ledger entries, recompute every limit/evidence gate, and then continue, recover, or halt.

- [ ] **Step 4: Enforce signer transport lifetime and grant kind**

Construct authenticated transport only after independent validation. Destroy primary transport on
completion/kill; allow only the recovery route set until its 120-second deadline; then retain
read-only reconciliation or require a fresh recovery passkey.

- [ ] **Step 5: Run focused execution tests and commit**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_execution.py tests/predictions/test_execution_coordinator.py tests/predictions/test_execution_recovery.py -q`

Expected: PASS.

```bash
git add src/polytrading/predictions/pilot/sessions.py src/polytrading/predictions/execution/coordinator.py src/polytrading/predictions/polymarket_execution/signer.py tests/predictions/test_pilot_execution.py tests/predictions/test_execution_coordinator.py tests/predictions/test_execution_recovery.py
git commit -m "feat(predictions): execute bounded pilot strategies"
```

### Task 9: Automation session, presence, manifest promotion, and kill clearance

**Files:**
- Create: `src/polytrading/predictions/pilot/presence.py`
- Create: `src/polytrading/predictions/pilot/presence_macos.py`
- Modify: `src/polytrading/predictions/pilot/sessions.py`
- Modify: `src/polytrading/predictions/execution/kill_switch.py`
- Modify: `src/polytrading/predictions/manifest.py`
- Test: `tests/predictions/test_pilot_sessions.py`
- Test: `tests/predictions/test_pilot_presence.py`
- Test: `tests/predictions/test_pilot_activation.py`
- Modify: `tests/predictions/test_execution_kill_switch.py`

**Interfaces:**
- Produces `SessionDecision`, `PresenceMonitor.record_browser_heartbeat()`,
  `record_native_state()`, `AutomationSessionRunner.tick()`, `promote_pilot_manifest()`,
  `invalidate_pilot_manifest()`, and
  `clear_pilot_kill(clearance_request) -> PilotKillClearanceEvent`.

- [ ] **Step 1: Write injected-clock session/presence tests**

Test heartbeat every two seconds, kill after two misses or five seconds, immediate sleep/lock kill,
15-minute hard expiry, no new strategy in final 60 seconds, USD 50 deployment, one active strategy,
USD 5 session loss, USD 10 UTC-day loss, stop, restart, and no prompt per opportunity.

- [ ] **Step 2: Implement monitor and sequential session runner**

```python
class AutomationSessionRunner:
    def tick(self, now: datetime) -> SessionDecision:
        state = self._load_authoritative_state(now)
        return self._derive_and_persist_decision(state, now)

    def stop(self, reason: str, now: datetime) -> PilotExecutionSession:
        return self._persist_stopped_session(reason, now)
```

Every tick reads persisted/authoritative state; it never trusts browser clocks or counters.
Implement `MacOSPresenceSource` behind an injected port: screen-lock notification kills
immediately, and a monotonic-clock jump beyond the heartbeat budget classifies sleep/wake as a
kill before any new decision. Tests inject notifications and clocks; they do not lock or sleep the
development machine.

- [ ] **Step 3: Write manifest promotion/invalidation tests**

Promotion requires Stages 0–2, current attestation/geoblock/protocol/qualification/account/policy,
and a Stage 3 passkey assertion. It appends LIVE_ELIGIBLE but creates no capability. Expiry,
source change, evidence failure, account mismatch, or operator deactivation appends LIVE_DISABLED,
revokes grants, and kills.

- [ ] **Step 4: Write and implement clearance tests**

Require no active/UNKNOWN submissions, authoritative order/trade/balance/allowance/position/
settlement/ledger equality, discrepancy review hash, exact phrase, and fresh passkey. Return an
append-only clearance event with no standing grant. Empty production history remains killed.

- [ ] **Step 5: Run focused tests and commit**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_sessions.py tests/predictions/test_pilot_presence.py tests/predictions/test_pilot_activation.py tests/predictions/test_execution_kill_switch.py -q`

Expected: PASS.

```bash
git add src/polytrading/predictions/pilot src/polytrading/predictions/execution/kill_switch.py src/polytrading/predictions/manifest.py tests/predictions/test_pilot_sessions.py tests/predictions/test_pilot_presence.py tests/predictions/test_pilot_activation.py tests/predictions/test_execution_kill_switch.py
git commit -m "feat(predictions): enforce pilot sessions and activation"
```

### Task 10: Coherent Pilot read models and observer posture

**Files:**
- Create: `src/polytrading/predictions/pilot/read_models.py`
- Modify: `src/polytrading/predictions/dashboard_models.py`
- Modify: `src/polytrading/predictions/dashboard.py`
- Test: `tests/predictions/test_pilot_read_models.py`
- Modify: `tests/predictions/test_dashboard_models.py`

**Interfaces:**
- Produces `PilotSnapshot` with readiness, limits, opportunities, active session, activation, and
  audit sections from one cutoff; adds sanitized LIVE_ELIGIBLE/LIVE_DISABLED posture to observer.

- [ ] **Step 1: Write one-cutoff and redaction tests**

Build two revisions and prove the snapshot rejects mixed hashes/cutoffs. Search serialized payloads
for wallet/API/passkey/capability canaries. Hide all financial totals when reconciliation is not
exact and mark stale/unknown fields explicitly.

- [ ] **Step 2: Implement focused read-model builders**

Keep each section builder small and pure. Include evidence age/hash, immutable versus requested
limit, rank tie-break, current/stressed surplus, incomplete exposure, recovery tree, authority
expiry, heartbeat, loss budgets, and blocker codes.

- [ ] **Step 3: Adapt observer dashboard without control imports**

Remove the assumption that every stored manifest must be LIVE_DISABLED; display current sanitized
pilot posture and keep every route/method/dependency read-only. Add an authority scan asserting no
Pilot control module is reachable from observer construction.

- [ ] **Step 4: Run tests and commit**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_read_models.py tests/predictions/test_dashboard_models.py tests/predictions/test_dashboard.py -q`

Expected: PASS.

```bash
git add src/polytrading/predictions/pilot/read_models.py src/polytrading/predictions/dashboard_models.py src/polytrading/predictions/dashboard.py tests/predictions/test_pilot_read_models.py tests/predictions/test_dashboard_models.py
git commit -m "feat(predictions): expose sanitized pilot state"
```

### Task 11: Beautiful local Pilot UI and confirmation ceremonies

**Files:**
- Create: `src/polytrading/predictions/pilot_web_assets/__init__.py`
- Create: `src/polytrading/predictions/pilot_web_assets/index.html`
- Create: `src/polytrading/predictions/pilot_web_assets/app.css`
- Create: `src/polytrading/predictions/pilot_web_assets/app.js`
- Create: `src/polytrading/predictions/pilot_web_assets/api.js`
- Create: `src/polytrading/predictions/pilot_web_assets/store.js`
- Create: `src/polytrading/predictions/pilot_web_assets/views.js`
- Modify: `src/polytrading/predictions/pilot/server.py`
- Modify: `pyproject.toml`
- Create: `docs/predictions/assets/polymarket-live-pilot/readiness-1440.png`
- Create: `docs/predictions/assets/polymarket-live-pilot/approval-900.png`
- Create: `docs/predictions/assets/polymarket-live-pilot/live-1440.png`
- Create: `docs/predictions/assets/polymarket-live-pilot/recovery-900.png`
- Create: `docs/predictions/assets/polymarket-live-pilot/killed-390.png`
- Test: `tests/predictions/test_pilot_web_assets.py`
- Test: `tests/predictions/test_pilot_server.py`

**Interfaces:**
- Produces four primary views: Readiness, Limits, Opportunity Approval, and Live Session; browser
  API uses only the Task 6 route table.

- [ ] **Step 1: Write structural asset tests**

Assert semantic landmarks, labels, live regions, visible focus, reduced motion, no inline/remote
script/style/font, no `innerHTML`, no local/session storage, no external URL, no raw token/order
builder, exact confirmation text, immutable ceiling copy, prominent Stop-and-kill, and disabled
cross-venue cards.

- [ ] **Step 2: Implement the Market Atlas execution cockpit**

Use existing slate/cyan/amber/coral tokens with a distinct execution rail. Build responsive
desktop/tablet/mobile layouts. Show proof, legs, FAK/FOK, current/stressed economics, incomplete
loss, recovery, evidence hashes, capability expiry, time/capital/loss budgets, heartbeat,
reconciliation, and stable blocker codes. Stop-only mobile behavior applies without the enrolled
platform credential.

- [ ] **Step 3: Implement typed confirmation and WebAuthn browser flow**

The server supplies `ORDER <amount> USD`, `STRATEGY <amount> USD`, or
`SESSION 15 MIN <amount> USD`; exact input unlocks the WebAuthn call. Keep the final summary visible
through approval. Do not optimistically mark success; fetch the next coherent snapshot.

- [ ] **Step 4: Add UI state and security tests**

Cover ready/blocked/stale/unknown/killed/recovery/live/reconciled, validation errors, reused
challenge, CSRF failure, disconnect, reduced motion, keyboard order, and accessible names.

- [ ] **Step 5: Run focused tests and deterministic screenshots**

Run: `.venv/bin/python -m pytest tests/predictions/test_pilot_web_assets.py tests/predictions/test_pilot_server.py -q`

Expected: PASS.

Start only the fake-data local Pilot server. Capture the five exact documentation assets listed in
this task for readiness, opportunity approval, live session, UNKNOWN/recovery, and killed states.
Never load the real keychain or authenticated transport. Inspect every image and fix clipping,
overflow, weak hierarchy, ambiguous danger controls, focus, and contrast before committing.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/polytrading/predictions/pilot_web_assets src/polytrading/predictions/pilot/server.py tests/predictions/test_pilot_web_assets.py tests/predictions/test_pilot_server.py docs/predictions/assets/polymarket-live-pilot
git commit -m "feat(predictions): add live pilot cockpit"
```

### Task 12: Operator documentation, activation gate, and repository verification

**Files:**
- Create: `docs/predictions/polymarket-live-pilot.md`
- Modify: `README.md`
- Modify: `tests/predictions/test_execution_authority_scan.py`
- Modify: `tests/predictions/test_execution_secret_scan.py`
- Create: `tests/predictions/test_pilot_acceptance.py`

**Interfaces:**
- Produces the complete operator runbook and one acceptance test mapping every spec criterion to a
  stable gate; does not trigger Stage 4.

- [ ] **Step 1: Write the operator guide**

Include ten-minute setup, external wallet/backup, native Keychain enrollment, passkey registration,
signer-only credential provisioning, manual funding/allowance, readiness fields, proof/economics/
FAK-FOK/UNKNOWN/reconciliation explanations, all three mode walkthroughs, annotated screenshots,
pre/post checklists, Stage 0–4 activation, and every recovery playbook from spec section 11. State
prominently that secrets never belong in UI, CLI, `.env`, logs, screenshots, tickets, chat, email,
or support messages.

- [ ] **Step 2: Write acceptance and authority tests**

Test every design acceptance criterion, including observer isolation, no real-network factory in
tests, credential grant separation, compiled ceilings at three boundaries, three modes, FAK/FOK,
cross-venue/AI/value-transfer exclusion, evidence recomputation, restart killed, clearance with no
capability, and first-live ceiling `min(USD 5, venue-valid complete size)`.

- [ ] **Step 3: Run focused acceptance and static checks**

Run:

```bash
.venv/bin/python -m pytest tests/predictions/test_pilot_acceptance.py tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py -q
.venv/bin/ruff check src/polytrading/predictions tests/predictions
.venv/bin/ruff format --check src/polytrading/predictions tests/predictions
```

Expected: all commands pass and no secret canary appears in output.

- [ ] **Step 4: Measure focused and full-suite performance**

Run:

```bash
time .venv/bin/python -m pytest tests/predictions/test_pilot_*.py -q --durations=25
time .venv/bin/python -m pytest -q --durations=50
```

Expected: focused p50 under 30 seconds; full-suite p50 at or below 181 seconds; every new test over
two seconds is identified. Replace real waits, repeated key generation, per-test migrations, or
uncached immutable fixtures before requesting an exception.

- [ ] **Step 5: Verify packaging and clean installation**

Build/install in a new temporary virtual environment, assert migration 011/package assets/fixtures
are present, run conformance and Pilot `--help`, and prove the server starts killed with fake data.
Do not provide a real secret, unlock Keychain, call credential provisioning, or enter Stage 4.

- [ ] **Step 6: Review final diff against the spec**

Confirm no staged `.claude/settings.json`, `.playwright-mcp/`, screenshots, database, credential,
keychain artifact, or generated secret. Confirm all official source hashes and dependency pins are
recorded and all documentation consistently says only the operator may trigger live execution.

- [ ] **Step 7: Commit documentation and final gates**

```bash
git add README.md docs/predictions/polymarket-live-pilot.md tests/predictions/test_pilot_acceptance.py tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py
git commit -m "docs(predictions): document local live pilot"
```

## Spec Coverage Map

| Design section | Implemented and verified by |
| --- | --- |
| 1–2 Decision and success | Global constraints; Tasks 6, 8, 11, 12 |
| 3 Pilot envelope | Tasks 1, 2, 8, 9 |
| 4 Trust boundaries | Tasks 4, 5, 6; authority scans in Tasks 6 and 12 |
| 5 Eligibility/protocol/evidence | Tasks 1, 2, 3, 9 |
| 6 Deterministic strategy | Tasks 7 and 8 |
| 7 Capability/session lifecycle | Tasks 4, 8, 9 |
| 8 Persistence | Task 1; tamper/replay checks in Tasks 8–10 |
| 9 End-to-end flow | Tasks 5, 6, 8, 9 |
| 10 Pilot UI | Tasks 10 and 11 |
| 11 Operator documentation | Task 12 |
| 12 Error/recovery rules | Tasks 5, 8, 9 |
| 13 Testing/performance | Focused gates in every task; final gate in Task 12 |
| 14 Staged rollout | Tasks 2, 9, 12 |
| 15 Boundaries/non-goals | Global constraints and Task 12 authority scan |
| 16 Delivery sequence | Tasks 1–12 in dependency order |
| 17 Acceptance criteria | Task 12 acceptance matrix |
| 18 Current references | Task 3 source checkpoint and Task 12 runbook |

## Execution Handoff Checkpoint

After Task 12 passes, implementation is complete only through Stage 3 readiness. Stop with the
account killed and no standing capability. Report evidence-window status, protocol/source hashes,
operator-drill status, test timings, and every remaining blocker. Do not trigger credential
provisioning or the first live strategy on the operator's behalf.
