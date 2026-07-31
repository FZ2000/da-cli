# Troubleshooting

Find the message you got, or the symptom you are seeing. Each entry
gives the cause, the fix, and how to confirm it worked.

If you are not sure what is wrong, start here:

```bash
da diagnose
```

It checks configuration, credentials, token liveness, the destination
folder, the index, and the scheduled job, then prints a categorised
report. Exit code `0` means healthy, `1` warnings, `2` something is
broken.

## Seeing more output

`da` prints to the terminal and keeps no log file of its own.

```bash
da -v sync feed          # per-request detail
da -q sync feed          # warnings and errors only
```

**Scheduled runs** write to `~/Library/Logs/da-cli.log`:

```bash
tail -f ~/Library/Logs/da-cli.log
```

That file is the first place to look when an unattended sync did
something unexpected.

### When a command fails with one line

Failures print what went wrong and exit `2`:

```console
$ da whoami
[error] HTTP 503 from https://www.deviantart.com/api/v1/oauth2/user/whoami: Service Unavailable
[error] re-run with -v for the full traceback
```

The short form keeps the common case readable and keeps the exit code
usable from a script. When you need more than that, `-v` prints the
traceback the message replaced:

```bash
da -v whoami
```

Nothing is discarded — the traceback is written at debug level, so it is
one flag away rather than gone. Include it when reporting a problem; it
names the exact call that failed. Secrets are stripped from the URLs
`-v` logs, so the output is safe to paste.

## Installation

### `da: command not found`

`~/.local/bin` is not on your `PATH`.

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
da --version
```

Use `~/.bashrc` instead if your shell is bash.

### `openssl not found on PATH — required to generate the loopback TLS cert`

`da auth` needs `openssl` once, to create the certificate for the local
callback listener.

- macOS: preinstalled. If it is missing, `brew install openssl`.
- Debian/Ubuntu: `sudo apt install openssl`
- Fedora: `sudo dnf install openssl`

## Authentication

### `da auth` opens DeviantArt, then bounces to the home page

DeviantArt rejected your `redirect_uri` because it does not match the
OAuth app's whitelist **byte for byte**. Trailing slash, port, case,
scheme, and any query string all matter.

Open the developer portal at
<https://www.deviantart.com/developers/>, click your app, and check
"OAuth2 Redirect URI Whitelist" against what `da` sends — by default
`https://localhost:8765/`. (Not
<https://www.deviantart.com/settings/applications>: that page lists the
apps you have *authorised* and is where you revoke them. It cannot edit
the whitelist.)

| Whitelisted | `da` sends | Result |
| --- | --- | --- |
| `https://localhost:8765` | `https://localhost:8765/` | mismatch — missing trailing slash |
| `https://127.0.0.1:8765/` | `https://localhost:8765/` | mismatch — different host literal |
| `https://Localhost:8765/` | `https://localhost:8765/` | mismatch — case |
| `https://localhost:8765/` followed by a space | `https://localhost:8765/` | mismatch — an invisible trailing space |
| `http://localhost:8765/` | — | DeviantArt refuses to save plain HTTP |

Confirm with `da whoami` once `da auth` completes.

### `da auth` waits five minutes, then `did not receive an auth code`

Same cause as above: the browser was redirected somewhere other than
the local listener, usually back to the DeviantArt home page after a
whitelist rejection. Fix the whitelist.

### Your browser warns "Your connection is not private"

Expected, and safe. The local listener terminates HTTPS with a
self-signed certificate generated on first use and stored at
`~/.local/state/da-cli/loopback-{cert,key}.pem` (mode 0600). It only
handles the callback on your own machine and never sees DeviantArt
traffic. DeviantArt's dashboard refuses plain-HTTP whitelist entries,
which is why the loopback uses HTTPS at all.

Click through — "Advanced → Proceed to localhost (unsafe)" in Chrome,
or the equivalent elsewhere. To avoid the warning entirely, add the
certificate to your system trust store.

### `cannot listen on 127.0.0.1:8765 (Address already in use)`

Something else holds the port. Stop it, or point `da` at a different
one:

```bash
da auth --redirect-uri https://localhost:8799/
```

The new URI must also be whitelisted on your OAuth app. Or skip the
listener entirely with `--paste`, below.

### Running over SSH, or in a container with no browser

Use the paste-back flow:

```bash
da auth --paste --redirect-uri https://your-whitelisted.example/
```

`da` prints the authorization URL; open it in a browser anywhere,
authorise, then copy the URL DeviantArt redirects you to — it contains
`?code=…` — and paste it back. The redirect URI must be whitelisted and
reachable from that browser.

### `no client_id configured` / `client_id not set`

Credentials were never stored. Create an OAuth app as described in
[getting started](../getting-started.md), then:

```bash
da config set client_id 12345
da config set client_secret <YOUR_SECRET>
da config show          # secrets are masked
```

### `token exchange returned: {'error': 'invalid_client'}`

The `client_secret` does not match the `client_id`. Re-copy it from the
developer portal, <https://www.deviantart.com/developers/> — DeviantArt
secrets are 32 characters, so a truncated paste is the usual cause.

### `Refresh token is invalid` / `no refresh_token stored`

DeviantArt refresh tokens last **90 days**, and are also invalidated if
you revoke the app. Re-authorise:

```bash
da auth
```

Check how long you have left before it happens again:

```bash
da auth status
```

```json
{"state": "ok", "days_remaining": 87.3, "issued_at_iso": "2026-01-15T12:00:00Z"}
```

`ok` is more than 14 days, `warn` is 3–14 days, `crit` is under 3 days.
`da diagnose` reports the same thing as a warning at 14 days.

### `/user/friends/<user> returned 403 — token lacks 'user' scope`

Your token was issued with `browse` scope only, which cannot enumerate
who you watch. Either re-authorise with a wider scope:

```bash
da auth --scope "browse user"
```

…or discover artists from the feed instead, which works with `browse`
alone:

```bash
da sync watched --via-feed
```

## Syncing

### `da sync feed` downloads nothing

Usually one of three things:

1. **Everything is already downloaded.** This is the normal steady
   state — the run reports `caught up` and costs one API call.
   `da index show` tells you how many items are on disk.
2. **Your watch feed is empty**, or the token lacks the scope to read
   it. See the 403 entry above.
3. **The time budget is too small.** A budget under about 15 seconds
   leaves no time to do any work. The default is 540 seconds.

### `parent of destination does not exist`

The folder above your destination is missing. `da` creates the
destination itself, but not the whole path:

```bash
mkdir -p ~/Pictures
da config set destination ~/Pictures/DA
```

### Images are blurred, or `da` saves a tiny placeholder

DeviantArt serves blurred previews for mature content unless the
account is age-verified **and** the content filter is set to allow it.

1. Age-verify at <https://www.deviantart.com/settings/>
2. Set "Mature Content Level" to **Strict** (yes — Strict is the
   setting that *reveals* rather than hides; the label is DeviantArt's)
3. Delete the already-downloaded file and re-run the sync — `da` skips
   items already on disk, so it will not re-fetch on its own.

### A run stopped with `http 429`

DeviantArt rate-limited you. `da` stops rather than hammering. Wait a
few minutes and re-run; the sync resumes where it left off. To reduce
the chance of it recurring, slow down:

```bash
da sync feed --delay-api 8 --delay-image 2 --jitter 0.4
```

### Sync says another run holds the lock

Sync commands take an exclusive lock, so a manual run and the scheduled
run cannot corrupt each other's index. The second one exits
immediately. Either wait, or check what is running:

```bash
launchctl list com.fz2000.da-cli
```

## Index and files

### `no index yet — run a sync or 'da index rebuild'`

The SQLite index has not been created. It is built automatically on
first sync; `da index rebuild` recreates it from what is already on
disk, which is what you want after moving or restoring the destination
folder.

### I moved my destination folder and everything re-downloads

The index records absolute paths. Point `da` at the new location and
rebuild:

```bash
da config set destination /new/path
da index rebuild
```

### A file is 0 bytes

Older versions could commit an empty download. Delete the file and
re-run the sync — the index no longer treats a 0-byte image as
complete, so it will be fetched again.

## Scheduled runs

### The scheduled sync never runs

```bash
launchctl list com.fz2000.da-cli     # is it loaded?
tail -50 ~/Library/Logs/da-cli.log   # what did it say?
da diagnose                          # does it see the job?
```

If the job is loaded but produces nothing, the most common cause is
macOS Full Disk Access. A launchd job writing to `~/Documents`,
`~/Desktop`, `~/Downloads`, or `/Volumes/…` needs it, and there is no
prompt for a background job — it simply fails.

Grant it in System Settings → Privacy & Security → Full Disk Access,
adding `~/Applications/da-sync.app`. A destination elsewhere under your
home directory, such as `~/Pictures/DA`, does not need this.

### `da diagnose` reports the schedule is not installed

```bash
./install_schedule.sh                # install
./install_schedule.sh uninstall      # remove
```

See [scheduling](scheduling.md).

## Still stuck

Run `da diagnose` and include its output — it masks secrets — when
[opening an issue](https://github.com/FZ2000/da-cli/issues).
