# Scheduling a daily sync

`da sync feed` is incremental — a run with nothing new costs one API
call — so it is cheap to run every day and pick up whatever your
watched artists posted.

## macOS

```bash
./install_schedule.sh              # daily at 03:00
./install_schedule.sh uninstall    # remove it
```

The installer does three things:

1. Builds a small `.app` bundle at `~/Applications/da-sync.app` that
   wraps `da`. This exists so macOS Full Disk Access can be granted to
   a stable path you control, rather than to a Homebrew-versioned
   Python binary that moves on every upgrade.
2. Writes a LaunchAgent at
   `~/Library/LaunchAgents/com.fz2000.da-cli.plist`.
3. Loads it with `launchctl`.

Output goes to `~/Library/Logs/da-cli.log`.

### Grant Full Disk Access

> System Settings → Privacy & Security → Full Disk Access → **+** →
> add `~/Applications/da-sync.app`

Without this, a scheduled run writing to a protected location —
`~/Documents`, `~/Desktop`, `~/Downloads`, or anything under
`/Volumes/` — fails silently. launchd has no way to show the permission
prompt a foreground app would get.

A destination elsewhere in your home directory, such as
`~/Pictures/DA`, does not need it.

### Changing the schedule

Set the variable and re-run the installer:

```bash
DA_HOUR=21 DA_MINUTE=30 ./install_schedule.sh    # daily at 21:30
DA_INTERVAL_SECONDS=21600 ./install_schedule.sh  # every 6 hours
DA_TIME_BUDGET=1800 ./install_schedule.sh        # allow 30 min per run
DA_JITTER=0.4 ./install_schedule.sh              # ±40% on every sleep
```

See [environment variables](../reference/environment-variables.md) for
the full list and defaults.

### Checking on it

```bash
launchctl list com.fz2000.da-cli     # is it loaded?
launchctl start com.fz2000.da-cli    # run it now
tail -f ~/Library/Logs/da-cli.log    # watch it
da diagnose                          # does da-cli see the job?
```

## Linux

`install_schedule.sh` is macOS-only — it refuses to run elsewhere,
because launchd, `.app` bundles, and Full Disk Access have no
equivalents. Use a systemd user timer or cron instead.

### systemd user timer

Create `~/.config/systemd/user/da-sync.service`:

```ini
[Unit]
Description=Sync DeviantArt watch feed

[Service]
Type=oneshot
ExecStart=%h/.local/bin/da --quiet sync feed --time-budget 1200 --jitter 0.4
```

And `~/.config/systemd/user/da-sync.timer`:

```ini
[Unit]
Description=Daily DeviantArt sync

[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=30m
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now da-sync.timer
loginctl enable-linger "$USER"     # keep firing when you are logged out
```

That last line is the one people miss: without lingering, user timers
only run while you have an active session.

Check on it:

```bash
systemctl --user list-timers da-sync.timer
journalctl --user -u da-sync.service -n 50
```

`Persistent=true` means a run missed while the machine was off happens
at the next boot instead of being skipped.

### cron

Simpler, but it will not catch up on missed runs and gives you a
sparser environment:

```cron
0 3 * * * $HOME/.local/bin/da --quiet sync feed --time-budget 1200 --jitter 0.4 >> $HOME/da-sync.log 2>&1
```

Use the absolute path — cron's `PATH` usually does not include
`~/.local/bin`.

## Running by hand while a schedule exists

Safe. Sync commands take an exclusive lock, so if your manual run
overlaps the scheduled one, the second exits immediately with a message
instead of corrupting the index.

## Monitoring

`da diagnose --json` gives a stable schema and a meaningful exit code
(`0` healthy, `1` warnings, `2` broken), which is what you want in a
wrapper script. See [scripting](../reference/scripting.md) and
[exit codes](../reference/exit-codes.md).

The warning worth acting on is the refresh token: it expires 90 days
after `da auth`, and `da diagnose` starts warning 14 days out.
