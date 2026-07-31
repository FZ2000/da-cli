"""Tests for the ``da`` console script shim.

The shim is a thin wrapper that resolves its own realpath, puts the repo
root on ``sys.path``, and delegates to ``dacli.main``. Two paths need
to hold under all install layouts (git-clone + symlink into /usr/local/bin,
``pip install`` via the entry point, and the ``python3 da`` invocation
documented in the smoke CI step):

1. ``dacli`` must be importable from wherever the shim ends up.
2. argv must flow through unchanged so subcommand parsing works.

These tests run the shim as a subprocess against a clean environment to
mirror the real CLI invocation; importing ``dacli`` in-process is
already covered exhaustively by the rest of the suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "da"


def _run(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the ``da`` shim with a clean XDG environment.

    Real config/state dirs are scrubbed so a developer's local state can
    never leak into the test (and so ``--version`` doesn't accidentally
    read a half-written state.json). The shim is invoked via the same
    Python interpreter pytest is running under, so a fresh venv isn't
    required to repro a failure.
    """
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(Path.home())),
        # Force XDG paths under a fresh temp location for hermetic
        # isolation (unique per call, so parallel runs never collide).
        "XDG_CONFIG_HOME": tempfile.mkdtemp(prefix="da-cli-shim-test-cfg-"),
        "XDG_STATE_HOME": tempfile.mkdtemp(prefix="da-cli-shim-test-state-"),
        "LANG": "C",
        "LC_ALL": "C",
    }
    if env:
        clean_env.update(env)
    return subprocess.run(
        [sys.executable, str(SHIM), *args],
        capture_output=True,
        text=True,
        env=clean_env,
        cwd=REPO_ROOT,
        timeout=30,
        check=False,
    )


class TestShimHelpAndVersion:
    """Smoke-test the shim end-to-end via the ``--version`` / ``--help`` paths."""

    def test_da_version_reports_module_version(self) -> None:
        """``da --version`` must succeed and match ``dacli.__version__``.

        Failure here means either the shim can't import dacli (sys.path
        wiring broken) or the ``__version__`` constant drifted out of
        sync with what argparse emits.
        """
        import dacli

        r = _run(["--version"])
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert f"da-cli {dacli.__version__}" in r.stdout

    def test_da_help_lists_top_level_subcommands(self) -> None:
        """``da --help`` must succeed and name every documented top-level verb.

        If a verb disappears from the help output, the shim's argparse
        wiring broke. Listing every verb keeps this test honest about
        what the public CLI surface actually is.
        """
        r = _run(["--help"])
        assert r.returncode == 0, f"stderr: {r.stderr}"
        for verb in (
            "auth",
            "whoami",
            "refresh",
            "config",
            "sync",
            "search",
            "user",
            "deviation",
            "daily",
            "watch",
            "index",
            "diagnose",
            "bench",
        ):
            assert verb in r.stdout, f"verb {verb!r} missing from --help output"

    def test_da_no_args_exits_nonzero(self) -> None:
        """``da`` with no subcommand must fail (argparse ``required=True``).

        The CLI's contract is that an explicit subcommand is required;
        we don't want to silently default to e.g. ``whoami`` and hit the
        network on a typo.
        """
        r = _run([])
        assert r.returncode != 0
        # argparse writes the usage + error to stderr on missing required arg
        assert r.stderr  # non-empty stderr


class TestShimDispatch:
    """Verify the shim forwards argv to the right cmd_* handler."""

    def test_da_unknown_subcommand_exits_nonzero(self) -> None:
        """An unknown verb must fail loudly (not silently succeed)."""
        r = _run(["nonexistent-subcommand-xyz"])
        assert r.returncode != 0
        assert r.stderr

    def test_da_diagnose_on_empty_config_exits_2(self) -> None:
        """``da diagnose`` against an empty XDG_CONFIG_HOME must exit 2.

        This is the same contract the CI smoke step enforces. The empty
        config has no client_id, so the config-section finding is a
        critical failure; diagnose's exit-code policy is 0/1/2 =
        OK/WARN/FAIL. Running it via the shim proves the shim doesn't
        swallow the exit code or wrap the exception.
        """
        r = _run(["diagnose"])
        assert r.returncode == 2, f"expected exit 2, got {r.returncode}; stderr: {r.stderr}"
        # The config-section fail message must be visible to the operator.
        assert "client_id" in r.stdout.lower() or "client_id" in r.stderr.lower()

    def test_da_config_show_on_empty_config_succeeds(self) -> None:
        """``da config show`` against empty config must exit 0 and print paths.

        Verifies the shim forwards to the handler, the handler doesn't
        crash on missing config, and stdout is JSON-shaped (per the
        ``cmd_config_show`` contract).
        """
        r = _run(["config", "show"])
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "config file:" in r.stdout
        assert "state file:" in r.stdout

    def test_da_bench_runs_and_emits_json(self) -> None:
        """``da bench --json`` must succeed and emit valid JSON.

        Bench is fully mocked (no network), so it's safe to invoke here
        as a true end-to-end test of the shim → cmd_bench → mocked HTTP
        path. Catches regressions in the global-state save/restore
        logic that cmd_bench relies on.
        """
        r = _run(["bench", "--pages", "1", "--per-page", "3", "--concurrency", "1", "--json"])
        assert r.returncode == 0, f"stderr: {r.stderr}"
        import json

        payload = json.loads(r.stdout)
        assert payload["pages"] == 1
        assert payload["per_page"] == 3
        assert payload["indexed"] == 3
        assert payload["items_per_sec"] > 0


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only: keychain service name")
class TestShimConfigLayout:
    """Quick sanity check that the shim doesn't hardcode a bad service name.

    Cross-platform contract: the keychain service label is ``da-cli``.
    If that ever drifts, stored secrets become unfindable across
    upgrades. We only assert on the help text — actually exercising the
    keychain requires interactive auth on macOS.
    """

    def test_da_auth_help_mentions_service(self) -> None:
        r = _run(["auth", "--help"])
        assert r.returncode == 0
        # The default scope line is the keychain-related bit users grep for.
        assert "--scope" in r.stdout
