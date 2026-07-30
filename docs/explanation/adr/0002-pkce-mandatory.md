# 0002. OAuth 2.1 + PKCE mandatory for every login

## Status

Accepted (2026-04-22). DA's own OAuth deployment enforces this on
newly-registered apps, so it's not really a choice.

## Context

The CLI needs to authenticate the user to DA's API. Options on the
table when da-cli was started:

- **OAuth 2.0 Implicit grant** — deprecated in OAuth 2.1; DA still
  accepts it on grandfathered apps but the developer dashboard
  refuses new Implicit registrations.
- **OAuth 2.0 Authorization Code** without PKCE — works but is
  vulnerable to code interception on insecure transports (the
  standard rationale for PKCE in RFC 7636).
- **OAuth 2.0 Authorization Code + PKCE** (S256 challenge method) —
  the OAuth 2.1 MTI (mandatory-to-implement).
- **OAuth 2.0 Resource Owner Password Credentials** — eliminated
  early; see ADR 0006.
- **DA's private Eclipse API + form-scraped session cookies** —
  gallery-dl's path; rejected (see ADR 0001 for why we don't reverse-
  engineer undocumented APIs).

## Decision

**Authorization Code with PKCE (S256), per OAuth 2.1 §10.2.**

Concretely:

- `cmd_auth` generates a 40-byte cryptographically-random verifier
  via `os.urandom(40)` → base64url-encoded (~53 chars, within
  RFC 7636 §4.1's 43-128 range).
- The verifier never leaves the process until the token exchange.
- The challenge sent in the authorize URL is `base64url(sha256(verifier))`.
- DA's `code_challenge_method=S256` is the only one offered.

## Consequences

**Positive:**

- A code intercepted at the redirect step is useless without the
  verifier, which only ever existed in process memory. Defense
  against the most common OAuth code-injection attack class.
- Aligned with OAuth 2.1, so future DA API changes that tighten
  further (e.g., requiring PKCE on confidential clients) don't
  break us.
- The verifier doubles as per-session entropy — every `da auth`
  run uses a fresh pair, so a leaked verifier from yesterday is
  useless today.

**Negative:**

- Adds 3 lines of code (verifier generation + challenge derivation).
  This is the cheapest cost-benefit ratio in the entire CLI.

## Alternatives considered

- **Plain `code_challenge_method=S256` without PKCE** is a
  contradiction in terms; S256 IS PKCE.
- **`code_challenge_method=plain`** — RFC 7636 allows it; DA refuses
  it; we wouldn't use it even if DA accepted.
