# Command guides

One page per group of commands, each documenting every flag, what the
command writes, how it behaves on a second run, and worked examples.

These are the pages to read when you know roughly what you want and need
the full picture of one area. For a flat list of every command and flag,
generated from the parser, see the
[command reference](../reference/cli.md).

| Page | Commands | What it is for |
| --- | --- | --- |
| [Authentication](auth.md) | `da auth`, `auth logout`, `auth status`, `whoami`, `refresh` | Logging in once, and keeping the token alive |
| [Syncing art](sync.md) | `da sync feed`, `sync artist`, `sync watched` | Getting art onto your disk, and keeping it current |
| [Searching and browsing](search.md) | `da search tag`, `da search topic`, `da daily` | Finding art and artists without downloading anything |
| [Inspecting users and deviations](inspect.md) | `da user profile`, `da deviation show`, `da watch list` | Looking up the id or username a sync command needs |
| [Configuration commands](config.md) | `da config show`, `da config set`, `da config get` | Reading and changing settings and secrets |
| [Index, health and benchmarking](maintenance.md) | `da index show`, `da index rebuild`, `da diagnose`, `da bench` | Checking that everything still works, and repairing it |

## Where to start

If you have not run `da auth` yet, start with
[getting started](../getting-started.md) instead — it walks the whole
first-time setup in order, and takes about ten minutes.

Once you are set up, the two pages that matter most are
[syncing art](sync.md), which is what the tool exists to do, and
[index, health and benchmarking](maintenance.md), which is how you find
out whether a scheduled run is quietly failing.

## Conventions on these pages

Defaults in the parameter tables are the real ones, taken from the
parser. Where a default is a named constant the constant is given too,
so you can find it in `dacli/constants.py`.

Every command exits `0` on success and `2` when it could not do its job.
The handful that also use `1` say so in their own section; the full table
is in [exit codes](../reference/exit-codes.md).

Output shown in `console` blocks is real output from the command, not an
illustration.
