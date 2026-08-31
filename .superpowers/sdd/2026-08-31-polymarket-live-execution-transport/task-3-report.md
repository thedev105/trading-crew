# Task 3 report — hardened live REST constructor

## Work

- Enabled `HttpxPolymarketRestTransport(...)` to create exactly one owned `httpx.AsyncClient`.
- The live client has a closed configuration: `follow_redirects=False`, `trust_env=False`, empty headers and cookies, and the existing finite `RestTimeouts` values.
- The public constructor accepts no transport, URL, header, proxy, or TLS override. `_for_test(httpx.MockTransport, ...)` remains the fake-network seam.
- Shared existing retry, sleeper, and timeout validation between live and test construction.
- Updated the authority scan to allow only the exact hardened live-client AST shape and refreshed the reviewed REST source digest.

## Tests / TDD

- RED: `rtk .venv/bin/python -m pytest tests/predictions/test_polymarket_execution_rest.py -q -k "live_constructor"` failed because the live constructor raised `LIVE_TRANSPORT_UNAVAILABLE`.
- GREEN: the same focused command passed (`2 passed`).
- Verification: `rtk .venv/bin/python -m pytest tests/predictions/test_polymarket_execution_rest.py tests/predictions/test_execution_authority_scan.py -q` passed (`160 passed`).
- Lint: `rtk .venv/bin/python -m ruff check src/polytrading/predictions/polymarket_execution/rest.py tests/predictions/test_polymarket_execution_rest.py tests/predictions/test_execution_authority_scan.py` passed.

## Files

- `src/polytrading/predictions/polymarket_execution/rest.py`
- `tests/predictions/test_polymarket_execution_rest.py`
- `tests/predictions/test_execution_authority_scan.py`

## Self-review / concerns

- No signer composition, network operation, Keychain operation, or broader authority change was added.
- The client creates HTTPX's normal TLS context; ambient environment configuration is disabled through `trust_env=False` and the public API provides no TLS override.
