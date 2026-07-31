"""Cross-process command lock, and the last-sync summary it guards.

Extracted verbatim from the single-file module; see ADR 0007.

``STATE_DIR``, ``load_state`` and ``save_state`` are read through the
``dacli`` package at call time rather than imported. For ``STATE_DIR`` that
is required for correctness: the
test suite reassigns it (``dacli.STATE_DIR = tmp_path``) to keep lock
files and sync summaries out of the developer's real state directory.
An import here would bind the real path at import time and defeat that.
"""

import contextlib
import fcntl
import os
import time
from collections.abc import Generator

import dacli

from .constants import TOKEN_LOCK_POLL_S, TOKEN_LOCK_TIMEOUT_S
from .errors import DacliError


# --------------------------------------------------------------------------
# Cross-process command lock
#
# Protects against the scenario where the user kicks off a manual sync at
# the same moment the launchd 03:00 fire happens — both processes would
# walk the same feed and race on state.json writes. Using `fcntl.flock`
# (POSIX advisory lock; macOS-supported) we try to acquire an exclusive
# lock on a per-command sentinel file. If another process holds it, we
# log an info message and exit cleanly — the holder will catch up the
# work the second invocation would have done.
#
# Why advisory (not mandatory) locks: macOS supports both flock(2) and
# fcntl-style locks; flock is simpler, doesn't require write access to
# the locked region, and propagates correctly across fork(). The lock
# is on a SENTINEL file under STATE_DIR (e.g. `.sync.lock`); we never
# write data to that file, only acquire/release on its fd.
# --------------------------------------------------------------------------
class CommandLockedError(DacliError):
    """Raised when an exclusive command lock is already held by another process."""


@contextlib.contextmanager
def _cmd_lock(name: str) -> Generator[None, None, None]:
    """Try to acquire a non-blocking exclusive lock on `~/.local/state/da-cli/.{name}.lock`.

    Raises:
        CommandLockedError: if another process holds the lock.

    Releases on context exit. Used by `cmd_sync_*` to prevent overlapping
    launchd + manual runs.
    """
    dacli.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = dacli.STATE_DIR / f".{name}.lock"
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        os.close(fd)
        raise CommandLockedError(
            f"another `da {name}` is already running (lock held: {lock_path})"
        ) from e
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _token_lock(timeout: float = TOKEN_LOCK_TIMEOUT_S) -> Generator[None, None, None]:
    """Serialize refresh-token exchanges across processes.

    Blocking, unlike ``_cmd_lock``. A second sync can be skipped — the
    holder does the same work — but a second *refresh* cannot: the caller
    needs a token to continue, so it waits and then finds the token the
    holder just fetched.

    Why this exists: DeviantArt rotates the refresh token on every use, so
    two processes exchanging the same one means the loser presents a token
    DA has already consumed and gets 400 invalid_grant. That is not
    hypothetical — ``examples/diagnose-cron.sh`` runs `da diagnose` every
    ten minutes alongside the nightly `da sync feed`, and diagnose forces
    a refresh whenever the access token has expired.

    On timeout it proceeds *unlocked* rather than failing. A stuck holder
    should degrade this back to the old racy behaviour, not make the CLI
    unusable — and the re-read under the lock means the common case is
    already handled by then.
    """
    dacli.STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = dacli.STATE_DIR / ".token.lock"
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    dacli.log(
                        "waited for another da process to finish refreshing; "
                        "continuing without the lock",
                        "warn",
                    )
                    break
                time.sleep(TOKEN_LOCK_POLL_S)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _record_sync_summary(
    kind: str,
    started_at: float,
    totals: dict[str, int],
    stop_reason: str,
    **extra: object,
) -> None:
    """Persist a structured summary of the just-finished sync into state.

    Read by `da diagnose` to answer "what happened in the last run?".
    Atomic via save_state's tmp-file → rename. Survives across runs.
    Crash-safe: if the process dies mid-sync, last_sync simply reflects
    the prior run; the new run will overwrite on its next clean exit.
    """
    state = dacli.load_state()
    state["last_sync"] = {
        "kind": kind,
        "started_at": int(started_at),
        "ended_at": int(time.time()),
        "duration_s": round(time.time() - started_at, 1),
        "totals": dict(totals),
        "stop_reason": stop_reason,
        **extra,
    }
    dacli.save_state(state)
