# 0006. Refresh-token TTL surfacing instead of password-grant auto-auth

## Status

Accepted (2026-07-21). Re-evaluation unlikely — bound by DA's API,
not by da-cli's choices.

## Context

A recurring request: "store my DA username and password in a file,
get a token automatically every day, no browser interaction."

The ask is OAuth 2.0's Resource Owner Password Credentials (ROPC)
grant: client stores the password, client POSTs to `/oauth2/token`
with `grant_type=password`, client gets tokens without a browser.

## Decision

**Do NOT implement ROPC. Accept the 90-day refresh-token ceiling as
DA-imposed and surface the remaining TTL in `da diagnose` instead.**

## Evidence

- DA's `/.well-known/oauth-authorization-server` advertises
  `grant_types_supported = [authorization_code, client_credentials,
  refresh_token]`. `password` is absent.
- Empirical `POST /oauth2/token -d grant_type=password` returns
  HTTP 400 `unsupported_grant_type` *before evaluating credentials*.
- OAuth 2.1 (draft-09 §1.8) removes ROPC entirely.
- gallery-dl (the most-deployed DA client) doesn't use ROPC either;
  its password path scrapes DA's HTML login form + uses the private
  Eclipse API. Different code path entirely.
- DA's refresh tokens are hard-capped at 90 days; nothing extends them.
- Threat-model asymmetry: a stored-password compromise is full account
  takeover (the attacker can log in to deviantart.com, change the
  email, post, DM, re-authorize every other OAuth app). A stored
  refresh_token compromise is scoped to this app's grants, recoverable
  with one `POST /oauth2/revoke`.

## Consequences

**Positive:**

- The CLI's threat model (in `../security.md`) stays narrow: the
  worst-case compromise is a scoped, revocable OAuth misuse, not
  full account takeover.
- No new code path that would have to be re-evaluated every time DA
  changes their auth flow.
- The 90-day UX is the same as it would be with ROPC anyway — both
  require the user to do something at the 90-day mark (click
  "Authorize" vs enter password). The ROPC version just trades
  browser click for stored password; not a UX win.
- `da diagnose` now surfaces TTL (`WARN` ≤ 14 days, `FAIL` ≤ 3 days)
  so the operator gets a 2-week heads-up rather than a surprise
  failure at 03:00.

**Negative:**

- Every 90 days the user must run `da auth` and click "Authorize" in
  their browser. ~30 seconds of human time, ~3 times a year.
- The Keychain only ever holds `client_secret`; the refresh_token
  lives in `state.json` (0600). A Linux port would need a Secret
  Service backend to match the macOS threat model — that's roadmap
  work, not architectural change.

## Alternatives considered

- **Implement ROPC anyway**: technically impossible (DA rejects the
  grant type). Even if we hacked around it, we'd be storing a
  password for zero UX benefit (still re-auth at 90 days).
- **Scrape DA's HTML form + use the Eclipse API** (gallery-dl's
  path): would work but violates ADR 0001 (zero runtime dependencies,
  maintainable surface) and the threat model (stored password). The
  Eclipse API breaks multiple times per year when DA changes their
  JS bundle; gallery-dl's ~50 deviantart-tagged commits over 3 years
  show how much ongoing repair that path demands.
- **Browser automation via Playwright/Selenium**: would work for the
  90-day re-auth but adds 200 MB+ of npm deps, browser-profile-lock
  risks, and still doesn't solve the first-ever auth on a fresh
  machine. Violates ADR 0001.
- **Browser extension + Native Messaging**: would reduce the
  "Authorize" click to zero but doesn't help the 90-day ceiling and
  introduces a browser-extension maintenance surface. Deferred.
