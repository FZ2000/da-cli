# Architecture Decision Records

This directory records architectural decisions for da-cli — the *why*
behind the code, not just the *what*. ARCHITECTURE.md captures the
current state; ADRs capture the reasoning that produced it.

## Format

Each file is `NNNN-kebab-case-title.md` with the standard ADR
template (Michael Nygard, <http://thinkrelevance.com/blog/2011/11/15/documenting-architecture-decisions>):

```markdown
# NNNN. Title

## Status
Accepted | Superseded by NNNN | Deprecated

## Context
The problem / forces / constraints.

## Decision
What we chose.

## Consequences
What we got — positive + negative.

## Alternatives considered
What we didn't choose, and why.
```

## Index

- [0001 — Zero runtime dependencies](0001-stdlib-only.md)
- [0002 — OAuth 2.1 + PKCE mandatory for every login](0002-pkce-mandatory.md)
- [0003 — No `logging` module; custom `log()` helper](0003-no-logging-module.md)
- [0004 — File-based state (config.json + state.json) over a database](0004-file-state-over-db.md)
- [0005 — SQLite for the synced-deviation index, not JSON](0005-sqlite-index.md)
- [0006 — Refresh-token TTL surfacing instead of password-grant auto-auth](0006-refresh-token-ttl-not-ropc.md)
- [0007 — Package layout](0007-package-layout.md)

## When to write a new ADR

- Adding a new external dependency (we don't, but if we did)
- Changing the public CLI surface in a backward-incompatible way
- Choosing between two reasonable alternatives where future-you will
  forget why one was picked (e.g., "why flock and not fcntl.lockf?")
- Rejecting a feature request that would violate a documented
  architectural choice (so the next person to ask gets a pointer
  instead of a re-litigation)
