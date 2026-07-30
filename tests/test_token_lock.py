"""The token lock under contention — the branch its own tests never ran.

``_token_lock`` was added to stop two processes exchanging the same
single-use refresh token. Its *fast* path is covered by
``tests/test_refresh_race.py``, which spawns two real processes — but
those race on the uncontended path, so the wait loop, the timeout, and
the proceed-unlocked fallback were entirely unexercised.

The fallback is the branch that most deserves a test: on timeout it
deliberately continues **without** the lock, which is the racy behaviour
the lock exists to prevent. That is the right trade-off — a stuck holder
must not make the CLI unusable — but it is only right if it actually
warns and actually proceeds, and nothing checked either.

Contention is created with a real second process holding a real
``flock``, because that is the only thing the lock responds to; two
threads in one interpreter share the file descriptor's lock and would not
block each other.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import dacli
from dacli.lock import _token_lock

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def holder(tmp_path, monkeypatch):
    """A second process holding the token lock for as long as we let it.

    Returns a callable that starts the holder and waits until the lock is
    genuinely held, so tests never race the fixture itself.
    """
    monkeypatch.setattr(dacli, "STATE_DIR", tmp_path)
    started = tmp_path / "holding"
    procs: list[subprocess.Popen] = []

    def start(hold_for: float = 30.0) -> subprocess.Popen:
        script = textwrap.dedent(f"""
            import fcntl, os, pathlib, time
            lock = pathlib.Path({str(tmp_path)!r}) / ".token.lock"
            fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            pathlib.Path({str(started)!r}).write_text("held")
            time.sleep({hold_for})
        """)
        proc = subprocess.Popen([sys.executable, "-c", script])
        procs.append(proc)
        deadline = time.monotonic() + 10
        while not started.exists():
            if time.monotonic() > deadline:
                raise AssertionError("the holder never acquired the lock")
            time.sleep(0.01)
        return proc

    yield start
    for proc in procs:
        proc.kill()
        proc.wait(timeout=10)


class TestTheLockActuallyBlocks:
    def test_it_waits_while_another_process_holds_it(self, holder, tmp_path):
        """A short timeout must be spent waiting, not skipped.

        Pins that the wait loop runs at all: with a broken (non-blocking)
        implementation this returns immediately and the elapsed time is
        far below the timeout.
        """
        holder(hold_for=30.0)

        started = time.monotonic()
        with _token_lock(timeout=0.5):
            pass
        elapsed = time.monotonic() - started

        assert elapsed >= 0.4, f"did not wait for the holder (returned in {elapsed:.3f}s)"

    def test_it_acquires_as_soon_as_the_holder_exits(self, holder):
        """And it must not sit out the whole timeout once the lock frees.

        The other half of the same contract: a poll loop that only checked
        once, or slept for the full timeout regardless, would pass the
        test above and fail this one.
        """
        holder(hold_for=0.4)

        started = time.monotonic()
        with _token_lock(timeout=20.0):
            elapsed = time.monotonic() - started

        assert elapsed < 10.0, f"waited {elapsed:.1f}s after the holder had gone"
        assert elapsed >= 0.2, "the lock appeared free while it was still held"


class TestTheTimeoutFallback:
    def test_it_proceeds_unlocked_rather_than_failing(self, holder, capsys):
        """A stuck holder must not make the CLI unusable.

        This is the deliberate trade-off: past the timeout we continue
        without the lock, accepting the race, because refusing to run at
        all would be worse. Nothing tested that it actually proceeds.
        """
        holder(hold_for=30.0)

        entered = False
        with _token_lock(timeout=0.2):
            entered = True

        assert entered, "the body never ran — a stuck holder would block the CLI entirely"

    def test_the_fallback_says_so(self, holder, capsys):
        """Proceeding unlocked is worth one warning line.

        Silence here would make a rare, real race invisible in the logs of
        the scheduled runs where it matters.
        """
        holder(hold_for=30.0)

        with _token_lock(timeout=0.2):
            pass

        err = capsys.readouterr().err
        assert "without the lock" in err, f"no warning emitted; stderr was {err!r}"

    def test_no_warning_on_the_uncontended_path(self, tmp_path, monkeypatch, capsys):
        """The control: the common case must stay quiet.

        A warning on every refresh would train people to ignore it.
        """
        monkeypatch.setattr(dacli, "STATE_DIR", tmp_path)

        with _token_lock(timeout=0.2):
            pass

        assert "without the lock" not in capsys.readouterr().err


class TestTheLockIsAlwaysReleased:
    def test_released_after_a_normal_exit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dacli, "STATE_DIR", tmp_path)
        with _token_lock(timeout=1.0):
            pass
        # If the first acquire leaked, this second one would spend the
        # whole timeout and (per the fallback) proceed unlocked.
        started = time.monotonic()
        with _token_lock(timeout=5.0):
            pass
        assert time.monotonic() - started < 1.0, "the lock was not released"

    def test_released_after_an_exception_in_the_body(self, tmp_path, monkeypatch):
        """The refresh raises SystemExit on a rejected grant, so this path
        is the normal one on failure, not an edge case.
        """
        monkeypatch.setattr(dacli, "STATE_DIR", tmp_path)
        with pytest.raises(SystemExit), _token_lock(timeout=1.0):
            raise SystemExit(2)

        started = time.monotonic()
        with _token_lock(timeout=5.0):
            pass
        assert time.monotonic() - started < 1.0, "an exception leaked the lock"

    def test_it_does_not_leak_file_descriptors(self, tmp_path, monkeypatch):
        """One fd per refresh, unreleased, would exhaust a long sync."""
        monkeypatch.setattr(dacli, "STATE_DIR", tmp_path)
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        # A short timeout deliberately: there is no contention here, so
        # the only thing a longer one buys is a slow failure. With a
        # leaking lock every iteration would otherwise wait it out — 200
        # iterations at 1s made this take three and a half minutes to
        # report a bug it detects immediately.
        for _ in range(min(200, soft // 4)):
            with _token_lock(timeout=0.05):
                pass
        # Reaching here without OSError: EMFILE is the assertion.
        with _token_lock(timeout=1.0):
            pass


class TestItDoesNotContendWithTheSyncLock:
    def test_a_held_sync_lock_does_not_block_a_refresh(self, tmp_path, monkeypatch):
        """Different files, so a running sync must not delay a refresh.

        They are taken in the same process during a sync — `_cmd_lock`
        around the walk, `_token_lock` inside it — so sharing one lock
        file would self-deadlock the moment a token expired mid-sync.
        """
        monkeypatch.setattr(dacli, "STATE_DIR", tmp_path)
        from dacli.lock import _cmd_lock

        started = time.monotonic()
        with _cmd_lock("sync"), _token_lock(timeout=5.0):
            elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"the sync lock blocked the token lock for {elapsed:.1f}s"
        assert (tmp_path / ".sync.lock").exists()
        assert (tmp_path / ".token.lock").exists()
