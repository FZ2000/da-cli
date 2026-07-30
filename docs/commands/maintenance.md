# Index, health and benchmarking

These are the commands you run when you want to know whether `da` is
still working, and to repair it when it is not. `da index show` and
`da index rebuild` inspect and rebuild the SQLite index that makes
repeat syncs fast, `da diagnose` checks every layer that can quietly
break an unattended run, and `da bench` measures the CLI's own overhead
against a fake DeviantArt. None of them download art; `da bench` does
not even use the network.

## At a glance

| Command | What it does |
| --- | --- |
| `da index show` | Print index stats: file path, row count, size on disk, top ten artists. |
| `da index rebuild` | Walk the destination and rebuild the index from what is actually on disk. |
| `da diagnose` | Run every health check and exit `0` (OK), `1` (warnings) or `2` (failures). |
| `da bench` | Time a synthetic sync against a mocked HTTP layer. No network, no account. |

The transcripts on this page were produced against a throwaway
destination (`/tmp/da-demo/gallery`) and a throwaway `XDG_STATE_HOME`,
so the paths in them are temporary. On your machine the index lives at
`~/.local/state/da-cli/index.db` — see
[files on disk](../reference/files-on-disk.md).

## The synced index

The index is a SQLite database at `~/.local/state/da-cli/index.db`
(mode 0600) with one row per downloaded deviation: its deviation id,
artist, title, absolute folder path, image size in bytes, and the time
it was indexed. It exists so that a sync does not have to stat the
whole destination to answer "have I already got this?". Because a
gallery is returned newest-first, the first known id lets a sync stop
walking, which turns a daily run from O(gallery size) into
O(new content). See [syncing art](sync.md) for how the early stop
interacts with `--full`.

The index is a cache, not a record of truth. Every membership test
verifies the folder is still on disk and drops the row if it is not, so
deleting art by hand is safe — the next sync notices and downloads it
again. The first sync after the index is deleted (or after upgrading
from a version that had none) bootstraps it automatically: if the index
has zero rows and the destination has content, sync performs the same
walk `da index rebuild` does, logs
`synced-index is empty; bootstrapping from existing destination...`,
and carries on.

You need `da index rebuild` when per-row self-healing is not enough:

- You moved or restored the destination to a different path. Every row
  now points somewhere that no longer exists, and the early stop is
  defeated until each row has been visited. A rebuild fixes the whole
  index in one pass, without re-downloading anything.
- An image was truncated to zero bytes by something outside `da`. Sync
  still counts that folder as complete, because the membership test
  looks for a finished file rather than a non-empty one, so the item is
  never repaired. A rebuild drops the row and the next sync fetches the
  image again.
- You already have a gallery that `da` did not download, and want it
  indexed without waiting for a sync to walk it.
- The index file is corrupt. Delete it first — see the exit codes under
  [`da index rebuild`](#da-index-rebuild).

### What counts as a synced folder

Both the rebuild and the per-row check agree on the shape of a synced
deviation, and it is exactly the layout described in
[files on disk](../reference/files-on-disk.md):

```text
<destination>/<artist>/<title>/
├── description.json
└── image.png
```

A folder is indexed by a rebuild when all of these hold:

- It is exactly two levels below the destination. The walk is
  `<destination>/*/*/description.json`; anything nested deeper or
  shallower is invisible to it.
- `description.json` parses as JSON and has a non-empty `deviationid`.
  Unreadable or malformed files are skipped silently.
- The folder contains at least one `image.*` whose suffix is not
  `.part`. A crash mid-download leaves `image.png.part`, which does not
  count.
- That image is not zero bytes. A zero-byte file is a failed download,
  and indexing it would mean it never got repaired.

The artist recorded in the index comes from `author_username` inside
`description.json`, falling back to the containing folder name, and the
title from the `title` key, falling back to the folder name. That is
why `da index show` can list an artist name that differs slightly from
the folder name: the folder name has been sanitised for the filesystem,
the indexed one has not. If a folder somehow contains more than one
non-`.part` `image.*`, whichever the filesystem returns first decides
both the recorded size and whether the row survives the zero-byte test.

## `da index show`

```text
usage: da index show [-h]

options:
  -h, --help  show this help message and exit
```

Prints a summary of the index: where it is, how many deviations are in
it, how big the database file is, and which artists account for the
most rows. Reach for it to confirm a sync actually recorded what it
downloaded, or to see the shape of a collection at a glance.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `-h`, `--help` | flag | — | Print the usage above and exit. |

### Behaviour

Read-only. It never creates or modifies the index, never touches the
destination, and does not need credentials or a configured destination.

If `index.db` does not exist, it prints a warning and exits `0` without
creating one — an absent index is not an error, it just means nothing
has been synced yet. That warning goes to stderr; the stats go to
stdout.

`db size` is the size of `index.db` alone, reported in KB with one
decimal. The artist breakdown is the top ten by row count, in
descending order; when the index has no rows the breakdown is omitted
entirely and only the three summary lines are printed.

```console
$ da index show
index file:  /tmp/da-demo/state/da-cli/index.db
total rows:  5
db size:     16.0 KB

top 10 artists by indexed deviations:
      3  fernwood-studio
      2  lumen-ink
```

Before anything has been synced:

```console
$ da index show
[warn]  (no index yet — run a sync or `da index rebuild`)
```

### Exit codes

`0` normally, including the "no index yet" case above. If `index.db`
exists but is not a readable SQLite database, the command does not
handle the error: it prints a Python traceback ending in
`sqlite3.DatabaseError: file is not a database` and exits `1`. Use
`da diagnose`, which reports the same condition as a `fail` finding
with an actionable message.

## `da index rebuild`

```text
usage: da index rebuild [-h]

options:
  -h, --help  show this help message and exit
```

Walks the destination and rebuilds the index from the folders it finds.
This is the repair tool: run it after moving, restoring or hand-editing
the destination, when the index and the disk have drifted apart.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `-h`, `--help` | flag | — | Print the usage above and exit. |

### Behaviour

The rebuild is **authoritative, not incremental**. It deletes every row
and repopulates from disk, so a folder that is gone loses its row
rather than lingering as a stale entry. Anything present on disk but
missing from the index is added. The destination is only read: no
files are created, deleted or renamed there, and `.part` leftovers are
not cleaned up.

It is idempotent — running it twice in a row produces the same index —
but it is not a no-op internally: every row is written afresh, so each
one's indexed-at timestamp becomes the time of the rebuild. The index
therefore does not preserve when each deviation was first downloaded,
and nothing displays that timestamp today.

Reading the destination requires it to be configured, so
`da index rebuild` goes through the same resolution as a sync: the
`destination` setting, or the `DA_DESTINATION` environment variable —
see [configuration](../reference/configuration.md). It does not need
credentials and makes no network calls.

Because dropping every row is destructive, the rebuild refuses to run
when the destination looks absent, and it is worth knowing exactly
where that line falls, because it is not where you might expect:

- If the **parent** of the destination is missing — the usual shape of
  an unmounted external drive, where `/Volumes/Archive` disappears and
  takes `/Volumes/Archive/deviantart` with it — the command stops
  before touching the index with
  `[error] parent of destination does not exist: ...` and exits `2`.
  The index is left exactly as it was.
- If the parent exists but the destination itself does not, the
  destination is **created empty** and then walked, which finds nothing
  and empties the index. This is what happens when the destination
  is itself the mount point (`destination = /Volumes/Archive`) and the
  volume is not mounted: `/Volumes` exists, so an empty `Archive`
  directory is created in its place and the rebuild reports
  `done: 0 deviations indexed`.

The guard inside `index_rebuild_from_disk` that returns early on a
missing destination is therefore unreachable through this command,
because the destination is created before the walk begins. If your
destination is on removable storage, point it at a directory *inside*
the mount point rather than at the mount point itself, and confirm the
drive is mounted before rebuilding. Losing the index is recoverable —
nothing on disk is deleted, and a rebuild with the drive mounted
restores every row — but the sync that runs in between will re-walk
galleries it did not need to.

It takes no lock, so it can run while a sync is in progress. Rows for
items saved during the walk may be dropped, but they are not lost work:
the next sync finds the files on disk and backfills the index without
re-downloading.

A rebuild after five deviations were downloaded, then the same rebuild
after one artist's folder was deleted by hand:

```console
$ da index rebuild
rebuilding synced-index from /tmp/da-demo/gallery ...
  done: 5 deviations indexed

$ rm -rf /tmp/da-demo/gallery/lumen-ink

$ da index rebuild
rebuilding synced-index from /tmp/da-demo/gallery ...
  done: 3 deviations indexed

$ da index show
index file:  /tmp/da-demo/state/da-cli/index.db
total rows:  3
db size:     16.0 KB

top 10 artists by indexed deviations:
      3  fernwood-studio
```

The two counts are the point: the deleted artist's rows are gone, not
stale.

### Exit codes

`0` on success, including a rebuild that indexes nothing. `2` when the
destination is not configured, or when its parent directory does not
exist:

```console
$ da index rebuild
[error] no destination configured — set DA_DESTINATION or `da config set destination <PATH>`
```

If `index.db` exists but is corrupt, the command exits `1` with a
`sqlite3.DatabaseError` traceback — the rebuild cannot repair a
damaged database file, because it has to open it before it can drop
rows. Delete `~/.local/state/da-cli/index.db` and run the rebuild
again; nothing in the destination is affected.

## `da diagnose`

```text
usage: da diagnose [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      Emit machine-readable JSON instead of the human-readable report.
              Schema: {timestamp, overall: {status, warnings, criticals},
              findings: [{level, section, message}], exit_code}.
```

Runs every check that can tell you a scheduled sync is about to fail,
or has already been failing silently, and reports each as a pass,
warning or failure. Run it after setup to confirm everything is wired
up, after something goes wrong, and from a monitoring job on a
schedule — the exit code distinguishes "needs attention soon" from
"broken right now".

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `--json` | flag | off (human-readable report) | Print the findings as JSON and exit with the same code. |
| `-h`, `--help` | flag | — | Print the usage above and exit. |

### What it checks

Findings are emitted in a fixed order, grouped by section. Two sections
are conditional: `schedule` only appears on macOS, and `tls` only when
`redirect_uri` is an HTTPS loopback URL.

| Section | Check | Levels |
| --- | --- | --- |
| `config` | `client_id` is set | pass, or fail if unset |
| `config` | `destination` is set, exists and is writable | pass, or fail if unset, missing or not writable |
| `config` | free space on the destination | pass at 5 GiB or more, warn below 5 GiB, fail below 1 GiB, warn if it cannot be read |
| `config` | `redirect_uri` | pass, or warn when it starts with `http://` |
| `auth` | access token | pass when it has more than 60 seconds left; otherwise see below |
| `auth` | refresh-token chain age | pass above 14 days remaining, warn at 14 or below, fail at 3 or below |
| `index` | `index.db` readable | pass with row count and size, fail if unreadable, warn if there is no index yet |
| `last sync` | the summary the last sync wrote to `state.json` | pass on a clean finish with no failures, otherwise warn |
| `schedule` | macOS only: `launchctl list com.fz2000.da-cli` | pass when loaded and its last exit was 0, warn when not loaded or its last exit was non-zero |
| `tls` | loopback certificate and key both exist | pass, or warn if either is missing |

The `schedule` check looks for the launchd job that
`install_schedule.sh` installs, and a non-zero last exit means the job
fired and the sync failed — see [scheduling](../guides/scheduling.md).
On Linux, or anywhere without `launchctl`, the section is absent
entirely and a broken cron entry is not detected.

The destination path is expanded first, so a `~/…` destination is
checked as the directory you meant. The two free-space thresholds are
written as literals in `dacli/commands/diagnose.py`; the
`DEST_FREE_SPACE_WARN_GIB` and `DEST_FREE_SPACE_FAIL_GIB` constants
hold the same values but are not what the check reads, so editing them
alone changes nothing.

The `auth` section is the one with real behaviour behind it. When the
access token is still valid the check is local and reports the minutes
remaining and the scope recorded at login (the scope string is echoed,
not verified against DA). When the access token has expired or is
within a minute of it, and a refresh token is stored, diagnose
**actually exchanges the refresh token with DeviantArt** rather than
assuming a stored token still works — a grant revoked on DA's side is
precisely the failure that makes a nightly job stop working without
anyone noticing. That means diagnose can make a network call, and on
success it writes the new tokens to `state.json`; you may also see
`[warn]  refreshing access token via refresh_token` on stderr above the
report. If DA rejects the token the finding is a failure telling you to
re-run `da auth`; if DA is simply unreachable the finding is a warning,
because being offline is not a credential problem. With no access token
at all, or an expired one and no refresh token to trade in, the finding
is a failure and no request is made.

The chain-age check appears only once `state.json` has a
`refresh_token_issued_at` stamp, which is written when DA issues or
rotates a refresh token. DeviantArt's refresh tokens die 90 days after
issue (`REFRESH_TOKEN_TTL_DAYS`) and nothing can extend that; the
warning and failure thresholds are `REFRESH_TOKEN_WARN_DAYS` (14) and
`REFRESH_TOKEN_CRIT_DAYS` (3). See [authentication](auth.md).

The `last sync` finding reads the summary the last sync recorded and
follows its stop reason. A run that finished its work — `gallery
complete`, `caught up`, `feed exhausted`, `feed empty`, or
`all artists complete` for `sync watched` — with no failed items is a
pass. Anything else, including `time budget exhausted` and an HTTP 429
abort, is a warning, so a nightly job that never gets to the end of the
backlog is visibly different from a healthy one. These are never
failures: each resolves itself on a later run, and exiting `2` would
mean paging someone at 03:00 over a truncated backfill.

### Behaviour

Beyond the possible token refresh described above, diagnose writes
nothing. It does not create the index, the destination or the
certificate; a missing index is reported, not fixed.

The report header timestamp is local time, while the `timestamp` field
in `--json` is UTC with a `Z` suffix — the same run produces two
different-looking times. JSON is written with ASCII escapes, so the em
dashes inside messages arrive as `\u2014` rather than as the character
itself; key on `level` and `section` rather than matching message
text. Findings always appear in the order above, but do not index into
the array by position: the conditional sections and the branchy `auth`
checks change its length.

For monitoring recipes built on this output, see
[scripting](../reference/scripting.md).

A healthy install whose only complaint is that the scheduled job has
not been installed:

```console
$ da diagnose
================================================================
da-cli diagnose — 2026-07-28 17:51:27
================================================================
  ✓ [config    ] client_id: 12345
  ✓ [config    ] destination: /tmp/da-demo/gallery (writable)
  ✓ [config    ] destination free space: 144.4 GiB
  ✓ [config    ] redirect_uri: https://localhost:8765/
  ✓ [auth      ] access_token valid for 39 min (scope=browse)
  ✓ [auth      ] refresh_token chain: 68 days remaining (of 90)
  ✓ [index     ] 5 synced rows, 16 KB on disk
  ✓ [last sync ] feed ended 1.4h ago (caught up) — totals={'ok': 12, 'dup': 108, 'noimg': 0, 'fail': 0, 'dry': 0}
  ⚠ [schedule  ] launchd job not loaded — run `./install_schedule.sh` to install
  ✓ [tls       ] loopback cert generated
----------------------------------------------------------------
OVERALL: WARN  (1 warnings)
```

The same run as JSON:

```console
$ da diagnose --json
{
  "timestamp": "2026-07-29T00:51:31Z",
  "overall": {
    "status": "WARN",
    "criticals": 0,
    "warnings": 1
  },
  "findings": [
    {
      "level": "ok",
      "section": "config",
      "message": "client_id: 12345"
    },
    {
      "level": "ok",
      "section": "config",
      "message": "destination: /tmp/da-demo/gallery (writable)"
    },
    {
      "level": "ok",
      "section": "config",
      "message": "destination free space: 144.4 GiB"
    },
    {
      "level": "ok",
      "section": "config",
      "message": "redirect_uri: https://localhost:8765/"
    },
    {
      "level": "ok",
      "section": "auth",
      "message": "access_token valid for 39 min (scope=browse)"
    },
    {
      "level": "ok",
      "section": "auth",
      "message": "refresh_token chain: 68 days remaining (of 90)"
    },
    {
      "level": "ok",
      "section": "index",
      "message": "5 synced rows, 16 KB on disk"
    },
    {
      "level": "ok",
      "section": "last sync",
      "message": "feed ended 1.4h ago (caught up) \u2014 totals={'ok': 12, 'dup': 108, 'noimg': 0, 'fail': 0, 'dry': 0}"
    },
    {
      "level": "warn",
      "section": "schedule",
      "message": "launchd job not loaded \u2014 run `./install_schedule.sh` to install"
    },
    {
      "level": "ok",
      "section": "tls",
      "message": "loopback cert generated"
    }
  ],
  "exit_code": 1
}
```

`overall.status` is uppercase (`OK`, `WARN`, `FAIL`) and
`findings[].level` is lowercase (`ok`, `warn`, `fail`). The casing
really does differ.

### Exit codes

This is one of the few commands that uses `1`, and the code is the same
with and without `--json`:

| Code | Meaning |
| --- | --- |
| `0` | Every check passed. |
| `1` | At least one warning and no failures. A sync can still run. |
| `2` | At least one failure. The next sync will not work until it is fixed. |

The counts in `overall` are the number of failures and warnings, so
`exit_code` is derivable from them. See
[exit codes](../reference/exit-codes.md).

## `da bench`

```text
usage: da bench [-h] [--pages PAGES] [--per-page PER_PAGE]
                [--concurrency CONCURRENCY] [--json]

options:
  -h, --help            show this help message and exit
  --pages PAGES         Pages of synthetic feed (default 10)
  --per-page PER_PAGE   Deviations per page (default 24, DA's gallery cap)
  --concurrency CONCURRENCY
                        Image-download workers per page (default 4)
  --json                Emit JSON instead of the summary table
```

Runs a complete feed sync against a fake DeviantArt and reports how
long it took. Everything above the network is real code — argument
handling, folder resolution, metadata assembly, atomic writes, the
thread pool, the SQLite index — while every HTTP call is answered from
memory and every pacing delay is skipped. Use it to check that a change
did not make the CLI slower, or to compare concurrency settings without
spending real requests.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `--pages` | int | `10` | Number of synthetic feed pages to serve. Values below 1 are raised to 1. |
| `--per-page` | int | `24` (matches `GALLERY_PAGE_CAP`) | Deviations on each synthetic page. Values below 1 are raised to 1. |
| `--concurrency` | int | `4` (`DEFAULT_CONCURRENCY`) | Image-download workers per page, clamped to `CONCURRENCY_MIN` (1) … `CONCURRENCY_MAX` (16), exactly as a real sync clamps it. |
| `--json` | flag | off (summary table) | Print the result as JSON. Also suppresses the progress log. |

### Behaviour

No account, no network, no configuration. For the duration of the run
`urlopen`, `time.sleep`, the config loader, the state loader and saver,
and the index path are all replaced, so bench never reads your
`config.json` or `state.json`, never writes to your real index, and
never writes into your destination. The originals are restored
afterwards even if the run raises. It works on a machine that has never
run `da auth`.

Each run creates a fresh temporary directory (`da-bench-*` under
`$TMPDIR`) holding its own destination and index, and **leaves it
behind** — nothing deletes it. The path is printed as `bench dir: …` in
the progress log, so it is not shown when you pass `--json` or `-q`.
The files are tiny (each synthetic image is eight bytes), but repeated
benchmarking accumulates directories.

Because the index starts empty and the synthetic destination has no
content, every run also logs the bootstrap line
`synced-index is empty; bootstrapping from existing destination...`
with zero rows imported. That is the real sync path being exercised,
not a fault.

The synthetic run is a `sync feed` with mature content on, jitter off
and a one-hour time budget; those are fixed and there are no flags for
them. Every deviation it is served is new, so the run measures the
save path rather than the duplicate-detection path.

`elapsed` covers the sync call itself, not the setup or teardown around
it. The derived figures are `items_per_sec = indexed / elapsed` and
`pages_per_sec = pages / elapsed`, where `indexed` is the row count in
the bench index at the end of the run. In a clean run `indexed` equals
`total_items` (`pages × per_page`); a shortfall means items were not
saved and is worth investigating.

What the numbers mean, and what they do not: they are the CLI's own
overhead on your machine, with no network latency, no rate-limit
delays, and eight-byte images. A real sync is dominated by HTTP round
trips and the configured pacing delays, so bench throughput is orders
of magnitude higher than anything you will see in practice — treat it
as a relative baseline, comparable between two commits on the same
machine, and not as a prediction of sync speed. The concurrency figure
is likewise about dispatch overhead: with no real IO to overlap, more
workers rarely helps.

Keep `--per-page` at 50 or below. The real feed endpoint caps a page at
50 items (`FEED_PAGE_CAP`) and the sync clamps its request accordingly,
but the fake server keys its pages on the `--per-page` you asked for. At
`--pages 2 --per-page 60` the sync walks offsets 0, 50 and 100, so it is
served the first synthetic page twice: three fetches and a 60-item
all-duplicate page for a run that reports two pages. `pages_per_sec`
divides by the page count you asked for, not the number actually
fetched, so it understates the work done.

A default run, with `-q` to keep the per-item log out of the way:

```console
$ da --quiet bench

  pages          : 10
  per page       : 24
  concurrency    : 4
  total items    : 240
  indexed        : 240
  elapsed        : 0.076s
  items/sec      : 3,172.4
  pages/sec      : 132.18
```

A small run with the progress log left on, showing the mocked feed
being walked:

```console
$ da bench --pages 2 --per-page 3 --concurrency 2
bench: pages=2 per_page=3 concurrency=2 total_items=6
bench dir: /tmp/da-demo/tmp/da-bench-10co7ndm
synced-index is empty; bootstrapping from existing destination...
  imported 0 existing deviations into the index
image downloads: 2-way concurrency
[0s] feed offset=0
  + alice/T-0-0                                              0 KB
  + alice/T-0-1                                              0 KB
  + alice/T-0-2                                              0 KB
[0s] feed offset=3
  + alice/T-1-0                                              0 KB
  + alice/T-1-1                                              0 KB
  + alice/T-1-2                                              0 KB
feed sync stopped: feed exhausted; ok=6 dup=0 noimg=0 fail=0

  pages          : 2
  per page       : 3
  concurrency    : 2
  total items    : 6
  indexed        : 6
  elapsed        : 0.005s
  items/sec      : 1,121.7
  pages/sec      : 373.89
```

The JSON form is what to record in CI and diff between commits:

```console
$ da bench --json --pages 10
{
  "pages": 10,
  "per_page": 24,
  "concurrency": 4,
  "total_items": 240,
  "indexed": 240,
  "elapsed_s": 0.072,
  "items_per_sec": 3321.3,
  "pages_per_sec": 138.39
}
```

The keys are stable; the numbers are machine-specific, so compare runs
from the same machine.
