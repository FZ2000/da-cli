# Scripting and unattended use

How to wrap `da` in a cron job, a launchd agent, or a monitoring
script without parsing human-readable output.

## Where the logs are

If a scheduled run misbehaved, this is the first thing to check.

- **Scheduled runs** (installed by `install_schedule.sh`) write to
  `~/Library/Logs/da-cli.log`. Tail it with `tail -f ~/Library/Logs/da-cli.log`.
- **Manual runs** write to the terminal. Add `-v` for per-request
  detail, or `-q` to print only warnings and errors.
- **`da` never writes a log file of its own.** There is no hidden log
  directory; if you want one, redirect output yourself.

```bash
da sync feed >> ~/da-sync.log 2>&1
```

## Machine-readable output

Eleven commands accept `--json`: every `search` subcommand,
`deviation show`, `deviation morelikethis`, `diagnose`, and `bench`.
Use it rather than parsing the human-readable form, which is not a
stable interface.

```bash
da diagnose --json               # health report, stable schema
da search topic nature --json    # any search/browse command
da deviation show <id> --json
da bench --json
```

`da auth status` is JSON-only — it takes no `--json` flag because it
has no human-readable mode:

```console
$ da auth status
{"state": "ok", "days_remaining": 87.3, "issued_at_iso": "2026-01-15T12:00:00Z"}
```

`state` is `ok`, `warn`, `crit`, `unknown`, `revoked`, or `unreachable`.

This asks DeviantArt. A token revoked from the website, or superseded by
a re-auth on another machine, is `revoked` here rather than reported
healthy by the calendar — which is what makes it worth running on a
schedule:

```bash
da auth status || notify "da-cli needs re-authenticating"
```

`unreachable` means the network was down, not that the token is dead, so
a monitor can tell "fix your credentials" from "try again later".

Validation goes through `/placebo`, refreshing the access token only when
it has actually expired, so calling this hourly does not rotate your
refresh token hourly. For a TTL reading with no network at all, use
[`da diagnose`](../commands/maintenance.md).

`da diagnose --json` is the one to build monitoring on. Note that
`overall.status` is uppercase while `findings[].level` is lowercase:

```json
{
  "timestamp": "2026-01-15T12:00:00Z",
  "overall": { "status": "WARN", "warnings": 1, "criticals": 0 },
  "findings": [
    { "level": "warn", "section": "auth", "message": "refresh_token expires in 11 days" }
  ],
  "exit_code": 1
}
```

## Detecting success

Check the exit code, not the output. See [exit codes](exit-codes.md)
for the full table; the short version is `0` success, `1` warnings,
`2` failure, `130` interrupted.

```bash
if ! da --quiet sync feed; then
  echo "sync failed" | mail -s "da-cli" me@example.com
fi
```

## Bounding how long a run takes

`--time-budget SECONDS` stops a sync cleanly at the limit rather than
being killed mid-download. The run finishes the item it is on, records
its progress, and exits — the next run resumes where it stopped.

```bash
da sync feed --time-budget 1200     # stop after 20 minutes
```

The budget covers the **whole run**. For `sync watched` that means all
artists together, not each one: with a 20-minute budget the walk gets
through as many galleries as it can, then stops and records how many
were left. Run it again to pick up the rest.

```bash
da sync watched --time-budget 3600  # one hour total, however many artists
```

Set this to comfortably less than your job's own timeout. A budget
below about 15 seconds leaves no time to do any work.

A truncated run is not a failure — it exits 0 — but it is also not a
finished one, so it does not advance the feed checkpoint and
`da diagnose` reports it as a warning rather than a clean sync. If your
scheduled job warns every night, the budget is too small for the amount
of art you are syncing.

## Being kind to the API

DeviantArt publishes no rate limit, so `da` self-throttles: 5 seconds
between API calls and 1.5 seconds between image downloads by default.

```bash
da sync feed --delay-api 5 --delay-image 1.5 --jitter 0.4
```

`--jitter 0.4` varies each sleep by ±40%, so a daily job does not
produce a perfectly regular request pattern. Lower the delays only if
you know what you are doing; you will be rate-limited before da-cli is.

## Two runs at once

Sync commands take an exclusive lock. If a manual `da sync feed`
overlaps the scheduled one, the second exits immediately with a message
rather than corrupting the index — so you can run one by hand whenever
you like without checking the schedule first.

## A complete example

```bash
#!/usr/bin/env bash
set -uo pipefail

da diagnose --json > /tmp/da-health.json
health=$?
if [ "$health" -eq 2 ]; then
  echo "da-cli is broken:" >&2
  python3 -c "import json;print(*[f['message'] for f in json.load(open('/tmp/da-health.json'))['findings'] if f['level']=='fail'],sep='\n')" >&2
  exit 1
fi

da --quiet sync feed --time-budget 1200 --jitter 0.4
```

More recipes are in the
[`examples/` directory](https://github.com/FZ2000/da-cli/tree/main/examples).
