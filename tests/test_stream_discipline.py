"""stdout carries output; stderr carries everything about the run.

Two defects that both broke scripting, and both had the CLI advising one
thing while doing another.

``log()`` sent ``debug`` to **stdout**, and ``net.py`` emits one debug
line per request. So ``da -v ... --json`` put ``GET https://...`` in front
of the JSON body: ``docs/reference/scripting.md`` recommends
``da diagnose --json > file`` followed by ``json.load``, and
``_fail_with_context`` tells the operator to "re-run with -v to see the
request that failed" — following both at once produced output
nothing could parse.

``BrokenPipeError`` is a ``ConnectionError`` is an ``OSError``, so a
downstream ``head`` closing the pipe reached the transport advice and
claimed "The connection to DeviantArt was interrupted... nothing has been
lost" — including from ``da bench``, which makes no network calls. Worse,
the process exited **120** (CPython's "flushing std files failed"),
outside the documented 0/1/2/130 contract, so a wrapper doing
``case $? in 0|1|2)`` fell through.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str], home: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "da"), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "cfg"),
            "XDG_STATE_HOME": str(home / "state"),
            "NO_COLOR": "1",
            **extra,
        },
        timeout=120,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    for sub in ("cfg", "state"):
        (tmp_path / sub).mkdir()
    return tmp_path


class TestDebugGoesToStderr:
    def test_a_debug_line_does_not_land_in_a_json_payload(self, home, monkeypatch, capsys):
        """The corruption itself, at the seam where it happened.

        Driven through log() rather than a subprocess because the two
        no-credential commands that emit --json cannot show it: `bench`
        replaces log() with a no-op in --json mode, and `diagnose` makes
        no HTTP calls, so neither produces a debug line to misplace. A
        subprocess test using them passes against the bug — mine did.

        The end-to-end case is covered live in
        tests/integration/test_verbose_json_live.py.
        """
        import dacli

        monkeypatch.setitem(dacli._OUTPUT_STATE, "verbosity", "debug")
        monkeypatch.setitem(dacli._OUTPUT_STATE, "color", False)

        dacli.log("GET https://www.deviantart.com/api/v1/oauth2/browse/tags?tag=cats", "debug")
        print(json.dumps({"results": [], "has_more": False}, indent=2))

        captured = capsys.readouterr()
        json.loads(captured.out)  # the assertion: stdout is the payload, alone
        assert "GET https://" in captured.err

    def test_debug_lines_are_on_stderr(self, home):
        """And they must still be *emitted* — silencing them would also
        make the test above pass, while removing the only reason to use -v.
        """
        result = _run(["-v", "bench", "--pages", "2", "--per-page", "4"], home)

        assert "[debug]" in result.stderr, "no debug output at -v"
        assert "[debug]" not in result.stdout

    def test_without_verbose_there_are_no_debug_lines(self, home):
        """The control for the control."""
        result = _run(["bench", "--pages", "2", "--per-page", "4"], home)
        assert "[debug]" not in result.stderr
        assert "[debug]" not in result.stdout

    def test_a_failure_report_is_not_split_across_streams(self, home):
        """`-v` also puts the traceback next to the message that needs it.

        _fail_with_context logs one error line to stderr and the traceback
        at debug level. With debug on stdout, `da -v ... > out 2> err` put
        the message in one file and its evidence in the other.
        """
        result = _run(["-v", "sync", "feed"], home)  # no config -> handled failure

        assert result.returncode == 2
        assert "[error]" in result.stderr
        assert "Traceback" not in result.stdout, "evidence landed in the data stream"


class TestABrokenPipeIsNotAnOutage:
    def _pipe_to_head(self, home: Path) -> tuple[int, str]:
        """Run `da bench | head -1` and return the WRITER's exit status."""
        writer = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "da"),
                "bench",
                "--pages",
                "200",
                "--per-page",
                "24",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / "cfg"),
                "XDG_STATE_HOME": str(home / "state"),
                "NO_COLOR": "1",
            },
        )
        assert writer.stdout is not None
        writer.stdout.readline()
        writer.stdout.close()  # what `head` does when it has enough
        _out, err = writer.communicate(timeout=120)
        return writer.returncode, err

    def test_the_writer_exits_within_the_documented_contract(self, home):
        """120 is not one of 0/1/2/130.

        It is CPython's "flushing std files failed", which overrides
        whatever sys.exit was given — so a wrapper matching the documented
        codes fell through entirely.
        """
        code, _err = self._pipe_to_head(home)

        assert code in (0, 1, 2, 130), (
            f"exit {code} is outside the documented contract in docs/reference/exit-codes.md"
        )
        assert code == 0, f"a consumer closing the pipe is not a failure, got {code}"

    def test_it_does_not_blame_deviantart(self, home):
        """`da bench` makes no network calls at all.

        The message said "The connection to DeviantArt was interrupted...
        The next run resumes where this one stopped; nothing has been
        lost" — three claims, none of them true here.
        """
        _code, err = self._pipe_to_head(home)

        assert "DeviantArt" not in err, f"blamed the network for a closed pipe:\n{err}"
        assert "BrokenPipeError" not in err, "the traceback leaked to the user"

    def test_nothing_is_printed_about_it_at_all(self, home):
        """A consumer leaving is normal, documented usage — see the `head`
        examples in docs/commands/search.md. It warrants no output.
        """
        _code, err = self._pipe_to_head(home)
        assert "[error]" not in err
