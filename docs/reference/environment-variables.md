# Environment variables

Every variable `da` reads, and what it overrides.

## Credentials and paths

These take precedence over `config.json` but are overridden by an
explicit command-line flag. See [configuration](configuration.md) for
the full precedence rules.

| Variable | Overrides | Example |
| --- | --- | --- |
| `DA_CLIENT_ID` | `client_id` | `12345` |
| `DA_CLIENT_SECRET` | `client_secret` | your app's secret |
| `DA_DESTINATION` | `destination` | `~/Pictures/DA` |
| `DA_REDIRECT_URI` | `redirect_uri` | `https://localhost:8765/` |

Setting credentials in the environment is the right choice for CI and
for containers. On a personal machine prefer `da config set`, which
stores the secret in the macOS Keychain rather than in a shell history
or a process listing.

## Output

| Variable | Effect |
| --- | --- |
| `NO_COLOR` | Set to any value to disable coloured output. Honoured regardless of `--color`, per [no-color.org](https://no-color.org/). |

Colour is also disabled automatically when output is not a terminal, so
piping to a file or a log needs no configuration.

## File locations

`da` follows the XDG Base Directory specification.

| Variable | Default | What moves |
| --- | --- | --- |
| `XDG_CONFIG_HOME` | `~/.config` | `da-cli/config.json` |
| `XDG_STATE_HOME` | `~/.local/state` | `da-cli/state.json`, `index.db`, the loopback TLS cert |

Pointing both at a temporary directory is the supported way to run `da`
without touching your real configuration:

```bash
tmp=$(mktemp -d)
XDG_CONFIG_HOME="$tmp/cfg" XDG_STATE_HOME="$tmp/state" da diagnose
```

## Scheduling

Read by `install_schedule.sh` when it writes the launchd job, not by
`da` itself. Change one and re-run the installer to apply it.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DA_HOUR` | `3` | Hour of day to run (0–23). |
| `DA_MINUTE` | `0` | Minute of the hour (0–59). |
| `DA_INTERVAL_SECONDS` | *(unset)* | Run every N seconds instead of at a fixed time. Mutually exclusive with `DA_HOUR`/`DA_MINUTE`. |
| `DA_TIME_BUDGET` | `1200` | Seconds the scheduled sync may run before stopping cleanly. |
| `DA_JITTER` | `0.4` | Randomise each sleep by ±40%, so the request pattern is not perfectly regular. |

## Testing only

| Variable | Effect |
| --- | --- |
| `DA_REFRESH_TOKEN` | Supplies a refresh token to the live test suite instead of reading `state.json`. |
| `DA_SCOPE` | OAuth scope the live tests expect (default `browse`). |
| `VCR_RECORD` | Set to `1` to re-record HTTP cassettes instead of replaying them. |

See the [testing guide](../guides/testing.md).
