---
name: da-cli
description: |
  Operate the DeviantArt CLI (`da`) — OAuth-authenticated sync of watched
  artists, search, browse, and metadata fetch. TRIGGER when: the user
  asks about DeviantArt content, watching artists, syncing/downloading
  galleries, browsing tags, or any subcommand of `da` (auth, sync,
  search, watch, deviation, daily, config). DO NOT TRIGGER for generic
  image-scraping, generic OAuth, or non-DeviantArt social platforms.
tags:
  - deviantart
  - cli
  - oauth
  - sync
  - download
---

# da-cli — DeviantArt CLI skill

A Python 3 CLI (the `dacli` package + `da` shim, stdlib-only at
runtime) that authenticates against DeviantArt with OAuth 2.1 + PKCE,
syncs watched-artist galleries to a local destination directory, and
exposes search / browse / metadata helpers.

This file is the **agent-facing reference**: every subcommand, every
flag, the configuration model, the on-disk layout, the failure modes,
and the recipes for common goals. Read it once before issuing any `da`
command; do not infer behaviour from command names alone.

## When to use this skill

Use `da` when the user wants to:

- Authenticate against the DeviantArt API.
- Pull a snapshot of watched artists' galleries to disk.
- Walk a single artist's full back-catalog.
- Search / browse DA by tag, query, topic, or daily picks.
- Inspect a deviation's metadata (`description.json` is the raw body).
- Manage the local config / token state (e.g. clear creds before
  changing accounts).

Do **not** use `da` to:

- Post / publish content (DA write APIs are not wired up).
- Download videos as `.mp4` (`image.{ext}` is the still preview; the
  video URL is in `description.json`'s `metadata.videos[]`).
- Modify a watch list (DA's API doesn't expose follow/unfollow).

## Mental model

1. **Config** (`~/.config/da-cli/config.json`, mode 0600) holds
   non-secret settings: `client_id`, `destination`, `redirect_uri`,
   `delay_api`, `delay_image`, `jitter`, `concurrency`.
2. **Secrets** (`client_secret`) live in the macOS Keychain (service
   `da-cli`). On non-Darwin or if Keychain is unavailable they fall
   back to the same config file with a warning.
3. **State** (`~/.local/state/da-cli/state.json`, mode 0600) holds
   tokens (`access_token`, `refresh_token`, `expires_at`, `scope`)
   and sync checkpoints (`last_feed_deviationid`,
   `last_feed_sync_at`).
4. **Synced index** (`~/.local/state/da-cli/index.db`, mode 0600,
   SQLite/WAL) is a flat per-deviation table — primary key is the
   `deviationid`. It powers O(1) "have I seen this?" checks and the
   per-artist early-stop. Auto-bootstraps from disk on first sync if
   empty. See "Synced index" below.
5. **Destination** (no default — must be set via
   `da config set destination ...` or `DA_DESTINATION`) is where
   deviations land:

   ```text
   <destination>/
     <artist_username>/
       <deviation_title>/
         description.json   # full metadata blob, atomic-written
         image.<ext>        # highest-res CDN variant
   ```

6. **Sync is idempotent.** The synced index is the source of truth
   for "already fetched"; if a deviationid is in the index, sync
   skips it without any disk stat. As a fallback, a folder
   containing both `description.json` and any `image.*` is also
   treated as already-fetched (and gets backfilled into the index
   the next time sync touches it). Half-written `image.<ext>.part`
   files never count.
7. **Title collisions** are resolved by appending the first 8 chars
   of the deviationid to the folder name (`<title>--<shortid>/`)
   on the second deviation onward.

## Synced index

Without an index, every sync paid for the full feed/gallery walk even
when nothing was new — O(all stories). The SQLite-backed index makes
sync O(new content):

- **`sync feed`** — for each page, `index_filter_known(ids)` returns
  the subset that's already saved. If every id on the page is known,
  skip the metadata batch entirely. Combined with the existing
  `last_feed_deviationid` checkpoint, a no-op feed run is ~1 API call.
- **`sync artist <user>`** — gallery is returned reverse-chronological,
  so once we hit the first known id we stop walking. A second-day run
  for an unchanged artist is **one API call**, not the full gallery.
  Override with `--full` to disable the early-stop (use after rotating
  content or to fix a missed deviation).
- **`sync watched`** — same per-artist early-stop applies to every
  watched artist iterated through. `--full` propagates.

**Operations:**

```bash
da index show                 # row count, top artists, db size
da index rebuild              # walk destination, re-import everything
da sync artist X --full       # disable early-stop for one run
da sync watched --full        # paranoid full-walk of every watched artist
```

**Auto-bootstrap:** if you already have content on disk but the index
is empty (e.g. first run after upgrading from v0.2.x), the next sync
imports everything from disk before running. One-time cost, ~10 sec
per 10 k items.

**Failure modes:**

- Corrupted db → delete `~/.local/state/da-cli/index.db` and run
  `da index rebuild`. Idempotent — safe to re-run.
- Partial writes → atomic at the row level (sqlite commit-on-write).
  Worst case: one row missing from a crashed run, recovered on the
  next sync (which re-walks that page once and re-adds it).
- Concurrent runs → SQLite WAL mode tolerates this; the manual sync
  - the launchd schedule can overlap without corruption.

## Configuration priority (highest first)

| Rank | Source | Used for |
|-----:|---|---|
| 1 | CLI flag (e.g. `--delay-api 5.0`) | per-invocation overrides |
| 2 | env var (`DA_CLIENT_ID`, `DA_CLIENT_SECRET`, `DA_DESTINATION`, `DA_REDIRECT_URI`) | shell / launchd / CI |
| 3 | macOS Keychain (`security` CLI, service `da-cli`) | secret keys only |
| 4 | `~/.config/da-cli/config.json` | non-secret defaults |

Inspect with `da config show` (secrets masked); `da config get <key>
--unmask` prints the raw value (use sparingly).

## First-time setup

```bash
# 1. Create an OAuth app at https://www.deviantart.com/developers
#    DA's developer dashboard REJECTS http:// entries from the OAuth
#    Redirect URI Whitelist — only HTTPS is accepted there. Two options:
#      a. Whitelist `https://localhost:8765/` (CLI default; the listener
#         terminates TLS using a self-signed cert generated on first run.
#         Browser shows a one-time cert warning — click through.)
#      b. Whitelist any HTTPS URL you control (e.g. `https://yoursite.com/`)
#         and use paste-back: `da auth --paste --redirect-uri <that-url>`
#    Whitelist match is BYTE-EXACT — trailing slash, port, case, scheme.
da config set client_id 12345
da config set client_secret <SECRET>          # → Keychain on macOS (if Confidential)
da config set destination /path/to/library

# 2. Browser-based PKCE login. Opens a browser, captures code on
#    127.0.0.1:8765, exchanges for tokens.
da auth                                       # default scope: "browse"
da auth --scope "browse user"                 # broader scope (whoami / watch list)

# 3. Verify
da whoami
```

Re-run `da auth` only if the refresh token expires or the app is
revoked — DA refresh tokens are long-lived.

## Subcommand reference

### Auth lifecycle

```bash
da auth [--scope SCOPE] [--redirect-uri URI] [--paste]   # PKCE login flow
da auth logout                                            # delete state.json
da whoami                                                 # confirm token + identity
da refresh                                                # force-refresh access token
```

- `--scope` accepts space-separated DA scopes. Default `"browse"`
  unlocks read endpoints (gallery, browse, search, deviation,
  daily). `"user"` adds `whoami`, `watch list`, and the friends-based
  watched-list enumeration.
- `--redirect-uri` overrides the default `https://localhost:8765/`.
  Must match the whitelisted URI on the OAuth app **byte-exactly**.
- The CLI auto-detects loopback vs. paste-back:
  - **Loopback** (`localhost`, `127.0.0.1` — NOT `::1`): binds a local TLS
    listener using a self-signed cert (generated on first run via
    `openssl req -x509`, stored at `~/.local/state/da-cli/loopback-{cert,key}.pem`).
    Browser shows a one-time "connection not private" warning the
    user clicks through.
  - **Non-loopback** (any other host): paste-back mode. The CLI
    prints the auth URL, the user authorizes in a browser, copies
    the URL DA redirects them to (containing `?code=...`), pastes
    it at the `>` prompt. The CLI extracts the code.
- `--paste` forces paste-back even on a loopback URI (useful when
  running over SSH or in a container with no localhost binding).
- `whoami` degrades to a warning (not crash) if the token lacks
  `user` scope. The placebo endpoint still confirms the access
  token is valid.

**Whitelist gotcha**: DA's developer dashboard rejects HTTP entries.
You must whitelist `https://...` URIs only. Common byte-mismatch
slips: trailing slash, host literal (`localhost` vs `127.0.0.1`),
case, leading/trailing whitespace.

### Config

```bash
da config show                          # full dump, secrets masked
da config path                          # paths only (config + state)
da config set <key> <value>             # write; secrets → Keychain
da config get <key> [--unmask]          # read; pass --unmask for raw
da config unset <key>                   # remove from config + Keychain
```

Settable keys: `client_id`, `client_secret` (secret), `destination`,
`redirect_uri`, `delay_api`, `delay_image`, `jitter`, `concurrency`.

### Sync

The three sync commands share most of an option set. `sync watched` is
the exception: it has no `--limit` and no `--offset`, because it walks
each artist at the page cap and lets each resume its own position.

| Flag | Type | Default | Notes |
|---|---|---|---|
| `--mature` / `--no-mature` | bool | `--mature` | DA's `mature_content` query param. `--no-mature` excludes 18+ content from results. |
| `--time-budget N` | int (sec) | 540 | Wall-clock cap for the whole run, stopping `TIME_BUDGET_MARGIN_S` early. On `sync watched` it bounds all artists together — each is handed the remainder, and once less than `MIN_ARTIST_BUDGET_S` is left the rest are skipped. Pick to fit your launchd schedule. |
| `--delay-api SEC` | float | 5.0 | Sleep between API calls (between pages, between metadata batches). |
| `--delay-image SEC` | float | 1.5 | Sleep between image downloads. |
| `--jitter PCT` | float (0–0.95) | 0 | Multiply each sleep by `uniform(1-PCT, 1+PCT)`. `0.4` makes a 1.5s base 0.9–2.1s. Floor 0.05s. |
| `--limit N` | int | 24 | Page size. CLI clamps to DA's per-endpoint cap (24 gallery, 50 feed). **`sync feed` and `sync artist` only** — `sync watched` does not take it. |

```bash
# Incremental walk of /browse/deviantsyouwatch, top-down. Stops at
# `last_feed_deviationid` checkpoint or when time budget hits.
da sync feed [--limit N] [--time-budget N] [--delay-api S] [--delay-image S] [--jitter PCT]

# Full back-catalog walk of one artist via /gallery/all.
da sync artist <username> [--offset N] [--limit N] [--time-budget N] ...
#   On interruption, prints a resume hint:
#     resume: da sync artist <username> --offset <last_offset>

# Walk every watched user's gallery. Two discovery modes:
da sync watched                         # tries /user/friends/{me} → falls back to feed if 403
da sync watched --user <username>       # skip whoami; use this username for friends call
da sync watched --via-feed              # skip friends entirely; discover via feed walk
da sync watched --via-feed --feed-max N # cap feed scan at N deviations (default 2000)
```

`--via-feed` is the right choice when the token only has `browse`
scope. Caveat: it discovers an artist only if they've posted recently
enough to appear in the first `feed_max` deviations.

### Search / browse / daily

```bash
# Live endpoints
da search tag <tag>          [--limit N] [--mature] [--json]
da search topic <name>       [--limit N] [--mature] [--json]   # DA-curated feed
da search topics             [--limit N] [--offset N] [--json] # list valid topics
da search toptopics          [--mature] [--json]               # top topics + 1 example
da search tag-suggest <PFX>  [--json]                          # tag-prefix autocomplete
da search user <query>...                                       # username resolver

da daily [YYYY-MM-DD] [--mature]            # default: today
```

**Deprecated** (DA retired the underlying endpoints — verified 2026-04-28
that `/browse/popular` and `/browse/newest` return HTTP 404 from DA's
API; same token works on every other browse endpoint):

```bash
da search popular   # exits 2 — use `da search topic <name>` or `da daily`
da search newest    # exits 2 — use `da search tag <tag>` or `da search topic`
```

The deprecation stubs print an actionable error pointing at the live
replacements and never make an HTTP call.

**`search user`** uses POST against `/user/whois` with `usernames[]=`
form fields (DA's API requires POST here; GET returns 400).

All live commands print a summary table; `_print_results()` renders one
line per deviation: `{author} {title} {url} [is_mature]`. Pass `--json` for
structured output (deviationid + content URL + description).

### User / deviation / watch list

```bash
da user profile <username>                  # bio, stats, profile_url
da deviation show <deviationid> [--json]    # one-deviation lookup; --json dumps raw API blob
da watch list [--limit N] [--offset N]      # needs `user` scope; gracefully exits with hint otherwise
```

## Atomicity guarantees

- `description.json`, `state.json`, and `config.json` are written via
  tmp-file → `os.chmod` → `os.replace`. A crash mid-write never
  leaves a partial file.
- Image downloads stream to `image.<ext>.part`, then rename. The
  dedup check looks for the final filename, so a half-written `.part`
  doesn't falsely count as fetched.
- Token refresh, when triggered by `access_token()`, mutates
  `state.json` atomically before returning.

## Pacing / jitter

The default 5 s API delay + 1.5 s image delay is the lower bound that
DA's rate limiting tolerates over long runs. Tighten only with care.
`--jitter 0.3` is the recommended setting for any long-running
background sync — randomising each sleep avoids a
perfectly regular request cadence.

The jitter floor is hard-coded at 0.05 s; even with `pct=0.95` and a
tiny base the actual sleep can never go below that. `pct` is clamped
to 0.95 internally — passing 1.0 or higher does not produce zero
sleeps.

## Recipes (agent-facing)

### "Sync everyone the user watches, hands-off, in the background"

```bash
nohup da sync watched --via-feed --feed-max 5000 \
  --time-budget 86400 --jitter 0.4 \
  > ~/Library/Logs/da-cli-watched.log 2>&1 &
```

Notes:

- `--via-feed` works without the `user` scope.
- `--time-budget 86400` lets it run a full day before exiting.
- `log()` uses `flush=True`, so the log file gets real-time output
  even under `nohup`.

### "Just keep the feed up to date every day at 03:00"

```bash
./install_schedule.sh                        # writes launchd plist
./install_schedule.sh uninstall              # remove
```

Under the hood: `~/Library/LaunchAgents/com.fz2000.da-cli.plist`
runs `da sync feed --time-budget 1200 --jitter 0.4` daily at 03:00
by default. Override the cadence with `DA_HOUR` / `DA_MINUTE`, or
switch to fixed-interval with `DA_INTERVAL_SECONDS=21600` (every 6h).
Edit the plist or re-run the script to change cadence.

### "Get the full gallery of an artist you just discovered"

```bash
da sync artist <username>                    # resumes if interrupted
```

If you see `resume: da sync artist <username> --offset N` in the log,
the run hit the time budget. Re-run the printed command to continue
from where it left off.

### "Force-refresh an item you deleted"

Sync is idempotent based on `description.json` + `image.*` existence.
To force a refetch, delete the per-deviation folder (or just the file
you want refreshed) and re-run the relevant sync command.

### "Mature images are still blurred after age-verifying the account"

The CDN serves blurred variants based on the *requesting account's*
verification state, not the API token. After verifying:

1. Settings → Privacy → "Mature Content Level" → **Strict** on
   deviantart.com.
2. Delete the affected `image.*` files.
3. Re-run `da sync artist <user>` or `da sync feed`.

## Failure modes & how to read them

| Symptom | Cause | Fix |
|---|---|---|
| `auth: client_id not set` (exit 2) | No `client_id` in config/env | `da config set client_id <id>` |
| `token exchange returned: {'error': 'invalid_client'}` | `client_secret` mismatch | Re-set the secret; verify exact length (DA secrets are 32 chars). |
| `whoami` warns about 403 | Token lacks `user` scope | Re-run `da auth --scope "browse user"` |
| `watch list` exits with "needs user scope" | Same | Same |
| `gallery/all` returns empty for known artist | DA fallback returned the calling user's profile, not a 404 | Verify spelling via `da search user <name>`. DA usernames are case-sensitive sometimes. |
| Image saved as 0 bytes / wrong type | CDN returned an HTML 401 page rather than the image | Token expired mid-run; refresh and re-run. A 0-byte file is treated as not-synced, so the next run re-fetches it without any manual deletion. |
| Hits HTTP 429 mid-feed-walk | DA rate limit | The walk stops cleanly with `stop_reason="HTTP 429 at offset N"`. Wait, retry; consider `--jitter 0.4`. |

## Security model (short version)

- Secrets in macOS Keychain or 0600 file; never in CLI args (visible
  to `ps`).
- Tokens in `state.json` (0600), auto-refreshed before each call.
- All DA + CDN traffic is HTTPS.
- PKCE: fresh `code_verifier`/`code_challenge` per `da auth`. The
  verifier never leaves the machine.
- No telemetry, no third-party runtime deps. Stdlib only.

Full threat model: [docs/explanation/security.md](docs/explanation/security.md).
Every setting, flag, and path: [docs/reference/configuration.md](docs/reference/configuration.md).

## Internal entry points (for agents reading source)

If an agent needs to extend the CLI rather than just operate it,
these are the load-bearing functions in the package:

| Function | Role |
|---|---|
| `cmd_*` (e.g. `cmd_sync_feed`) | argparse handlers. Each has a single `args: argparse.Namespace` argument. |
| `build_parser()` | full argparse tree. Add new subcommands here + a `cmd_*` handler. |
| `access_token(cfg, state)` | returns a valid bearer; refreshes if expired; writes new state. |
| `http_json` / `http_post_json` / `http_bytes` | the only HTTP entry points. All retry with backoff and respect 429. `http_post_json` accepts an optional `token=` for endpoints that need Bearer auth on POST — `cmd_search_user` uses it for `/user/whois`, passing a list value so `urlencode(..., doseq=True)` produces the repeated `usernames[]=` shape. It used to hand-build that request, which cost it retries, `-v` logging, and visibility to every test that stubs the HTTP layer. |
| `_save_one(d, md_by_id, dest, ...)` | the actual deviation→folder writer. Returns `(status, artist, title, size)`. Consults the index first (O(1) lookup); on miss, downloads and adds to index. |
| `_resolve_folder` | dedup-aware folder picker; handles title collisions. |
| `_keychain_get` / `_keychain_set` | macOS Keychain helpers; safe no-ops on Linux. |
| `_capture_code_via_listener` / `_capture_code_via_paste` | OAuth code capture — TLS-loopback or paste-back. |
| `_ensure_self_signed_cert` | generates / reuses the loopback TLS cert via `openssl req -x509`. |
| `index_has` / `index_add` / `index_filter_known` / `index_count` / `index_rebuild_from_disk` | SQLite synced-deviation index API. |

Tests live under `tests/`:

- `test_util.py`, `test_config.py`, `test_http_and_auth.py`,
  `test_sync.py`, `test_cli.py`, `test_commands.py`,
  `test_auth_flow.py`, `test_index.py`, `test_faults.py`,
  `test_shim.py` — focused unit tests
- `test_integration.py` — end-to-end test suite with HTTP mocked at
  `urllib.request.urlopen`.

To add a feature, add the handler in the right module, wire it into
`build_parser()`, write a unit test in the relevant `test_*.py`, plus
an integration test in `test_integration.py` covering the full
parser→handler→HTTP path. Then confirm:

```bash
ruff check . && ruff format --check . && mypy dacli && pytest -q
```

Coverage gate: ≥92 % on the `dacli` package (currently 94 %).
CI matrix: Python 3.10–3.14, on Linux. `.github/workflows/ci.yml`
defines 16 jobs: `lint`, `test`, `integration`, `smoke`, `artifact`, `secret-scan`, `verified-secret-scan`, `codespell`, `pip-audit`, `markdownlint`, `link-check`, `link-check-external`, `cassette-replay`, `network-anonymous`, `discoverability` — plus `ci-gate`, which aggregates
the rest so branch protection has one check to require instead of a
matrix's worth of names. Two more workflows, `codeql.yml` and
`dependency-review.yml`, activate when the repository is public.

## Versioning & changelog

`__version__` lives in `dacli/constants.py`. Bump on any user-visible change
and add a section to `CHANGELOG.md` (Keep-a-Changelog format).
