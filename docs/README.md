# da-cli documentation

`da` is a command-line tool that syncs the galleries of the DeviantArt
artists you watch to a folder on your disk, and lets you search and
browse DeviantArt from the terminal.

## Which page do I want?

| I want to… | Page |
| --- | --- |
| install it and get my first download working | [Getting started](getting-started.md) |
| understand one command properly | [Command guides](commands/README.md) |
| look up a command or a flag | [Command reference](reference/cli.md) |
| fix an error I just got | [Troubleshooting](guides/troubleshooting.md) |
| download new art automatically every day | [Scheduling](guides/scheduling.md) |
| change a setting | [Configuration](reference/configuration.md) |
| find where files are written, or uninstall | [Files on disk](reference/files-on-disk.md) |
| wrap `da` in a cron job or monitor it | [Scripting](reference/scripting.md) |
| know what an exit code means | [Exit codes](reference/exit-codes.md) |
| set something via the environment | [Environment variables](reference/environment-variables.md) |
| know what da-cli does with my OAuth secret | [Security model](explanation/security.md) |
| run the tests | [Testing](guides/testing.md) |
| understand why it is built this way | [Decision records](explanation/adr/README.md) |

## How this is organised

Four kinds of page, because readers arrive with four different kinds
of question:

- **[Getting started](getting-started.md)** — a tutorial. Follow it
  start to finish, once, and you will have art on disk. It assumes no
  prior command-line experience.
- **[Command guides](commands/README.md)** (`commands/`) — one page per
  group of commands, documenting every flag, what it writes and how it
  behaves on a second run. Read these when you know what you want and
  need the whole picture of one area:
  [authentication](commands/auth.md), [syncing](commands/sync.md),
  [searching](commands/search.md), [inspecting](commands/inspect.md),
  [configuration](commands/config.md),
  [index and health](commands/maintenance.md).
- **Guides** (`guides/`) — how to accomplish one specific thing you
  already know you want: [scheduling](guides/scheduling.md),
  [troubleshooting](guides/troubleshooting.md), [testing](guides/testing.md).
- **Reference** (`reference/`) — look-up material, complete and dry.
  [Every command and flag](reference/cli.md) is generated from the parser,
  so it is exhaustive but says nothing about behaviour; the command
  guides above are where the behaviour lives. Also:
  [every setting](reference/configuration.md),
  [what gets written where](reference/files-on-disk.md),
  [environment variables](reference/environment-variables.md),
  [exit codes](reference/exit-codes.md), and
  [scripting](reference/scripting.md) for unattended use.
- **Explanation** (`explanation/`) — background you read to understand
  rather than to do: the [security model](explanation/security.md) and
  the [decision records](explanation/adr/README.md).

## Elsewhere in the repository

- [README](../README.md) — what da-cli is, and the 30-second tour
- [CONTRIBUTING](../CONTRIBUTING.md) — the bar for a pull request
- [ARCHITECTURE](../ARCHITECTURE.md) — a map of the package for contributors
- [AGENTS.md](../AGENTS.md) — reference for AI coding agents driving `da`
- [CHANGELOG](../CHANGELOG.md) — what changed in each release
