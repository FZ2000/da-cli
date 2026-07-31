# Authentication

Every `da` command that reaches DeviantArt carries an OAuth access token,
and these five commands are how that token comes into existence, gets
checked on, and gets thrown away. You will run `da auth` once when you
set the tool up, and roughly once a quarter afterwards, because
DeviantArt caps a refresh-token chain at 90 days and nothing the CLI can
do extends that. The other four exist so you can answer "is my token
still good?" from the terminal or from a monitoring job, without
guessing.

If you have not registered a DeviantArt OAuth application yet, do that
first — [Getting started](../getting-started.md) walks through it. You
need at minimum a `client_id` and a redirect URI whitelisted on the app.

## At a glance

| Command | Purpose |
| --- | --- |
| `da auth` | Run the OAuth 2.1 PKCE login flow and store the resulting tokens |
| `da auth logout` | Delete the local token file (the DeviantArt-side grant survives) |
| `da auth status` | Print the refresh-token chain's remaining lifetime as JSON |
| `da whoami` | Verify the token is live and show which account it belongs to |
| `da refresh` | Force an access-token refresh now, instead of waiting for expiry |

## `da auth`

```text
usage: da auth [-h] [--redirect-uri REDIRECT_URI] [--paste] [--scope SCOPE]
               {logout,status} ...
```

Runs the interactive login: it builds a DeviantArt authorisation URL,
opens your browser, captures the authorisation code that comes back,
exchanges it for tokens at DeviantArt's token endpoint, and writes those
tokens to `state.json`. Run it on first setup, after `da auth logout`,
after `da diagnose` or `da auth status` warns that the refresh-token
chain is running out, and whenever you want to change the scope the
token carries.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `--redirect-uri REDIRECT_URI` | string | the configured `redirect_uri` (`DA_REDIRECT_URI` beats `config.json`), and `https://localhost:8765/` when neither is set | The URI DeviantArt redirects to after you click Authorize. A loopback host (`localhost` or `127.0.0.1`) runs a local listener; anything else — including `::1`, deliberately — switches to paste mode automatically. |
| `--paste` | flag | off | Force paste mode even for a loopback URI. |
| `--scope SCOPE` | string | `browse` | Space-separated OAuth scopes to request. `browse` covers galleries, feeds and search; add `user` for identity and watch-list enumeration. |
| `-h`, `--help` | flag | — | Print the usage above and exit. |

The two subcommands, `logout` and `status`, are documented in their own
sections below. Bare `da auth` with no subcommand is the login flow.

### The PKCE flow

`da` uses OAuth 2.1 Authorization Code with PKCE, `S256` challenge
method — see [ADR 0002](../explanation/adr/0002-pkce-mandatory.md) for
why there is no other option. Each run generates a fresh 40-byte random
verifier, sends only `base64url(sha256(verifier))` to DeviantArt in the
authorisation URL, and keeps the verifier in process memory until the
token exchange. Nothing about the verifier is written to disk, so an
interrupted `da auth` leaves nothing behind to clean up; just run it
again.

`client_id` is required. If it is not set in `config.json` or the
environment, the command stops before doing anything. It is not a secret,
so it is never read from the Keychain — only `client_secret` is:

```console
$ da auth
[error] set DA_CLIENT_ID env var or `da config set client_id <ID>` first
```

`client_secret` is optional: if one is configured it is added to both the
authorisation-code exchange and every later refresh, and if not, the
requests go out as a public client. See
[configuration](../reference/configuration.md) for where each value is
read from.

### The loopback listener and why it speaks HTTPS

When the redirect URI's host is `localhost` or `127.0.0.1`, and
you have not passed `--paste`, `da` binds a one-shot HTTP server on
`127.0.0.1` at the URI's port (8765 if the URI names none) and waits for
the browser to deliver the authorisation code to it. The listener is
fully bound before the browser is opened, so a fast redirect cannot
arrive at a socket that is not listening yet.

The default redirect URI is `https://localhost:8765/`, not `http://`,
because DeviantArt's developer dashboard refuses plain-HTTP entries in
an application's OAuth Redirect URI Whitelist — a URI you cannot
whitelist is a URI the flow cannot use. That forces the local listener
to terminate TLS, so on the first HTTPS run `da` shells out to `openssl`
and generates a self-signed certificate:

- 3072-bit RSA, SHA-256, valid 825 days, subject `/CN=localhost`, with
  `subjectAltName=DNS:localhost,IP:127.0.0.1`
- written to `$XDG_STATE_HOME/da-cli/loopback-cert.pem` and
  `loopback-key.pem`, both `chmod 0600`
- reused on every later run, and regenerated — with a
  `[warn] loopback cert/key exist but cannot be loaded … — regenerating`
  line on stderr — if the pair exists
  but can no longer be loaded (a half-written file from a killed run, a
  full disk, a partial restore from backup)

`openssl` must be on `PATH`. It ships with macOS; on Linux install it
through your package manager. Without it `da auth` exits 2 and tells you
so.

Because the certificate is self-signed, your browser will interrupt the
redirect with a security warning — "Your connection is not private" on
Chrome, "Warning: Potential Security Risk Ahead" on Firefox. You have to
click through it (Advanced, then "Proceed to localhost (unsafe)") for the
code to reach the listener. This is expected. The certificate never
leaves your machine, is not added to any trust store, and only ever
terminates one connection from your own browser to your own loopback
interface. `da` prints a reminder before it opens the browser:

```console
$ da auth
opening https://www.deviantart.com/oauth2/authorize?response_type=code&client_id=12345&redirect_uri=https%3A%2F%2Flocalhost%3A8765%2F&scope=browse&code_challenge=ZqQsji_gpHi9ce1NATnKGAqwQFi5I7TTyjspw_utsA4&code_challenge_method=S256
(if it doesn't open, copy/paste the URL into your browser)
note: the local listener uses a SELF-SIGNED cert, so your browser will warn you about the connection. Click through (Advanced → 'Proceed to localhost (unsafe)' on Chrome, similar on others). It's safe — the cert is only on your machine, only used to capture the localhost callback.
waiting for browser redirect (5 minute timeout)...
```

(That transcript is a real run with a placeholder client ID, stopped at
the wait. In a real login the browser then shows DeviantArt's Authorize
page; approving it returns you to a localhost page reading "Authorized.
You can close this tab and return to the CLI", and the command finishes
with a line naming the scope you were granted and the path the tokens
were written to.)

Details worth knowing about the wait:

- The listener gives up after 300 seconds — the "5 minute timeout" in the
  line above. A timeout is treated exactly like a refused login: no code,
  exit 2.
- Only a request that actually carries a `code` query parameter counts.
  Browsers fetch `/favicon.ico` immediately after the redirect, and
  speculative preconnects are common; those are answered and ignored.
- Each connection is served on its own thread with a 20-second cap, so a
  client that opens a socket and says nothing cannot block the real
  callback.
- If the port is already in use the command fails immediately rather than
  hanging, and names the three ways out: stop the other process, point
  `redirect_uri` at a free port, or re-run with `--paste`.
- Ctrl-C during the wait exits 130, the usual interrupted-command code.

If you would rather not deal with TLS at all, setting `redirect_uri` to
`http://localhost:8765/` does work on the CLI side — the listener simply
runs without a certificate and your browser shows no warning — but you
still have to whitelist that exact string on the DeviantArt app, which
is the part the dashboard rejects. `da diagnose` reports an HTTP
redirect URI as a warning for that reason.

### Paste mode

Paste mode is the fallback for any situation where the browser cannot
reach a socket on the machine running `da`: an SSH session, a container,
a headless box, or a redirect URI that points at a web page you own
rather than at loopback. It is selected automatically whenever the
redirect URI's host is not a loopback host, and can be forced for a
loopback URI with `--paste`.

Instead of listening, `da` prints the authorisation URL for you to open
yourself, waits on stdin, and pulls the `code` parameter out of whatever
URL you paste back:

```console
$ da auth --paste
open this URL in your browser and authorize:
  https://www.deviantart.com/oauth2/authorize?response_type=code&client_id=12345&redirect_uri=https%3A%2F%2Flocalhost%3A8765%2F&scope=browse&code_challenge=kSciCubun62uNFwmmbZYGoBLgB8W85IykYAIlXlRD6U&code_challenge_method=S256
DA will redirect you to https://localhost:8765/ (a page you own — likely 404).
after authorizing, paste the URL you were redirected to (Ctrl+C to abort):
> [error] did not receive an auth code
[error] common cause: redirect_uri='https://localhost:8765/' is not in your DA app's whitelist. DA matches the whitelist BYTE-EXACTLY — trailing slash, port, case, and scheme all matter. If your browser landed on the DA home page instead of the redirect URI, that's the symptom of a whitelist miss. Fix at https://www.deviantart.com/developers/ → Apps & Keys → OAuth2 Redirect URI Whitelist; add EXACTLY: https://localhost:8765/
```

The page you land on does not have to work. A 404 is fine, because the
only thing that matters is the URL in the address bar. Paste the whole
thing, including the query string; anything without a `code` parameter
produces the error above.

That error is also what you get from the listener path when the redirect
never arrives, and its diagnosis is right far more often than not: the
whitelist is matched byte for byte, so `https://localhost:8765` and
`https://localhost:8765/` are different URIs and only one of them will
work. The symptom of a mismatch is landing on the DeviantArt home page
instead of your redirect URI.

### Scopes

`--scope` takes a space-separated list and defaults to `browse`.

- **`browse`** is enough for everything that reads public content:
  `da sync feed`, `da sync artist`, the whole of `da search`,
  `da daily`, `da deviation show`, and the `/placebo` check that
  `da whoami` leads with. It is also enough for
  `da sync watched --via-feed`, which discovers artists from your feed
  rather than from your friends list.
- **`user`** adds the two calls that read your account:
  `/user/whoami`, which is the identity line `da whoami` prints, and
  `/user/friends/{username}`, which is the authoritative watch-list
  enumeration `da sync watched` prefers. Without it, `da whoami` still
  confirms the token but warns that it cannot name you, and
  `da sync watched` falls back to feed-based discovery with a warning.

To get both:

```bash
da auth --scope "user browse"
```

Two things about scope that surprise people. First, `--scope` is not
sticky: it defaults to `browse` on every invocation, so a later plain
`da auth` silently narrows a token you had broadened. If you use
`user` scope, always pass it. Second, the scope recorded in `state.json`
is the scope DeviantArt says it granted, which is not necessarily the
one you asked for, and a token refresh never updates that record — it is
written by `da auth` and only by `da auth`. That recorded value is what
`da whoami` and `da diagnose` display.

### What it writes

On success the command rewrites `$XDG_STATE_HOME/da-cli/state.json`
atomically with mode `0600`, setting `access_token`, `refresh_token`,
`expires_at` (now plus DeviantArt's `expires_in`, or 3600 seconds if
absent), `scope`, and `refresh_token_issued_at`. Anything already in
that file — sync checkpoints in particular — is preserved. Nothing is
written to `config.json`, and `state.json` is not touched on any failure
path, so a failed `da auth` cannot cost you the tokens you already had.
The loopback certificate and key described above are the exception: on an
HTTPS redirect URI they are generated before the listener starts waiting,
so an attempt that then times out or is interrupted still leaves both on
disk. They are reused by the next run.

`refresh_token` and `refresh_token_issued_at` are only touched if
DeviantArt actually returned a refresh token. If it returns none, your
previous refresh token stays in place and its 90-day clock keeps running
from where it was.

### The 90-day ceiling

DeviantArt hard-caps a refresh-token chain at 90 days from issue and
offers no way to extend it; there is no password grant to fall back on
either, which is the subject of
[ADR 0006](../explanation/adr/0006-refresh-token-ttl-not-ropc.md). `da`
records the issue time locally as `refresh_token_issued_at` — DeviantArt
does not return one — and resets it whenever a refresh hands back a
rotated refresh token, so the countdown follows the live chain rather
than your first-ever login.

When the chain dies, everything that needs a token starts failing with
`no refresh_token stored` or a rejected refresh, and the fix is always
the same: run `da auth` again and click Authorize. The point of
`da auth status` and of `da diagnose`'s auth section is to tell you that
two weeks early instead of at 03:00 in a cron log.

### Exit codes

Standard: 0 on success, 2 when the flow could not complete — no
`client_id`, an unparsable redirect URI, a port already in use, no
`openssl`, no code received within the timeout, or a token exchange
DeviantArt rejected. One deviation: pressing Ctrl-C at the paste prompt
is treated as "no code received" and exits 2, whereas Ctrl-C while the
loopback listener is waiting exits 130.

## `da auth logout`

```text
usage: da auth logout [-h]
```

Deletes the local token file. Use it when you are handing the machine
over, switching accounts, or want to force a clean re-authorisation.

This command defines no flags of its own beyond `-h`, `--help`; the
global flags described in [the CLI reference](../reference/cli.md) apply
as usual.

### Behaviour

It removes `$XDG_STATE_HOME/da-cli/state.json` and nothing else. Two
consequences follow, and both matter.

The first is local. `state.json` is not only a token store — it also
holds the sync checkpoints: `last_feed_deviationid` and
`last_feed_sync_at`, which let `da sync feed` stop as soon as it reaches
something it has already seen, and `last_sync`, the run summary
`da diagnose` reports on. Logging out deletes those too, so the next
`da sync feed` after a re-login walks the feed with no "caught up"
marker. Your files are safe and nothing is re-downloaded, because the
synced-deviation index lives in a separate database (`index.db`, left
untouched) — but the first sync afterwards does more API work than
usual. Left alone are `config.json`, the `client_secret` in the Keychain,
`index.db`, and the loopback certificate and key.

The second is remote, and is the more important one: **logout does not
revoke anything at DeviantArt.** Your application stays authorised on
your account, and the refresh token that was in the deleted file remains
valid server-side until its 90 days run out. Deleting the file makes it
unusable by you, not unusable by anyone who already copied it. To
actually invalidate it, revoke the application at
<https://www.deviantart.com/settings/applications>. The command says so
on every successful run:

```console
$ da auth logout
removed /Users/you/.local/state/da-cli/state.json
[warn]  note: DA-side authorisation is unchanged. Revoke the app at https://www.deviantart.com/settings/applications to invalidate the refresh token server-side.
```

Logging out when there is nothing to log out of is not an error:

```console
$ da auth logout
not logged in (no state file)
```

Both branches exit 0. That line is info-level, so `da --quiet auth
logout` prints nothing at all in the second case.

One trap for multi-account setups: the global `--config` flag repoints
the config file only. `state.json` always comes from `XDG_STATE_HOME`,
so `da --config ./other.json auth logout` deletes the tokens of whatever
account owns the default state directory. To keep two accounts genuinely
separate, give each its own `XDG_STATE_HOME` as well — see
[environment variables](../reference/environment-variables.md).

## `da auth status`

```text
usage: da auth status [-h]
```

Prints one line of JSON describing how much life the refresh-token chain
has left, and encodes the same answer in its exit code. It is built for
cron, launchd and monitoring wrappers: one small object on stdout, no
prose. Reach for it when you want a machine to notice the 90-day ceiling
coming; reach for `da diagnose` when you want a person to read the whole
health picture.

It **does** talk to DeviantArt — see below. Poll it on a schedule, not in
a tight loop.

This command defines no flags of its own beyond `-h`, `--help`.

### Behaviour

It reads `refresh_token_issued_at` from `state.json` and subtracts the
elapsed time from 90 days (`REFRESH_TOKEN_TTL_DAYS`) — and then it
confirms the answer with DeviantArt, by resolving an access token and
calling `/placebo`. So a green result means both "the chain has not aged
out" *and* "DeviantArt still accepts it", which is the answer a
monitoring wrapper actually wants; the alternative was reporting healthy
for a grant that had been revoked server-side.

Two consequences worth planning around:

- **It takes the token lock and can rotate your refresh token.** An
  access token lives an hour and is refreshed 60 s early, so anything
  polling on roughly an hourly cadence will refresh — and therefore
  rotate — on almost every call. That is safe (rotation is serialised),
  but it is not a read-only probe.
- **It needs the network.** No connectivity is reported as
  `unreachable`, distinct from `revoked`, so a dropped wifi link does not
  send you to re-authenticate.

The output object always carries these three keys, plus `error` on the
two failure states:

| Field | Type | Meaning |
| --- | --- | --- |
| `state` | string | `ok`, `warn`, `crit`, `unknown`, `revoked` or `unreachable` |
| `days_remaining` | float or null | Days left in the chain, rounded to one decimal; `null` when `state` is `unknown`, `0.0` when `revoked` |
| `issued_at_iso` | string or null | When the chain was issued, ISO 8601 in UTC; `null` when `state` is `unknown` |
| `error` | string | Only on `revoked` / `unreachable`: what DeviantArt or the network said |

The thresholds are `REFRESH_TOKEN_WARN_DAYS` (14) and
`REFRESH_TOKEN_CRIT_DAYS` (3), and the comparisons are strict: more than
14 days left is `ok`, more than 3 and at most 14 is `warn`, and 3 or
fewer — including a negative number for an already-expired chain — is
`crit`. `unknown` means `state.json` has no usable
`refresh_token_issued_at`, which happens when you have never logged in,
after `da auth logout`, or when DeviantArt issued the current chain
without returning a refresh token.

```console
$ da auth status
{"state": "ok", "days_remaining": 85.0, "issued_at_iso": "2026-07-24T00:47:27Z"}

$ da auth status
{"state": "warn", "days_remaining": 10.0, "issued_at_iso": "2026-05-10T00:47:27Z"}

$ da auth status
{"state": "crit", "days_remaining": 1.0, "issued_at_iso": "2026-05-01T00:47:27Z"}

$ da auth status
{"state": "unknown", "days_remaining": null, "issued_at_iso": null}
```

A wrapper can key on either the exit code or the field:

```bash
da auth status > /tmp/da-auth.json || notify "da-cli: $(cat /tmp/da-auth.json)"
```

### Exit codes

This command uses all three, deliberately, so the shell can act without
parsing JSON. It never returns anything else.

| Code | `state` |
| --- | --- |
| `0` | `ok` |
| `1` | `warn` |
| `2` | `crit` or `unknown` |

That is the same convention `da diagnose` uses; see
[exit codes](../reference/exit-codes.md).

## `da whoami`

```text
usage: da whoami [-h]
```

Answers "does my token actually work, and whose is it?". It calls
DeviantArt's `/placebo` endpoint, which exists solely to be called with
a token and say yes, then reports the scope and remaining access-token
life from local state, then tries `/user/whoami` for your username and
user ID. Run it after a fresh `da auth` to confirm the login took, and
whenever you suspect the credentials rather than the network.

This command defines no flags of its own beyond `-h`, `--help`.

### Behaviour

It is not read-only. Getting a token goes through the normal path, so if
the cached access token is missing or within 60 seconds of expiry, the
command refreshes it against DeviantArt and rewrites `state.json` before
doing anything else. With no refresh token stored it stops early:

```console
$ da whoami
[error] no refresh_token stored — run `da auth` first
```

On success it prints up to four lines: `token: valid (placebo OK)`, then
`scope:` and `access_token expires in: <n>s` if state has them, then
`@username  userid=<id>`. The scope shown is the one recorded at last
login, not one re-read from DeviantArt.

The last line is the part that needs `user` scope. With a `browse`-only
token, `/user/whoami` returns 403 and the command degrades rather than
failing — the token has still been proved live by `/placebo`, so it
prints the first three lines, warns, and exits 0:

```text
[warn]  /user/whoami needs `user` scope — current token only has the listed scope
[warn]  (re-run `da auth --scope "user browse"` to broaden)
```

`da whoami` goes through `authed_http_json`, the same retry-on-401
wrapper the sync commands use, so an access token that is locally fresh
but has been revoked server-side is recovered automatically: the wrapper
forces a refresh and retries once. You see the refresh happen, not an
error. If the refresh itself fails — a dead refresh token, or no network
— the command reports it in one line and exits 2.

### Exit codes

0 when the token is valid, including the `browse`-only degraded case; 2
when there is no refresh token, when `/placebo` does not report success,
or when an HTTP or network error prevents asking. Every failure path here
ends in 2 — there is no case that exits 1.

## `da refresh`

```text
usage: da refresh [-h]
```

Exchanges the stored refresh token for a new access token immediately,
instead of waiting for the current one to expire. Day to day you do not
need it — every command that talks to DeviantArt refreshes on its own
when the cached token is within a minute of expiry — but it is useful
for warming the token before a long unattended run, and for testing
whether the refresh chain is still alive without touching anything else.

This command defines no flags of its own beyond `-h`, `--help`.

### Behaviour

It forces the exchange rather than clearing the cached token first, so a
failed refresh leaves your existing, still-valid access token intact in
`state.json`. It needs both a stored `refresh_token` and a configured
`client_id`, and it sends `client_secret` too if you have one.

On success it rewrites `state.json` with the new `access_token` and
`expires_at`, prints `ok — access token refreshed.`, and exits 0. If
DeviantArt rotates the refresh token as part of the response — it
sometimes does — the new one replaces the old and
`refresh_token_issued_at` is reset to now, restarting the 90-day
countdown. That is the only way the countdown ever moves without a full
`da auth`, and you cannot rely on it: an ordinary refresh usually
returns no new refresh token and leaves the clock exactly where it was.
`scope` is never updated by a refresh.

The `refreshing access token via refresh_token` line is warn-level and
goes to stderr, so it survives `--quiet`. A rejected refresh reports
DeviantArt's own error body, truncated:

```console
$ da refresh
[warn]  refreshing access token via refresh_token
[error] DeviantArt rejected the refresh token (HTTP 401): {"error":"invalid_client","error_description":"Client authentication failed.","status":"error"}. Run `da auth` to sign in again.
```

`invalid_client` there means the `client_id` or `client_secret` is wrong;
`invalid_grant` means the refresh token itself is dead and you need
`da auth`. With nothing stored at all you get the same early error as
`da whoami`:

```console
$ da refresh
[error] no refresh_token stored — run `da auth` first
```

## Where the credentials live

Tokens are written to `$XDG_STATE_HOME/da-cli/state.json` with mode
`0600`, via a temporary file and a rename so a crash cannot leave a
half-written file. The `client_secret` lives in the macOS Keychain when
one is available, and in `config.json` (also `0600`) otherwise. The full
picture, including what to delete when you uninstall, is in
[files on disk](../reference/files-on-disk.md), and the reasoning behind
the storage choices is in the
[security model](../explanation/security.md).

## Related

- [Configuration](../reference/configuration.md) — `client_id`,
  `client_secret`, `redirect_uri` and where each is read from
- [Troubleshooting](../guides/troubleshooting.md) — what to do about a
  specific error message
- [Scheduling](../guides/scheduling.md) — keeping an unattended job
  alive across the 90-day boundary
- [Index and health](maintenance.md) — `da diagnose`, which exercises
  the credentials rather than just checking their age
- [Syncing](sync.md) — the commands that spend the token
