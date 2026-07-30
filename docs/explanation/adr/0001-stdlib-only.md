# 0001. Zero runtime dependencies

## Status

Accepted (2026-04-22). Binding.

## Context

da-cli holds credentials that unlock a user's whole DeviantArt
identity: a long-lived refresh token and, on macOS, a client secret in
the Keychain. It also runs unattended on a schedule, where a
supply-chain compromise would go unnoticed for a long time.

That shapes what the project can afford to depend on. Every runtime
dependency is code the user implicitly trusts with those credentials,
did not choose, and which can change under them on any upgrade. The
comparable tools each pull in five or more.

The project also has to install on more than one machine without
per-host virtualenvs or dependency-upgrade chores.

## Decision

**The runtime imports only the Python standard library. No third-party
package may appear in `[project.dependencies]`.**

Development tooling — ruff, mypy, pytest, pytest-cov, vcrpy — lives in
the `dev` and `integration` extras and never ships to users.

Python 3.10 is the floor: the code uses PEP 604 unions (`X | None`)
evaluated at runtime, without `from __future__ import annotations`.

## Consequences

### Positive

- The dependency audit is `pip show da-cli` → nothing.
- No CVE triage, no upstream breaking changes, no transitive-dependency
  surprises inside a 03:00 cron job.
- Installation is a file copy. No virtualenv is needed to *use* the
  tool, only to develop it.

### Negative

- Things a library would give free are hand-rolled: HTTP retry and
  backoff (`dacli/net.py`), the PKCE flow and its loopback TLS listener
  (`dacli/auth.py`), the SQLite index layer (`dacli/index.py`).
- `urllib.request` is clumsier than `requests`, and its exception
  hierarchy is easy to get subtly wrong — so the retry contract is
  pinned by tests rather than inherited from a library's defaults.
- Features needing a dependency are rejected outright. AES decryption
  for browser-cookie extraction would require `cryptography`, which
  takes the cookie-based auth path off the table entirely (see
  [ADR 0006](0006-refresh-token-ttl-not-ropc.md)).
- Some capabilities are simply unavailable: no Linux Secret Service
  without `secretstorage`, so on non-macOS the client secret falls back
  to a 0600 file with a warning.

### Neutral

- The dev toolchain is unconstrained; strictness there costs users
  nothing.

## Alternatives considered

**A small, well-known dependency set** (`requests`, `keyring`).
Rejected: it defeats the audit argument, and `keyring` would pull a
platform-specific tree into a credential path.

**`click` or `typer` for the CLI layer.** Rejected: `argparse` is
stdlib and the command surface is small enough that decorator
ergonomics do not pay for a dependency.

**Vendoring dependencies into the repository.** Rejected: the install
stays simple, but the user inherits responsibility for tracking
upstream security fixes with none of the tooling that normally helps.

## See also

[ADR 0007](0007-package-layout.md) — how the code is organised inside
the package.
