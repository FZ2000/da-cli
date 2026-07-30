"""`da -v <cmd> --json` must still emit parseable JSON, against the real API.

The unit-level check in ``tests/test_stream_discipline.py`` pins the seam
— ``log(..., "debug")`` writes to stderr — but only a real API command
produces the ``GET https://...`` line that used to corrupt the payload.
The two commands that need no credentials cannot show it: ``bench``
silences ``log`` in ``--json`` mode and ``diagnose`` makes no HTTP calls.

Opt in with ``-m integration``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.integration


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "da"), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
    )


class TestVerboseDoesNotCorruptJson:
    def test_search_tag_json_parses_at_verbose(self, cli_environment):
        """The exact command that failed: stdout began with `GET https://`."""
        result = _run(["-v", "search", "tag", "nature", "--limit", "2", "--json"], cli_environment)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert "results" in payload

    def test_the_request_line_is_on_stderr(self, cli_environment):
        """And it is still emitted — that is the point of -v."""
        result = _run(["-v", "search", "tag", "nature", "--limit", "2", "--json"], cli_environment)

        assert "GET https://www.deviantart.com" in result.stderr
        assert "GET https://" not in result.stdout
