# Security model

## Threat model

da-cli runs on the user's local machine and stores credentials locally.
It does not protect against:

- Other local users on the same machine reading your home directory (UNIX permissions only — `0600` files limit to owner, but any process running as you can read them)
- A compromised machine. Anyone with code execution as your user can extract your tokens from `~/.local/state/da-cli/state.json` and from the Keychain via your authenticated UI session
- Compromise of the DA OAuth provider itself

It does protect against:

- **Accidentally committing secrets to git.** The `.gitignore` excludes
  `config.json`, `state.json`, `sync.log`, `*.log`, and `*.tmp`. The
  example file (`config.example.json`) is a stub with no real values.
- **Secrets leaking via process listings.** Secrets are never passed as CLI arguments (you'd see them in `ps`). They flow only through Keychain APIs, environment variables (visible only in your own shell), and `0600`-permissioned files.
- **Stale tokens persisting in plaintext.** The state file is rewritten atomically with `0600` permissions on every refresh.

## Where each secret lives

| Item               | Default home                                              | Override via                                  |
| ------------------ | --------------------------------------------------------- | --------------------------------------------- |
| `client_id`        | `~/.config/da-cli/config.json`                            | `DA_CLIENT_ID` env, `da config set`           |
| `client_secret`    | macOS Keychain (service `da-cli`, account `client_secret`) | `DA_CLIENT_SECRET` env, `da config set`       |
| `access_token`     | `~/.local/state/da-cli/state.json` (0600)                 | (managed by the CLI; do not edit)             |
| `refresh_token`    | `~/.local/state/da-cli/state.json` (0600)                 | (managed by the CLI; do not edit)             |

On non-macOS platforms, `client_secret` falls back to the config file with a warning. Linux Secret Service / Windows Credential Manager support is on the roadmap.

## OAuth flow (PKCE)

`da auth` runs the OAuth 2.1 authorization-code-with-PKCE flow:

1. Generate a fresh 40-byte random `code_verifier`, never written to disk.
2. Compute `code_challenge = base64url(sha256(code_verifier))`.
3. Open the DA authorize URL with the challenge.
4. A short-lived loopback HTTPS server on `127.0.0.1:<port>` (terminated
   by a self-signed cert generated on first use) captures the redirect
   and extracts the `code`.
5. POST to `/oauth2/token` with the `code` + the original `code_verifier`. Only the verifier proves we're the same client that started the flow.
6. Tokens are written atomically to `state.json` with `0600`.

For confidential clients (where DA gives you a `client_secret`), the secret is also sent — but PKCE remains in force, so a stolen code without the verifier is useless.

The redirect URI defaults to `https://localhost:8765/`. **You must add this URI to your DA application's OAuth2 Redirect URI Whitelist** in the developer portal, or the authorize step will reject with `redirect_uri mismatch`. The loopback listener terminates TLS using a self-signed certificate generated on first use (stored at `~/.local/state/da-cli/loopback-{cert,key}.pem`, mode 0600) — DA's developer dashboard rejects plain-HTTP whitelist entries, so HTTPS is required even on loopback.

## What the CLI does NOT log

- Full access or refresh tokens
- Plaintext `client_secret`
- The raw `code` from the OAuth redirect
- The signed `token` on an image CDN URL

`da config show` masks `client_secret` to `XXXX...XXXX` form. Network
errors are logged with a 200-character truncation of the response body.

Under `-v` the HTTP layer prints one `GET <url>` line per request, and
every URL passes through a redactor that replaces the value of
`access_token`, `client_secret`, `code` and `token` with `<redacted>`.
DeviantArt's own API sends its credential in the `Authorization` header,
so for API calls that is belt-and-braces — but wixmp signs every image
URL with a bare `?token=<JWT>`, so the URLs the image path fetches carry
a live credential as a matter of course. `-v` output is what people paste
into bug reports, which is the whole reason the redactor exists.

## Why we don't store the password

DA's OAuth token endpoint rejects `grant_type=password` with
`unsupported_grant_type` (verified 2026-07-21 via
`POST /oauth2/token` and via the RFC 8414 metadata at
`/.well-known/oauth-authorization-server`, which omits `password` from
`grant_types_supported`). The Resource Owner Password Credentials grant
is also removed from OAuth 2.1. So storing the password for "daily
auto-token" would be both technically impossible against the OAuth API
and security-regressive even if it worked.

The threat-model trade-off is asymmetric: a stolen password means full
account takeover and forces a password rotation, while a stolen
refresh_token means scoped misuse that one `POST /oauth2/revoke`
recovers from. See [ADR 0006](adr/0006-refresh-token-ttl-not-ropc.md)
for the full decision record.

The 3-month refresh-token re-authorization is DA-imposed; da-cli surfaces
the remaining TTL in `da diagnose` (WARN at ≤14 days, FAIL at ≤3 days) so
the operator isn't surprised by a dead token at 03:00.

## Rotating credentials

Whenever you suspect a compromise:

```bash
# 1. Regenerate the secret on deviantart.com → Developers → your app → Reset
# 2. Update the local copy:
da config set client_secret <NEW>
# 3. Force a token refresh (the cached access_token may still be valid until expiry,
#    but you want a clean restart):
rm -f ~/.local/state/da-cli/state.json
da auth
```

The old refresh token is not invalidated by DA when the secret rotates — DA only invalidates it if you explicitly revoke the app on <https://www.deviantart.com/settings/applications>. Revoking is the one true kill switch.

## Secret-scanning defense layers

Three independent layers keep credentials out of the repo. Each one is
assumed fallible; a secret has to slip past all three to land in
history.

1. **pre-commit** (`gitleaks` hook, first in `.pre-commit-config.yaml`)
   scans the staged diff before a commit exists. Best-case UX, but
   advisory only: contributors may skip `pre-commit install` or commit
   with `--no-verify`.
2. **CI** (`secret-scan` + `verified-secret-scan` jobs) is the
   authoritative gate: gitleaks scans the **full git history** on every
   push and PR, and TruffleHog re-checks candidate credentials against
   the issuing provider to flag ones that are currently live.
3. **GitHub secret scanning + push protection** — once the repo is
   hosted on GitHub, enable both under *Settings → Advanced Security*
   (free for public repos). This layer only knows provider-issued
   token formats (AWS, GitHub, OpenAI, …).

A DeviantArt `client_secret` is a bare 32-char hex string with no
provider prefix, so layer 3 can never recognise it — and gitleaks'
default `generic-api-key` rule only matches assignment-shaped text
(`secret=...`), not CLI examples like `da config set client_secret
<hex>`. The custom `da-client-secret-near-keyword` rule in
`.gitleaks.toml` closes exactly that gap.

Allowlisting policy: benign fixtures are allowlisted **value by value**
in `.gitleaks.toml`, never by directory — docs, examples, and tests are
precisely where a real secret would be pasted. If a secret does reach a
public remote despite all this, **rotate it immediately**; deleting or
rewriting history does not un-leak it.

## Reporting a vulnerability

Open a private security advisory on the repo (Security → Advisories). Don't file a public issue.
