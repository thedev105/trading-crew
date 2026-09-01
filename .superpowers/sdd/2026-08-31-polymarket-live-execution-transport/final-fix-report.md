# Final live-execution fix wave report

## Outcome

- `LivePilotServices` retains a launch-scoped callback that sends the issuer-signed
  `SIGNER_KILL` directive. Every parent kill latches and revokes parent authority first, then calls
  the signer under exception suppression, so stop and terminal presence return only after the
  signer attempt without allowing a broken signer link to undo the parent kill.
- Mutation-side invalid handler results, order-signing failures, authentication-handler failures,
  and generic handler failures latch the signer-local kill before returning their stable sanitized
  code. The same read-side failures do not latch the signer.
- Kill clearance requires the current reconciliation provider, current same-account account state,
  and current allowed geoblock evidence. It rejects provider failure, malformed/stale evidence, or
  any reconciliation evidence change after the signed challenge with `409 EVIDENCE_STALE`.
- Both operator documents now state that signer-owned transport and the manual execution path
  exist, while every fresh launch remains killed and the external 45-day qualification, 30-day
  shadow, legal/KYC/venue-terms, geoblock, manual funding/allowance, and separate activation gates
  remain prerequisites that code completion does not satisfy.

## TDD evidence

RED was observed before production changes:

- Stop and terminal presence regressions: `2 failed` because the last signer request remained
  `READ_TRADES` instead of `SIGNER_KILL`.
- Clearance freshness regression: `1 failed` because a reconciliation hash changed after challenge
  issuance was still cleared with HTTP 200.
- Signer exceptional-failure regressions: `4 failed, 1 passed`; fresh mutations remained accepted
  after invalid mutation output and signing/auth/generic handler failures, while the read-only
  invalid-result case already remained non-latching.

Focused GREEN:

- Stop/presence: `2 passed, 28 deselected`.
- Clearance freshness: `1 passed, 16 deselected`.
- Signer exceptional/read-only behavior: `5 passed, 147 deselected`.
- Complete directly affected modules, excluding only sandbox-incompatible spawned-sidecar cases:
  `188 passed, 11 deselected`.

## Final verification

- Full predictions suite, excluding only the 11 known sandbox-incompatible spawned-sidecar tests:
  `3142 passed, 11 deselected, 13 existing Pydantic deprecation warnings` in 66.37 seconds.
- Authority source scan: `68 passed` after reviewing and sealing signer hash
  `5bff3d0a9ec32f9db5865809e6d5fa7cc42b2d79882d253e0c5219553cb894c4`.
- Secret scan plus signer-link, signer-service, and activation modules: `61 passed`.
- Ruff format was run only on the Python files touched by this fix wave; `read_models.py` was not
  modified. Targeted Ruff check reported `All checks passed!`.
- `git diff --check` passed.

No network, venue, Keychain, browser, or subagent operation was used.
