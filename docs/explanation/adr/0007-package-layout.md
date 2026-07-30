# 0007. Package layout

## Status

Accepted (2026-07-28). Binding.

## Context

da-cli spans several distinct concerns — paths, logging, config,
secrets, a SQLite index, an HTTP client, OAuth 2.1, a concurrent sync
engine, thirteen command handlers, and the argument parser. Held in one
namespace they were mutually reachable, so nothing recorded which parts
were meant to depend on which, and any documentation of the internal
structure had to be maintained by hand against line numbers.

The credential-handling code in particular needs boundaries a reader
can see: `auth.py` writes TLS private key material and `config.py`
touches the Keychain, and both should be auditable without reading the
sync engine.

## Decision

**`dacli` is a package of focused modules, each owning one concern.**

```text
dacli/__init__.py   command handlers + argparse wiring, and the
                    re-export surface that keeps `dacli.X` working
dacli/constants.py  paths and tunables
dacli/errors.py     exception hierarchy
dacli/output.py     logging, colour state, small pure helpers
dacli/config.py     config, state, secret storage
dacli/lock.py       cross-process command lock
dacli/index.py      the SQLite synced-deviation index
dacli/net.py        the HTTP layer
dacli/auth.py       OAuth 2.1 (PKCE) lifecycle
dacli/sync.py       the sync engine
```

Each module depends only on those above it in that list.

### `import dacli` is the public surface

`__init__` re-exports every name the modules define, so `dacli.X`
resolves regardless of which module X lives in. Callers and the test
suite address the package, never a submodule path — the suite reaches
for 70 distinct `dacli.*` attributes and patches 98 of those sites.

### The rule every module follows

A submodule reads through the package (`dacli.http_json`,
`dacli.CONFIG_PATH`) rather than importing the value, for any name the
test suite patches or `cmd_bench` swaps.

This is not stylistic. `from .net import http_json` binds the real
function at import time, so `patch.object(dacli, "http_json")` would no
longer intercept it — and the suite would hit the live DeviantArt API
and the developer's real `~/.config` **while still reporting green**.
The same applies to `CONFIG_PATH`, `STATE_DIR`, `LOOPBACK_CERT`, the
keychain helpers, and `log`.

Two module-level flags (`_STATE_CORRUPTION_WARNED`,
`_BOOTSTRAP_CHECKED_THIS_PROCESS`) are rebound by tests, so they live
on the package rather than as submodule globals; a `global` rebind
inside a submodule would be invisible to the fixture that resets them.

`_INDEX_CONN` is the deliberate exception: nothing outside `index.py`
touches it, so it stays a plain module global.

## Consequences

### Positive

- Each concern is readable on its own; the largest module is 848 lines.
- Dependencies between concerns are explicit imports rather than
  shared-namespace assumptions.
- `ARCHITECTURE.md` describes modules, so it cannot drift the way a
  line-number map does.

### Negative

- The package must not contain a module named after a stdlib module it
  imports. Enforced by `tests/test_package_layout.py`.
- The re-export surface in `__init__` is boilerplate that must be kept
  in step with the modules, and `F401` is suppressed there.
- Reading a patched name as `dacli.X` inside the package looks like
  private access to the linter; `SLF001` is suppressed per-module.
- Installation copies a directory rather than a file. `install.sh`
  mirrors it and removes the destination first, so a module deleted
  upstream does not linger.

### Neutral

- Still zero runtime dependencies. ADR 0001's stdlib-only rule is
  untouched and remains binding.
- Packaging changed from `py-modules` to `packages`; the version is
  still read from `dacli.__version__`.

## Alternatives considered

**One module.** Rejected: at several thousand lines the "read it in one
sitting" argument stops holding, and every concern can reach every
other.

**Develop as a package, ship a generated single file** (the SQLite
amalgamation pattern). Rejected: two representations of the same code,
stack traces pointing at generated line numbers, and a class of build
bug that otherwise does not exist.

**Address submodules directly** (`from dacli.net import http_json`)
instead of re-exporting. Rejected: it moves the patch surface into
module paths, so every caller and test binds to where a function
currently lives rather than to what it is.

## Verification

`tools/verify_refactor.sh` checks the package still behaves as one
module:

1. the test suite, unmodified
2. the public API surface — names, kinds, signatures
3. every `dacli.*` name the tests reach for still resolves
4. `docs/reference/cli.md` regenerated from `build_parser()` is
   byte-identical, covering all 29 commands and every flag, default,
   and help string in one check
5. isolation probes — patching `STATE_DIR` redirects lock files, and
   patching `http_json` intercepts callers
