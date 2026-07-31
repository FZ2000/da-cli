# da-cli

[![CI](https://github.com/FZ2000/da-cli/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/FZ2000/da-cli/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://docs.astral.sh/ruff/)
[![Type checked: mypy](https://img.shields.io/badge/types-mypy-blue.svg)](https://mypy-lang.org/)
[![Coverage: 92%+](https://img.shields.io/badge/coverage-92%25%2B-brightgreen.svg)](CONTRIBUTING.md#tests)
[![Status: Beta](https://img.shields.io/badge/status-beta-orange.svg)](https://github.com/FZ2000/da-cli/releases)
[![Dependencies: 0 runtime](https://img.shields.io/badge/runtime%20deps-0-success.svg)](pyproject.toml)

Sync your DeviantArt gallery to a local folder from the command line — a backup of the art you watch, kept current. **Zero runtime dependencies**: the whole tool is the Python 3.10+ standard library. A local SQLite index means a re-run costs one API call when nothing new was posted, and `launchd` (macOS) or a systemd timer (Linux) keeps it running unattended. Plus search and browse helpers.

> **New to da-cli?** Follow the **[Setup Guide](docs/getting-started.md)** — it walks you through everything from install to first sync in about 10 minutes, with screenshots.
> **Status: Beta (v0.3.x)** — core sync + search flow is stable. macOS Keychain integration is production-ready; Linux Secret Service support is planned. See [CHANGELOG.md](CHANGELOG.md) for details.

## Documentation

| I want to… | Page |
| --- | --- |
| install it and download my first art | [Getting started](docs/getting-started.md) |
| understand a command properly | [Command guides](docs/commands/README.md) |
| look up a command or a flag | [Command reference](docs/reference/cli.md) |
| fix an error I just got | [Troubleshooting](docs/guides/troubleshooting.md) |
| sync automatically every day | [Scheduling](docs/guides/scheduling.md) |
| change a setting | [Configuration](docs/reference/configuration.md) |
| script it or monitor it | [Scripting](docs/reference/scripting.md) |
| know what it does with my OAuth secret | [Security model](docs/explanation/security.md) |
| contribute | [CONTRIBUTING](CONTRIBUTING.md) · [ARCHITECTURE](ARCHITECTURE.md) |

Full index: [docs/](docs/README.md)

## Quick tour

Each group below has a page documenting every flag, what it writes and
how it behaves on a second run — linked after the block.

```bash
da --version                            # print version
da auth                                 # one-time browser-based login
da auth logout                          # forget local tokens (DA-side authorisation persists; revoke at deviantart.com)
da whoami                               # confirm token + identity
da refresh                              # force-refresh the access token

da sync feed                            # incremental: pull new content from your watch feed
da sync feed --no-mature                # ...excluding mature content (included by default)
da sync artist <username>               # walk one artist's full gallery
da sync watched                         # walk every watched user's gallery
da sync watched --via-feed              # discover artists via watch feed (works with `browse` scope alone)
da sync feed --jitter 0.4               # randomise sleeps by ±40% — avoids burst patterns

da search tag nature                    # browse by tag
da search topic digitalart              # browse a curated DA topic
da search topics                        # list all valid topics
da search user deviantart               # resolve a username
da daily 2026-01-15                     # daily-deviation picks for a date

da user profile <username>              # who is this person
da deviation show <deviationid>         # full metadata for one deviation
da deviation morelikethis <id>          # related deviations via DA's recommender
da watch list                           # show who you watch (needs `user` scope)

da config show                          # inspect config + resolved paths
da config path                          # just the file locations
da config set <key> <value>             # store; secrets go to Keychain on macOS
da config get <key> [--unmask]          # read back (secrets masked unless --unmask)
da config unset <key>                   # remove

da index show                           # synced-index stats (rows, top artists)
da index rebuild                        # walk dest, re-import (idempotent)
da diagnose                             # end-to-end health check
da bench                                # synthetic sync benchmark (no network)
```

> `da search popular` and `da search newest` were retired (DA dropped the underlying endpoints). Use `da search topic <name>`, `da search tag <tag>`, or `da daily` instead — both subcommands now exit 2 with an actionable hint.

Read more, by area:

- **[Authentication](docs/commands/auth.md)** — `da auth`, `auth logout`,
  `auth status`, `whoami`, `refresh`. The PKCE flow, scopes, and the
  90-day refresh-token ceiling.
- **[Syncing art](docs/commands/sync.md)** — `sync feed`, `sync artist`,
  `sync watched`. What each mode walks, the checkpoint, resuming an
  interrupted backfill, pacing, and time budgets.
- **[Searching and browsing](docs/commands/search.md)** — `search tag`,
  `search topic`, `daily`. Read-only; downloads nothing.
- **[Inspecting users and deviations](docs/commands/inspect.md)** —
  `user profile`, `deviation show`, `watch list`. How to find the id or
  username a sync command needs.
- **[Configuration commands](docs/commands/config.md)** — `config show`,
  `set`, `get`, `unset`. Where each setting lives and which source wins.
- **[Index, health and benchmarking](docs/commands/maintenance.md)** —
  `index show`, `index rebuild`, `diagnose`, `bench`. How to tell whether
  a scheduled run is quietly failing.

## Install

da-cli is a stdlib-only Python package with no runtime dependencies. Two install paths:

**Option A — git clone + shim** (zero global state, recommended for developers):

```bash
git clone https://github.com/FZ2000/da-cli.git ~/Documents/da-cli
cd ~/Documents/da-cli
./install.sh                           # copies da + the dacli package to ~/.local/share/da-cli/ and symlinks ~/.local/bin/da → that copy
```

**Option B — pip install** — *not yet available; da-cli is not published to
PyPI. Use Option A.* When it is published, this will work:

```bash
pipx install da-cli                    # or: pip install da-cli
da --version
```

Python 3.10+ required (uses `argparse.BooleanOptionalAction` and `X | None` syntax). No third-party runtime dependencies.

## First-time setup

> **New? Follow the [Setup Guide](docs/getting-started.md) instead** — it has
> screenshots and walks through every step from zero.

Quick version for experienced users:

1. Create a Confidential OAuth app at
   **<https://www.deviantart.com/developers/>** → Register Your
   Application. Set the Redirect URI Whitelist to
   `https://localhost:8765/` (exactly — trailing slash matters).
   Manage your apps at **<https://www.deviantart.com/studio/apps>**.

2. Configure da-cli:

   ```bash
   da config set client_id 12345
   da config set client_secret <YOUR_SECRET>
   da config set destination ~/Pictures/DA
   da auth
   da whoami
   ```

The CLI auto-refreshes the access token before each call; you only
repeat `da auth` if you revoke the app or the refresh token expires
(every 90 days — `da diagnose` warns 14 days before).

## Scheduled sync

```bash
./install_schedule.sh              # macOS: daily at 03:00 via launchd
```

On macOS this installs a `launchd` user agent; the script writes the plist
and loads it. On Linux, use a systemd user timer or cron. Both are covered, with the
Full Disk Access step macOS needs and the `enable-linger` step systemd
needs, in [docs/guides/scheduling.md](docs/guides/scheduling.md).

## Troubleshooting

`da diagnose` checks configuration, credentials, the destination
folder, the index, and the scheduled job, and tells you what is wrong.

For specific errors — a redirect-URI mismatch, an expired refresh
token, a scheduled run that silently does nothing — see
[docs/guides/troubleshooting.md](docs/guides/troubleshooting.md).

## Security model

This is documented separately in [docs/explanation/security.md](docs/explanation/security.md); to report a vulnerability see [SECURITY.md](SECURITY.md). Short version:

- **Secrets** (`client_secret`) live in macOS Keychain (or, on other platforms, in a 0600-permissioned config file). Never in this repo, never in git history, never in command-line arguments visible to `ps`.
- **Tokens** (access + refresh) live in `~/.local/state/da-cli/state.json` (0600). Refreshing happens automatically before every API call.
- **Network**: all DA traffic is HTTPS. Image downloads from wixmp CDN are also HTTPS.
- **PKCE is mandatory**: the CLI generates a fresh PKCE `code_verifier`/`code_challenge` pair for every `da auth` run. The verifier never leaves your machine; only the SHA-256 hash is sent in the auth URL.
- **No telemetry**, no third-party code at runtime. Stdlib only — `urllib`, `argparse`, `json`, `hashlib`, `subprocess` (for the macOS `security` keychain helper).
- **`.gitignore`** excludes `config.json`, `state.json`, `sync.log`, `*.log`, and `*.tmp` — secrets-bearing files cannot be committed by accident.

## Examples

The [`examples/` directory](examples/) has runnable recipes: a
post-sync webhook, a cron health check, a CSV export, and a topic
browser that lists what a curated DA topic holds. Each is a short shell
or Python script you can copy and edit.

## Contributing

Style: stdlib only at runtime. If a feature would need a third-party library, open an issue first. See [CONTRIBUTING.md](CONTRIBUTING.md) for the lint/type/test bar (every change must pass `ruff check`, `ruff format --check`, `mypy dacli`, and `pytest` with ≥92 % coverage). See [ARCHITECTURE.md](ARCHITECTURE.md) for a map of the package.

The Makefile wraps the four-command pipeline:

```bash
make dev-setup    # one-time venv + dev-deps install
make check        # ruff + ruff format --check + mypy + pytest (what CI runs)
```

Pre-commit hooks mirror CI locally — install once with
`pip install pre-commit && pre-commit install`, then every `git commit`
runs ruff / mypy / markdownlint / gitleaks. Config in
[`.pre-commit-config.yaml`](.pre-commit-config.yaml).

## License

MIT — see [LICENSE](LICENSE).
