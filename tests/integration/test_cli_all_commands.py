"""CLI smoke test — invokes every subcommand via subprocess and asserts
exit code + non-empty output.

This is the "wiring is correct" integration test. It proves that the
argparse parser dispatches to the right handler and that handler can
make a real API call without crashing. Each test is a single
``subprocess.run`` invocation against the installed ``da`` shim with a
tmp HOME (so the developer's real files are never touched).

Run with::

    pytest -m integration_authenticated

Exit-code policy:
  0 = success
  2 = expected failure (deprecated commands, missing config)
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
    is running under."""
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "da"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=timeout,
        check=False,
    )


class TestReadCommandsSucceed:
    """Every read-only command should exit 0 against live DA."""

    def test_whoami(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["whoami"], cli_environment)
        assert r.returncode == 0, r.stderr

    def test_refresh(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["refresh"], cli_environment)
        assert r.returncode == 0, r.stderr

    def test_auth_status(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["auth", "status"], cli_environment)
        assert r.returncode in (0, 1), r.stderr  # 0=ok, 1=warn
        payload = json.loads(r.stdout)
        assert "days_remaining" in payload

    def test_config_show(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["config", "show"], cli_environment)
        assert r.returncode == 0, r.stderr
        assert "config file:" in r.stdout

    def test_config_path(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["config", "path"], cli_environment)
        assert r.returncode == 0, r.stderr
        assert "config:" in r.stdout

    def test_diagnose(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["diagnose"], cli_environment)
        assert r.returncode in (0, 1, 2), r.stderr
        assert len(r.stdout) > 50

    def test_diagnose_json(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["diagnose", "--json"], cli_environment)
        assert r.returncode in (0, 1, 2), r.stderr
        payload = json.loads(r.stdout)
        assert "findings" in payload
        assert "exit_code" in payload

    def test_bench_json(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(
            ["bench", "--pages", "2", "--per-page", "3", "--json"],
            cli_environment,
        )
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert payload["indexed"] == 6

    def test_index_show(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["index", "show"], cli_environment)
        # Exit 0 if index exists, or exit 0 with a "no index yet" warning.
        assert r.returncode == 0, r.stderr

    def test_search_tag(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["search", "tag", "nature", "--limit", "3"], cli_environment)
        assert r.returncode == 0, r.stderr

    def test_search_tag_json(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(
            ["search", "tag", "nature", "--limit", "3", "--json"],
            cli_environment,
        )
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert "results" in payload

    def test_search_topics(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["search", "topics", "--limit", "3"], cli_environment)
        assert r.returncode == 0, r.stderr

    def test_search_toptopics(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["search", "toptopics"], cli_environment)
        assert r.returncode == 0, r.stderr

    def test_search_tag_suggest(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["search", "tag-suggest", "fan"], cli_environment)
        assert r.returncode == 0, r.stderr

    def test_search_user(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["search", "user", "deviantart"], cli_environment)
        assert r.returncode == 0, r.stderr

    def test_daily_today(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["daily"], cli_environment)
        assert r.returncode == 0, r.stderr

    def test_user_profile(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["user", "profile", "deviantart"], cli_environment)
        assert r.returncode == 0, r.stderr

    def test_watch_list(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["watch", "list", "--limit", "3"], cli_environment)
        # Exit 0 if token has user scope; exit 2 if browse-only.
        assert r.returncode in (0, 2), r.stderr

    def test_search_topic(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["search", "topic", "Fan Art", "--limit", "3"], cli_environment)
        assert r.returncode == 0, r.stderr


class TestDeprecatedCommandsExit2:
    """Deprecated subcommands should exit 2 with an actionable message."""

    def test_search_popular_exits_2(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["search", "popular"], cli_environment)
        assert r.returncode == 2
        assert (
            "retired" in (r.stdout + r.stderr).lower()
            or "unavailable" in (r.stdout + r.stderr).lower()
        )

    def test_search_newest_exits_2(self, cli_environment: dict[str, str]) -> None:
        r = _run_da(["search", "newest"], cli_environment)
        assert r.returncode == 2
        assert (
            "retired" in (r.stdout + r.stderr).lower()
            or "unavailable" in (r.stdout + r.stderr).lower()
        )
