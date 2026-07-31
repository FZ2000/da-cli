# Security Policy

## Supported versions

Security fixes land on `main` and go out in the next release. There is no
backport branch: this is a single-maintainer project, and promising
backports it cannot deliver would be worse than saying so.

| Version | Supported |
| --- | --- |
| `main` | ✅ fixes land here first |
| latest release | ✅ |
| any earlier release | ❌ upgrade |

Practically: if you are on the latest release or on `main`, report it and
it will be fixed. If you are on an older one, the first step is to
upgrade — the fix will not be backported.

## Reporting a vulnerability

**Do not file a public issue for security problems.** Instead, open a private
security advisory on the repo: go to the **Security** tab and choose
"Report a vulnerability". (Reporters do not have a Settings tab — that
path only exists for maintainers.)

Please include:

- A description of the issue and its potential impact
- Steps to reproduce (proof-of-concept, log excerpt, or stack trace)
- Affected version (commit SHA or tag)

You will receive an acknowledgement within 7 days. Please do not disclose
the issue publicly until a fix has been released.

## Threat model

da-cli runs on the user's local machine and stores credentials locally
(Keychain or 0600 files). It protects against:

- **Accidentally committing secrets to git.** The `.gitignore` excludes
  `config.json`, `state.json`, `sync.log`, `*.log`, and `*.tmp` —
  secrets-bearing files cannot be committed by accident.
- **Secrets leaking via process listings.** Secrets are kept out of argv
  wherever the CLI controls the call — they flow through Keychain APIs,
  environment variables (visible only in your own shell), and
  `0600`-permissioned files. **One documented exception:** storing the
  client_secret in the macOS Keychain shells out to
  `security add-generic-password -w <value>`, so for the lifetime of that
  one short-lived process the value is visible in `ps`, and if you typed it
  at a prompt it is in your shell history. Prefer `DA_CLIENT_SECRET` in the
  environment, and note the secret is optional — da-cli authenticates as a
  public client with PKCE when none is set.
- **Stale tokens persisting in plaintext.** The state file is rewritten
  atomically with `0600` permissions on every refresh.

It does **not** protect against:

- Other local users on the same machine reading your home directory. UNIX
  permissions limit access to the owner (`0600` files), but any process
  running as your user can read them.
- A compromised machine. Anyone with code execution as your user can
  extract your tokens from `~/.local/state/da-cli/state.json` and from
  the Keychain via your authenticated UI session.
- Compromise of the DA OAuth provider itself.

For a deeper walkthrough of where each secret lives, the PKCE flow, and
credential-rotation procedures, see [docs/explanation/security.md](docs/explanation/security.md).
