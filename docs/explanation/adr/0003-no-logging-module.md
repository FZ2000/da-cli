# 0003. No `logging` module; custom `log()` helper

## Status

Accepted (2026-04-22). Periodically re-evaluated when structured
logging is requested.

## Context

Python's stdlib `logging` module is the conventional choice for any
non-trivial CLI. It supports levels, handlers, filters, structured
formatting, and integrates with `logging.exception()` for tracebacks.

For da-cli, the alternatives were:

- **stdlib `logging`** with a single `StreamHandler` to stderr.
- **Custom `log(msg, level)` helper** that wraps `print(..., flush=True)`.
- **`structlog`** (third-party) — rejected per ADR 0001.

## Decision

**Custom `log(msg, level="info"|"warn"|"error"|"debug")` helper,
not `logging`.**

## Consequences

**Positive:**

- Output is line-buffered with `flush=True` by default — critical for
  the launchd / cron use case where the log file is opened by the
  scheduler, not by an interactive shell. `logging` defaults to
  block-buffered when stderr is redirected, which makes a 540-second
  sync look frozen until exit. The CLI's `log()` force-flushes every
  line so the log file is real-time.
- The output format is dead-simple (`[error] msg` / `[warn]  msg` /
  plain `msg`), no module-name prefix, no timestamp clutter. The
  launchd log file is human-readable without parsing.
- No log-config surface — no `logging.yaml`, no `dictConfig`, no
  surprise "why isn't my level change taking effect" surprises.
- The `ruff` rule `TRY400` ("use `logging.exception`") is correctly
  disabled because we don't use `logging`.

**Negative:**

- We don't get `logging.exception()`'s automatic traceback capture.
  This matters maybe twice a year; the workaround is `log(f"...:
  {type(e).__name__}: {e}", "error")`.
- Structured logging (JSON output for log shippers) requires more
  work than `logging.basicConfig(format=...)`. We have it for
  `da diagnose --json` (the only structured surface that matters
  operationally); other commands stay human-readable.
- The CLI doesn't have a `--log-level=DEBUG` knob wired to `logging`;
  we now have `--verbose` / `--quiet` that toggle `_OUTPUT_STATE`
  directly. Functionally equivalent for the small surface we have.

## Alternatives considered

- **stdlib `logging`**: rejected for the buffer-flush issue alone —
  the launchd use case dominates the design and `logging` requires
  extra plumbing to flush every line.
- **`structlog`**: rejected per ADR 0001.
- **`print()` everywhere with no helper**: rejected — we'd lose the
  level distinction and the `flush=True` default.
