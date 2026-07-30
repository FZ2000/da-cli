# Searching and browsing

These commands read DeviantArt and print what they find. They are how
you decide what is worth syncing: which tag actually exists, which
curated topic is busy today, which of six similar usernames is the
artist you meant. Nothing on this page downloads an image, writes to
your destination folder or touches the synced index — the only file any
of them can write is `state.json`, and only when your access token
needed refreshing first.

## At a glance

| Command | What it does |
| --- | --- |
| `da search tag <tag>` | Deviations carrying an exact tag |
| `da search topic <topic>` | Deviations from one curated DA topic |
| `da search topics` | List the curated topics, ten at a time |
| `da search toptopics` | The busiest topics, one example each |
| `da search tag-suggest <prefix>` | Autocomplete a tag prefix before you search it |
| `da search user <name>...` | Resolve usernames to user records |
| `da daily [<date>]` | DeviantArt's Daily Deviation picks for a date |
| `da search popular`, `da search newest` | Retired. Exit `2` with a pointer to what replaced them |

For single deviations and user profiles see
[inspecting users and deviations](inspect.md); for getting art onto
disk see [syncing art](sync.md).

## Tags and topics

A **tag** is free text the artist typed when they uploaded. Anyone can
invent one, nobody curates them, and matching is exact: `nature`
matches the tag `nature` and nothing else. There is no fuzzy search and
no phrase search: quoting the two words `nature photography` asks for a
single tag with a space in it, which no one has ever applied, so the
command prints nothing and exits `0`. Use
[`tag-suggest`](#da-search-tag-suggest) to find out what the real tag is
called before you search it.

A **topic** is one of DeviantArt's editorial categories — `digitalart`,
`fanart`, `photography`, `cosplay` and so on. Membership is decided by
DA, not by the artist, so topic results are tidier and narrower than a
tag search over the same subject, and the set of valid names is fixed
and small. [`topics`](#da-search-topics) lists them all;
[`toptopics`](#da-search-toptopics) lists the busiest ones. A topic name
that does not exist is not an error — DA returns an empty result set,
so the command prints nothing and exits `0`.

## Behaviour shared by every command here

**Authentication.** Each command needs an access token, so run
[`da auth`](auth.md) first. The `browse` scope that `da auth` requests
by default is what the browse endpoints want; `da search user` is the
exception, because it calls `/user/whois` and so needs a token
authorised for `/user/` endpoints. With no stored token you get:

```console
$ da search tag nature
[error] no refresh_token stored — run `da auth` first
```

and exit `2`. If the cached access token has expired the command
refreshes it in passing and rewrites `state.json`; that is the only
write these commands ever perform. See
[files on disk](../reference/files-on-disk.md).

**Mature content.** `--mature` sets DA's `mature_content` query
parameter to `true`; leaving it off sets it to `false`. This is the
opposite default from `sync`, which includes mature content unless you
pass `--no-mature`
(see [configuration](../reference/configuration.md)). Filtering happens
on DA's side, and the filtered items are removed from the page rather
than replaced: a filtered search can hand back fewer rows than `--limit`
asked for and still report the same `next_offset` as the unfiltered one.
In human-readable output a mature result is suffixed `[mature]`.

**Pagination and the caps DA enforces.** `--limit` is the page size the
CLI asks DA for, not a client-side truncation. Each endpoint has its own
ceiling, and exceeding it is a hard failure rather than a clamp:

| Command | Largest `--limit` DA accepts |
| --- | --- |
| `search tag` | 50 |
| `search topic` | 24 |
| `search topics` | 10 |

One over the ceiling and DA answers HTTP 400. Only `search topics` has
an `--offset` flag, so it is the only command here you can page through.
`search tag` and `search topic` print a `has_more` line with a
`next_offset` you have no flag to pass back; if you need the next page,
take `--json` and drive the API yourself using the `next_offset` or
`next_cursor` in the body.

**`--json`.** Every subcommand on this page takes it, including
`da search user` and `da daily`. With `--json` the CLI prints DA's response body
verbatim, indented two spaces, with non-ASCII characters left intact.
The human-readable form is a lossy summary — it drops the
`deviationid`, the image URLs and the statistics — so scripts should
always use `--json`
(see [scripting](../reference/scripting.md)).

For the deviation-shaped endpoints (`tag`, `topic`, `daily`) the body
is an envelope of `has_more`, `next_offset`, `next_cursor` (and
`prev_cursor` on `topic`) plus a `results` list. Each result carries
`deviationid`, `url`, `title`, `published_time` (a Unix timestamp as a
string), `is_mature`, `is_downloadable`, an `author` object
(`userid`, `username`, `usericon`, `type`, `is_watching`), a `stats`
object (`comments`, `favourites`), and `preview` / `content` / `thumbs`
image entries whose `src` values are signed CDN URLs.

**Errors.** These commands do not catch HTTP failures. A rejected
`--limit`, a malformed date or an unauthorised endpoint surfaces as a
Python traceback ending in a line like
`urllib.error.HTTPError: HTTP Error 400: Bad Request`, and the process
exits `1` — not the `2` that the rest of the CLI uses for "could not do
the job" (see [exit codes](../reference/exit-codes.md)). An empty result
set is not an error: nothing is printed and the exit code is `0`.

**Rate limiting.** Each invocation makes exactly one API request and
sleeps for nothing, so none of the `--delay-api` / `--jitter` throttling
that `sync` applies is in play here. The shared HTTP layer times out
after 30 seconds (`HTTP_TIMEOUT_JSON_S`) and retries 500, 502, 503 and
504 twice (`HTTP_RETRY_DEFAULT = 2`) with exponential backoff from 1.5
seconds (`HTTP_RETRY_BACKOFF_BASE_S`), jittered ±10 %. Every 4xx,
including 429, fails immediately.

## `da search tag`

```text
usage: da search tag [-h] [--limit LIMIT] [--mature] [--json] tag
```

Fetches deviations carrying one exact tag. This is the broadest net on
the page and the one you reach for when you know the subject but not the
artist — it is also the replacement DA users are pushed towards now that
`search newest` is gone.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `tag` | positional string | required | The tag to look up, exactly. URL-encoded for you, so quoting a value with spaces is safe — it will just match nothing. |
| `--limit` | int | `10` (a literal in the parser, not `DEFAULT_LIMIT`) | Page size requested from DA. Above 50 the request fails with HTTP 400. |
| `--mature` | flag | off | Sends `mature_content=true`. Off means DA filters mature results out. |
| `--json` | flag | off | Print DA's raw body instead of summary lines. |

### Behaviour

Calls `/browse/tags`. Ordering is DA's own and it is not chronological,
whatever the hint printed by the retired `search newest` says: the first
five results for `nature` were published in April 2023, July 2023, April
2023, May 2017 and July 2017. There is no `--offset`, so the `has_more`
line at the end tells you a next page exists without giving you any way
to ask for it.

Human-readable rows are author, title, URL. The title column is padded
to 50 characters but never truncated, so a long title pushes the URL to
the right rather than being cut.

```console
$ da search tag nature --limit 3
  Vilone                   Blooming in the swamp                              https://www.deviantart.com/vilone/art/Blooming-in-the-swamp-956548839
  solron                   Sunset lake                                        https://www.deviantart.com/solron/art/Sunset-lake-969992712
  t1na                     Bloom                                              https://www.deviantart.com/t1na/art/Bloom-956295875

has_more: True (next_offset=3)
```

The mature filter is visible if you run the same search both ways. With
`--mature` you get the full page and mature rows are marked:

```console
$ da search tag boudoir --limit 5 --mature
  StudioExperience         AnaDita - 05 - Boudoir Nude Erotic                 https://www.deviantart.com/studioexperience/art/AnaDita-05-Boudoir-Nude-Erotic-947672431 [mature]
  inspired-impressions     Allie                                              https://www.deviantart.com/inspired-impressions/art/Allie-605449359
  LienSkullova             Leijla #5                                          https://www.deviantart.com/lienskullova/art/Leijla-5-944666894
  Fight-The-Light          Natalia                                            https://www.deviantart.com/fight-the-light/art/Natalia-915198148
  BoudoirDebonair          Julie                                              https://www.deviantart.com/boudoirdebonair/art/Julie-952884886

has_more: True (next_offset=5)
```

Without it the same `--limit 5` yields four rows — the filtered item is
removed from the page, not replaced — while `next_offset` still advances
by five:

```console
$ da search tag boudoir --limit 5
  inspired-impressions     Allie                                              https://www.deviantart.com/inspired-impressions/art/Allie-605449359
  LienSkullova             Leijla #5                                          https://www.deviantart.com/lienskullova/art/Leijla-5-944666894
  StudioExperience         Little_miss_modele, Miss.c_darkgirl 7 - Boudoir    https://www.deviantart.com/studioexperience/art/Little-miss-modele-Miss-c-darkgirl-7-Boudoir-906439951
  ByYulli                  boudoir                                            https://www.deviantart.com/byyulli/art/boudoir-758952821

has_more: True (next_offset=5)
```

## `da search topic`

```text
usage: da search topic [-h] [--limit LIMIT] [--mature] [--json] topic
```

Fetches deviations from one curated topic. Reach for this instead of
`search tag` when you want a representative sample of a whole category
rather than everything that happens to share a word.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `topic` | positional string | required | Canonical topic name: lowercase, single token, as printed in the first column of `da search topics`. |
| `--limit` | int | `10` | Page size requested from DA. Above 24 the request fails with HTTP 400. |
| `--mature` | flag | off | Sends `mature_content=true`. |
| `--json` | flag | off | Print DA's raw body instead of summary lines. |

### Behaviour

Calls `/browse/topic`. Output format is identical to `search tag`, and
so is the missing `--offset`: you get a `has_more` line you cannot act
on from the CLI.

An unrecognised topic name is silently empty rather than an error, which
is easy to mistake for "this topic has nothing in it". If a topic
returns nothing, check the spelling against `da search topics` — and
note that the canonical name is often not the display name
(`digitalpainting` is displayed as "Drawings and Paintings").

```console
$ da search topic digitalart --limit 3
  Softyrider62             The Last Portal                                    https://www.deviantart.com/softyrider62/art/The-Last-Portal-963009673
  Ethemos                  Dragon                                             https://www.deviantart.com/ethemos/art/Dragon-958792892
  kloir                    Wind Valley                                        https://www.deviantart.com/kloir/art/Wind-Valley-970474945

has_more: True (next_offset=3)
```

`--json` on a name that does not exist shows the empty envelope plainly:

```console
$ da search topic notatopic --limit 3 --json
{
  "has_more": false,
  "next_offset": null,
  "next_cursor": null,
  "prev_cursor": null,
  "results": []
}
```

## `da search topics`

```text
usage: da search topics [-h] [--limit LIMIT] [--offset OFFSET] [--mature]
                        [--json]
```

Lists the curated topics themselves, so you can discover the canonical
names `search topic` expects. This is the only command on the page with
working pagination.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `--limit` | int | `10` | Topics per page. 10 is also DA's maximum here; `--limit 11` fails with HTTP 400. |
| `--offset` | int | `0` | Index of the first topic to return. Feed it the `next_offset` printed at the end of the previous page. |
| `--mature` | flag | off | Sends `mature_content=true` on the request. |
| `--json` | flag | off | Print DA's raw body instead of summary lines. |

### Behaviour

Calls `/browse/topics`. Each row is the canonical name, the display
name, and a count of example deviations DA attached to that topic — the
list endpoint attaches one each, so the count is `1` throughout; the
examples themselves are only visible with `--json`. Topics come back in
alphabetical order by display name, so walking `--offset 0`, `10`, `20`
covers the set without repeats.

```console
$ da search topics --limit 5
  adoptable                       Adoptables                      (1 examples)
  anime                           Anime and Manga                 (1 examples)
  handmade                        Artisan Crafts                  (1 examples)
  comic                           Comics                          (1 examples)
  cosplay                         Cosplay                         (1 examples)
  -- has_more, next_offset=5
```

```console
$ da search topics --limit 5 --offset 5
  customization                   Customization                   (1 examples)
  digitalart                      Digital Art                     (1 examples)
  digitalpainting                 Drawings and Paintings          (1 examples)
  emoji                           Emojis                          (1 examples)
  fanart                          Fan Art                         (1 examples)
  -- has_more, next_offset=10
```

## `da search toptopics`

```text
usage: da search toptopics [-h] [--mature] [--json]
```

Fetches the handful of topics DA currently considers most active, with
one example deviation each. It is the quickest way to see what the site
is busy with, and a short-cut past paging through `search topics` when
you only want the obvious categories.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `--mature` | flag | off | Sends `mature_content=true`. |
| `--json` | flag | off | Print DA's raw body instead of summary lines. |

### Behaviour

Calls `/browse/toptopics`. There is no `--limit` and no `--offset`: DA
decides how many topics to return, and the result is not paginated. The
example title in the third column is truncated to 50 characters for
display; `--json` gives you the whole example deviation object, in the
same shape as a `search topic` result.

```console
$ da search toptopics
  digitalart                 Digital Art                example: Home
  fanart                     Fan Art                    example: FLEE PUNY MORTALS FLEEEEE-
  photography                Photography                example: Yellow Line
  fantasyart                 Fantasy                    example: Canal Town
  anime                      Anime and Manga            example: 5 year anniversary Identity V
  cosplay                    Cosplay                    example: Girl (Le Petit Prince) #3
  adoptable                  Adoptables                 example: close | thank you
```

## `da search tag-suggest`

```text
usage: da search tag-suggest [-h] [--json] prefix
```

Autocompletes a tag prefix. Because tag matching is exact, this is the
command that saves you from searching a tag nobody uses: type the first
few letters, see what really exists, then feed the winner to
`da search tag`.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `prefix` | positional string | required | The prefix to complete. URL-encoded for you. |
| `--json` | flag | off | Print DA's raw body instead of one tag per line. |

### Behaviour

Calls `/browse/tags/search`. There is no `--limit` and no `--mature`
flag — DA returns ten suggestions and the request carries no
`mature_content` parameter at all, the only browse command here that
does not. Suggestions are ranked by DA, not alphabetically, and the
prefix match is on the whole tag string.

```console
$ da search tag-suggest nat
  naturephotography
  naturallight
  naturelandscape
  naturebeautiful
  natasharomanoff
  naturalbodymagic
  naturalbreasts
  naturephotograph
  natsukohiragi
  nativeamerican
```

The JSON body is a plain list of one-key objects:

```console
$ da search tag-suggest zzz --json | head -12
{
  "results": [
    {
      "tag_name": "zzzfanart"
    },
    {
      "tag_name": "zzzeroillustration"
    },
    {
      "tag_name": "zzz_fanart"
    },
    {
```

## `da search user`

```text
usage: da search user [-h] query [query ...]
```

Resolves one or more usernames to DeviantArt user records. Use it to
confirm the exact spelling and capitalisation of a username before
handing it to [`da sync artist`](sync.md), and to check whether an
account is a regular user or a group.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `query` | one or more positional strings | required | Usernames to resolve. Each becomes one `usernames[]` field in the request body. |
| `--json` | flag | off | Emit the raw DeviantArt payload instead of summary lines. |

### Behaviour

This is the odd one out. It is the only command on the page that does
not hit a `/browse/` endpoint — it POSTs to `/user/whois`, because that
endpoint rejects GET with HTTP 400 — and the only one with no `--limit`
and no `--mature`. The request hardcodes `mature_content=true`, so a
`--mature` flag would have nothing to do.
Being a `/user/` endpoint, it needs a token authorised for it; a token
that is not gets HTTP 401 back, which surfaces as a traceback and exit
`1` rather than a message.

Each record DA returns prints as one line: `@` and the username as DA
spells it, the user's UUID in parentheses, and `type=` followed by the
account type DA reports (`regular` for an ordinary account). The CLI
prints nothing for a name DA has no record of, and the exit code is
still `0` — so if you pass three names and get two lines, the third did
not resolve. The command needs credentials, so no transcript is shown
here.

```bash
da search user spyed devart
```

## `da daily`

```text
usage: da daily [-h] [--mature] [date]
```

Prints DeviantArt's Daily Deviation picks — the staff-selected
deviations for a given day. With no argument you get today's; with a
date you get that day's, which makes it the one command here that can
look backwards in time.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `date` | optional positional string | today, per DA | `YYYY-MM-DD`. Passed to DA untouched apart from URL-encoding; any other format is rejected server-side. |
| `--mature` | flag | off | Sends `mature_content=true`. |
| `--json` | flag | off | Emit the raw DeviantArt payload instead of summary lines. |

### Behaviour

Calls `/browse/dailydeviations`. Rows are formatted exactly like
`search tag` output. There is no `--limit` and no `--offset`: you get
however many picks DA made that day — seventeen on
the day this page was written — and only the human-readable form.

A malformed date is not validated locally. A day-first date such as
`15-01-2026` is sent to DA as-is, comes back 400, and ends in a
traceback with exit `1`.

```console
$ da daily 2026-01-15 | head -5
  FairyCastel              Floral Dreams                                      https://www.deviantart.com/fairycastel/art/Floral-Dreams-1210031742
  Devilishfours            Pixel art Portrait commission [OPEN]               https://www.deviantart.com/devilishfours/art/Pixel-art-Portrait-commission-OPEN-1261236105
  ForonZia                 [Close] - The Bird of Paradise                     https://www.deviantart.com/foronzia/art/Close-The-Bird-of-Paradise-1270280304
  stevanbg                 1157 - December 2025 Fog                           https://www.deviantart.com/stevanbg/art/1157-December-2025-Fog-1274302937
  gerenholm                Good Morning Oak                                   https://www.deviantart.com/gerenholm/art/Good-Morning-Oak-1285029947
```

## Retired: `da search popular` and `da search newest`

```text
usage: da search popular [-h] [--limit LIMIT] [--mature] [--json]
usage: da search newest [-h] [--limit LIMIT] [--mature] [--json]
```

DeviantArt retired `/browse/popular` and `/browse/newest`; every variant
of both now answers HTTP 404 with `"Api endpoint not found."` even for a
token that works fine against `/browse/topic` and
`/browse/dailydeviations` in the same second. Rather than let that
surface as a confusing 404, both subcommands were reduced to a stub that
explains the situation and points at the replacement.

Both still parse `--limit`, `--mature` and `--json`. All three are
accepted and then ignored — the handler never looks at them, and never
makes a network request.

```console
$ da search popular
[error] `search popular` is unavailable — DA retired /browse/popular.
[error] use `da search topic <name>` (curated topics like digitalart / nature / animals), `da search tag <tag>`, or `da daily` instead.
$ echo $?
2
```

```console
$ da search newest --limit 5
[error] `search newest` is unavailable — DA retired /browse/newest.
[error] use `da search tag <tag>` for tag-anchored browsing, or `da search topic <name>`.
$ echo $?
2
```

Take the second hint with a pinch of salt: `search tag` is the right
replacement, but its results are not in publication order, so it is not
a like-for-like substitute for a "newest" feed. If you want genuinely
recent work from artists you care about, the watch feed is the thing
that is walked newest-first — see [syncing art](sync.md).

Both messages go to stderr, so a script that captures stdout sees an
empty result and a non-zero status rather than an explanation.

### Exit codes

`0` and `2` are the norm across the CLI; these two are the exception in
that they *always* exit `2`, whatever arguments you pass. Anything that
still calls `da search popular` on a schedule will fail every run, which
is the intent — see [troubleshooting](../guides/troubleshooting.md) if
one of your own scripts started failing this way.

The retired names are still listed in the subcommand line printed by
`da search --help`. So are `tag` and `user`, which have no help text of
their own, so only four of the eight subcommands get a description
there; this page and the [command reference](../reference/cli.md) are
the complete lists.
