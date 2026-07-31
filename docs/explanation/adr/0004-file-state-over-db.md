# 0004. File-based state (config.json + state.json) over a database

## Status

Accepted (2026-04-22). Re-evaluated when adding the synced-index —
see ADR 0005 for why the *index* chose SQLite even though tokens /
config didn't.

## Context

da-cli persists three categories of state:

- **Configuration** (`client_id`, `destination`, `redirect_uri`, …) —
  written rarely, read every command.
- **Tokens** (`access_token`, `refresh_token`, `expires_at`, `scope`) —
  written on every refresh (hourly), read every API call.
- **Sync checkpoints** (`last_feed_deviationid`, `last_sync.*`) —
  written once per sync run.

The total state is a handful of string-keyed values, <1 KB serialized.
Candidates:

- **JSON file (atomic write via tmp → rename)** — stdlib only.
- **SQLite database** — stdlib, but heavier setup; pay-per-write
  transaction cost.
- **`configparser` / `.ini`** — stdlib, less ergonomic than JSON.
- **`tomllib`** — stdlib since 3.11, but write-support isn't (and
  we'd have to depend on `tomli-w` to write).

## Decision

**JSON file, atomic-written via `_atomic_write(path, content, mode=0o600)`.**

- `~/.config/da-cli/config.json` (mode 0600) — non-secret config.
- `~/.local/state/da-cli/state.json` (mode 0600) — tokens + sync
  checkpoints. Secrets (`client_secret`) live in macOS Keychain
  separately; see ../security.md.
- `~/.local/state/da-cli/index.db` (SQLite, see ADR 0005) — the
  one state category that grows unboundedly.

## Consequences

**Positive:**

- The whole state file fits in one `cat` for debugging. The operator
  can edit it directly if needed (rare; usually `da config set` is
  the right path).
- `_atomic_write`'s tmp-file → `os.chmod` → `os.replace` sequence
  is crash-safe — a SIGKILL mid-write leaves a stale `.tmp` rather
  than a partial file. The file is never briefly world-readable.
- `load_state` self-heals from a corrupt file (renames to
  `state.json.corrupt-<timestamp>` and warns once per process).
- The whole state can be backed up with `cp state.json state.json.bak`.

**Negative:**

- Write contention is real if two processes refresh the token
  concurrently (manual + launchd overlap). Mitigated by the cross-
  process `flock` lock in `_cmd_lock` (every sync command acquires
  it before touching state).
- A multi-GB state file would be slow to load — not a concern at
  <1 KB.

## Alternatives considered

- **SQLite for tokens/config too**: rejected — overkill for <10 keys.
  SQLite shines for the index (ADR 0005) where the row count is
  unbounded; here it's bounded and tiny.
- **`configparser`**: rejected — JSON is universally readable and
  Python-native.
