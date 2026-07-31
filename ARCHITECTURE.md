# Architecture

`da-cli` is a Python 3 package (`dacli/`) plus a small `da` shim that
resolves its own location and delegates to `dacli.main`. The runtime has
**zero third-party dependencies** — only the standard library is
imported. The dev toolchain (ruff, mypy, pytest) is declared under
`[project.optional-dependencies] dev` in `pyproject.toml`.

> Decisions behind this structure are recorded in the
> [Architecture Decision Records](docs/explanation/adr/README.md).
> This file describes the *current* state; the ADRs explain *why*.
> The package layout in particular is
> [ADR 0007](docs/explanation/adr/0007-package-layout.md).

## The modules

Listed roughly bottom-up: each depends only on the ones above it.

| Module | Holds |
|---|---|
| `constants.py` | Paths and every tunable — timeouts, delays, page caps, token TTLs. |
| `errors.py` | The exception hierarchy: `DacliError` and four subclasses (`ConfigError`, `AuthError`, `HttpError`, `SyncError`). A fifth, `CommandLockedError`, lives in `lock.py` beside the lock it reports on. |
| `output.py` | `log()`, colour state, and the pure helpers: `safe_filename`, `mask_secret`, `_atomic_write`. |
| `config.py` | `config.json`, `state.json`, and secret storage (macOS Keychain, 0600 file elsewhere). |
| `lock.py` | The cross-process flock and the last-sync summary it guards. |
| `index.py` | The SQLite synced-deviation index and its self-healing. |
| `net.py` | Every outbound request: `http_json`, `http_post_json`, `http_bytes`, retry and backoff. |
| `auth.py` | OAuth 2.1 with PKCE — the loopback listener, token refresh, `whoami`. |
| `sync.py` | The sync engine: pagination, early-stop, atomic saves, the thread pool. |
| `__init__.py` | argparse wiring, `main()` and its error-advice handlers, and the re-export surface. No command handlers: they live in `auth.py`, `sync.py` and `commands/`. |
| `commands/` | The remaining handlers, one module per area: `config.py`, `search.py`, `user.py`, `index.py`, `diagnose.py`, `bench.py`. |

Named `net.py` rather than `http.py` deliberately: a submodule named
after a stdlib module the package imports would shadow it. See ADR 0007;
`tests/test_package_layout.py` enforces it.

### Finding things

Use the symbols, not line numbers — an earlier version of this file
carried a line-range map and it drifted out of date twice.

```bash
grep -rn "def cmd_sync_feed" dacli/     # a command handler
grep -rn "def index_" dacli/index.py    # the index API
grep -rn "add_argument" dacli/__init__.py   # a flag's definition
```

### The one rule when editing a module

A submodule reads names the tests patch — `http_json`, `CONFIG_PATH`,
`STATE_DIR`, `LOOPBACK_CERT`, the keychain helpers, `log` — through the
package (`dacli.http_json`) rather than importing them. Importing binds
the real value, so a test patch would no longer reach the caller and the
suite would use the live API and your real config while still passing.
`tools/verify_refactor.sh` checks this.

## Data model

Six on-disk artefacts, all under the user's home:

```text
~/.config/da-cli/config.json        (mode 0600)  non-secret settings
~/.local/state/da-cli/state.json    (mode 0600)  tokens + sync checkpoints
~/.local/state/da-cli/index.db      (mode 0600)  SQLite synced-deviation index
~/.local/state/da-cli/loopback-*.pem(mode 0600)  self-signed TLS cert for OAuth loopback
~/.local/state/da-cli/.sync.lock    (mode 0600)  cross-process flock sentinel, one sync at a time
~/.local/state/da-cli/.token.lock   (mode 0600)  serialises refresh-token rotation
```

The synced index (`index.db`) is the load-bearing state for incremental
sync — without it, every run would re-walk the entire feed/gallery. The
SQLite schema is a single `synced` table with `deviationid` primary key
plus `(artist, synced_at DESC)` secondary index for the per-artist
early-stop. Self-healing: `index_has`/`index_filter_known` delete rows
whose on-disk folder has vanished, so operator `rm -rf` of a sub-tree
results in those deviations being re-downloaded on the next sync.

## Concurrency model

- **Within one process**: a single cached SQLite connection guarded by
  `_INDEX_LOCK` (threading.Lock). Image downloads run in a bounded
  thread pool (`concurrency` workers, default 4, max 16) but every
  SQLite op serialises through the lock. Lock hold time is sub-millisecond;
  image downloads dominate by 4–5 orders of magnitude.
- **Across processes**: `cmd_sync_*` acquires an exclusive POSIX flock
  on `~/.local/state/da-cli/.sync.lock` via `_cmd_lock("sync")` before
  doing any work. If another process holds the lock, the second
  invocation logs `skipping: another "da sync" is already running` and
  exits 0 — the holder will catch up.

## Failure model

`da` exits with one of:

| Code | Meaning                                                          |
|------|------------------------------------------------------------------|
| 0    | Success (or "skipped because another sync held the lock").       |
| 1    | Recoverable warning (e.g. `config get` on a missing key).        |
| 2    | Critical failure (config missing, auth required, disk full, ...).|
| 130  | KeyboardInterrupt (Ctrl-C).                                      |

Long-running syncs persist a structured summary to `state.json` under
`last_sync` (`kind`, `started_at`, `ended_at`, `duration_s`, `totals`,
`stop_reason`, plus per-mode extras). `da diagnose` reads it.

## Atomicity guarantees

- `config.json`, `state.json`, and per-deviation `description.json`
  are written via `_atomic_write`: tmp-file → `os.chmod` → `os.replace`.
- Image downloads stage to `image.<ext>.part`, `fsync`, then rename to
  `image.<ext>`. The dedup check looks for the final name only, so a
  half-written `.part` after a SIGKILL doesn't falsely count as fetched.
- `state.json` is rewritten on every token refresh with mode 0600.
- SQLite uses WAL journal mode; a crash mid-INSERT rolls back the row
  but never corrupts the file.

## Where to add things

| You want to...                          | Touch...                                            |
|-----------------------------------------|-----------------------------------------------------|
| Add a new CLI subcommand                | `build_parser()` + a new `cmd_*` handler.           |
| Add a new HTTP endpoint                 | The relevant `cmd_*`, via `http_json` / `authed_http_json`. |
| Add a new config field                  | `load_config()` (env map if env-overridable) + a constant for the default. |
| Change the index schema                 | `_INDEX_SCHEMA` + a migration step in `_index()`.   |
| Tighten the retry policy                | `RETRYABLE_HTTP_CODES` / `_retry_backoff()`.        |
| Add a macOS-only feature                | Gate on `sys.platform == "darwin"`; add a `[tool.coverage.report] exclude_also` line if it's a branch CI can't exec. |

## Testing strategy

Tests live under `tests/` and split by concern (one `test_*.py` per
area). The split is **not** unit/integration/e2e — instead, each file
covers a slice of the package:

- `test_util.py` — pure helpers.
- `test_config.py` — config/state file round-trip.
- `test_http_and_auth.py` — HTTP retry contract + OAuth refresh.
- `test_sync.py` — `_save_one`, `_resolve_folder`.
- `test_index.py` — SQLite index + self-healing.
- `test_faults.py` — fault-injection suite (5xx, network errors, malformed JSON).
- `test_auth_flow.py` — PKCE flow + keychain helpers.
- `test_cli.py` — argparse wiring + small commands.
- `test_commands.py` — command bodies (sync, search, etc.).
- `test_integration.py` — end-to-end mocked-HTTP tests.
- `test_shim.py` — the `da` wrapper as a subprocess.

Fixtures in `conftest.py`:

- `isolated_paths` — redirects `CONFIG_PATH`/`STATE_PATH`/`INDEX_PATH` to `tmp_path`.
- `mock_urlopen` — patches `urllib.request.urlopen`.
- `_no_keychain_autouse` (autouse) — stubs `_keychain_get`/`_keychain_set` so tests can't touch the real macOS Keychain. Use `real_keychain` to opt out.

Coverage gate: **≥ 92 %** on the `dacli` package.
