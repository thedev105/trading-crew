# Polymarket credential commands design

## Purpose

Provide two explicit local operator commands for CLOB credential readiness without accepting,
printing, persisting, or exposing a wallet key or CLOB credential. This creates no trading authority
and never submits, cancels, or manages an order.

## Command surface

```text
polytrading predictions pilot credentials check
polytrading predictions pilot credentials create --confirm
```

The commands accept no secret, database, network target, wallet, or credential flags. They use the
fixed macOS Keychain service `polytrading.polymarket.pilot` and its four reviewed account labels.
They are unavailable on unsupported platforms.

`check` is local-only. It validates that the wallet Keychain item normalizes to a valid Ethereum
private key and reports whether the three CLOB items are all present, all absent, or partial. Its
stable output contains only booleans/status codes; it does not send a network request or derive a
remote credential.

`create --confirm` is the only mutating command. The explicit confirmation flag prevents an
accidental external credential creation. It launches a short-lived signer child, gives that child
the wallet key via the existing inherited-descriptor boundary, and permits exactly one official
credential `CREATE` request. The returned `apiKey`, `secret`, and `passphrase` move directly from
the child into the fixed Keychain items as one atomic trio. The command returns only a credential
fingerprint and `CREATED`; it never returns any credential value.

If the remote venue reports that creation is unavailable or rejects the request, the command
returns a stable sanitized failure and leaves no partial Keychain trio. It does not silently derive
or overwrite a credential. A remote derive/recovery ceremony is deliberately outside this change.

## Boundaries and invariants

- The parent CLI never receives a wallet key or a CLOB credential.
- No secret may enter argv, environment variables, stdout, stderr, logs, DuckDB, exceptions, or
  request bodies outside the signer.
- `check` has no network capability.
- `create` may call only the frozen CLOB create-credential route with the reviewed L1 signature.
  It has no order, cancellation, allowance, transfer, deposit, withdrawal, or arbitrary HTTP
  surface.
- Credential creation is not pilot activation. The runtime remains killed and no execution
  capability, passkey authority, or automatic action is created.
- A failure after any Keychain write removes every newly written CLOB item, leaving no partial trio.
- Existing CLOB credentials are not overwritten by `create`; the command fails closed and instructs
  the operator to use a future separately reviewed recovery flow.

## Implementation shape

Reuse `MacOSKeychainSecretStore`, `SecretBuffer`, `CredentialProvisioner`,
`HttpxCredentialClient`, and the fixed protocol snapshot. Add a narrow signer-side credential
operation that constructs the client and performs the one ceremony, plus a small CLI adapter that
prints only typed public result models.

Do not add a generic secret reader, arbitrary HTTP client, generic signer RPC, or credential
arguments. Keep `predictions pilot polymarket --db --port` unchanged.

## Error handling and verification

Map all Keychain, signer, transport, response, and storage failures to stable public codes. Never
include exception text, Keychain item values, request headers, bodies, or raw remote responses.

Tests use fake Keychain and credential-client implementations. They prove: check never opens a
network client; create requires `--confirm`; valid creation writes all three values atomically;
client/store failures leave no partial values; no observable includes secret canaries; and neither
command invokes execution operations. No live credential request is used in tests.
