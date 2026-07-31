# Configuration reference

Every settable key, with type, default, env-var override, CLI-flag
override, and where it lives on disk. The canonical resolution order
is **CLI flag > env var > macOS Keychain (secrets only) > config.json**.

## Keys

| Key | Type | Default | Env | CLI flag | Lives in | Notes |
|---|---|---|---|---|---|---|
| `client_id` | str | *(none — required)* | `DA_CLIENT_ID` | — | `config.json` (or env) | DA OAuth app ID. Non-secret; fine in plaintext. |
| `client_secret` | str | *(none — required for confidential apps)* | `DA_CLIENT_SECRET` | — | **macOS Keychain** (service `da-cli`); `config.json` (0600) on Linux | DA OAuth app secret. Never logged. |
| `destination` | path | *(none — required)* | `DA_DESTINATION` | — | `config.json` | Where deviations land. `~` is expanded. |
| `redirect_uri` | URL | `https://localhost:8765/` | `DA_REDIRECT_URI` | `da auth --redirect-uri` | `config.json` | DA's developer dashboard rejects `http://` whitelist entries — must be HTTPS even on loopback. |
| `delay_api` | float (sec) | `5.0` | — | `da sync ... --delay-api` | `config.json` | Sleep between API calls (between pages, between metadata batches). |
| `delay_image` | float (sec) | `1.5` | — | `da sync ... --delay-image` | `config.json` | Sleep between image downloads. |
| `jitter` | float (0–0.95) | `0.0` | `DA_JITTER` | `da sync ... --jitter` | `config.json` | Multiply each sleep by `uniform(1-pct, 1+pct)`. `0.4` makes a 1.5 s base sleep 0.9–2.1 s. Floor `0.05` s. |
| `concurrency` | int (1–16) | `4` | — | `da sync ... --concurrency` | `config.json` | Parallel image-download workers per page. |

> **`sync` includes mature content by default.** `--mature` defaults to
> on for `sync feed`, `sync artist`, and `sync watched` (it defaults to
> off for `search`). A filtered sync skips items mid-gallery, which looks
> like data loss on an unattended run. Pass `--no-mature` to exclude it.

## Per-command flags

Not in the config file — only on the CLI:

| Command | Flag | Type | Default | Notes |
|---|---|---|---|---|
| (root) | `--verbose` / `-v` | flag | off | Emit debug-level log lines. |
| (root) | `--quiet` / `-q` | flag | off | Suppress info-level; only warn/error. Mutually exclusive with `--verbose`. |
| (root) | `--color {auto,always,never}` | choice | `auto` | `auto` honors `NO_COLOR` and `isatty()`. |
| (root) | `--config PATH` | path | `$XDG_CONFIG_HOME/da-cli/config.json` | Override the config file path. Moves the config file only — tokens, checkpoint and index stay put. For a separate second account set `XDG_CONFIG_HOME` and `XDG_STATE_HOME`. |
| `sync feed` | `--limit N` | int | 24 | Page size; clamped to ≤ 50 (DA's cap). |
| `sync feed` | `--time-budget N` | int (sec) | 540 | Wall-clock cap for the whole run; stops cleanly a few seconds early. On `sync watched` this covers all artists together, not each one. |
| `sync feed` | `--mature` / `--no-mature` | bool | `--mature` | DA's `mature_content` query param. |
| `sync feed` | `--dry-run` | flag | off | Report what would be downloaded without writing. |
| `sync artist` | `--offset N` | int | 0 | Resume from a prior interrupted offset. |
| `sync artist` | `--full` | flag | off | Disable the synced-index early-stop. |
| `sync watched` | `--user USERNAME` | str | *(from whoami)* | Skip whoami; use this username for `/user/friends/{user}`. |
| `sync watched` | `--via-feed` | flag | off | Skip friends; discover via `/browse/deviantsyouwatch` (works with `browse` scope alone). |
| `sync watched` | `--feed-max N` | int | 2000 | Cap on deviations to scan when discovering via feed. |
| `search tag/topic/topics/toptopics/user/daily` | `--json` | flag | off | Emit raw JSON instead of summary lines. |
| `deviation show` | `--json` | flag | off | Emit raw metadata JSON. |
| `diagnose` | `--json` | flag | off | Emit machine-readable JSON; stable schema. |
| `bench` | `--pages N` | int | 10 | Synthetic feed pages. |
| `bench` | `--per-page N` | int | 24 | Deviations per page. |

## File locations

| Path | Mode | Purpose |
|---|---|---|
| `$XDG_CONFIG_HOME/da-cli/config.json` (or `~/.config/da-cli/config.json`) | 0600 | Non-secret config keys. |
| `$XDG_STATE_HOME/da-cli/state.json` (or `~/.local/state/da-cli/state.json`) | 0600 | Tokens + sync checkpoints. |
| `$XDG_STATE_HOME/da-cli/index.db` | 0600 | SQLite synced-deviation index. |
| `$XDG_STATE_HOME/da-cli/loopback-{cert,key}.pem` | 0600 | Self-signed TLS cert for the OAuth loopback listener. |
| `$XDG_STATE_HOME/da-cli/.sync.lock` | advisory | Cross-process flock sentinel; never written to. |
| macOS Keychain (service `da-cli`, account `client_secret`) | n/a | The OAuth client secret on macOS. |
| `~/Library/LaunchAgents/com.fz2000.da-cli.plist` | 0644 | launchd schedule (if `install_schedule.sh` was run). |
| `~/Applications/da-sync.app` | 0755 | launchd wrapper bundle (TCC target for Full Disk Access). |
| `~/Library/Logs/da-cli.log` | 0644 | launchd-captured stdout+stderr of the daily sync. |

## Defaults that aren't user-settable

These are module-level constants in `dacli/constants.py`, gathered for greppability:

| Constant | Value | Why |
|---|---|---|
| `HTTP_TIMEOUT_JSON_S` | 30 | `http_json` / `http_post_json` timeout. |
| `HTTP_TIMEOUT_BYTES_S` | 60 | `http_bytes` (image CDN) timeout; larger because images are bigger. |
| `HTTP_RETRY_DEFAULT` | 2 | Retry attempts before propagating a 5xx / network error. |
| `HTTP_RETRY_BACKOFF_BASE_S` | 1.5 | Exponential backoff base; `attempt=0` ≈ base, `attempt=1` ≈ 2×base, etc. |
| `AUTH_LISTENER_TIMEOUT_S` | 300 | 5-minute browser-window wait for the OAuth code. |
| `AUTH_DEFAULT_PORT` | 8765 | Loopback OAuth callback port. |
| `TOKEN_REFRESH_SKEW_S` | 60 | Refresh this many seconds before real expiry. |
| `METADATA_BATCH_SIZE` | 50 | `/deviation/metadata` caps at 50 deviationids per call. |
| `GALLERY_PAGE_CAP` | 24 | `/gallery/all` caps at 24 per page. |
| `FEED_PAGE_CAP` | 50 | `/browse/deviantsyouwatch` caps at 50 per page. |
| `JITTER_FLOOR_S` | 0.05 | Jittered sleep can never go below this. |
| `JITTER_MAX_PCT` | 0.95 | `--jitter` clamped to this; never zeroes the base. |
| `CONCURRENCY_MIN` / `CONCURRENCY_MAX` | 1 / 16 | `--concurrency` clamped to this range. |
| `REFRESH_TOKEN_TTL_DAYS` | 90 | DA's hard ceiling on refresh tokens. |
| `REFRESH_TOKEN_WARN_DAYS` | 14 | `da diagnose` reports WARN at or below. |
| `REFRESH_TOKEN_CRIT_DAYS` | 3 | `da diagnose` reports FAIL at or below. |
| `LOOPBACK_CERT_VALIDITY_DAYS` | 825 | macOS notary's recommended max. |
| `LOOPBACK_CERT_KEY_BITS` | 3072 | NIST SP 800-57 floor for new RSA issuance in 2026. |
| `DEST_FREE_SPACE_WARN_GIB` | 5.0 | `da diagnose` warns below this. |
| `DEST_FREE_SPACE_FAIL_GIB` | 1.0 | `da diagnose` fails below this. |
| `LOG_BODY_TRUNCATE` | 200 | Truncate HTTP error bodies in log lines to this many chars. |
