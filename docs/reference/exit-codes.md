# Exit codes

`da` uses the same exit codes everywhere, so a wrapper script can act
on the result without parsing output.

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | The command ran, but the answer was negative — see below. |
| `2` | The command could not do its job. Configuration missing, credentials rejected, destination unwritable, network gone. |
| `130` | Interrupted with Ctrl-C. |

`da` exits `0` when something downstream closes the pipe — `da search tag
nature --json | head -5` is normal usage, and the consumer getting what it
asked for is not a failure. Nothing is printed about it. (This previously
exited `120`, which is CPython's "could not flush stdout at shutdown" and
is not one of the codes above, so a wrapper matching them fell through.)

## When 1 and 2 differ

Most commands only ever return 0 or 2. Four return 1, and the
distinction matters if you are scripting:

- **`da config get <key>`** exits `1` when the key is simply not set.
  That is an answer, not a failure — the command worked.
- **`da diagnose`** exits `0` when every check passes, `1` when the
  worst finding is a warning, and `2` when any check fails. This lets a
  monitor distinguish "needs attention soon" (a refresh token expiring
  in ten days) from "sync is broken right now".
- **`da auth status`** exits `1` when the refresh-token chain is inside
  its warning window — the credential still works, but it needs renewing
  soon. This is the command advertised for cron health checks, so the
  distinction between "renew this week" and "broken now" is the whole
  point.
- **`da sync watched`** walks many galleries in one run and keeps going
  when one of them fails. It exits `1` if some artists failed and `2` if
  every one did, so a job that quietly stopped working is not mistaken
  for a healthy one. Artists skipped because the
  [time budget](scripting.md) ran out are not failures and do not affect
  the exit code.

## Failures `da` recognises, and failures it does not

Every failure `da` recognises exits `2` and prints what to do about it:

```console
$ da sync feed
[error] DeviantArt rejected the credentials (HTTP 401).
Run `da auth` to sign in again. `da auth status` will confirm whether the
stored token is still accepted.
```

There is no separate code for "temporary" — a rate limit and a dead token
both exit `2`. What tells them apart is the message, and `da diagnose`
afterwards, which records why the last run stopped.

A failure `da` does **not** recognise is left alone, traceback and all:

```console
$ da sync feed
Traceback (most recent call last):
  File ".../dacli/sync.py", line 214, in _save_one
    ...
TypeError: unsupported operand type(s) for +: 'int' and 'str'
```

That is deliberate. An error the tool cannot explain is far more likely a
defect in `da` than something wrong with your setup, and summarising it
into a tidy line would disguise a bug as your problem. If you see one,
it is worth reporting, and the traceback is what makes the report useful.

## Using this in a scheduled job

```bash
#!/usr/bin/env bash
da diagnose --json > /tmp/da-health.json
case $? in
  0) ;;                                    # healthy, nothing to do
  1) notify "da-cli needs attention soon" ;;
  2) notify "da-cli is broken"; exit 1 ;;
esac

da --quiet sync feed || notify "da sync feed failed"
```

`da diagnose --json` emits a stable schema, so you can key on fields
rather than on message text:

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

`overall.status` is uppercase (`OK`, `WARN`, `FAIL`); `findings[].level`
is lowercase (`ok`, `warn`, `fail`). The casing genuinely differs — match
on it exactly. `section` groups findings by subsystem: `config`, `auth`, `tls`,
`index`, `last sync`, `schedule`.

See [scripting](scripting.md) for the full unattended-operation guide.
