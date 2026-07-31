"""Terminal output, plus the small pure helpers that have no other home.

Extracted verbatim from the single-file module; see ADR 0007.

``_OUTPUT_STATE`` stays a mutable dict rather than module-level
booleans: tests mutate it in place, so it works identically whether a
caller reaches it as ``dacli._OUTPUT_STATE`` or ``dacli.output._OUTPUT_STATE``.
"""

import contextlib
import os
import re
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------------------
# Logging + small helpers
# --------------------------------------------------------------------------
# Output state — single mutable dict so the global-statement lint
# (PLW0603) doesn't fire and tests have one obvious seam to patch.
# `_OUTPUT_STATE["verbosity"]` is "quiet" / "info" / "debug";
# `_OUTPUT_STATE["color"]` is True / False.
_OUTPUT_STATE: dict[str, object] = {
    "verbosity": "info",
    "color": sys.stdout.isatty() and "NO_COLOR" not in os.environ,
}


def log(msg: str, level: str = "info") -> None:
    """Print to stdout (info) or stderr (warn/error) with flush=True.

    flush=True matters under `nohup ... > log 2>&1 &`: stdout redirected
    to a file is full-buffered by default (4-8 KB chunks), which makes
    a long-running sync look frozen until the buffer fills or the
    process exits. Forcing a flush on every line keeps background logs
    real-time.

    Verbosity is governed by ``_OUTPUT_STATE["verbosity"]`` (set via
    ``--quiet`` / ``--verbose`` / defaults). Levels:

    * ``"quiet"`` — only ``error`` and explicit ``warn`` from the
      sync loop survive. Cron / launchd-friendly.
    * ``"info"`` (default) — everything.
    * ``"debug"`` — includes ``log(msg, "debug")`` calls that are
      normally filtered out.

    Color is governed by ``_OUTPUT_STATE["color"]`` (set via
    ``--color`` / ``NO_COLOR``). When enabled, warn prints yellow and
    error prints red on a TTY. Disabled by default; respect
    https://no-color.org/ env var.
    """
    if _OUTPUT_STATE["verbosity"] == "quiet" and level == "info":
        return
    if level == "debug" and _OUTPUT_STATE["verbosity"] != "debug":
        return
    color = bool(_OUTPUT_STATE["color"])
    if level == "error":
        out = f"\033[31m[error] {msg}\033[0m" if color else f"[error] {msg}"
        print(out, file=sys.stderr, flush=True)
    elif level == "warn":
        out = f"\033[33m[warn]  {msg}\033[0m" if color else f"[warn]  {msg}"
        print(out, file=sys.stderr, flush=True)
    elif level == "debug":
        # stderr, not stdout. Debug lines are diagnostics ABOUT the run,
        # never part of its output — and net.py emits one per request, so
        # on stdout they landed in front of the JSON body:
        #
        #     $ da -v search tag cats --json | jq .
        #     parse error: Invalid numeric literal at line 1
        #
        # docs/reference/scripting.md recommends `da diagnose --json > f`
        # and _fail_with_context tells the operator to "re-run with -v",
        # so the two pieces of advice used to contradict each other.
        print(f"[debug] {msg}", file=sys.stderr, flush=True)
    else:
        print(msg, flush=True)


def _configure_output(*, quiet: bool, verbose: bool, color: str) -> None:
    """Translate the global CLI flags into the module-level state used by ``log``.

    Called once from ``main()`` after argparse. Kept as a separate
    function so tests can drive it directly without invoking the full CLI.
    """
    if quiet:
        _OUTPUT_STATE["verbosity"] = "quiet"
    elif verbose:
        _OUTPUT_STATE["verbosity"] = "debug"
    else:
        _OUTPUT_STATE["verbosity"] = "info"
    if color == "never":
        _OUTPUT_STATE["color"] = False
    elif color == "always":
        _OUTPUT_STATE["color"] = True
    else:  # "auto"
        _OUTPUT_STATE["color"] = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def safe_filename(s: str | None, n: int = 100) -> str:
    """Sanitise an arbitrary string into a filesystem-safe form.

    Collapses runs of non-alphanumeric chars to a single underscore,
    strips leading/trailing underscores, truncates to ``n`` chars. Empty
    or all-unsafe input returns ``"untitled"``. Used for both artist
    and title folder names.
    """
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", s or "untitled")[:n].strip("_")
    # "." and ".." survive the character class but are path traversal:
    # a deviation titled ".." would resolve its folder to the parent and
    # write into the destination root. Remote titles are attacker-
    # influenced content, so reject dot-only names outright.
    if not out or set(out) == {"."}:
        return "untitled"
    # A leading "-" makes the directory name an OPTION to every un-"--"-
    # terminated shell tool a user might run over their gallery: tar,
    # rsync, find, rm. A deviation titled "-rf" or "--exclude" is the same
    # attacker-influenced content the ".." guard above already reasons
    # about, so it gets the same treatment rather than being left to
    # whoever writes the next one-liner.
    return out.lstrip("-") or "untitled"


# A secret must be several times longer than the 8 characters the
# first-4/last-4 form reveals before that form is safe. At 9 characters it
# showed 8 of 9 — 89% of the secret — which is not masking.
_MASK_MIN_LEN = 24


def mask_secret(v: str | None) -> str | None:
    """Mask a secret for display: ``XXXX...XXXX``, or ``*****`` if short.

    Empty and None pass through unchanged.

    The threshold is on the length of the secret, not on whether the
    substrings fit. The old bound was ``len(v) <= 8``, so a 9-character
    secret rendered as ``SSSS...SSSS`` and revealed 8 of its 9
    characters. A real DeviantArt client_secret is 32 hex characters, so
    nothing in practice was exposed — but the function is generic, the
    failure was silent, and the docstring said "shorter than 8" while the
    code tested ``<= 8``.
    """
    if not v:
        return v
    if len(v) < _MASK_MIN_LEN:
        return "*****"
    return f"{v[:4]}...{v[-4:]}"


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    """Write `content` to `path` via tmp-file → chmod → os.replace.

    Crash-safe: a SIGKILL mid-write leaves a stale `.tmp` file rather
    than a partial `path`. `mode` is applied to the tmp before the
    rename, so the final file appears atomically with the requested
    permissions (no window where it's world-readable).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique tmp per writer, not a fixed "<name>.tmp". The sync lock
    # covers sync-vs-sync only, while any authed command (whoami, search,
    # auth status) can refresh a token and land here at the same moment.
    # Two writers sharing one tmp path interleave: the first rename
    # publishes the second's half-written payload, and the second rename
    # raises FileNotFoundError because the first already moved the file.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        # mkstemp already created it 0600 and empty. Apply the caller's
        # mode before any content lands, so the payload (tokens,
        # client_secret) is never briefly readable by other local users,
        # and the final file appears atomically with the right bits.
        os.chmod(tmp, mode)
        # encoding is pinned, never left to the locale: description.json
        # is written with ensure_ascii=False, so a CJK deviation title is
        # real UTF-8 bytes. Under a non-UTF-8 locale the default would
        # raise UnicodeEncodeError and lose the deviation.
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        # A named tmp that no longer collides is also one nothing will
        # ever clean up, so remove it on the way out. replace() having
        # succeeded means it is already gone, hence missing_ok.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
