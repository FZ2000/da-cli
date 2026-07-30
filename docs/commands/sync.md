# Syncing art

`da sync` is the half of da-cli that puts files on your disk. Three
commands walk DeviantArt in three different ways — the watch feed, one
artist's gallery, every artist you watch — and all three end in the same
place: one folder per deviation under your destination, plus a row in a
local SQLite index so the next run does not fetch it again.

Reach for `sync feed` for the daily catch-up, `sync artist` when you want
one gallery in full, and `sync watched` for a backfill across everyone
you follow. None of them need flags to be useful; the flags exist to
bound how long a run takes and how hard it leans on the API.

## At a glance

| Command | What it does |
| --- | --- |
| `da sync feed` | Walks your watch feed from the top and stops at the checkpoint the last run left behind. |
| `da sync artist` | Walks one user's gallery newest-first, stopping at the first page you already have. |
| `da sync watched` | Discovers everyone you watch, then runs the artist walk for each of them under one shared time budget. |

All three take an exclusive lock, write to the same destination, and
share the same [index](#the-synced-index), [disk
layout](#what-lands-on-disk) and [pacing](#pacing-and-concurrency); those
are documented once, below the three command sections.

## da sync feed

```text
usage: da sync feed [-h] [--limit LIMIT] [--mature | --no-mature]
                    [--time-budget SECONDS] [--delay-api DELAY_API]
                    [--delay-image DELAY_IMAGE] [--jitter JITTER]
                    [--concurrency CONCURRENCY] [--dry-run]
```

Pages down `/browse/deviantsyouwatch` — the same feed the DeviantArt
website shows you — saving anything you do not already have, and stops as
soon as it reaches the newest deviation the previous run saw. This is the
command to schedule: on a day when nothing new was posted it costs a
single API call and writes nothing. It is also the only sync mode that
follows *your* feed rather than a gallery, so it picks up new artists you
start watching without being told about them.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `--limit LIMIT` | int | `24` (`DEFAULT_LIMIT`) | Deviations requested per feed page. Clamped at runtime to 1–50; DeviantArt caps this endpoint at 50 (`FEED_PAGE_CAP`). |
| `--mature` / `--no-mature` | bool | `--mature` (on) | Sets DeviantArt's `mature_content` query parameter. Sync defaults this **on** — unlike `search` — because a filtered walk drops items from the middle of the feed and looks like data loss on an unattended run. |
| `--time-budget SECONDS` | int | `540` | Wall-clock cap on the whole walk. See [bounding a run](#bounding-a-run-with---time-budget). |
| `--delay-api DELAY_API` | float (s) | config `delay_api`, else `5.0` (`DEFAULT_DELAY_API`) | Sleep between API calls. `0` is honoured and disables the sleep. |
| `--delay-image DELAY_IMAGE` | float (s) | config `delay_image`, else `1.5` (`DEFAULT_DELAY_IMAGE`) | Sleep after each successful image download, inside each worker. |
| `--jitter JITTER` | float | config `jitter`, else `0.0` (`DEFAULT_JITTER`) | Multiplies every sleep by `uniform(1-pct, 1+pct)`. Clamped to 0–0.95 (`JITTER_MAX_PCT`); jittered sleeps never fall below 0.05 s (`JITTER_FLOOR_S`). |
| `--concurrency CONCURRENCY` | int | config `concurrency`, else `4` (`DEFAULT_CONCURRENCY`) | Parallel image-download workers per page. Clamped to 1–16; `1` is sequential. |
| `--dry-run` | flag | off | Fetch and report, write nothing. Metadata calls still happen; no image bytes are downloaded, no files written, the index is untouched and the checkpoint is not advanced. |

Delays, jitter and concurrency are resolved as CLI flag, then
`config.json`, then the built-in default — see
[configuration](../reference/configuration.md). `--limit`,
`--time-budget`, `--mature` and `--dry-run` are CLI-only.

### Behaviour

The walk starts at offset 0 and pages down. Before anything else it
resolves your access token (refreshing it if needed), checks the
destination exists, and bootstraps the index from disk if the index is
empty. Then, per page:

1. If every deviation id on the page is already in the index, the page is
   counted as duplicates and the metadata call is skipped entirely. If
   that page also contained the checkpoint the walk stops there;
   otherwise the next page is requested after a shortened API delay of
   `min(--delay-api, 1.0)` seconds — only one call was made, so the full
   five-second gap is not warranted.
2. Otherwise metadata is fetched for the unknown ids only (50 per call),
   and every deviation on the page is handed to the download workers.
   Known ones return immediately as duplicates.

**The checkpoint.** `state.json` holds `last_feed_deviationid`: the id of
the first item on the first page of the last clean run, meaning
"everything newer than this is already synced". Each run records the top
id it saw at offset 0, and when it encounters that stored id on a page it
truncates the page there and stops — that is the `caught up` stop reason.

The checkpoint is advanced **only** when all four of these hold:

- the run actually saw a first page with results, so there is a new top id;
- it ended on `caught up`, `feed exhausted` (the API said `has_more:
  false`) or`feed empty` (a page came back with no results);
- it was not a `--dry-run`;
- **no deviation failed** — a single `fail` on any page holds the
  checkpoint back.

Anything else leaves the checkpoint where it was and prints
`checkpoint not advanced — incomplete run (<reason>); next sync will
re-check these items`, or`— dry run` for a dry run. This is deliberate
and it is the reason a truncated feed sync is safe: the next run starts
from the top again and re-checks the gap rather than stepping over it
forever. The cost of a held-back checkpoint is one cheap all-known page
per run, not a re-download. The wording is misleading in one case: a run
that reached the end of the feed but had a single failed image reports
`incomplete run (feed exhausted)`. That run did finish — the checkpoint
is being held back because of the failure, not because of the stop
reason.

`last_feed_sync_at` (Unix seconds) is written alongside the checkpoint.

**Stop reasons** are `caught up`, `feed exhausted`, `feed empty`,
`HTTP 429 at offset N`, and `time budget exhausted`. A 429 stops the walk
cleanly with exit 0; every other HTTP error propagates (see
[exit codes](#exit-codes) below). The stop reason is recorded in
`state.json` under `last_sync` and is what `da diagnose` grades a
scheduled run by.

**What it logs.** One `[<elapsed>s] feed offset=<n>` line per page, then
`+ <artist>/<title> <n> KB` per saved deviation (titles truncated to 50
characters), or `would fetch <artist>/<title>` under `--dry-run`, or
`page all-known (<n> dups) — skipping metadata fetch` on the fast path.
A deviation that fails to save logs `! <artist>/<title> — fail:<Class>`
at warn level, so it survives `--quiet`; one with no downloadable content
logs `- <artist>/<title> — no downloadable image`.
The final line is
`feed sync stopped: <reason>; ok=N dup=N noimg=N fail=N`. That summary
line has no `dry=` field, so on a dry run everything reads zero — the
would-fetch count is only in the per-item lines and in
`da diagnose`'s `last_sync` totals.

Feed sync never revisits items older than the checkpoint. If you delete
something from a gallery you synced months ago, this command will not
bring it back; use `da sync artist <artist> --full`.

### Examples

Every sync command needs credentials and a destination, and fails fast
without them. Before `da auth` has ever run:

```console
$ da sync feed
[error] no refresh_token stored — run `da auth` first
```

And once a token exists but no destination has been set:

```console
$ da sync feed
[error] no destination configured — set DA_DESTINATION or `da config set destination <PATH>`
```

A real run against DeviantArt cannot be reproduced here, but `da bench`
drives the exact same feed-sync engine against a mocked API, so its
output shows the real line formats — two pages of three deviations, two
download workers:

```console
$ da bench --pages 2 --per-page 3 --concurrency 2
bench: pages=2 per_page=3 concurrency=2 total_items=6
bench dir: /var/folders/60/59kzhwyj4h7b28150q1k4pz00000gn/T/da-bench-rakhe6y9
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
  elapsed        : 0.007s
  items/sec      : 912.4
  pages/sec      : 304.14
```

The images are eight bytes of fake PNG, hence `0 KB`. A real feed run
prints the same shape with real sizes, plus a
`jitter on: API ±40% / image ±40% around base` line when `--jitter` is
non-zero.

Typical invocations:

```bash
da sync feed                              # daily catch-up
da sync feed --dry-run                    # what would a first sync pull?
da --quiet sync feed --time-budget 1200 --jitter 0.4   # scheduled
```

### Exit codes

`0` on any clean stop, including `time budget exhausted` and a 429. `2`
when it could not start: no refresh token, no `client_id`, no
destination, a destination whose parent directory does not exist, a
corrupt index, or a 401 that survives a forced token refresh. `0` — with
a `skipping:` message — when another sync already holds the lock.

One deviation from the norm: an unexpected HTTP status from the feed
endpoint (anything that is not 429 or a retried 5xx) is **not** caught.
It escapes as a Python traceback and the process exits `1`, not `2`.
`sync artist` handles the same situation differently, which is worth
knowing before you write a wrapper script around either.

## da sync artist

```text
usage: da sync artist [-h] [--offset N] [--limit LIMIT]
                      [--mature | --no-mature] [--time-budget SECONDS]
                      [--delay-api DELAY_API] [--delay-image DELAY_IMAGE]
                      [--full] [--jitter JITTER] [--concurrency CONCURRENCY]
                      [--dry-run]
                      artist
```

Walks one user's `/gallery/all` newest-first and saves what you do not
have. Use it to pull in an artist you have just discovered, to backfill
someone whose older work predates your watch, or to repair a gallery
after deleting files from it. If you are unsure of the exact username,
`da search user` on the [searching and browsing](search.md) page and
`da user profile` on the [inspecting](inspect.md) page will confirm it
before you spend a walk on a typo.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `artist` | positional str | *(required)* | DeviantArt username, URL-encoded before the request. The destination sub-folder is named after the username the API returns, not after what you typed — your spelling is only the fallback for the rare deviation with no author block. |
| `--offset N` | int | continue from the last unfinished walk of this gallery | Start the walk this many deviations down the gallery. Omit it and the walk resumes where the previous one stopped; pass `0` to force a walk from the newest deviation. Negative values are clamped to 0. |
| `--limit LIMIT` | int | `24` (`DEFAULT_LIMIT`) | Deviations per gallery page. Clamped at runtime to 1–24 — `/gallery/all` caps at 24 (`GALLERY_PAGE_CAP`), so raising this above 24 does nothing. |
| `--mature` / `--no-mature` | bool | `--mature` (on) | As for `sync feed`. |
| `--time-budget SECONDS` | int | `540` | Wall-clock cap on this gallery walk. |
| `--delay-api DELAY_API` | float (s) | config `delay_api`, else `5.0` | Sleep between API calls. |
| `--delay-image DELAY_IMAGE` | float (s) | config `delay_image`, else `1.5` | Sleep after each successful image download. |
| `--full` | flag | off | Disable the early stop and page through the entire gallery. Still skips what is already indexed — see below. |
| `--jitter JITTER` | float | config `jitter`, else `0.0` | As for `sync feed`; clamped to 0–0.95. |
| `--concurrency CONCURRENCY` | int | config `concurrency`, else `4` | Parallel image-download workers per page, clamped to 1–16. |
| `--dry-run` | flag | off | Fetch and count, write nothing. Prints no per-item lines in this mode — see below. |

### Behaviour

`/gallery/all` returns deviations in reverse-chronological order. That
single fact is what makes a repeat sync cheap: if any deviation on the
current page is already in the index, every page *below* it must be older
still, and therefore already synced too. So the walk marks itself
`caught up` and stops after finishing the current page. A second run over
an unchanged gallery costs exactly one API call and zero downloads.

Two details make the early stop safe rather than merely fast:

- The page that triggers the stop is still processed in full. The walk
  does not stop at the first known id and skip the rest of the page —
  it downloads every unknown deviation on that page first. This matters
  because the index [self-heals](#the-synced-index): a page can read
  known, known, *missing*, known, and that missing slot is real work.
- Known ids are filtered out before the metadata batch, so a page that
  is 23/24 duplicates costs one metadata call for one deviation.

What the early stop does **not** do is compensate for a walk that
stopped part-way down. There is no per-artist "how far did I get" marker
in `state.json`; the index is the only memory, and it says nothing about
order. So if a run is truncated by its time budget at offset 96, the
newest four pages are indexed and everything older is not — and the next
plain run reads page 0, finds it entirely known, reports `caught up` and
stops without ever requesting offset 96 again. Resuming is manual, which
is why the summary is followed by a
`resume: da sync artist <name> --offset <n>` line whenever the walk
stopped for a non-terminal reason.

The same offset is recorded in `state.json` as `last_sync.last_offset`
(only for non-terminal stops, so `gallery complete` and `caught up` do
not record one). Re-run with that `--offset`, or with `--full`, or the
rest of the gallery stays unsynced.

**`--full`** disables the early stop only. It walks every page to the
end of the gallery but still skips anything the index already knows, so
it re-downloads nothing and its cost is one API call per page plus one
metadata call per page containing unknowns. It is the right tool for
"I deleted some files, put them back" and for "I am not convinced the
early stop saw everything". It is not a way to force a re-download —
for that, delete the folder (or the image inside it) and then run with
`--full`, which drops the stale index row and fetches the deviation
again.

**Stop reasons** are `gallery complete` (the API said `has_more: false`),
`caught up`, `empty page` (a page came back with no results — a
non-existent user, an empty gallery, or an `--offset` past the end),
`http <code>`, and `time budget exhausted`. Only the first two count as
terminal, so a walk that ends on `empty page` is shown by `da diagnose`
as a warning even though nothing is wrong. The resume hint is printed for
every non-terminal reason, including `http 404`, where it is not much
help.

**What it logs.** One `[<elapsed>s] gallery/all offset=<n> artist=<name>`
line per page, then `+ <title> <n> KB` per saved deviation (titles
truncated to 60 characters; unlike the feed there is no artist prefix),
plus `! <title> — fail:<Class>` at warn level for a deviation that failed
to save and `- <title> — no downloadable image` for one with no content,
then
`artist sync stopped at offset <n>: <reason>; ok=N dup=N noimg=N fail=N`.

`--dry-run` in this mode is quieter than you would expect: the per-item
log line only fires for genuinely saved deviations, so a dry run prints
the `DRY RUN` warning, the page headers, and a summary reading
`ok=0 dup=N noimg=0 fail=0`, where `N` counts the already-indexed
deviations the walk passed over — that tally is not suppressed by
`--dry-run`. Only `ok`, `noimg` and `fail` are held at zero. The
would-fetch count is recorded — as
`totals.dry` under `last_sync` — but the only way to read it is
`da diagnose`. `da sync feed --dry-run` does print a `would fetch` line
per deviation; the artist walk does not.

### Examples

The command needs a destination and a token, so a run cannot be shown
here end to end. Without them it fails immediately:

```console
$ da sync artist someone
[error] no destination configured — set DA_DESTINATION or `da config set destination <PATH>`
```

The flag list and defaults come straight from the parser:

```console
$ da sync artist --help
usage: da sync artist [-h] [--offset N] [--limit LIMIT]
                      [--mature | --no-mature] [--time-budget SECONDS]
                      [--delay-api DELAY_API] [--delay-image DELAY_IMAGE]
                      [--full] [--jitter JITTER] [--concurrency CONCURRENCY]
                      [--dry-run]
                      artist

positional arguments:
  artist

options:
  -h, --help            show this help message and exit
  --offset N            Start at this page offset. Omit to continue from
                        where the last unfinished walk of this gallery
                        stopped.
  --limit LIMIT
  --mature, --no-mature
  --time-budget SECONDS
                        Wall-clock cap for this gallery walk (default: 540).
                        On a truncated run the resume offset is printed and
                        recorded.
  --delay-api DELAY_API
  --delay-image DELAY_IMAGE
  --full                Disable the synced-index early-stop and walk the
                        entire gallery. Use this if you suspect missed
                        deviations or have rotated content.
  --jitter JITTER       Randomise each sleep by ±PCT (0-0.95). Default 0.
  --concurrency CONCURRENCY
                        Parallel image-download workers per page. Default 4,
                        max 16.
  --dry-run             Report what would be downloaded without writing any
                        files.
```

Typical invocations:

```bash
da sync artist alice                      # newest work you are missing
da sync artist alice --full               # whole gallery, downloads only gaps
da sync artist alice --offset 96          # resume a truncated walk
da sync artist alice --time-budget 3600 --jitter 0.4   # big backfill
```

### Exit codes

`0` and `2` as everywhere else — with one deviation worth knowing. An
HTTP error from the gallery endpoint is caught, logged as
`[error] HTTP <code> from gallery/all: <first 200 characters of the
body>`, and recorded as the stop reason`http <code>` — and then the
command exits **`0`**. A typo in the username produces that error, the
usual summary line with all counters at zero, a resume hint, and success
in `$?`. If you are scripting this command, check `da diagnose --json`
for the stop reason rather than trusting the exit code.

## da sync watched

```text
usage: da sync watched [-h] [--user USER] [--via-feed] [--feed-max FEED_MAX]
                       [--mature | --no-mature] [--time-budget SECONDS]
                       [--delay-api DELAY_API] [--delay-image DELAY_IMAGE]
                       [--full] [--jitter JITTER] [--concurrency CONCURRENCY]
                       [--dry-run]
```

Works out who you watch, then runs the artist walk for each of them in
turn, sharing one time budget across the whole run. This is the backfill
command: `sync feed` only sees what people post from now on, so the way
to get an artist's back catalogue for everyone you follow is to run this
once — probably several times — with a generous budget. It is not a good
daily job; `sync feed` is.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `--user USER` | str | *(from `/user/whoami`)* | Username to enumerate friends for. Skips the whoami call. Needs `user` scope either way. |
| `--via-feed` | flag | off | Skip the friends endpoint entirely and discover artists by walking the watch feed. Works with `browse` scope alone. |
| `--feed-max FEED_MAX` | int | `2000` | Cap on deviations scanned during feed discovery. Only applies to the feed strategy. |
| `--mature` / `--no-mature` | bool | `--mature` (on) | Applies to both discovery and every gallery walk. |
| `--time-budget SECONDS` | int | `540` | Wall-clock cap on the **entire run**, discovery included — not per artist. |
| `--delay-api DELAY_API` | float (s) | config `delay_api`, else `5.0` | Passed to each artist walk. Does not affect the discovery phase, which uses its own fixed delays. |
| `--delay-image DELAY_IMAGE` | float (s) | config `delay_image`, else `1.5` | Passed to each artist walk. |
| `--full` | flag | off | Passed to each artist walk: disables the per-artist early stop. Slow — every page of every gallery. |
| `--jitter JITTER` | float | config `jitter`, else `0.0` | Passed to each artist walk. |
| `--concurrency CONCURRENCY` | int | config `concurrency`, else `4` | Passed to each artist walk, clamped to 1–16. |
| `--dry-run` | flag | off | Passed to each artist walk. Nothing is written and the index is untouched, but every gallery is still paged and metadata is still fetched. |

There is no `--limit` and no `--offset`. Each artist is walked with
`--limit 24` and `--offset 0`, always.

### Behaviour

**Discovery** happens first, and comes in two flavours:

1. *Friends* (the default, and authoritative). Calls `/user/whoami` for
   your username unless `--user` supplied one, then pages
   `/user/friends/<username>` 50 at a time with a fixed one-second gap
   between pages. Requires `user` scope. On a 403 it warns —
   ``token lacks`user` scope — falling back to feed-based artist
   discovery`` — and switches to strategy 2 rather than failing. The walk
   stops on an empty page whatever `has_more` says, and at 200 pages
   (10,000 artists) regardless; reaching either backstop with the server
   still reporting more warns that the list may be short, since an
   unattended run would otherwise page indefinitely.
2. *Feed* (`--via-feed`, or the 403 fallback). Pages
   `/browse/deviantsyouwatch` 50 at a time with a fixed two-second gap,
   collecting unique author names until it has scanned `--feed-max`
   deviations or run out of feed. It only finds artists who have posted
   recently, so it under-reports; the warning to re-authorise with
   ``da auth --scope "user browse"`` is printed for a reason.

Either way you get `found N watched users (via friends|feed)`, in the
order the API returned them, de-duplicated. If discovery finds nobody the
command warns and returns — and, unusually, records no `last_sync`
summary at all, so `da diagnose` will still be describing the run before
this one.

**The budget is shared.** The deadline is fixed when the command starts,
before discovery, and each artist is handed only what is left of it, not
a fresh copy. Before starting an artist the run checks the remaining
time: below 30 seconds (`MIN_ARTIST_BUDGET_S`) there is not enough left
for even one page, so the rest are skipped with a warning reading
`time budget exhausted after <done>/<total> artists (<n> not attempted)
— re-run to continue`.

Skipped artists are not failures. Re-running picks up where it stopped in
the useful sense: the artists already walked are all-known, so each costs
one API call before the walk moves on — but a *truncated* artist, one cut
off mid-gallery, is not resumed, for exactly the reason described under
[`sync artist`](#behaviour-1). Its remaining pages need
`da sync artist <name> --offset N` or `--full`.

**Failure handling.** Each artist runs inside a `try`. An artist that
exits or crashes is logged, counted as failed, and the run moves on to
the next one. The exception is a missing refresh token: if the token has
gone, every remaining artist would fail identically, so that one
propagates and the run stops. Note what is *not* counted as a failure —
an HTTP error from a gallery endpoint is handled inside the artist walk
itself, so a watched run in which every gallery returned 404 finishes
with zero failures and exits 0.

The summary recorded in `state.json` is
`{"kind": "watched", "totals": {"artists_total": …, "artists_done": …,
"artists_failed": …, "artists_skipped": …}, "stop_reason": …, "via": …}`,
where the stop reason is `time budget exhausted`,
`N of M artists failed`, or `all artists complete`. Each artist walk also
writes its own `artist` summary as it goes, so a run interrupted with
Ctrl-C leaves the last artist's summary in place rather than a watched one.

### Example

The lock is shared by all three sync commands, so this is what a manual
run looks like when the scheduled one is already going:

```console
$ da sync watched
skipping: another `da sync` is already running (lock held: /Users/you/.local/state/da-cli/.sync.lock)
$ echo $?
0
```

A real run prints the discovery line, then a banner per artist —
`=== [<i>/<total>] <artist> ===` — followed by that artist's normal walk
output.

Typical invocations:

```bash
da sync watched --time-budget 3600            # an hour of backfill, then stop
da sync watched --via-feed                    # no `user` scope
da sync watched --full --time-budget 7200     # paranoid: every page of every gallery
da sync watched --dry-run --time-budget 600   # how much is missing?
```

### Exit codes

This command deviates from the usual `0`/`2` split:

| Code | When |
| --- | --- |
| `0` | Every artist walked, or some were skipped because the budget ran out. |
| `1` | Some artists failed but at least one succeeded. |
| `2` | Every attempted artist failed, or setup failed (no token, no destination). |

Time-budget skips never affect the exit code, so a nightly job that
truncates still exits 0. See [exit codes](../reference/exit-codes.md).

## What lands on disk

Per deviation, one folder under `<destination>/<artist>/<title>/`
containing `description.json` and `image.<ext>`:

- **`description.json`** holds `deviationid`, `url`, `title`,
  `author_username`, `author_userid`, `is_mature`, `is_favourited`,
  `published_time`, `stats`, and the full `metadata` block from
  `/deviation/metadata` (description, tags, and for video deviations the
  time-limited video URLs).
- **`image.<ext>`** is the `content.src` URL, or `preview.src` when the
  deviation has no full content. The extension is taken from the URL —
  `jpg`, `jpeg`, `png`, `gif` or `webp` — defaulting to `.jpg`.

Artist and title are sanitised: any run of characters outside
`A-Za-z0-9._-` collapses to a single underscore, the result is truncated
to 100 characters and stripped of leading and trailing underscores, and
anything that sanitises to nothing (or to dots) becomes `untitled`.

**Title collisions.** Two deviations by one artist whose titles sanitise
identically would land in the same folder. When the folder already exists
and its `description.json` names a *different* deviation, the newcomer
gets `<title>--<shortid>/`, where `shortid` is the first eight characters
of the deviation id, lowercased. The folder that got there first keeps
its unsuffixed path, so the suffix only ever appears on the collision.
Folder resolution is serialised across download workers, so two colliding
deviations on the same page cannot both claim the plain name.

**Atomic staging.** The image is downloaded to memory, written to
`image.<ext>.part`, `fsync`ed, then renamed into place;
`description.json` goes through the same `.tmp`-then-rename dance. A
crash mid-download leaves a `.part` file, which every "do I have this?"
check ignores, so the next run fetches it again cleanly. A zero-byte
response body is treated as a failure (`fail:EmptyBody`) rather than
committed, because a 0-byte image would otherwise be indexed as done and
never retried.

Two consequences worth knowing:

- `description.json` is written *before* the image. A failed download
  leaves a folder holding only the metadata, and no index row — the next
  run redownloads and overwrites.
- A deviation with no downloadable URL at all (some literature and text
  posts) is counted `noimg`, keeps its metadata folder, and is never
  indexed. It will be reported `noimg` again on every run that reaches
  it.

Full layout, permissions and cleanup are in
[files on disk](../reference/files-on-disk.md).

## The synced index

The index is a SQLite database at
`$XDG_STATE_HOME/da-cli/index.db` (mode 0600), one row per synced
deviation: id, artist, title, folder path, image size, timestamp. It is
what makes a repeat sync cost one API call instead of a full re-walk, and
it is the single source of truth for "do I already have this".

- **Bootstrap.** The first sync in a process finds the index empty,
  walks the destination, and imports every folder holding both a
  `description.json` and a finished image (`synced-index is empty;
  bootstrapping from existing destination...`). It happens once per
  process, so `sync watched` does not repeat it per artist.
- **Self-healing.** A row only counts as a hit if the folder still
  contains `description.json` *and* a non-`.part` image. Delete either
  and the row is dropped as the sync passes over it, so the deviation is
  fetched again. This is what makes "delete the bad file and re-run" work
  — with the caveat that a plain artist re-run stops at page 0 and may
  never pass over the row at all. Pair the deletion with `--full`.
- **Corruption** is detected once per process with a single `COUNT(*)`;
  an unreadable database aborts the sync with exit 2 and tells you to
  delete the file and run `da index rebuild`.

`da index show` and `da index rebuild` are on the [index, health and
benchmarking](maintenance.md) page.

## Pacing and concurrency

DeviantArt publishes no rate limit, so `da` throttles itself
conservatively rather than discovering the real limit the hard way. The
defaults are 5 seconds between API calls (`delay_api`) and 1.5 seconds
after each image (`delay_image`), with jitter off.

Where the sleeps actually land:

| Point in the walk | Sleep |
| --- | --- |
| Before the metadata batch for a page | `delay_api` |
| After the metadata batch, before saving | `delay_api` |
| After each successful image download, inside the worker | `delay_image` |
| Between pages on the all-known fast path (`sync feed`) | `min(delay_api, 1.0)` |
| Between friends-list pages (`sync watched` discovery) | 1.0 s, fixed |
| Between feed pages (`sync watched --via-feed` discovery) | 2.0 s, fixed |

So a page of new work costs about 10 seconds of deliberate waiting plus
the downloads, and a page you already have costs about 1 second. The two
discovery delays are not configurable — `--delay-api` does not reach
them.

`--jitter 0.4` multiplies every one of those sleeps by a random factor
between 0.6 and 1.4. It is off by default because a predictable run is
easier to reason about, and worth turning on for anything scheduled so a
nightly job does not hit the API in a metronome pattern;
`install_schedule.sh` sets it to 0.4 for exactly that reason. The value
is clamped to 0.95 so jitter can never zero a delay out, and a jittered
sleep never drops below 0.05 seconds.

`--concurrency` is the number of image downloads in flight per page,
default 4, clamped to 1–16. Higher is unfriendly to the CDN and buys
little — image downloads are IO-bound and the index writes serialise on a
single lock anyway. Two things to be aware of:

- `--delay-image` is applied *inside* each worker, so with concurrency 4
  the effective pause between images is roughly a quarter of what you
  set. If you want a genuine 1.5 seconds between requests, use
  `--concurrency 1`.
- Concurrency does not reorder the log lines. The workers never log;
  `_save_page_concurrent` writes each result into an index-keyed slot and
  returns them in page order, and the main thread prints the `+` lines
  from that list once the page's pool has finished. Two runs at different
  concurrencies produce the same lines in the same order.

Underneath, every request retries twice on 5xx and network errors with
exponential backoff from 1.5 seconds. 4xx responses, including 429, are
never retried — the sync loop decides what to do with those. A 401 is
handled separately: the access token is force-refreshed and the call
retried once, and only a second 401 gives up with exit 2.

## Bounding a run with --time-budget

`--time-budget` (540 seconds by default on all three commands) caps
wall-clock time. The walk checks the clock before starting each page and
stops if less than 10 seconds (`TIME_BUDGET_MARGIN_S`) of the budget
remain, so the page in flight always finishes and the summary is always
written. A budget of 10 seconds or less therefore does nothing at all: no
page is ever started and the run records `time budget exhausted`
immediately.

What truncation means depends on the mode:

- **`sync feed`** does not advance the checkpoint, so the next run walks
  from the top again and re-checks the gap. Genuinely self-resuming.
- **`sync artist`** prints and records the resume offset. Not
  self-resuming — re-run with `--offset` or `--full`.
- **`sync watched`** stops starting new artists once fewer than 30
  seconds remain, and records how many were never attempted. Artists
  already finished are cheap on the next run; a half-finished one needs
  the `--offset` treatment.

A truncated run is a warning, not a failure: it exits 0, but `da diagnose`
grades it as a warning rather than a clean sync, because "the nightly job
never actually finishes" is otherwise invisible. If yours warns every
night, the budget is too small for the volume you are syncing. See
[scheduling a daily sync](../guides/scheduling.md) and
[scripting and unattended use](../reference/scripting.md).

## Running two syncs at once

All three commands take the same advisory lock —
`$XDG_STATE_HOME/da-cli/.sync.lock` — for the whole run. A second sync
starting while one is in progress logs
``skipping: another`da sync`is already running`` and exits 0 rather
than racing on the index and the checkpoint. That is why running a sync by
hand while a schedule exists is safe, and why a wrapper script should not
treat a zero-length run as success without checking `da diagnose`. Inside
`sync watched` the per-artist walks call the unlocked implementation
directly, so the command does not deadlock against itself.
