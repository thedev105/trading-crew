# Polymarket Ubuntu Headless Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing killed-by-default Polymarket pilot and explicit CLOB credential ceremonies on an Ubuntu 24.04 headless server using host-key encrypted systemd credentials.

**Architecture:** Preserve the `SecretStore` boundary and move fixed credential labels into a platform-neutral module. A factory selects macOS Keychain or a narrow Linux systemd credential adapter; the Linux adapter reads only systemd's private runtime credential directory and writes only fixed-name encrypted `*.cred` blobs through `systemd-creds`. Runtime, CLI, and the clean credential child consume that factory, while hardened systemd units are the only supported Linux launch path.

**Tech Stack:** Python 3.12+, Ubuntu 24.04 LTS, systemd 255+, `systemd-creds`, filesystem descriptor APIs, existing `SecretBuffer`/`SecretStore`, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-02-polymarket-ubuntu-headless-design.md`

## Global Constraints

- Support only Ubuntu 24.04 LTS with systemd 255+ and `systemd-creds`; no TPM is required, and `--with-key=host` is the only Linux encryption mode.
- Linux is supported only from a systemd-created `CREDENTIALS_DIRECTORY`; direct shell use, arbitrary secret paths, `.env`, desktop keyrings, remote secret managers, and plaintext persistent storage fail closed.
- Keep the fixed service `polytrading.polymarket.pilot` and only `wallet-private-key`, `clob-api-key`, `clob-api-secret`, and `clob-passphrase` labels. Do not add arbitrary account or service input.
- Secret bytes may not appear in argv, environment values, stdout, stderr, logs, exceptions, DuckDB, browser state, IPC responses, or persistent plaintext files. The non-secret credential-directory path is the sole permitted environment lookup.
- Only the three CLOB labels may be encrypted and persisted by `create_protected`; wallet creation, rotation, overwrite, arbitrary deletion, and recovery outside the fixed create/derive flows are refused.
- Preserve macOS behavior, the fresh-interpreter `posix_spawn` safety boundary, fixed CLOB routes, explicit confirmation, absent-only credential creation/derivation, rollback ownership, and the killed-by-default pilot posture.
- Unit tests use fake filesystem/runner seams only. No test calls systemd, `systemd-creds`, an OS secret store, or live Polymarket.

---

### Task 1: Extract fixed labels and add the platform store factory

**Files:**
- Create: `src/polytrading/predictions/polymarket_execution/secret_labels.py`
- Create: `src/polytrading/predictions/polymarket_execution/secret_store_factory.py`
- Modify: `src/polytrading/predictions/polymarket_execution/keychain_macos.py`
- Test: `tests/predictions/test_polymarket_secret_store_factory.py`
- Test: `tests/predictions/test_polymarket_keychain.py`

**Interfaces:**
- Consumes: `SecretStore`, `SecretStoreError`, `sys.platform`, current four label constants, and `MacOSKeychainSecretStore`.
- Produces: `CLOB_SERVICE`, `WALLET_PRIVATE_KEY_ACCOUNT`, `CLOB_API_KEY_ACCOUNT`, `CLOB_API_SECRET_ACCOUNT`, `CLOB_PASSPHRASE_ACCOUNT`, and `ALLOWED_ACCOUNTS` from `secret_labels.py`.
- Produces: `open_pilot_secret_store(*, platform: str = sys.platform) -> SecretStore`; Linux construction reads the non-secret runtime directory once and passes it with the fixed encrypted directory to `SystemdCredentialSecretStore`, while every unsupported platform raises `SecretStoreError("SECRET_STORE_UNAVAILABLE")`.

- [ ] **Step 1: Write failing fixed-label and factory tests**

```python
def test_factory_selects_the_linux_systemd_store_only_for_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/test")
    monkeypatch.setattr(factory, "SystemdCredentialSecretStore", lambda **_kwargs: expected)
    assert factory.open_pilot_secret_store(platform="linux") is expected


def test_factory_refuses_unknown_platforms() -> None:
    with pytest.raises(SecretStoreError) as raised:
        open_pilot_secret_store(platform="freebsd")
    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"


def test_keychain_reexports_the_canonical_fixed_labels() -> None:
    assert keychain_macos.CLOB_SERVICE == secret_labels.CLOB_SERVICE
    assert keychain_macos.ALLOWED_ACCOUNTS == secret_labels.ALLOWED_ACCOUNTS
```

- [ ] **Step 2: Run the new tests to verify the missing factory failure**

Run: `rtk .venv/bin/python -m pytest -q tests/predictions/test_polymarket_secret_store_factory.py`

Expected: FAIL because `secret_labels` and `open_pilot_secret_store` do not exist.

- [ ] **Step 3: Create the canonical labels and factory**

```python
# secret_labels.py
CLOB_SERVICE: Final = "polytrading.polymarket.pilot"
WALLET_PRIVATE_KEY_ACCOUNT: Final = "wallet-private-key"
CLOB_API_KEY_ACCOUNT: Final = "clob-api-key"
CLOB_API_SECRET_ACCOUNT: Final = "clob-api-secret"
CLOB_PASSPHRASE_ACCOUNT: Final = "clob-passphrase"
ALLOWED_ACCOUNTS: Final = frozenset({...})


def open_pilot_secret_store(*, platform: str = sys.platform) -> SecretStore:
    if platform == "darwin":
        return MacOSKeychainSecretStore(platform=platform)
    if platform == "linux":
        return SystemdCredentialSecretStore(
            runtime_directory=_systemd_credentials_directory(),
            encrypted_directory=Path("/var/lib/polytrading/credentials"),
        )
    raise SecretStoreError("SECRET_STORE_UNAVAILABLE") from None
```

Move the constants without changing their values. `keychain_macos.py` imports and re-exports the
canonical constants, preserving every current importing module. Import the Linux class lazily in
the Linux branch so Darwin never imports Linux-only code. The factory must not accept a custom
service, account, path, command, or secret value.

- [ ] **Step 4: Run the factory and existing Keychain tests**

Run: `rtk .venv/bin/python -m pytest -q tests/predictions/test_polymarket_secret_store_factory.py tests/predictions/test_polymarket_keychain.py`

Expected: PASS. The macOS adapter retains its current unsupported-platform behavior when
constructed directly; only the factory gives Linux a reviewed store.

- [ ] **Step 5: Commit the platform-neutral boundary**

```bash
rtk git add src/polytrading/predictions/polymarket_execution/secret_labels.py src/polytrading/predictions/polymarket_execution/secret_store_factory.py src/polytrading/predictions/polymarket_execution/keychain_macos.py tests/predictions/test_polymarket_secret_store_factory.py tests/predictions/test_polymarket_keychain.py
rtk git commit -m "feat(predictions): add platform secret store factory"
```

### Task 2: Implement read-only systemd runtime credential loading

**Files:**
- Create: `src/polytrading/predictions/polymarket_execution/systemd_credentials_linux.py`
- Test: `tests/predictions/test_polymarket_systemd_credentials.py`

**Interfaces:**
- Consumes: `SecretBuffer`, `SecretStore`, `SecretStoreError`, canonical labels, `os.open`, `os.read`, `os.fstat`, and `Path` supplied only through constructor/test seams.
- Produces: `SystemdCredentialSecretStore(runtime_directory: Path, encrypted_directory: Path, *, runner: SystemdCredsRunner | None = None, effective_uid: int | None = None)`.
- Produces: `read_required(service: str, account: str, prompt: str) -> SecretBuffer`, accepting only a fixed service/label and a safe systemd runtime credential file.

- [ ] **Step 1: Write failing filesystem-boundary tests**

```python
@pytest.mark.parametrize("name", ["../wallet-private-key", "wallet-private-key/child"])
def test_runtime_store_refuses_path_traversal(name: str, store: SystemdCredentialSecretStore) -> None:
    with pytest.raises(SecretStoreError) as raised:
        store.read_required(CLOB_SERVICE, name, "unlock")
    assert raised.value.code == "SECRET_LABEL_INVALID"


def test_runtime_store_reads_only_a_private_regular_file(store: SystemdCredentialSecretStore) -> None:
    write_runtime_credential("wallet-private-key", b"00" * 32, mode=0o400)
    value = store.read_required(CLOB_SERVICE, WALLET_PRIVATE_KEY_ACCOUNT, "unlock")
    try:
        assert value.use(bytes) == bytes(32)
    finally:
        value.close()


@pytest.mark.parametrize("prepare", [make_symlink, make_group_readable, make_wrong_owner])
def test_runtime_store_refuses_unsafe_credential_files(prepare: Callable[[], None]) -> None:
    prepare()
    with pytest.raises(SecretStoreError) as raised:
        store().read_required(CLOB_SERVICE, WALLET_PRIVATE_KEY_ACCOUNT, "unlock")
    assert raised.value.code == "SECRET_STORE_UNAVAILABLE"
```

- [ ] **Step 2: Run the runtime-loading tests to verify they fail**

Run: `rtk .venv/bin/python -m pytest -q tests/predictions/test_polymarket_systemd_credentials.py -k runtime`

Expected: FAIL because `SystemdCredentialSecretStore` does not exist.

- [ ] **Step 3: Add the validated runtime reader**

```python
def read_required(self, service: str, account: str, prompt: str) -> SecretBuffer:
    del prompt
    self._require_label(service, account)
    descriptor = os.open(account, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._runtime_fd)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise SecretStoreError("SECRET_STORE_UNAVAILABLE")
        if status.st_uid != self._effective_uid or status.st_mode & 0o077:
            raise SecretStoreError("SECRET_STORE_UNAVAILABLE")
        raw = _read_at_most(descriptor, MAXIMUM_ITEM_BYTES)
        return SecretBuffer.from_bytes(_normalize_wallet_private_key(raw) if account == WALLET_PRIVATE_KEY_ACCOUNT else raw)
    finally:
        os.close(descriptor)
```

Open the runtime directory itself with `O_DIRECTORY | O_NOFOLLOW`; reject a missing, non-directory,
unsafe-mode, or wrong-owner directory. The factory alone reads `CREDENTIALS_DIRECTORY`, requires
an absolute path, and passes the fixed `/var/lib/polytrading/credentials` location to this
constructor. It never formats the directory value into a public error. Ensure temporary
`bytearray` values are zeroized on every error path.

- [ ] **Step 4: Run reader, secret-boundary, and formatting tests**

Run: `rtk .venv/bin/python -m pytest -q tests/predictions/test_polymarket_systemd_credentials.py tests/predictions/test_polymarket_secret_boundary.py && rtk .venv/bin/ruff check src/polytrading/predictions/polymarket_execution/systemd_credentials_linux.py tests/predictions/test_polymarket_systemd_credentials.py`

Expected: PASS. Tests must verify that a secret canary never occurs in `repr`, exception text, or
captured output.

- [ ] **Step 5: Commit Linux runtime credential reads**

```bash
rtk git add src/polytrading/predictions/polymarket_execution/systemd_credentials_linux.py tests/predictions/test_polymarket_systemd_credentials.py
rtk git commit -m "feat(predictions): read Linux systemd credentials safely"
```

### Task 3: Persist only newly created CLOB credentials as encrypted blobs

**Files:**
- Modify: `src/polytrading/predictions/polymarket_execution/systemd_credentials_linux.py`
- Modify: `tests/predictions/test_polymarket_systemd_credentials.py`
- Modify: `tests/predictions/test_polymarket_credentials.py`

**Interfaces:**
- Consumes: `CredentialProvisioner`'s existing `create_protected`/`delete_created` transaction, `SecretCreation`, fixed CLOB labels, and a private test-injectable `SystemdCredsRunner`.
- Produces: `create_protected(service: str, account: str, value: SecretBuffer) -> SecretCreation` and `delete_created(creation: SecretCreation) -> None` for absent CLOB credentials only.
- Produces: fixed runner invocation `systemd-creds encrypt --with-key=host --name=<account> - -`; standard input carries plaintext and standard output carries only encrypted bytes.

- [ ] **Step 1: Write failing create/rollback tests**

```python
def test_create_protected_encrypts_a_new_fixed_clob_slot_without_exposing_plaintext() -> None:
    store, runner = store_with_fake_runner()
    canary = b"clob-secret-canary"
    creation = store.create_protected(CLOB_SERVICE, CLOB_API_SECRET_ACCOUNT, SecretBuffer.from_bytes(canary))
    assert runner.calls == [("encrypt", CLOB_API_SECRET_ACCOUNT)]
    assert encrypted_path(CLOB_API_SECRET_ACCOUNT).read_bytes() == runner.encrypted_output
    assert canary not in repr(creation)
    assert canary not in captured_public_output()


def test_create_protected_refuses_an_existing_runtime_or_encrypted_slot() -> None:
    create_runtime_credential(CLOB_API_KEY_ACCOUNT)
    with pytest.raises(SecretStoreError) as raised:
        store().create_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, SecretBuffer.from_bytes(b"new"))
    assert raised.value.code == "SECRET_ITEM_EXISTS"


def test_delete_created_removes_only_its_own_encrypted_blob() -> None:
    store = store_with_fake_runner()[0]
    creation = store.create_protected(CLOB_SERVICE, CLOB_API_KEY_ACCOUNT, SecretBuffer.from_bytes(b"key"))
    replace_encrypted_blob(CLOB_API_KEY_ACCOUNT, b"external-encrypted-replacement")
    with pytest.raises(SecretStoreError) as raised:
        store.delete_created(creation)
    assert raised.value.code == "SECRET_OWNERSHIP_LOST"
```

- [ ] **Step 2: Run the create tests to verify they fail**

Run: `rtk .venv/bin/python -m pytest -q tests/predictions/test_polymarket_systemd_credentials.py -k 'create or rollback'`

Expected: FAIL because the Linux adapter has no encrypted create transaction.

- [ ] **Step 3: Implement the narrow encrypted writer and rollback ownership**

```python
def create_protected(self, service: str, account: str, value: SecretBuffer) -> SecretCreation:
    self._require_clob_label(service, account)
    self._require_absent(account)
    encrypted = self._runner.encrypt(account=account, value=value)
    try:
        token = object()
        self._publish_encrypted_new(account, encrypted, token)
        return SecretCreation(service=service, account=account, token=token)
    finally:
        _zeroize(encrypted)
```

The production runner has a fixed executable/argument vector, `stdin=PIPE`, `stdout=PIPE`,
`stderr=DEVNULL`, `env={"PATH": "/usr/bin:/bin"}`, `check=False`, and a bounded encrypted-output
size. It maps any launch, status, empty-output, or oversized-output failure to
`SECRET_WRITE_FAILED` without formatting process data. `_publish_encrypted_new` uses an exclusive
temporary file in the fixed encrypted directory, mode `0600`, `fsync`, a no-replace rename, and a
directory `fsync`. Its ownership token includes the final file identity; `delete_created` compares
that identity before unlinking. `write_protected` and `delete` always raise
`SECRET_WRITE_FAILED` for Linux. Close/zeroize all buffers even when encrypt, write, rename, or
rollback fails.

- [ ] **Step 4: Prove transaction behavior through the existing provisioner**

Run: `rtk .venv/bin/python -m pytest -q tests/predictions/test_polymarket_systemd_credentials.py tests/predictions/test_polymarket_credentials.py`

Expected: PASS. Add a provisioner integration test using the Linux fake runner that refuses a
second CLOB write and proves no encrypted CLOB blob remains. Add a replacement-during-rollback
test that retains the replacement and yields `CREDENTIAL_ROLLBACK_FAILED`.

- [ ] **Step 5: Commit encrypted CLOB persistence**

```bash
rtk git add src/polytrading/predictions/polymarket_execution/systemd_credentials_linux.py tests/predictions/test_polymarket_systemd_credentials.py tests/predictions/test_polymarket_credentials.py
rtk git commit -m "feat(predictions): persist new CLOB credentials encrypted"
```

### Task 4: Wire Linux into runtime, CLI, and the clean credential child

**Files:**
- Modify: `src/polytrading/predictions/pilot/runtime.py`
- Modify: `src/polytrading/predictions/pilot/signer_bootstrap.py`
- Modify: `src/polytrading/predictions/cli.py`
- Modify: `tests/predictions/test_pilot_runtime.py`
- Modify: `tests/predictions/test_pilot_signer_bootstrap.py`
- Modify: `tests/predictions/test_pilot_credential_commands.py`
- Modify: `tests/predictions/test_cli.py`

**Interfaces:**
- Consumes: `open_pilot_secret_store`, existing `launch_signer_sidecar`, `create_credentials`, `derive_credentials`, and the existing public command codes.
- Produces: platform-neutral signer bootstrap and credential child construction, while all existing CLI command names/output remain unchanged.
- Produces: Linux failure `SECRET_STORE_UNAVAILABLE` when no safe systemd credential directory is available; it must not construct `MacOSKeychainSecretStore` on Linux.

- [ ] **Step 1: Write failing Linux wiring tests**

```python
def test_linux_runtime_uses_the_platform_factory(database: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemorySecretStore()
    monkeypatch.setattr(runtime, "open_pilot_secret_store", lambda **_kwargs: store)
    built = runtime.build_launch_runtime(database, PORT, platform="linux")
    built.close()


def test_linux_clean_credential_child_never_imports_the_macos_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(signer_bootstrap, "open_pilot_secret_store", lambda **_kwargs: wallet_only_store())
    result = run_clean_credential_child_for_test(operation="DERIVE")
    assert result == 0


def test_linux_credential_command_without_systemd_directory_is_sanitized() -> None:
    assert main(["predictions", "pilot", "credentials", "check"]) == 64
    assert captured_stderr() == "polytrading: credential command failed: KEYCHAIN_UNAVAILABLE\n"
```

- [ ] **Step 2: Run the wiring tests to verify they fail**

Run: `rtk .venv/bin/python -m pytest -q tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_pilot_credential_commands.py tests/predictions/test_cli.py -k linux`

Expected: FAIL because the runtime, CLI, and clean child construct `MacOSKeychainSecretStore` directly.

- [ ] **Step 3: Replace direct macOS construction with the factory**

```python
def _bootstrap_signer(platform: str) -> Callable[[SignerServiceFactory], SignerChannel]:
    return lambda service_factory: launch_signer_sidecar(
        store=open_pilot_secret_store(platform=platform),
        service_factory=service_factory,
    )


def _run_clean_credential_child(response_fd: int, operation: str) -> int:
    try:
        store = open_pilot_secret_store()
    except BaseException:
        _write_credential_result(response_fd, CredentialCeremonyResult(False, "SIGNER_BOOTSTRAP_FAILED"))
        _close(response_fd)
        return 1
```

Use the factory in CLI `check`, runtime secret-store availability probing, launch composition, and
the clean child. Preserve the macOS `posix_spawn` process shape. Give runtime public defaults
`platform=sys.platform`, while retaining the explicit `platform` test parameter. Do not add a
platform flag to public commands. The Linux credential one-shots receive their runtime directory
only from systemd.

- [ ] **Step 4: Run the credential, runtime, and authority-focused proof**

Run: `rtk .venv/bin/python -m pytest -q tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_pilot_credential_commands.py tests/predictions/test_cli.py tests/predictions/test_polymarket_credential_client.py tests/predictions/test_polymarket_credentials.py tests/predictions/test_execution_secret_scan.py`

Expected: PASS. Verify Linux direct execution reports only a stable code, create never falls back
to derive, and the runtime remains killed after a Linux store is successfully composed.

- [ ] **Step 5: Commit platform wiring**

```bash
rtk git add src/polytrading/predictions/pilot/runtime.py src/polytrading/predictions/pilot/signer_bootstrap.py src/polytrading/predictions/cli.py tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_pilot_credential_commands.py tests/predictions/test_cli.py
rtk git commit -m "feat(predictions): run pilot with Linux systemd credentials"
```

### Task 5: Add hardened units, operator guidance, and reviewed-source checks

**Files:**
- Create: `deploy/systemd/polytrading-pilot.service`
- Create: `deploy/systemd/polytrading-credentials-create.service`
- Create: `deploy/systemd/polytrading-credentials-derive.service`
- Modify: `README.md`
- Modify: `docs/predictions/polymarket-live-pilot.md`
- Modify: `docs/predictions/polymarket-execution-hardening.md`
- Modify: `tests/predictions/test_execution_authority_scan.py`
- Modify: `tests/predictions/test_execution_secret_scan.py`
- Modify: `tests/predictions/test_pilot_runtime.py`

**Interfaces:**
- Consumes: fixed account/blob paths, `CREDENTIALS_DIRECTORY`, existing authority source manifest, and public credential commands.
- Produces: three static systemd unit templates and Ubuntu operator instructions; no application config surface.
- Produces: source scans that permit one non-secret `CREDENTIALS_DIRECTORY` lookup in the factory and reject all other secret environment/file/provider ingress.

- [ ] **Step 1: Write failing unit and static-boundary tests**

```python
def test_linux_units_load_only_fixed_encrypted_credentials_and_are_hardened() -> None:
    pilot = unit("polytrading-pilot.service")
    assert "LoadCredentialEncrypted=wallet-private-key:/var/lib/polytrading/credentials/wallet-private-key.cred" in pilot
    assert "LoadCredentialEncrypted=clob-api-key:/var/lib/polytrading/credentials/clob-api-key.cred" in pilot
    assert "NoNewPrivileges=true" in pilot
    assert "ProtectSystem=strict" in pilot
    assert "CapabilityBoundingSet=" in pilot
    assert "ReadWritePaths=/var/lib/polytrading/credentials" not in pilot


def test_linux_secret_store_allows_only_the_nonsecret_systemd_directory_environment_lookup() -> None:
    source = (PREDICTIONS_ROOT / "polymarket_execution/secret_store_factory.py").read_text()
    assert source.count("CREDENTIALS_DIRECTORY") == 1
    assert "POLYMARKET" not in source
    assert "private_key" not in source
```

- [ ] **Step 2: Run the unit/static tests to verify they fail**

Run: `rtk .venv/bin/python -m pytest -q tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py tests/predictions/test_pilot_runtime.py -k 'linux or systemd'`

Expected: FAIL because the unit files and Linux-specific static rules do not exist.

- [ ] **Step 3: Add exact unit templates and operator documentation**

Use this common security baseline in all three units:

```ini
[Service]
User=polytrading
Group=polytrading
UMask=0077
StateDirectory=polytrading
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
CapabilityBoundingSet=
```

The pilot unit loads the wallet plus the three CLOB encrypted blobs and has no `ReadWritePaths`.
Each credential one-shot loads only `wallet-private-key`, has
`ReadWritePaths=/var/lib/polytrading/credentials`, and uses exactly one fixed command:
`credentials create --confirm` or `credentials derive --confirm`. Do not use a templated operation
parameter. Give every `LoadCredentialEncrypted` directive a fixed account and matching `.cred`
path.

Document Ubuntu 24.04/systemd 255 prerequisites, the no-TPM host-key limitation, creation of the
dedicated service account/state directory, out-of-application wallet blob deployment from a trusted
non-terminal secret source, `daemon-reload`, each one-shot invocation, pilot restart after a
successful CLOB ceremony, and incident handling. Preserve the command's existing exact sanitized
output. State that successful credentials do not satisfy trading eligibility or clear the kill.

After all sensitive Python source is final, compute the two new module digests with `rtk shasum -a
256`, add them to `_REVIEWED_SOURCE_SHA256`, and include both source paths in every pre-import
authority source inventory. Keep `credential_client.py` as the sole code that owns the fixed CLOB
HTTP requests.

- [ ] **Step 4: Run final focused verification**

Run: `rtk .venv/bin/ruff format --check src tests && rtk .venv/bin/ruff check src tests && rtk .venv/bin/python -m pytest -q tests/predictions/test_polymarket_secret_store_factory.py tests/predictions/test_polymarket_systemd_credentials.py tests/predictions/test_polymarket_keychain.py tests/predictions/test_pilot_runtime.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_pilot_credential_commands.py tests/predictions/test_cli.py tests/predictions/test_polymarket_credentials.py tests/predictions/test_polymarket_credential_client.py tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py && rtk git diff --check`

Expected: PASS. If the sandbox blocks pre-existing Unix-socket tests, rerun only those failures
with local Unix-socket permission and record that no network, systemd service, Keychain, or live
Polymarket request was used.

- [ ] **Step 5: Commit units, docs, and reviewed-source manifest**

```bash
rtk git add deploy/systemd README.md docs/predictions/polymarket-live-pilot.md docs/predictions/polymarket-execution-hardening.md tests/predictions/test_execution_authority_scan.py tests/predictions/test_execution_secret_scan.py tests/predictions/test_pilot_runtime.py
rtk git commit -m "docs(predictions): document Ubuntu headless pilot deployment"
```

## Plan self-review

- Spec coverage: Tasks 1–4 implement the factory, runtime credential reader, encrypted CLOB
  transaction, and platform wiring. Task 5 supplies hardened deployment, operator guidance, and
  authority/secret scans. The host-key limitation and restart requirement are documented in Task 5.
- Placeholder scan: the plan contains no deferred implementation markers or unspecified error
  behavior. Every task includes an exact failing test, test command, implementation shape, green
  proof, and commit command.
- Type consistency: every task uses `open_pilot_secret_store`, `SystemdCredentialSecretStore`,
  `SystemdCredsRunner`, `SecretCreation`, and the existing `SecretStore` method names consistently.
