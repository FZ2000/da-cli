"""Four independent ways the CLI mishandled unusual-but-real conditions.

Each was found by asking what happens at a boundary the happy path never
visits: an environment variable that is set but empty, two unlocked
writers landing on one temp filename, an interrupt arriving mid-page, and
a retry that picks a different file extension than the attempt before it.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import dacli

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestAnEmptyXDGVariableMeansUnset:
    """`XDG_STATE_HOME=` is unset per the spec, not a relative path.

    ``os.environ.get(name, default)`` returns ``""`` as a perfectly good
    value, so an exported-but-empty variable produced the *relative* path
    ``da-cli/``. State, the index and the sync lock then fragmented per
    working directory — and two syncs started from different directories
    ran concurrently against one destination, which is exactly what the
    lock exists to prevent.
    """

    @pytest.mark.parametrize("var", ["XDG_STATE_HOME", "XDG_CONFIG_HOME"])
    def test_empty_value_falls_back_to_an_absolute_path(self, var, tmp_path):
        probe = (
            f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); import dacli; "
            "print(dacli.STATE_PATH.is_absolute(), dacli.CONFIG_PATH.is_absolute())"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, var: "", "HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "True True", (
            f"{var}='' produced a relative path: {result.stdout}{result.stderr}"
        )

    def test_a_real_value_is_still_honoured(self, tmp_path):
        """The control: a set variable must still be used."""
        probe = (
            f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
            "import dacli; print(dacli.STATE_PATH)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "XDG_STATE_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path)},
        )
        assert str(tmp_path / "xdg" / "da-cli" / "state.json") == result.stdout.strip()


class TestConcurrentAtomicWrites:
    """Two writers must not collide on one staging file.

    The sync lock covers sync-vs-sync only; any authed command (whoami,
    search, auth status) can refresh a token and reach ``_atomic_write``
    at the same moment. With a fixed ``<name>.tmp`` the two interleave:
    one rename publishes the other's half-written payload, and the second
    rename raises FileNotFoundError because the file already moved.
    """

    def test_parallel_writers_all_produce_valid_content(self, tmp_path):
        target = tmp_path / "state.json"
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def writer(n: int) -> None:
            payload = json.dumps({"writer": n, "padding": "x" * 4096})
            try:
                barrier.wait(timeout=10)
                for _ in range(20):
                    dacli._atomic_write(target, payload, 0o600)
            except BaseException as exc:
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(writer, range(8)))

        assert not errors, f"a concurrent writer failed: {errors[:3]}"
        # Whoever wrote last, the file must be one writer's payload in
        # full — never a splice of two.
        final = json.loads(target.read_text(encoding="utf-8"))
        assert final["padding"] == "x" * 4096

    def test_no_staging_files_are_left_behind(self, tmp_path):
        target = tmp_path / "state.json"
        for i in range(5):
            dacli._atomic_write(target, json.dumps({"n": i}), 0o600)
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "state.json"]
        assert leftovers == [], f"staging files left behind: {leftovers}"

    def test_a_failed_write_cleans_up_its_staging_file(self, tmp_path, monkeypatch):
        """A unique tmp name is also one nothing else will ever collect."""
        target = tmp_path / "state.json"
        real_write_text = Path.write_text

        def boom(self, *a, **kw):
            if self.suffix == ".tmp":
                raise OSError(28, "No space left on device")
            return real_write_text(self, *a, **kw)

        monkeypatch.setattr(Path, "write_text", boom)
        with pytest.raises(OSError, match="No space left"):
            dacli._atomic_write(target, "{}", 0o600)

        assert list(tmp_path.iterdir()) == [], "the staging file outlived the failure"

    def test_the_file_keeps_its_requested_mode(self, tmp_path):
        """0600 for secrets, 0644 for description.json — both must hold."""
        secret = tmp_path / "state.json"
        public = tmp_path / "description.json"
        dacli._atomic_write(secret, "{}", 0o600)
        dacli._atomic_write(public, "{}", 0o644)
        assert oct(secret.stat().st_mode)[-3:] == "600"
        assert oct(public.stat().st_mode)[-3:] == "644"


class TestInterruptCancelsQueuedDownloads:
    PAGE_SIZE = 40

    WORKERS = 2
    # Long enough that the pool cannot drain the page while the main
    # thread is still noticing the interrupt; short enough that the
    # cancelled case finishes immediately.
    BLOCK_S = 0.5

    def _run_page_interrupted(self, tmp_path, monkeypatch) -> int:
        """Interrupt a page mid-flight; return how many items were worked.

        The count is of ``_save_one`` *invocations*, which is what
        cancellation changes. Counting downloads would prove nothing: the
        interrupting stub returns before fetching either way.

        Determinism is the whole difficulty, and the first version of this
        test got it wrong. It raised on the *third* invocation and let the
        others return instantly — but ``as_completed`` yields whichever
        future finishes first, so the two workers could burn through all
        40 items before the main thread ever observed the raising one. It
        passed on macOS and Linux/3.10 by luck and failed on Linux/3.14,
        which is exactly the shape of a test that measures scheduling
        rather than behaviour.

        So: the FIRST invocation raises, and every other one blocks. With
        two workers that pins the state precisely — one raises, one is
        occupied, the remaining 38 are still queued and therefore
        cancellable. The block is bounded rather than released by the
        test, because the executor's ``__exit__`` waits for running
        workers and a test that had to unblock them would deadlock.
        """
        monkeypatch.setattr(dacli, "INDEX_PATH", tmp_path / "index.db")
        dest = tmp_path / "dest"
        dest.mkdir()
        page = [
            {
                "deviationid": f"D-{i}",
                "title": f"T{i}",
                "author": {"username": "alice"},
                "content": {"src": f"https://cdn.example/{i}.png"},
            }
            for i in range(self.PAGE_SIZE)
        ]

        worked: list[int] = []
        lock = threading.Lock()
        never_set = threading.Event()

        def counting_save_one(*_args: object, **_kwargs: object):
            with lock:
                worked.append(1)
                first = len(worked) == 1
            if first:
                # Stands in for the user's Ctrl-C: a BaseException from a
                # worker propagates out of fut.result() in the main loop.
                raise KeyboardInterrupt
            # Occupy this worker long enough that the queue cannot drain
            # behind our back. Never actually set; the wait times out.
            never_set.wait(self.BLOCK_S)
            return ("ok", "alice", "T", 5)

        monkeypatch.setattr(dacli.sync, "_save_one", counting_save_one)

        with pytest.raises(KeyboardInterrupt):
            dacli.sync._save_page_concurrent(
                page,
                {},
                dest,
                fallback_artist=None,
                image_delay=0,
                jitter_pct=0,
                concurrency=self.WORKERS,
            )
        return len(worked)

    def test_pending_work_is_cancelled(self, tmp_path, monkeypatch):
        """Ctrl-C during a page must not work through the rest of it.

        Every task is submitted up front, and the executor's ``__exit__``
        waits for all of them without cancelling — so an interrupt was
        followed by the whole remaining page. On 50 items at concurrency 4
        with the default 1.5s image delay that is another ~19 seconds.
        """
        worked = self._run_page_interrupted(tmp_path, monkeypatch)

        # Bounded by the pool, not "some number below 40" — a slow machine
        # could satisfy that by accident. WORKERS + 1 because the raising
        # invocation frees its own worker instantly, so that worker picks
        # up one more item before the main thread observes the exception
        # and cancels: one raiser plus WORKERS occupied. Measured at
        # exactly 3 with 2 workers, stably.
        assert worked <= self.WORKERS + 1, (
            f"the interrupt cancelled nothing: {worked} of {self.PAGE_SIZE} items were "
            f"worked, with only {self.WORKERS} workers running"
        )


class TestStalePartFilesAreSweptUp:
    def test_a_part_from_a_different_extension_is_removed(self, tmp_path, monkeypatch):
        """A retry can land on a different extension than the attempt before.

        The extension comes from the first one matched in the CDN URL, and
        wixmp URLs carry several (".../f/x.png/v1/fill/.../y.jpg"), so a
        crashed attempt can leave image.jpg.part beside a finished
        image.png forever — nothing else removes them, because both the
        dedup check and the rebuild only skip .part files.
        """
        monkeypatch.setattr(dacli, "INDEX_PATH", tmp_path / "index.db")
        monkeypatch.setattr(dacli, "http_bytes", lambda url, **kw: b"IMAGE-BYTES")
        dest = tmp_path / "dest"
        dest.mkdir()
        folder = dest / "alice" / "Sunset"
        folder.mkdir(parents=True)
        (folder / "image.jpg.part").write_bytes(b"half a download from last time")

        status, _a, _t, _s = dacli._save_one(
            {
                "deviationid": "D-1",
                "title": "Sunset",
                "author": {"username": "alice"},
                "content": {"src": "https://cdn.example/x.png"},
            },
            {},
            dest,
            image_delay=0,
        )

        assert status == "ok"
        assert (folder / "image.png").exists()
        assert not list(folder.glob("*.part")), (
            f"stale staging files remain: {[p.name for p in folder.glob('*.part')]}"
        )
