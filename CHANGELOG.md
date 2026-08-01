# Changelog

All notable changes to da-cli are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version in `dacli/constants.py` is the single source of truth:
`pyproject.toml` reads it dynamically, the release workflow refuses a tag
that disagrees with it, and PyPI therefore always matches the tag.

## 0.1.1 — 2026-08-01

Documentation only; no code changes.

The 0.1.0 project page on PyPI carried the pre-launch README, which told
visitors "**Option B — pip install.** Not published yet. Use Option A for
now." — on the page of the published package. A project description is
frozen into the uploaded artifact and PyPI has no way to edit it, so
correcting it requires a release. That is what this one is for.

### Changed

- README documents the real `pipx install da-sync` path now that 0.1.0 is
  published, replacing the placeholder that said it was unavailable.
- The getting-started guide now offers the PyPI install as well. It had
  only ever documented `git clone` + `./install.sh`, so the tutorial a new
  user is pointed at was the one place that never mentioned the published
  package.
- `install_schedule.sh` ships in the repository but not in the wheel, so
  the scheduling guide and the tutorial's scheduling step now say where to
  get it rather than assuming a checkout. Its "not found on PATH" error
  names both install paths instead of only `./install.sh`.

### Fixed

- The getting-started transcript of `./install.sh` omitted the
  `(N modules)` suffix the script actually prints. `check_doc_references`
  now asserts any documented module count against `dacli/`, so the number
  cannot drift again — `docs/commands/` examples are executed by
  `tests/test_docs_examples.py`, but `getting-started.md` is not, which is
  how this survived.

## 0.1.0 — 2026-07-30

First public release.

### Added

- **`da sync`** — three ways to walk DeviantArt and save what you have not
  got yet. `sync feed` follows your watch feed from the top and stops at
  the checkpoint the previous run left, so a quiet day costs one API call.
  `sync artist` walks one gallery newest-first. `sync watched` discovers
  everyone you watch and runs the artist walk for each under one shared
  time budget.
- **Resumable walks.** Each artist's position is checkpointed in
  `state.json`, so a run cut short by its time budget resumes where it
  stopped rather than starting over.
- **A local SQLite index** of what has been downloaded, so re-runs do not
  re-fetch. Self-healing: a row whose folder has gone is dropped, and
  `da index rebuild` reconstructs the whole index from disk without
  re-downloading anything.
- **OAuth 2.1 with PKCE**, against a loopback HTTPS listener with a
  self-signed certificate generated on first run. The `client_secret` is
  optional — DeviantArt's own guidance for desktop apps is a public client
  with PKCE and no secret. Where one is used on macOS it lives in the
  Keychain, not on disk.
- **Scheduled syncs** — `install_schedule.sh` writes a launchd agent on
  macOS; a systemd user timer is documented for Linux.
- **`da search` / `da user` / `da deviation` / `da daily`** — read-only
  browse helpers for tags, topics, daily deviations, profiles and
  metadata. Thirteen commands accept `--json` for scripting.
- **`da diagnose`** — one command that checks every layer that can quietly
  break an unattended run: config, destination writability and free space,
  token expiry and scope, index drift, and whether the launchd job is
  loaded.
- **`da auth status`** — a small JSON object plus an exit code, for cron
  and monitoring wrappers.
- **Zero runtime dependencies.** The whole tool is the Python 3.10+
  standard library. The CI `artifact` job installs the built wheel into a
  clean virtualenv and imports every submodule, so the claim is tested
  rather than asserted.
- **Typed.** Ships `py.typed`; `mypy` runs in CI.

### Security

- Credentials never land in a world-readable file: config, state, the
  index and both lock files are created `0600`, and the loopback TLS key
  is generated inside a `0700` directory so it is never briefly readable.
- The OAuth flow generates and verifies a `state` parameter
  (RFC 6749 §10.12) and accepts a callback on the expected path only.
- Debug output (`-v`) passes every URL through a redactor covering
  `access_token`, `client_secret`, `code` and `token` — the last because
  the image CDN signs every content URL with a live JWT.
- `da config show` masks secrets; `--unmask` writes to stderr so
  redirecting stdout cannot capture the value.

See [SECURITY.md](SECURITY.md) for the threat model, including what this
explicitly does not protect against.
