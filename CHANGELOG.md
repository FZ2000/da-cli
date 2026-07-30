# Changelog

All notable changes to da-cli are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version headings are not linked to release diffs yet: no release has been
tagged, so the `v0.1.0`/`v0.2.0`/`v0.3.0` refs a comparison URL needs do
not exist. The release checklist in
[CONTRIBUTING.md](CONTRIBUTING.md#releases) adds the link definitions
along with the first tag.

## Unreleased

### Added

- **Auto-recover-on-401**: a new `authed_http_json(url, cfg, state)`
  helper wraps `http_json` with on-401-refresh-and-retry-once logic.
  Recovers automatically from server-side grant revocation — DA
  invalidates a token mid-session (e.g. after a recent re-auth
  rotated the refresh_token chain), the cached `expires_at` says it's
  still locally valid, the API call returns 401, the helper forces a
  fresh token-endpoint exchange and retries. Only if the retry also
  401s does it exit asking for `da auth`. Wired into the two main
  sync HTTP calls (browse/deviantsyouwatch, gallery/all).
- `access_token()` gained a `force_refresh: bool = False` keyword to
  bypass the cached-token-still-fresh check.
- **CI perf floor**: `da bench --json` runs as a `smoke` step;
  fails the build if `items_per_sec` drops below 500. A typical
  dev laptop clears 2,500+ items/sec — the floor catches
  genuine regressions (accidental O(n²), missed cache, etc) without
  flapping on CI noise.
- **Multi-process command lock** — `cmd_sync_feed/artist/watched`
  acquire an exclusive POSIX `flock(2)` advisory lock on
  `~/.local/state/da-cli/.sync.lock` before doing any work. If
  another process (e.g. the launchd 03:00 fire colliding with a
  manual run) holds the lock, the second invocation logs
  `skipping: another "da sync" is already running` and exits 0.
  Cron+manual overlap is now safe — no double-walks of the same
  feed page, no state.json races.
  - New `_cmd_lock(name)` context manager and `CommandLocked`
    exception. Distinct names ("sync", "bench", etc.) don't conflict.
  - 5 tests in `TestCommandLock` (test_integration.py) cover
    acquire/release, recursive-acquire-rejected, distinct-name
    independence, and that `cmd_sync_*` exits cleanly when locked.
  - `cmd_sync_watched` now calls `_cmd_sync_artist_impl` (the
    unlocked inner function) for its per-artist iterations — the
    outer command already holds the lock, so re-acquiring would
    fail.
- **`da diagnose --json`** — machine-readable mode for the
  health-check command. Emits a stable schema:
  `{timestamp, overall: {status, warnings, criticals}, findings:
  [{level, section, message}], exit_code}`. Pipe to `jq` for
  monitoring or alerting cron jobs. Suppresses the human-decoration
  output (no banner, no `✓/⚠/✗` markers) so stdout is one valid
  JSON object.
- **`da diagnose`** — end-to-end self-test that prints a categorized
  report of config, auth, index, last-sync, schedule, and TLS-cert
  status. Exits 0 if all OK, 1 on warnings, 2 on critical failures
  (suitable for shell pipelines and CI).
- **`da bench`** — synthetic feed-sync against a fully mocked HTTP
  layer. Measures CLI overhead (parsing, index ops, file IO,
  thread-pool dispatch) without touching the network. Args:
  `--pages N`, `--per-page N`, `--concurrency N`, `--json`. Reports
  pages/sec, items/sec, total elapsed. Stable JSON schema for
  perf-regression CI gating.
- **Bounded-concurrency image downloads** — sync feed/artist/watched
  now download images in parallel using a thread pool (default 4
  workers per page, configurable via `--concurrency N` / config
  field `concurrency`, clamped to [1, 16]). Each worker keeps its
  own per-image jitter sleep, so per-image rate limiting is
  preserved while page-level wall time drops by ~Nx. Page-level
  metadata fetch + feed page fetch remain sequential (they're
  rate-limited by DA more strictly than the image CDN). 5 new
  tests in `TestConcurrentDownloads` (test_integration.py): result
  ordering, index correctness under contention, sequential
  fallback at concurrency=1, actual parallelism via thread
  inspection, and bounds clamping.
- **Per-run sync summary** persisted to `state.json` under
  `last_sync`: `kind`, `started_at`, `ended_at`, `duration_s`,
  `totals`, `stop_reason`, plus per-mode extras (artist for
  `artist`, via for `watched`, etc.). Read by `da diagnose`.
  Survives across runs.
- **`da auth` paste-back mode**: when `--redirect-uri` is
  non-loopback (DA now requires HTTPS for non-localhost redirects),
  the CLI prints the authorization URL, you authorize in a browser,
  and paste back the URL DA redirects you to. The `code` is
  extracted from the URL and exchanged for tokens. Force the flow
  on a loopback URI with the new `--paste` flag.
- **`tests/test_faults.py`** — comprehensive fault-injection suite
  (25 tests). Covers retryable HTTP errors (5xx + network/timeout),
  permanent HTTP errors (4xx fail-fast, 429 caller-policy),
  exponential-backoff verification, malformed responses
  (invalid/empty JSON), image-download retry symmetry, and
  per-deviation failure tolerance in concurrent sync (one bad
  image doesn't poison its 23 siblings).
- **`tests/test_integration.py`** — comprehensive end-to-end test
  suite exercising every CLI command through the argparse → handler
  pipeline with HTTP mocked at the `urlopen` level. Covers the
  full parser surface (45 invocation shapes), config round-trip,
  sync feed/artist/watched happy paths and edge cases (early-stop,
  metadata-skip on all-known pages, 429 handling), every search
  endpoint (live + deprecated), user/watch/deviation/daily,
  index commands, full auth lifecycle (logout/refresh/auto-refresh),
  whoami 403 graceful degradation, cross-command round-trip, and
  KeyboardInterrupt → exit(130).
- **Open-source readiness**: `CODE_OF_CONDUCT.md`,
  `SECURITY.md` at repo root, `.github/ISSUE_TEMPLATE/`,
  `.github/PULL_REQUEST_TEMPLATE.md`, `.github/CODEOWNERS`,
  `renovate.json` (for automated dev-dep updates), `CITATION.cff`,
  a Python 3.10–3.14 matrix in CI, and a `[project.scripts]`
  entry point so `pip install da-cli` puts `da` on PATH.
- **Top-tier OSS polish**:
  `ARCHITECTURE.md` (vertical-slice map of the 3,000-line dacli.py),
  `Makefile` (developer-convenience targets), `.editorconfig`,
  `.gitattributes` (LF-only + sdist export-ignore),
  `.git-blame-ignore-revs` (format-pass commits excluded from blame),
  `.pre-commit-config.yaml` (ruff/mypy/markdownlint/gitleaks),
  `.markdownlint.yaml` + `.prettierrc.yaml`,
  `AGENTS.md` (renamed from `SKILL.md` per the 2024-2026 convention),
  `examples/` (4 executable recipes),
  `.github/workflows/ci.yml` now matrix-tests on Linux across
  Python 3.10–3.14.
- **Auto-auth research**: documented (now in ADR 0006)
  why "store password for daily token" is technically impossible on
  DA's OAuth API (`unsupported_grant_type`) and why the 3-month
  re-auth ceiling is DA-imposed. `da diagnose` now surfaces the
  remaining refresh-token TTL (WARN ≤14 days, FAIL ≤3 days) so
  operators aren't surprised by a dead token at 03:00.
- **Exception hierarchy**: `DacliError` (umbrella) + `ConfigError` /
  `AuthError` / `HttpError` / `SyncError` subclasses +
  `CommandLockedError` re-parented under `DacliError`. Wrappers can
  now `except dacli.AuthError` instead of parsing exit codes.
- **`__all__`** declared (50+ entries) — the public surface is now
  machine-declared; SLF001 lint catches `_private` leaks.
- **`mature_content_param()`** helper replaces 11 inline copies of
  `'true' if X.mature else 'false'`.
- **Named constants** for every magic number: HTTP_TIMEOUT_*_S,
  HTTP_RETRY_DEFAULT, AUTH_LISTENER_TIMEOUT_S, TOKEN_REFRESH_SKEW_S,
  METADATA_BATCH_SIZE, GALLERY_PAGE_CAP, FEED_PAGE_CAP, JITTER_FLOOR_S,
  JITTER_MAX_PCT, CONCURRENCY_MIN/MAX, REFRESH_TOKEN_TTL_DAYS,
  REFRESH_TOKEN_WARN_DAYS, REFRESH_TOKEN_CRIT_DAYS, etc.
- **Self-signed cert hardening**: RSA 2048 → 3072 (NIST SP 800-57
  2026 floor), validity 3650d → 825d (macOS notary limit), explicit
  `-sha256`.
- **SQLite perf pragmas**: added `synchronous=NORMAL`, `temp_store=MEMORY`,
  `cache_size=-65536` (64 MiB), `mmap_size=268435456` (256 MiB).
- **`--unmask` security**: `da config get <secret> --unmask` now writes
  to stderr so piping stdout to a file can't silently capture the
  raw secret.

### Changed

- **HTTP retry contract tightened**:
  - Only 5xx (500, 502, 503, 504) and network/timeout errors retry.
    4xx including 404, 401, 403, 422 fail fast (they're permanent
    for this request shape; retrying just delays the inevitable).
  - 429 still fails fast — caller-policy, not http-layer policy.
    `cmd_sync_feed` already breaks its loop on 429.
  - Backoff is now **exponential with ±10% jitter**: ~base, ~2·base,
    ~4·base. Avoids thundering-herd retries when many concurrent
    workers hit the same transient failure.
  - `http_bytes` (image CDN) follows the same contract.
  - New constant `RETRYABLE_HTTP_CODES = {500, 502, 503, 504}` and
    helper `_retry_backoff(attempt, base)`.
- **Default `redirect_uri` is now `https://localhost:8765/`** (was
  plain HTTP). DA's developer-dashboard whitelist UI rejects HTTP
  entries — you'd never be able to whitelist the old default.
- The loopback listener now terminates TLS using a self-signed cert
  generated on first use (`openssl req -x509 …`) and stored at
  `~/.local/state/da-cli/loopback-{cert,key}.pem` (mode 0600).
  Browsers will show a one-time "connection not private" warning
  the first time `da auth` runs — click through it. The cert never
  leaves your machine and is only used for the localhost callback.
  Requires `openssl` on PATH (preinstalled on macOS).
- `install_schedule.sh` now builds a stable-path `.app` bundle at
  `~/Applications/da-sync.app` and the launchd plist invokes the
  bundle's executable. This lets users grant macOS Full Disk Access
  to the bundle (a path you control) instead of the brew-versioned
  Python binary, which moves on every `brew upgrade`. Required when
  the destination is on `/Volumes/` or any other TCC-protected path
  — without it the launchd job hangs in `mkdir` because there's no
  UI to surface the permission prompt.
- `install_schedule.sh uninstall` now also removes the bundle.
- README documents the FDA grant step.

### Removed

- **`da search popular`** and **`da search newest`** are now
  deprecation stubs (exit 2 with an actionable error). DA retired
  `/browse/popular` and `/browse/newest` — every variant returns
  `HTTP 404 "Api endpoint not found."`. The deprecation message
  points at the live alternatives: `da search topic`,
  `da search tag`, `da daily`.

### Fixed

- **`da sync watched --time-budget` now bounds the whole run**, not each
  artist. It previously handed the full budget to every artist in turn,
  so `--time-budget 300` across 200 watched artists could still be
  running many hours later — for a scheduled job, the flag's entire
  purpose defeated. Each artist is now given whatever remains, and once
  less than `MIN_ARTIST_BUDGET_S` is left the rest are skipped and
  recorded as not attempted.
- **A sync stopped by the clock no longer records `stop_reason:
  "complete"`.** Both walks initialise the reason to `"complete"` and
  every early exit overwrites it, so a run truncated by the time budget
  was indistinguishable from a finished one. It now records
  `"time budget exhausted"`.
- **`da diagnose` no longer reports every last sync as `ok`.** The level
  was hardcoded, so a truncated walk, an HTTP 429 abort and a clean pass
  all looked identical to a monitor — a nightly job that never finished
  appeared healthy indefinitely. Anything that is not a clean finish, or
  that had per-item failures, is now a warning (never a failure: each of
  these resolves itself on the next run).
- **`da sync watched` no longer exits 0 when artists fail.** It exits 1
  on partial failure and 2 when every artist failed, matching the
  documented `da sync ... || notify` pattern.
- **`da search user`** now uses POST against `/user/whois` with
  `usernames[]=` form fields, matching DA's actual API. The previous
  GET request returned HTTP 400. `http_post_json` gained an optional
  `token` parameter so the handler can attach the Bearer token.
- Auth listener now uses `SO_REUSEADDR` to avoid `Address already
  in use` if a prior run is still in `TIME_WAIT`.
- Auth listener is bound BEFORE `webbrowser.open` is invoked so a
  fast browser redirect can't race the bind.
- `da auth` validates the redirect URI's scheme and host before
  binding (rejects unparsable URIs cleanly instead of falling into
  paste mode).
- **A single stalled connection no longer wedges the `da auth` listener.**
  The loopback server was a single-threaded `TCPServer`, so a client that
  opened a socket and sent nothing held the only slot and the real OAuth
  callback was never accepted — `da auth` waited out its full timeout and
  failed. Browsers open speculative preconnects, so this was reachable in
  ordinary use. The server now threads, each connection carries
  `AUTH_CONNECTION_TIMEOUT_S`, and the TLS handshake happens in the
  per-connection worker rather than on the accept path (wrapping the
  listening socket put a blocking handshake back in `accept()`).
- **A corrupt loopback cert is now replaced instead of used.**
  `_ensure_self_signed_cert` returned any pair that merely existed, so a
  truncated or stray-written cert made `da auth` fail with a bare
  `[SSL] PEM lib` forever, with nothing to suggest that deleting two
  files would fix it. The pair is now load-tested and regenerated when
  unusable, and a generated-but-unloadable pair reports what to do.
- **The test suite no longer writes into the real state directory.**
  `LOOPBACK_CERT` / `LOOPBACK_KEY` are derived from `STATE_DIR` at import
  time, so redirecting `STATE_DIR` alone left them aimed at
  `~/.local/state/da-cli`. Two tests wrote 4-byte stubs there, which then
  triggered the `[SSL] PEM lib` failure above on the developer's own
  machine.

## 0.3.0 — 2026-04-26

### Added

- **Synced-deviation index** (`~/.local/state/da-cli/index.db`,
  SQLite/WAL, mode 0600). Primary key on `deviationid`, secondary
  index on `(artist, synced_at DESC)`. O(1) membership tests and
  bulk filtering — replaces per-deviation disk stats.
- **Per-artist early-stop** in `sync artist`: gallery walks now
  exit as soon as they hit a known deviationid (gallery is returned
  reverse-chronologically, so subsequent pages are guaranteed dups).
  A no-change second-day run is **one API call**, not the full gallery.
- **Page-level skip** in `sync feed`: if every id on a page is
  already in the index, the metadata batch is skipped entirely.
- **Auto-bootstrap**: if the index is empty but the destination has
  content, the next sync walks the disk to populate the index
  (one-time cost, idempotent).
- `da index show` — print row count, top artists, db size.
- `da index rebuild` — walk destination and re-import. Idempotent.
- `--full` flag on `sync artist` and `sync watched` — disable the
  early-stop for paranoid backfills or after content rotation.

### Changed

- `_save_one()` now consults the index first (O(1)) before any disk
  stat. Disk hit triggers a lazy backfill into the index.
- Default `install_schedule.sh` cadence is now **daily at 03:00**
  (StartCalendarInterval), with `DA_HOUR`/`DA_MINUTE` overrides.
  `DA_INTERVAL_SECONDS` still works for fixed-interval scheduling.
- Default jitter on the scheduled `sync feed` run is now `0.4`
  (was 0). Smooths the request cadence.
- `__version__` bumped to `0.3.0`.

## 0.2.0 — 2026-04-26

### Added

- `--jitter` flag (and `DA_JITTER` env / `jitter` config field) to add
  human-like fuzziness to API and image-download delays. Range:
  `0.0`–`0.95`; defaults to `0` (deterministic).
- `sync watched --via-feed` mode: discover watched artists by walking
  `/browse/deviantsyouwatch` instead of `/user/friends/{me}`. Lets users
  with `browse`-scope tokens sync their watch list without re-auth for
  `user` scope.
- `sync watched --feed-max <N>`: cap the feed-discovery walk at N
  deviations (default 2000).
- `daily [YYYY-MM-DD]`: pull a single day's Daily Deviations.
- `auth logout`: deletes the local state file.
- `config get [--unmask]` and `config unset` for round-tripping the
  config store from the CLI.
- `whoami` now degrades gracefully when the token lacks `user` scope.
- Atomic file writes (tmp → rename, fsync) for `state.json`,
  `config.json`, and `description.json`.
- Dedup-aware folder resolution: title collisions append the
  `deviationid` suffix instead of overwriting.
- Comprehensive test suite (158 tests, 90%+ coverage) under `tests/`.
- Strict `ruff` + `mypy` configuration in `pyproject.toml`.
- Documentation: `README.md`, `docs/security.md`, this changelog,
  `CONTRIBUTING.md`.
- CI workflow at `.github/workflows/ci.yml` for GitHub Actions.

### Changed

- `log()` now uses `flush=True`, so background runs under `nohup` /
  launchd produce real-time log output instead of full-buffered chunks.
- `safe_filename()` now collapses runs of unsafe chars to a single
  underscore and strips leading/trailing underscores.
- Config priority is now: CLI flag > env var > macOS Keychain (secrets)
  > `~/.config/da-cli/config.json`. Previously, secrets-on-disk took
  precedence over Keychain.

### Fixed

- `cmd_whoami` no longer crashes on a 403 from the `whoami` endpoint
  when the token has only `browse` scope.
- Half-written `image.{ext}.part` files are now treated as in-progress
  rather than complete; sync restarts the download instead of skipping.

## 0.1.0 — 2026-04-22

### Added

- Initial release: `auth`, `whoami`, `refresh`, `sync feed`,
  `sync artist`, `sync watched`, `search popular|newest|tag|user`,
  `user profile`, `watch list`, `deviation show`, `config show|set`.
- OAuth 2.1 + PKCE flow with loopback redirect.
- macOS Keychain integration for `client_secret` storage.
- XDG-compatible config and state paths.
