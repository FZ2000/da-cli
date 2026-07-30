"""CLI subprocess integration tests against live DeviantArt.

Invokes the actual ``da`` binary via ``subprocess.run`` against the
installed shim with a tmp ``HOME`` that mirrors the developer's real
config + state, so the real files are never touched.

Run with::

    pytest -m integration_authenticated

**Read-only.** Never invokes ``da sync``, ``da config set``,
``da auth``, or ``da auth logout``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.integration_authenticated]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_da(
    args: list[str], env: dict[str, str], timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    """Invoke the ``da`` shim via the same Python interpreter pytest
    is running under. Returns the completed process."""
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "da"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=timeout,
        check=False,
    )


class TestWhoamiCommand:
    def test_whoami_succeeds(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["whoami"], cli_environment)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "token: valid (placebo OK)" in r.stdout


class TestDiagnoseCommand:
    def test_diagnose_reports_refresh_token_ttl(self, cli_environment: dict[str, str]) -> None:
        """``da diagnose`` must include the refresh_token TTL finding.
        This is the single most important operator-facing signal for
        the 90-day re-auth cadence."""
        r = _run_da(["diagnose"], cli_environment)
        assert r.returncode in (0, 1, 2), f"unexpected exit: {r.stderr}"
        combined = r.stdout + r.stderr
        assert "refresh_token chain" in combined


class TestSearchTopicCommand:
    def test_search_topic_returns_json(self, cli_environment: dict[str, str]) -> None:
        """``da search topic Fan Art --json`` hits the real DA topic
        endpoint. If the JSON parses and contains results, the search
        path works end-to-end against the live API."""
        r = _run_da(
            ["search", "topic", "Fan Art", "--limit", "3", "--json"],
            cli_environment,
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"
        parsed = json.loads(r.stdout)
        assert "results" in parsed
        assert isinstance(parsed["results"], list)


class TestDailyCommand:
    def test_daily_today_returns_output(self, cli_environment: dict[str, str]) -> None:
        """``da daily`` (no date means today) hits
        ``/browse/dailydeviations``. Non-empty stdout means the
        daily-picks path works."""
        r = _run_da(["daily"], cli_environment)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert len(r.stdout) > 50, "expected non-empty daily-deviation output"
