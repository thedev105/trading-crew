# Polymarket Ubuntu headless secret-store design

## Purpose

Enable the existing loopback-only Polymarket pilot and its explicit CLOB credential ceremonies on
an Ubuntu 24.04 LTS headless server. macOS behavior remains unchanged. The Linux deployment uses
systemd encrypted credentials with the host key because this server has no TPM 2.0.

This design does not authorize trading. The pilot remains killed by default, and every existing
external eligibility, legal/KYC/terms, geoblock, funding/allowance, evidence, passkey, and
explicit-action gate remains mandatory.

## Decision and threat boundary

The supported Linux target is Ubuntu 24.04 LTS with systemd 255 or newer and `systemd-creds`.
The deployment runs as a dedicated unprivileged `polytrading` system account. Wallet and CLOB
credentials persist only as systemd host-key encrypted `*.cred` files below the service state
directory. On each service invocation, systemd decrypts those blobs into its private credential
runtime directory and exposes only that directory path through `CREDENTIALS_DIRECTORY`.

The host key protects encrypted credential files at rest and prevents secrets from reaching CLI
arguments, environment values, logs, the database, or persistent plaintext files. It does not
protect against an attacker that has root access to the host: root can read the host key and the
running service's memory. A TPM-backed deployment is intentionally not claimed by this design.

Unsupported Linux distributions, direct shell launches without the systemd credential directory,
missing `systemd-creds`, invalid ownership or modes, and a partial credential state fail closed.
There is no `.env`, plaintext-file, secret CLI flag, generic secret provider, or remote secret
manager fallback.

## Components

### Fixed labels and platform factory

Move the fixed service/account labels from `keychain_macos.py` to a platform-neutral
`secret_labels.py`; retain re-exports from `keychain_macos.py` for compatibility. Add
`secret_store_factory.py` with one public function:

```python
def open_pilot_secret_store(*, platform: str = sys.platform) -> SecretStore: ...
```

It selects `MacOSKeychainSecretStore` on Darwin and `SystemdCredentialSecretStore` on Linux. The
factory is the only code that reads `CREDENTIALS_DIRECTORY`; it treats that value only as a path,
never as a secret. It rejects all other platforms with `SECRET_STORE_UNAVAILABLE`. CLI, pilot
runtime, and the clean credential child use this factory instead of importing the macOS adapter.

### Linux systemd credential store

`systemd_credentials_linux.py` implements the existing `SecretStore` protocol without changing
its public shape. Its constructor receives two validated paths from the factory: the private
systemd runtime credential directory and the fixed persistent encrypted-credential directory. It
accepts only the four reviewed labels and the fixed service `polytrading.polymarket.pilot`.

`read_required` opens only `<runtime-dir>/<account>` using directory file descriptors and
`O_NOFOLLOW`, requires a regular single-link file owned by the service user with no group/world
permissions, bounds it to 4096 bytes, copies it into `SecretBuffer`, and closes the descriptor.
It normalizes only the wallet's accepted 64-hexadecimal representation, exactly as the macOS
adapter does. The runtime plaintext copy is created and cleaned up by systemd; the application
never creates, replaces, deletes, or names a plaintext secret file.

`create_protected` is available only for the three CLOB labels. It refuses an existing runtime
credential or encrypted target, sends the bounded `SecretBuffer` to the fixed
`systemd-creds encrypt --with-key=host --name=<account> - -` child through standard input, captures
only its encrypted output, and atomically publishes that encrypted blob to the fixed state
directory with mode `0600`. The credential bytes are never passed in argv or environment. The
implementation tracks an opaque `SecretCreation` token so `delete_created` can roll back only a
blob created by that store instance. `write_protected` and `delete` fail closed on Linux: neither
rotation nor arbitrary secret deletion is a supported pilot operation.

The credential-create or -derive one-shot service has only the wallet credential loaded. When a
ceremony succeeds, the encrypted CLOB blobs are available for the next systemd invocation; the
current runtime directory is not modified. The operator must start a new systemd service
invocation before `check` or the pilot can observe them.

### Clean signer and credential ceremony

The inherited-descriptor signer architecture does not change. The runtime parent still reads four
`SecretBuffer`s once, frames them to pipes, closes its copies, and starts the signer child. The
macOS clean child stays `posix_spawn` based to avoid Objective-C-after-fork behavior. The same
clean child becomes platform-neutral by constructing its store through `open_pilot_secret_store`;
on Linux it performs no macOS import.

The credential commands remain exactly `check`, `create --confirm`, and `derive --confirm`.
`create` remains POST-only; `derive` remains GET-only. Both run only with explicit confirmation,
all local CLOB slots absent, a valid wallet, the existing lock, and a short-lived fixed grant.
They encrypt returned CLOB values before persistence and report only the existing public result
and fingerprint. They neither start the pilot nor alter its killed posture.

### systemd deployment units

Commit three example units under `deploy/systemd/`:

- `polytrading-pilot.service` loads all four encrypted credential blobs and starts the unchanged
  `predictions pilot polymarket --db ... --port ...` CLI.
- `polytrading-credentials-create.service` loads only `wallet-private-key` and runs the explicit
  create command once.
- `polytrading-credentials-derive.service` loads only `wallet-private-key` and runs the explicit
  derive command once.

Each unit uses `User=polytrading`, `Group=polytrading`, `UMask=0077`, `StateDirectory=polytrading`,
`NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectHome=true`, `ProtectSystem=strict`, an empty
`CapabilityBoundingSet=`, and a narrow `ReadWritePaths=/var/lib/polytrading/credentials` only for
the credential one-shots. Credential blobs are loaded by fixed absolute paths below
`/var/lib/polytrading/credentials`; no arbitrary operator path or service name enters the Python
process. The pilot unit has no credential-write permission.

The initial wallet `wallet-private-key.cred` is a deployment prerequisite. It is generated outside
this application with `systemd-creds encrypt --with-key=host --name=wallet-private-key` from a
trusted non-terminal secret source, written as mode `0600` for the `polytrading` account, and then
loaded by the units. The application never accepts or generates a wallet key.

## Invariants

- The only plaintext credential location on Linux is the systemd-created private runtime
  credential directory; it is read-only to the application.
- Wallet/CLOB bytes never appear in CLI arguments, environment values, output, logs, exceptions,
  database records, browser state, IPC responses, or persistent plaintext files.
- The only persistent credential artifacts produced by application code are host-key encrypted
  blobs with fixed names and atomic replace protection; existing blobs are never overwritten.
- The runtime and credential commands can use only the four fixed labels, the frozen Polymarket
  routes, and existing signer IPC. Linux support adds no generic signer, HTTP, secret-store, or
  operation surface.
- A failed CLOB write rolls back every encrypted blob newly created by that ceremony. A rollback
  ownership failure is reported as `CREDENTIAL_ROLLBACK_FAILED` and preserved for operator review.
- Direct execution outside the systemd credential runtime fails with a stable sanitized code and
  cannot fall back to an environment value, file path flag, or macOS adapter.

## Verification and operations

Tests use fake filesystem descriptors and a fake `systemd-creds` runner. They do not invoke
systemd, read host credentials, or make a live Polymarket request. They cover label allowlisting,
path traversal/symlink/mode/ownership rejection, bounded reads, wallet normalization, encrypted
create rollback, refusal of overwrite, factory platform selection, clean-child construction, CLI
sanitization, and unit hardening/static scans. The reviewed authority-source manifest is updated
only after final source review.

The runbook documents installation, encrypted wallet deployment, service invocation, the required
restart after credential creation/derivation, host-key limitations, revocation/incident response,
and the continuing trading-precondition gates. It explicitly tells operators not to paste secrets
into a shell, journal, ticket, chat, or the application UI.
