# Inspecting users and deviations

Four read-only lookups: who an artist is, what a single deviation
holds, what DeviantArt considers similar to it, and who you watch. None
of them downloads an image, writes to your destination folder or
touches the synced index. Reach for them when you need the username or
the deviation id that a sync command takes as an argument, or when you
want to see what DeviantArt holds for one item without pulling a whole
gallery.

## At a glance

| Command | Purpose |
| --- | --- |
| `da user profile` | Six-line summary of one account: userid, profile URL, whether you watch them, bio |
| `da deviation show` | Metadata for a single deviation — title, author, tags, description — or the raw record with `--json` |
| `da deviation morelikethis` | DeviantArt's "More Like This" suggestions for a seed deviation, split into more from the artist and more from DA |
| `da watch list` | One page of the accounts you watch; needs the `user` scope |

## What the four have in common

All of them are authenticated calls, so each starts by resolving an
access token. If the stored one has expired it is refreshed silently,
which rewrites `state.json` — the only file any of these commands can
write. With no stored token they stop before making any request:

```console
$ da user profile deviantart
[error] no refresh_token stored — run `da auth` first
```

That exits `2`; [authentication](auth.md) covers fixing it.

Beyond that:

- They do not need `destination` to be configured, and they never read
  or write the destination folder or the SQLite index.
- They take no lock, so they are safe to run while a `da sync` is in
  progress — the sync lock is held by sync commands only.
- The only failures they handle *themselves* are the ones documented in
  each section below. Any other non-2xx response from DeviantArt reaches
  `main()`'s backstop handler, which prints one line naming the status and
  exits `2` — the usual code, not a traceback. `-v` shows the traceback,
  which is written at debug level. A 5xx or a network error is retried
  twice before any of that (`HTTP_RETRY_DEFAULT = 2` in
  `dacli/constants.py`); 4xx responses are never retried.

## `da user profile`

```text
usage: da user profile [-h] username
```

Looks up one DeviantArt account and prints the handful of fields you
usually want before syncing it: the internal userid, the profile page,
whether you already watch them, and their bio. Use it to confirm you
have a username exactly right — the argument goes straight into the URL
path, so a typo is a failed request rather than a fuzzy match. To
search for a username rather than confirm one, use `da search user`,
covered in [searching and browsing](search.md).

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `username` | positional string | required | The DeviantArt username to look up. URL-quoted into the request path; no leading `@`. |
| `-h`, `--help` | flag | — | Print the usage above and exit. |

### Behaviour

One `GET` to `/user/profile/{username}`, always with
`mature_content=true`. There is no `--mature` flag here, so profiles
resolve regardless of how you sync.

The output is exactly six lines, in this order:

- `@` followed by `user.username`
- `userid:` — `user.userid`, the id DeviantArt uses internally
- `profile:` — `profile_url`
- `watching:` — whether *you* watch them, coerced to `True` or `False`
- `artist?:` — `user_is_artist`, printed as DeviantArt returns it, so
  it can read `None` when the field is absent
- `bio:` — printed verbatim on a single line, with no HTML stripping
  and no truncation. This differs from `da deviation show`, which
  cleans up its description field before printing it.

Everything else in the response is discarded, and there is no `--json`
on this command, so those six fields are the whole interface to it.
Nothing is cached: each run is a fresh API call.

There is no "not found" handling either. A username DeviantArt rejects
fails the request and surfaces as a one-line error with exit `2`; a
response carrying no `user` object prints the labels with `None` values
rather than erroring.

### Example

```bash
da user profile deviantart
```

With a working token this prints the six-line block described above,
beginning `@deviantart` and ending with the account's bio. Without
credentials it prints the `no refresh_token stored` error shown at the
top of this page and exits `2`.

## `da deviation show`

```text
usage: da deviation show [-h] [--json] deviationid
```

Fetches DeviantArt's metadata record for one deviation and prints
either a readable summary or the record itself. Run it when you have an
id — from `da search … --json`, from a `description.json` already on
disk, or from someone else — and want to know what it is before
deciding to sync the artist behind it.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `deviationid` | positional string | required | The deviation id. URL-quoted into a single `deviationids[]` query parameter. |
| `--json` | flag | off | Print the raw metadata record instead of the summary, and return immediately. |
| `-h`, `--help` | flag | — | Print the usage above and exit. |

### Behaviour

One `GET` to `/deviation/metadata` with a single id, plus
`mature_content=true&ext_camera=true&ext_stats=true&ext_submission=true`.
Two consequences are worth knowing:

- Mature content is always requested. There is no `--mature` flag on
  this command, unlike `da deviation morelikethis`.
- The three `ext_` parameters are always on, so the record carries the
  extended camera, stats and submission data. That is why `--json`
  returns considerably more than the summary suggests.

`--json` prints the metadata record itself, unwrapped from
DeviantArt's `{"metadata": [...]}` envelope — one object, indented two
spaces, with non-ASCII characters left as characters rather than `\u`
escapes. Nothing else on this page happens when `--json` is given.

The summary prints, in order, `deviationid:`, `title:`, `author:`
(prefixed with `@`), `url:` — the deviation's page on DeviantArt, not a
download link — `is_mature:`, and `tags:` as a comma-separated list of
tag names. If the record has a non-empty description, a blank line and
a `description:` block follow: HTML tags are stripped, runs of
whitespace collapse to single spaces, and the result is truncated to
1000 characters with a trailing `...`. An empty description prints
nothing at all.

The command looks up exactly one id per run. The endpoint accepts up to
50 at a time — `METADATA_BATCH_SIZE = 50` in `dacli/constants.py`,
which is what the sync path batches with — but this command has no
multi-id form, so ten deviations means ten runs and ten API calls, with
no delay applied between them.

### Example

```bash
da deviation show <deviationid>
da deviation show <deviationid> --json
```

The first prints the labelled summary; the second prints the raw
record, already indented two spaces, so piping it through a formatter
is only worth it if you want to reshape it.
[Scripting](../reference/scripting.md) lists every command that takes
`--json`.

### Exit codes

Exits `2`, with `[error] no metadata for deviationid <id>` on stderr,
when the response contains no metadata entry for the id you asked
about. That is the empty-result path, not the bad-id path: an id
DeviantArt rejects outright fails the request instead — also exit `2`,
but with the HTTP status in the message rather than the id.

## `da deviation morelikethis`

```text
usage: da deviation morelikethis [-h] [--limit LIMIT] [--mature] [--json]
                                 deviationid
```

Given one deviation you like, asks DeviantArt's recommender what
resembles it. The answer comes back in two groups: more work by the
same artist, and more from across the site. It is the quickest way to
turn a single deviation into a list of artists worth
`da sync artist`, which [syncing art](sync.md) covers.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `deviationid` | positional string | required | The seed deviation id, sent as the `seed` query parameter. |
| `--limit LIMIT` | int | `10` | How many rows to print per group. Applied locally after the response arrives — see below. |
| `--mature` | flag | off | Sends `mature_content=true`. Off by default, so mature suggestions are filtered out unless you pass it. |
| `--json` | flag | off | Print the whole response body instead of the two group listings. |
| `-h`, `--help` | flag | — | Print the usage above and exit. |

### Behaviour

One `GET` to `/browse/morelikethis/preview` with the seed id and the
mature flag. The preview endpoint returns a different shape from the
search endpoints: `seed`, `author`, `more_from_artist` and
`more_from_da`, with no `has_more` and no offset. There is no way to
ask for a second page of suggestions.

`--limit` never reaches DeviantArt. It slices each group locally before
printing, which has two visible effects. The group header reports the
*full* size of the group, so `--- FROM ARTIST (25) ---` above ten rows
is correct rather than a bug. And `--limit` is ignored entirely under
`--json`, which prints the complete body, both groups intact, and
returns before any slicing happens.

The human-readable form opens with a seed line —
`seed: <id>  author: @<username>` — then prints each group in turn, one
row per suggestion: the deviation id, the author prefixed with `@` and
padded to 18 characters, and the title truncated to 60 characters. Ids
print at their full 36 characters, and a missing author renders as `?`.

`--mature` defaulting to off matches the rest of the search and browse
commands, but not `da deviation show`, which always asks for mature
content. An id you can inspect with `deviation show` therefore does not
guarantee unfiltered suggestions for it.

### Example

```bash
da deviation morelikethis <deviationid> --limit 5
da deviation morelikethis <deviationid> --mature --json
```

The first prints the seed line and up to five rows under each of
`--- FROM ARTIST (n) ---` and `--- FROM DA (n) ---`, where `n` is the
group's true size, not the number of rows shown. The second prints the
raw body and ignores the limit.

## `da watch list`

```text
usage: da watch list [-h] [--limit LIMIT] [--offset OFFSET]
```

Prints one page of the accounts you watch. Use it to check who
`da sync watched` will walk, or to pick a single username out of your
watch list for `da sync artist`. This is the one command on this page
that needs more than the default token scope.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `--limit LIMIT` | int | `50` | Page size, passed straight to `/user/friends`. Matches the page size the sync path uses against the same endpoint. |
| `--offset OFFSET` | int | `0` | Where the page starts. Advance it by hand from the `next_offset` printed at the end of a page. |
| `-h`, `--help` | flag | — | Print the usage above and exit. |

Neither number is validated or clamped by the CLI, so whatever limits
DeviantArt enforces on the endpoint are the ones that apply, and a
rejected value comes back as a one-line error with exit `2`.

### Behaviour

Two API calls per run: `GET /user/whoami` to learn your own username,
then `GET /user/friends/{username}` with your limit, your offset and
`mature_content=true`. There is no `--user` flag to skip the first call
— `da sync watched` has one, this does not.

Each result prints as `@username  (type)`, where the type is
DeviantArt's account type for that user. Only those two fields are
shown; the rest of each record is discarded. An empty page prints
nothing at all and exits `0`, so an empty watch list and an offset past
the end look identical.

Pagination is manual, one page per run. When more results exist, a
blank line and a summary line follow the listing:

```text
has_more: True (next_offset=50)
```

Pass that number as `--offset` to get the next page; when there is no
more, nothing is printed. Nothing is cached between runs either — the
watch list is never stored in the index or in `state.json`.

### The `user` scope requirement

`/user/whoami` and `/user/friends` both require the `user` scope, and
`da auth` requests only `browse` by default. With a browse-only token
the very first call fails, and the command says so rather than guessing:

```text
[error] `watch list` needs `user` scope — current token only has the listed scope
[warn]  (re-run `da auth --scope "user browse"` to broaden, or use `da sync watched --via-feed` which works with browse scope alone)
```

Both lines go to stderr and the command exits `2`. The message says
"the listed scope" without actually listing it; your recorded scope
comes from `da whoami`, which prints a `scope:` line, or from
`da diagnose`, which reports it in the auth section. That value is
whatever DeviantArt granted when you ran `da auth` — refreshing a token
does not change it.

Note the asymmetry with syncing. `da sync watched` treats the same 403
as a warning and falls back to discovering artists from your feed,
which works with `browse` alone; `watch list` has no fallback and
stops. If you only need the sync to work,
`da sync watched --via-feed` avoids the problem entirely. If you want
the authoritative list, re-run `da auth --scope "user browse"`.
[Troubleshooting](../guides/troubleshooting.md) gives the same advice
from the error-message side.

### Example

```bash
da watch list --limit 10
da watch list --limit 10 --offset 10
```

With a `user`-scoped token each run prints up to ten
`@name  (type)` lines, followed by the `has_more` line when further
pages remain. With a browse-only token it prints the two-line scope
error above and exits `2`.

### Exit codes

Exits `2` on the scope failure described above. An empty page is not a
failure and exits `0`.

## Finding the id you need

These lookups are usually one step in a longer path, and the ids have
to come from somewhere:

- `da search tag <tag> --json`, and every other `search` subcommand
  with `--json`, includes the `deviationid` and the content URL for
  each hit. The human-readable form deliberately drops both. See
  [searching and browsing](search.md).
- Anything already synced carries its own id: each deviation folder
  holds a `description.json` with the `deviationid` in it. See
  [files on disk](../reference/files-on-disk.md).
- `da search user <username>` resolves one or more usernames to userids
  in a single call, which `da user profile` cannot do.

Once you have a username, [syncing art](sync.md) is where it goes.
