# Configuration commands

`da config` is how you tell `da` which OAuth app to use and where to put
the art it downloads, and how you check what it thinks those answers are
right now. Reach for it when you are setting the tool up, when you are
rotating a client secret, or when a command is behaving as though it has
credentials you do not recognise. This page documents the five
subcommands; for the full list of settable keys with their types and
defaults, see [configuration](../reference/configuration.md).

## At a glance

| Command | What it does |
| --- | --- |
| `da config show` | Print the whole effective configuration, secrets masked, plus the paths in use. |
| `da config path` | Print the config and state file paths and nothing else. |
| `da config set` | Write one key — secrets go to the macOS Keychain, everything else to `config.json`. |
| `da config get` | Print one key's value. Exits 1 when the key is not set. |
| `da config unset` | Remove one key from the Keychain and from `config.json`. |

## Where a value comes from

Every command that needs a setting resolves it in this order, highest
first:

1. **A command-line flag** on the command being run — `da sync feed
   --concurrency 8`, `da auth --redirect-uri ...`.
2. **An environment variable.** Five exist: `DA_CLIENT_ID`,
   `DA_CLIENT_SECRET`, `DA_DESTINATION`, `DA_REDIRECT_URI` and
   `DA_JITTER`. No other config key can be set from the environment. See
   [environment variables](../reference/environment-variables.md).
3. **The macOS Keychain**, for secrets only. The only secret is
   `client_secret`; it is stored under service `da-cli`, account
   `client_secret`.
4. **`config.json`**, at `$XDG_CONFIG_HOME/da-cli/config.json` or
   `~/.config/da-cli/config.json`, mode 0600.

The Keychain now outranks `config.json`. That ordering is newer than the
tool: `load_config()` used to skip the Keychain entirely whenever
`config.json` already held a value for the same key, which inverted the
documented order and made rotation fail silently — `da config set
client_secret NEW` wrote the Keychain, a stale copy left behind in
`config.json` kept winning, and every request went out with the secret
you thought you had just replaced. If you have an old `config.json` with
a `client_secret` in it, the first `da config set client_secret` will
delete it for you.

`da config` itself only ever reads and writes the config sources. It
never touches `state.json`, so nothing here logs you out or discards a
sync checkpoint — that is [`da auth logout`](auth.md).

## config show

```text
usage: da config show [-h]
```

Prints the effective configuration as JSON with secrets masked, followed
by the paths `da` is using. This is the merged view — file plus
environment plus Keychain — not the contents of `config.json`, so it is
the right command for answering "what will the next `da sync` actually
use". It takes no options, so there is no parameter table.

### Behaviour

The JSON object is the same dictionary every other command receives from
`load_config()`. Keys appear in insertion order: whatever was in
`config.json` first, then any key introduced by an environment variable,
then `client_secret` if it came from the Keychain. Nothing in the output
tells you which source a value came from — if a value surprises you,
check your environment before you edit the file.

Masking is `first four + "..." + last four`. A value of eight characters
or fewer is replaced entirely by `*****`, so short values never leak a
usable fraction of themselves. Only keys in `SECRET_KEYS` are masked,
and that set currently contains `client_secret` alone; anything else you
have stored, including a `client_id`, prints in full.

The final `keychain:` line is printed only on macOS. On Linux and
Windows it is omitted because there is no Keychain to describe.

A `config.json` that is not valid JSON, or that parses to something
other than an object (`[]`, `"x"`, `42`), produces a warning on stderr
and is then ignored — `show` continues with an empty file and still
prints the environment and Keychain values. A `config.json` that exists
but cannot be read at all, for example mode `000`, raises
`PermissionError`, which `main()` catches as an `OSError`: one line
naming the path, and exit 2.

`show` writes nothing and creates nothing, including the directories it
prints.

### Example

```console
$ da config show
{
  "client_id": "12345",
  "destination": "~/Pictures/DA",
  "client_secret": "4f8c...3b5d"
}

config file: /tmp/da-demo/config/da-cli/config.json
state file:  /tmp/da-demo/state/da-cli/state.json
keychain:    service="da-cli" (used for ['client_secret'])
```

With a damaged config file:

```console
$ da config show
[warn]  /tmp/da-demo/config/da-cli/config.json: invalid JSON (Expecting property name enclosed in double quotes: line 1 column 3 (char 2)); ignoring
{}

config file: /tmp/da-demo/config/da-cli/config.json
state file:  /tmp/da-demo/state/da-cli/state.json
keychain:    service="da-cli" (used for ['client_secret'])
```

## config path

```text
usage: da config path [-h]
```

Prints the two paths and exits. Use it when you want to `cat`, back up
or delete the files, or to confirm that an `XDG_CONFIG_HOME` override or
a `--config` flag took effect. It takes no options.

### Behaviour

Both paths are printed whether or not the files exist; `path` never
creates a directory or a file. The config path is
`$XDG_CONFIG_HOME/da-cli/config.json`, falling back to
`~/.config/da-cli/config.json`; the state path is
`$XDG_STATE_HOME/da-cli/state.json`, falling back to
`~/.local/state/da-cli/state.json`. Both environment variables are read
once at import, so exporting them in the same shell before running `da`
is enough.

The global `--config PATH` flag repoints the config file for the whole
invocation, including for `set` and `unset`. It does **not** move the
state file, which is what makes multi-account setups slightly awkward:
two config files share one set of tokens unless you also point
`XDG_STATE_HOME` somewhere else.

The transcripts on this page were produced with `XDG_CONFIG_HOME` and
`XDG_STATE_HOME` pointed at a scratch directory, which is why the paths
below are not the usual `~/.config` and `~/.local/state`.

### Example

```console
$ da config path
config: /tmp/da-demo/config/da-cli/config.json
state:  /tmp/da-demo/state/da-cli/state.json
```

`--config` moves the first line only:

```console
$ da --config /tmp/da-demo/other.json config path
config: /tmp/da-demo/other.json
state:  /tmp/da-demo/state/da-cli/state.json
```

## config set

```text
usage: da config set [-h] key value
```

Writes one key. Secrets go to the macOS Keychain when one is available
and to `config.json` otherwise; everything else always goes to
`config.json`. This is the command you use during setup and whenever you
rotate the OAuth secret.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `key` | str (positional) | *(required)* | The config key to write. Not validated — see below. |
| `value` | str (positional) | *(required)* | The value to store. Always stored as a JSON string. |

### Behaviour

For `client_secret` — the only member of `SECRET_KEYS` — `set` first
tries `security add-generic-password -s da-cli -a client_secret -w <value> -U`. On
success it prints a confirmation and then removes any older copy of the
same key from `config.json`, printing a second line if it found one.
That cleanup exists because the Keychain now wins on read: a leftover
would be dead weight, but it would also be a secret sitting in a file
you believe no longer holds one.

If the Keychain write fails, or you are not on macOS, or `/usr/bin/security`
is missing, the secret falls back to `config.json` and `set` warns. The
file is written mode 0600, but it is a plaintext secret on disk — the
warning exists so that a Linux user does not assume the macOS guarantees
apply to them.

Non-secret keys are written straight to `config.json`. The write is
atomic (temp file, `chmod 0600`, `os.replace`), and the parent directory
is created if it does not exist, so `da config set` works on a machine
that has never run `da` before.

Three things about `set` are worth knowing before you trust it:

- **Key names are not validated.** `da config set favourite_colour blue`
  succeeds and stores the key. A mistyped setting name is accepted in
  silence and the real setting stays unset, so if something you set has
  no effect, check with [`config show`](#config-show) — the misspelled
  key will be sitting there next to the one you meant.

- **Values are not validated or converted.** Everything is stored as a
  string. Numeric keys such as `concurrency`, `delay_api`, `delay_image`
  and `jitter` are converted at use time, so `da config set delay_api
  fast` writes happily and breaks the next sync rather than this
  command.
- **`~` is not expanded.** `destination` is stored exactly as you typed
  it; the sync and diagnose commands expand it when they use it. This is
  why `config show` prints `~/Pictures/DA` rather than an absolute path.

One failure mode is less friendly than it should be. If `config.json`
contains unparsable JSON, `set` treats it as empty and overwrites the
file with just the key you passed — everything else in it is lost, with
no warning. A file containing valid JSON that is *not* an object is
handled better: `set` warns
`expected a JSON object; replacing it` and continues, exit 0. Either way
the file is worth inspecting by hand first; `config show` will tell you
which case you are in.

Finally, the value is a command-line argument. It appears in your shell
history and, for the moment the process runs, in `ps`. For a secret you
care about, prefer a leading space (if your shell is configured to skip
those) or supply it through `DA_CLIENT_SECRET` in a controlled
environment such as CI.

### Examples

Setting the two non-secret keys, then the secret:

```console
$ da config set client_id 12345
stored client_id in /tmp/da-demo/config/da-cli/config.json
$ da config set destination '~/Pictures/DA'
stored destination in /tmp/da-demo/config/da-cli/config.json
$ da config set client_secret <your-client-secret>
stored client_secret in macOS Keychain (service=da-cli)
```

Rotating a secret that had previously landed in `config.json`, on a
machine where the Keychain is available again. The second line is the
cleanup:

```console
$ da config set client_secret <your-client-secret>
stored client_secret in macOS Keychain (service=da-cli)
removed the older client_secret from /tmp/da-demo/config/da-cli/config.json
```

The fallback, as a Linux user sees it every time:

```console
$ da config set client_secret <your-client-secret>
[warn]  stored client_secret in /tmp/da-demo/config/da-cli/config.json (Keychain unavailable). Permissions are 0600 — the file is still secret-bearing; avoid syncing this directory to cloud storage.
```

### Exit codes

0 on success, 2 on a rejected value. `set` validates the numeric keys
before writing, so a bad value is caught rather than written and left to
break the next sync:

```console
$ da config set jitter 40%
[error] jitter must be a number — got '40%'
$ echo $?
2
```

`delay_api`, `delay_image` and `jitter` must parse as numbers;
`concurrency` and `time_budget` must be whole numbers. Values are stored
as given and coerced on read, so `3` and `3.0` both work. An unwritable
config directory raises `PermissionError`, which `main()` catches as an
`OSError` — also 2.

## config get

```text
usage: da config get [-h] [--unmask] key
```

Prints one value, with secrets masked unless you ask otherwise. It reads
the same merged configuration as `show`, so it answers "what is in
effect" rather than "what is in the file", and it prints the bare value
with no label, which makes it usable in `$(...)`.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `key` | str (positional) | *(required)* | The config key to read. |
| `--unmask` | flag | off | Print the secret in plaintext — to stderr, not stdout. |

### Behaviour

A key that is absent, or present with an empty string as its value,
counts as not set: `get` writes `(not set: <key>)` to stderr and exits 1.
An empty string is a value you can create — `da config set note ''` stores
`"note": ""` — but you can never read it back.

Masking is the same rule as `show`: first four, ellipsis, last four,
with anything eight characters or shorter collapsed to `*****`.

`--unmask` prints the raw secret **to stderr**. This is deliberate.
`da config get client_secret --unmask > cfg.txt` therefore captures
nothing, and the secret lands in your terminal instead of in a file you
may later commit or paste. If you genuinely want the value in a
variable, redirect explicitly: `secret=$(da config get client_secret
--unmask 2>&1 >/dev/null)`.

For a key that is not a secret, `--unmask` is accepted and has no
effect; the value goes to stdout either way.

### Examples

```console
$ da config get client_id
12345
$ da config get client_secret
4f8c...3b5d
$ da config get client_secret --unmask
4f8c2a1b9d3e7c5a6b0f2d4e8a1c3b5d
```

The unmasked value is on stderr, which you can see by discarding it:

```console
$ da config get client_secret --unmask 2>/dev/null
$
```

An unset key, with the exit code:

```console
$ da config get delay_api
[warn]  (not set: delay_api)
$ echo $?
1
```

Environment variables win, and `get` shows it:

```console
$ DA_CLIENT_ID=999 da config get client_id
999
```

### Exit codes

0 when the key has a value, **1 when it does not**. That is an answer,
not a failure — the command did its job. Reserve your error handling for
2, which `get` does not produce. See
[exit codes](../reference/exit-codes.md).

## config unset

```text
usage: da config unset [-h] key
```

Removes a key from every store `da config set` could have written it to.
Use it to clear a stale value, or before handing a machine to someone
else.

| Flag | Type | Default | What it does |
| --- | --- | --- | --- |
| `key` | str (positional) | *(required)* | The config key to remove. |

### Behaviour

`unset` tries both stores in turn and reports each removal separately.
On macOS, and only for keys in `SECRET_KEYS`, it runs
`security delete-generic-password -s da-cli -a <key>`. Then, for any
key, it rewrites `config.json` without it — atomically, at mode 0600. If
a secret existed in both places you will see two lines, and both copies
are gone.

If nothing was removed anywhere, `unset` warns `(nothing to unset:
<key>)` and still exits 0. It is idempotent: running it twice is
harmless, and the second run is the "nothing to unset" case.

Two things it does not do. It does not touch the environment, so a key
supplied by `DA_CLIENT_SECRET` survives an `unset` and will still show
up in `config show` — the fix there is your shell, not `da`. And it does
not touch `state.json`: unsetting `client_secret` does not log you out,
and an existing refresh token keeps working until it expires or you run
[`da auth logout`](auth.md).

A corrupt `config.json` is treated as empty, so `unset` reports nothing
to unset and leaves the damaged file exactly as it found it. Unlike
`set`, it will not overwrite it.

### Examples

Removing a secret that lives in both stores:

```console
$ da config unset client_secret
removed client_secret from macOS Keychain
removed client_secret from /tmp/da-demo/config/da-cli/config.json
```

Running it again:

```console
$ da config unset client_secret
[warn]  (nothing to unset: client_secret)
$ echo $?
0
```

A key that only exists in the environment cannot be unset:

```console
$ DA_CLIENT_ID=999 da config unset client_id
[warn]  (nothing to unset: client_id)
$ DA_CLIENT_ID=999 da config get client_id
999
```

### Exit codes

Always 0, including when there was nothing to remove.

## See also

- [Configuration reference](../reference/configuration.md) — every key,
  its type, its default and which flag overrides it.
- [Environment variables](../reference/environment-variables.md) — the
  four credential variables and the XDG paths.
- [Security model](../explanation/security.md) — what da-cli promises
  about your secret, and what it does not.
- [Files on disk](../reference/files-on-disk.md) — everything `da`
  writes, and how to remove it.
- [Authentication commands](auth.md) — `da auth`, `da auth logout`,
  `da whoami`, which use the values you set here.
