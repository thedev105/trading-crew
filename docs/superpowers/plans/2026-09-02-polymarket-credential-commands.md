# Polymarket credential commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit, secret-safe commands to check local CLOB credential readiness and to create the three missing CLOB credentials through the one reviewed Polymarket L1 endpoint.

**Architecture:** The CLI is a thin public adapter: it accepts only the fixed command shape and renders typed public status or stable error codes. A dedicated, short-lived credential sidecar reuses the inherited-descriptor secret boundary, validates a wallet-derived account binding, invokes `HttpxCredentialClient` with `operation="CREATE"` exactly once, and uses `CredentialProvisioner` to persist the returned trio before returning only a public fingerprint. The parent process never gets the wallet or returned credentials, and `check` uses only the macOS Keychain adapter.

**Tech Stack:** Python 3.12+, argparse, macOS Keychain Security.framework via ctypes, `httpx`, `eth-account`, Pydantic, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-02-polymarket-credential-commands-design.md`

## Global Constraints

- Commands are exactly `polytrading predictions pilot credentials check` and `polytrading predictions pilot credentials create --confirm`; no secret, wallet, database, endpoint, or credential flags are accepted.
- `check` is Keychain-only and must not construct a network client, derive a remote credential, or mutate the Keychain.
- `create` requires literal `--confirm`, supports only `POST https://clob.polymarket.com/auth/api-key`, and never silently derives, recovers, overwrites, trades, cancels, transfers, approves, or activates a pilot.
- Wallet and CLOB credential bytes may not appear in argv, environment, parent process state, stdout, stderr, logs, exceptions, database, browser, or IPC response; public fingerprints and stable codes are the only observable output.
- Use the fixed service `polytrading.polymarket.pilot` and only `wallet-private-key`, `clob-api-key`, `clob-api-secret`, and `clob-passphrase` account labels. Unsupported platforms fail closed.
- Existing CLOB items fail closed as `CREDENTIALS_ALREADY_PRESENT`; partial CLOB state fails closed as `CREDENTIALS_PARTIAL`; no create request is made in either case.
- A failed write rolls back every CLOB item written by this ceremony, leaving no partial trio. Automated tests use fake stores/transports only; do not make a live credential request.
- Preserve the existing `predictions pilot polymarket --db --port` surface and the killed-by-default, externally gated live-pilot posture.

---

### Task 1: Define the credential-command public model and local Keychain check

**Files:**
- Create: `src/polytrading/predictions/pilot/credential_commands.py`
- Test: `tests/predictions/test_pilot_credential_commands.py`

**Interfaces:**
- Consumes: `SecretStore.read_required(service, account, prompt) -> SecretBuffer`, `MacOSKeychainSecretStore`, `CLOB_SERVICE`, the four reviewed account constants, and `SecretStoreError.code`.
- Produces: `CredentialReadiness` with `wallet_ready: bool`, `credentials_state: Literal["PRESENT", "ABSENT", "PARTIAL"]`; `check_credential_readiness(store: SecretStore) -> CredentialReadiness`; `CredentialCommandError(code: str)` with no exception text.
- Produces: `render_credential_readiness(result: CredentialReadiness) -> str`, whose fixed output is `wallet_ready=<true|false>\ncredentials=<PRESENT|ABSENT|PARTIAL>`.

- [ ] **Step 1: Write failing readiness and secrecy tests**

```python
def test_check_is_local_only_and_reports_a_valid_wallet_with_no_clob_credentials() -> None:
    store = wallet_only_store()
    result = check_credential_readiness(store)
    assert result == CredentialReadiness(wallet_ready=True, credentials_state="ABSENT")
    assert store.network_opened is False
    assert "wallet-canary" not in render_credential_readiness(result)


def test_check_reports_partial_clob_state_without_returning_a_value() -> None:
    store = wallet_and_one_clob_item_store()
    result = check_credential_readiness(store)
    assert result.credentials_state == "PARTIAL"
    assert "clob-canary" not in render_credential_readiness(result)
```

- [ ] **Step 2: Run the new readiness tests to verify they fail**

Run: `rtk pytest -q tests/predictions/test_pilot_credential_commands.py -k readiness`

Expected: FAIL because `credential_commands` and its public readiness model do not exist.

- [ ] **Step 3: Implement the local-only check model and function**

```python
@dataclass(frozen=True, slots=True)
class CredentialReadiness:
    wallet_ready: bool
    credentials_state: Literal["PRESENT", "ABSENT", "PARTIAL"]


def check_credential_readiness(store: SecretStore) -> CredentialReadiness:
    wallet_ready = _item_is_valid_wallet(store)
    present = tuple(_item_is_present(store, account) for account in _CREDENTIAL_ACCOUNTS)
    state = "PRESENT" if all(present) else "ABSENT" if not any(present) else "PARTIAL"
    return CredentialReadiness(wallet_ready=wallet_ready, credentials_state=state)
```

Close every buffer immediately after using it. Map Keychain failures to the public codes `WALLET_MISSING`, `WALLET_INVALID`, `KEYCHAIN_ACCESS_DENIED`, and `KEYCHAIN_UNAVAILABLE`; never re-raise or format the underlying exception. Validate the normalized wallet only by its closed `SecretBuffer` length (`32`), relying on `MacOSKeychainSecretStore.read_required` for the reviewed hexadecimal normalization.

- [ ] **Step 4: Run the readiness tests to verify they pass**

Run: `rtk pytest -q tests/predictions/test_pilot_credential_commands.py -k readiness`

Expected: PASS, including absent, present, partial, denied, unavailable, and invalid-wallet cases with no canary observable.

- [ ] **Step 5: Commit the isolated local check**

```bash
rtk git add src/polytrading/predictions/pilot/credential_commands.py tests/predictions/test_pilot_credential_commands.py
rtk git commit -m "feat(predictions): add local credential readiness check"
```

### Task 2: Add a narrow create-only signer-side credential ceremony

**Files:**
- Modify: `src/polytrading/predictions/pilot/signer_bootstrap.py`
- Modify: `src/polytrading/predictions/pilot/credential_commands.py`
- Modify: `src/polytrading/predictions/polymarket_execution/credentials.py`
- Test: `tests/predictions/test_pilot_credential_commands.py`
- Test: `tests/predictions/test_pilot_signer_bootstrap.py`
- Test: `tests/predictions/test_polymarket_credentials.py`

**Interfaces:**
- Consumes: `launch_signer_sidecar`'s inherited descriptor layout, `read_secret_descriptors`, `SecretMaterial`, `HttpxCredentialClient`, `bind_account_signature`, `CredentialProvisioner`, `CredentialProvisioningGrant`, and the frozen protocol snapshot/credential-route hash.
- Produces: `create_credentials_in_sidecar(*, store: SecretStore, now: Callable[[], datetime]) -> CredentialFingerprint` that returns only the existing public `CredentialFingerprint`. Test-only child composition may inject a fake `CredentialClient`; the CLI has no transport parameter.
- Produces: `CredentialCommandError` codes `CONFIRMATION_REQUIRED`, `CREDENTIALS_ALREADY_PRESENT`, `CREDENTIALS_PARTIAL`, `SIGNER_BOOTSTRAP_FAILED`, `CREDENTIAL_CREATE_FAILED`, and `CREDENTIAL_STORE_FAILED`.

- [ ] **Step 1: Write failing create-boundary tests**

```python
def test_create_requires_confirmation_before_keychain_or_network_access() -> None:
    store = wallet_only_store()
    with pytest.raises(CredentialCommandError, match="CONFIRMATION_REQUIRED"):
        create_credentials(store=store, confirmed=False, child_runner=unexpected_child)
    assert store.calls == []


def test_create_runs_only_create_in_the_child_and_returns_a_fingerprint() -> None:
    store = wallet_only_store()
    result = create_credentials(store=store, confirmed=True, child_runner=fake_child)
    assert result.result == "CREATED"
    assert result.credential_fingerprint == sha256(API_KEY).hexdigest()
    assert fake_client.requests == [("POST", "https://clob.polymarket.com/auth/api-key")]
    assert no_public_observable_contains(CANARIES, result, store.parent_observables)


def test_existing_or_partial_credentials_fail_before_the_child_or_network() -> None:
    for store, code in ((complete_store(), "CREDENTIALS_ALREADY_PRESENT"), (partial_store(), "CREDENTIALS_PARTIAL")):
        with pytest.raises(CredentialCommandError, match=code):
            create_credentials(store=store, confirmed=True, child_runner=unexpected_child)
```

- [ ] **Step 2: Run the create-boundary tests to verify they fail**

Run: `rtk pytest -q tests/predictions/test_pilot_credential_commands.py -k create`

Expected: FAIL because the create-only ceremony does not exist.

- [ ] **Step 3: Factor the descriptor bootstrap only as far as needed for a one-shot child ceremony**

Add a private one-shot child launch path adjacent to `launch_signer_sidecar`, reusing `_read_secrets`, `_write_framed_secret`, and the exact four-descriptor contract. The parent must close its `SecretBuffer` copies before starting the child; the child must call `read_secret_descriptors`, build the wallet-derived `AccountSignatureBinding`, and close `SecretMaterial` in `finally`. The response pipe transports a canonical public result only:

```python
@dataclass(frozen=True, slots=True)
class CredentialCeremonyResult:
    ok: bool
    code: str
    credential_fingerprint: str | None = None

# The child only ever performs this fixed operation.
grant = CredentialProvisioningGrant(
    grant_kind="CREDENTIAL_PROVISIONING",
    operation="CREATE",
    protocol_version=POLYMARKET_PILOT_PROTOCOL_VERSION,
    issued_at=now,
    expires_at=now + MAXIMUM_GRANT_LIFETIME,
    ...,
)
```

Do not add a generic signer RPC, an operation argument, a derive branch, or a secret-bearing response. Before spawning, read only the CLOB account presence state; reject `PRESENT` and `PARTIAL`. In the child, construct `HttpxCredentialClient(private_key=..., timestamp=...)` with its private fixed production-client factory, bind the signer address from `Account.from_key`, and call `CredentialProvisioner(...).provision(grant, binding, now=...)`. The child writes the Keychain trio directly and zeroizes all material before it exits. Tests inject a fake child/client at this internal composition seam; the CLI cannot.

Tighten `CredentialProvisioner`'s rollback ownership so it can only delete CLOB entries newly written by this ceremony. The command's preflight absence check means a failed create leaves no partially created trio and never deletes a pre-existing credential.

- [ ] **Step 4: Add failure and rollback tests, then make them pass**

```python
@pytest.mark.parametrize("failure", ["transport", "invalid_response", "second_keychain_write"])
def test_create_failure_leaves_no_clob_trio(failure: str) -> None:
    store, factory = failing_create_setup(failure)
    with pytest.raises(CredentialCommandError):
        create_credentials(store=store, confirmed=True, client_factory=factory)
    assert clob_presence(store) == (False, False, False)
```

Run: `rtk pytest -q tests/predictions/test_pilot_credential_commands.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_polymarket_credentials.py`

Expected: PASS. Assert no child-side request uses `/auth/derive-api-key`, no order/cancel/allowance handler is reachable, and every fake response buffer is closed on both success and failure.

- [ ] **Step 5: Commit the create-only ceremony**

```bash
rtk git add src/polytrading/predictions/pilot/signer_bootstrap.py src/polytrading/predictions/pilot/credential_commands.py src/polytrading/predictions/polymarket_execution/credentials.py tests/predictions/test_pilot_credential_commands.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_polymarket_credentials.py
rtk git commit -m "feat(predictions): create CLOB credentials through signer sidecar"
```

### Task 3: Wire the fixed CLI commands and sanitized output

**Files:**
- Modify: `src/polytrading/predictions/cli.py`
- Modify: `src/polytrading/cli.py` only if top-level error mapping requires a new explicit `CredentialCommandError` branch
- Test: `tests/predictions/test_cli.py`
- Test: `tests/predictions/test_pilot_credential_commands.py`

**Interfaces:**
- Consumes: `check_credential_readiness`, `create_credentials`, and `CredentialCommandError` from `pilot.credential_commands`.
- Produces: parser fields `predictions_pilot_command == "credentials"`, `predictions_pilot_credentials_command in {"check", "create"}`, and `confirm: bool` only on `create`.
- Produces: `_run_pilot_credentials(arguments, *, stream: TextIO, error_stream: TextIO) -> int`, returning `0` for valid check/create and `64` for rejected local invocation or ceremony failure.

- [ ] **Step 1: Write failing parser and CLI-rendering tests**

```python
def test_pilot_credentials_command_has_only_check_and_confirmed_create() -> None:
    parsed = build_parser().parse_args(["predictions", "pilot", "credentials", "create", "--confirm"])
    assert parsed.predictions_pilot_command == "credentials"
    assert parsed.predictions_pilot_credentials_command == "create"
    assert parsed.confirm is True


@pytest.mark.parametrize("argv", [
    ["predictions", "pilot", "credentials", "create"],
    ["predictions", "pilot", "credentials", "check", "--private-key", "x"],
    ["predictions", "pilot", "credentials", "create", "--endpoint", "https://example.test"],
])
def test_pilot_credentials_rejects_missing_confirmation_and_secret_or_network_flags(argv: list[str]) -> None:
    assert main(argv) == 64
```

- [ ] **Step 2: Run the CLI tests to verify they fail**

Run: `rtk pytest -q tests/predictions/test_cli.py -k credentials`

Expected: FAIL because the `credentials` parser and runner do not exist.

- [ ] **Step 3: Add the narrow parser tree and runner**

```python
credentials = pilot_commands.add_parser("credentials", help="check or explicitly create CLOB credentials")
credentials_commands = credentials.add_subparsers(dest="predictions_pilot_credentials_command", required=True)
credentials_commands.add_parser("check", help="check local Keychain credential readiness")
create = credentials_commands.add_parser("create", help="create CLOB credentials once")
create.add_argument("--confirm", action="store_true")
```

Dispatch `credentials` before the existing `pilot polymarket` server branch. Use a `MacOSKeychainSecretStore()` created at the CLI boundary, pass no external endpoint or secret values, and print only stable fields. The check output must contain no remote-derived data. A successful create output must be exactly `result=CREATED` and `credential_fingerprint=<64-hex>`; all errors print only `polytrading: credential command failed: <STABLE_CODE>` with exit `64`.

- [ ] **Step 4: Run CLI and secret-boundary tests to verify they pass**

Run: `rtk pytest -q tests/predictions/test_cli.py -k credentials && rtk pytest -q tests/predictions/test_pilot_credential_commands.py tests/predictions/test_polymarket_secret_boundary.py`

Expected: PASS. Include a canary test that captures stdout/stderr and verifies neither stream contains wallet, API key, secret, passphrase, headers, response body, exception text, or a raw Keychain path.

- [ ] **Step 5: Commit CLI wiring**

```bash
rtk git add src/polytrading/predictions/cli.py src/polytrading/cli.py tests/predictions/test_cli.py tests/predictions/test_pilot_credential_commands.py
rtk git commit -m "feat(predictions): expose CLOB credential commands"
```

### Task 4: Preserve authority-scan guarantees and update operator documentation

**Files:**
- Modify: `tests/predictions/test_execution_authority_scan.py`
- Modify: `tests/predictions/test_execution_secret_scan.py` if the new child boundary needs an explicit static secret-flow assertion
- Modify: `src/polytrading/predictions/polymarket_execution/credential_client.py`
- Modify: `README.md`
- Modify: `docs/predictions/polymarket-live-pilot.md`
- Modify: `docs/predictions/polymarket-execution-hardening.md`

**Interfaces:**
- Consumes: `_REVIEWED_SOURCE_SHA256`, `_PREIMPORT_SOURCE_PATHS`, and existing AST authority/transport checks.
- Produces: reviewed source manifests that include `pilot/signer_bootstrap.py` and `pilot/credential_commands.py` in addition to changed execution modules, and reject unreviewed `httpx`, arbitrary routes, generic credential RPCs, argv/environment secret access, or browser/database secret flow.

- [ ] **Step 1: Add failing static-safety/documentation assertions**

```python
def test_credential_command_source_has_one_fixed_create_route_and_no_secret_ingress() -> None:
    source = (PREDICTIONS_ROOT / "pilot" / "credential_commands.py").read_text()
    assert "derive-api-key" not in source
    assert "os.environ" not in source
    assert "sys.argv" not in source
    assert "submit_order" not in source
    assert "cancel_order" not in source
```

Add the corresponding expected digest entries only after reviewing the complete final source bytes. Extend both `_PREIMPORT_SOURCE_PATHS` and `_authority_sensitive_source_bytes()` with the two credential-sidecar modules; do not weaken the manifest to skip the new sidecar module.

- [ ] **Step 2: Run the safety slice to verify it fails before manifest/update work**

Run: `rtk pytest -q tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py`

Expected: FAIL with the expected source-manifest mismatch or missing credential-command safety assertion.

- [ ] **Step 3: Update the reviewed digest manifest and write precise runbook instructions**

Move the production `httpx.Client` factory into `HttpxCredentialClient` behind a fixed, private factory used only by the sidecar. Update the AST allowlist precisely: `httpx.Client` may occur once in `polymarket_execution/credential_client.py`, with a fixed 10-second timeout and no caller-provided URL/transport; the existing four-item constructor inventory therefore becomes five items. The only public client method remains `create_or_derive`, and the command calls it only with literal `"CREATE"`.

Document the two commands, their exact output categories, macOS-only behavior, the valid wallet format (64 hexadecimal characters, optional `0x`), the requirement to run `check` first, and that `create --confirm` makes one real external CLOB credential request but never trades. State that the command must be run by the operator—not CI or an agent—and that it fails rather than overwrites/recovery-derives existing credentials. Retain all external eligibility, legal/KYC/terms/geoblock, funding/allowance, shadow-evidence, separate-activation, passkey, and killed-by-default gates.

- [ ] **Step 4: Run formatting and the complete targeted proof**

Run: `rtk ruff format --check . && rtk ruff check . && rtk pytest -q tests/predictions/test_pilot_credential_commands.py tests/predictions/test_cli.py tests/predictions/test_polymarket_credentials.py tests/predictions/test_polymarket_credential_client.py tests/predictions/test_polymarket_secret_boundary.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py`

Expected: PASS. The only permitted network constructor remains inside the reviewed signer-side credential client path, all test transports are fake, and no live Polymarket request is run.

- [ ] **Step 5: Commit safety manifest and docs**

```bash
rtk git add tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py README.md docs/predictions/polymarket-live-pilot.md docs/predictions/polymarket-execution-hardening.md
rtk git commit -m "docs(predictions): document safe CLOB credential setup"
```

### Task 5: Run the final regression proof and hand off safely

**Files:**
- Modify: no production files unless a failing test demonstrates a concrete regression
- Test: entire repository suite

**Interfaces:**
- Consumes: all commands and safety invariants from Tasks 1–4.
- Produces: evidence-backed completion report; no live credential creation or pilot restart.

- [ ] **Step 1: Run the full suite**

Run: `rtk pytest -q`

Expected: PASS, except only the documented sandbox-only AF_UNIX spawned-sidecar cases if the restricted environment reproduces that known limitation. Record exact counts and causes; do not classify an assertion/test failure as a sandbox limitation.

- [ ] **Step 2: Inspect the final diff and working tree**

Run: `rtk git diff --check && rtk git status --short && rtk git log --oneline -5`

Expected: no whitespace errors, only intended changes, and commits that map to the tasks above.

- [ ] **Step 3: Run verification-before-completion and request a code review**

Use `superpowers:verification-before-completion`, then `superpowers:requesting-code-review`. Include the exact targeted/full test results, any sandbox exception, and confirmation that no live `create` command was run.

- [ ] **Step 4: Commit any final deterministic fixes and present operator next steps**

```bash
rtk git add <only-reviewed-final-fix-files>
rtk git commit -m "fix(predictions): harden CLOB credential command"
```

The final handoff should direct the operator to run `check`, review `ABSENT` or `PRESENT`, and only personally run `create --confirm` after the external eligibility prerequisites are met. It must not display a real secret or instruct the operator to paste one.

## Plan Self-Review

### Spec coverage

- Fixed `check` and `create --confirm` command surface: Tasks 1 and 3.
- Local-only check with wallet normalization and three-state CLOB readiness: Task 1.
- Create-only, official L1 endpoint, inherited signer boundary, and no derive/overwrite: Task 2.
- Secret non-observability, stable errors, no trade authority, and atomic Keychain writes: Tasks 2 and 3.
- No live test request and fake-only test transport: Tasks 2 and 4.
- Authority/source-hash constraints and required operator documentation: Task 4.
- Repository-wide regression evidence and safe operator handoff: Task 5.

### Placeholder scan

Searched this plan for `TBD`, `TODO`, `implement later`, `fill in details`, `appropriate error handling`, `handle edge cases`, and `similar to`. None occur as implementation placeholders.

### Type consistency

`CredentialReadiness`, `CredentialCommandError`, `check_credential_readiness`, `create_credentials_in_sidecar`, `CredentialFingerprint`, and `CredentialProvisioningGrant` are defined in the task where they are first consumed. The create operation is consistently literal `"CREATE"`; no task introduces a derive/recovery public surface.
