# Task 4 report — live signer service composition

## Work

- Added `live_pilot_signer_service(capability_public_key=..., clock=...)`, whose fork-safe closure retains only public launch state. Secret-bearing credentials and the owned HTTP transport are created only when the child invokes the returned factory.
- Complete CLOB material is converted to `ClobCredentials`, then composed with `HttpxPolymarketRestTransport` and `SignerRestHandlers.as_operation_handlers()`.
- Wallet-only launches keep identity available, create no transport, and reject authenticated reads and post-proof mutations with the stable sanitized `CREDENTIALS_UNAVAILABLE` reason.
- Bound live reads to the wallet-derived account fingerprint, the exact `READ_ACCOUNT` / `READ_ORDERS` / `READ_TRADES` set, and a five-minute child-launch lifetime. The guard cannot authorize a mutation.
- Kept `offline_pilot_signer_service` socket-free and behaviorally unchanged. Full-credential mutations retain the existing authority-context denial until Task 8 can supply current manifest/evidence context.
- Narrowed `SignerServiceFactory` to the real `SecretMaterial -> SignerService` contract.
- Extended the closed authority-reason union and its reviewed source manifest for `CREDENTIALS_UNAVAILABLE`; the authority scan allows transport composition only in the exact nested live child factory shape.

## Tests / TDD

- RED: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_services.py tests/predictions/test_pilot_signer_bootstrap.py -q -k "live_factory or wallet_only"` failed at the missing `live_pilot_signer_service` API (`2 failed`).
- GREEN: the same command passed (`3 passed`).
- Guard mutation check: deliberately widened operations, inverted account matching, and loosened the expiry edge; all three direct guard regressions failed, then passed after restoring the closed implementation.
- Focused verification: `rtk .venv/bin/python -m pytest tests/predictions/test_pilot_signer_services.py tests/predictions/test_pilot_signer_bootstrap.py tests/predictions/test_polymarket_execution_rest.py -q` passed (`114 passed`).
- Authority scan: `rtk .venv/bin/python -m pytest tests/predictions/test_execution_authority_scan.py -q` passed (`68 passed`).
- Targeted Ruff check passed.

## Files

- `src/polytrading/predictions/execution/authority.py`
- `src/polytrading/predictions/pilot/signer_bootstrap.py`
- `src/polytrading/predictions/pilot/signer_services.py`
- `tests/predictions/test_execution_authority_scan.py`
- `tests/predictions/test_pilot_signer_services.py`

## Self-review / concerns

- No network or Keychain operation was performed. The live handler test replaces only HTTPX's external transport with `httpx.MockTransport`.
- Task 4 has no source of current manifest, reconciliation, geoblock, presence, or risk evidence for a child-side mutation `AuthorityContext`; mutation dispatch therefore remains fail-closed pending Task 8 composition, per the parent ruling.
