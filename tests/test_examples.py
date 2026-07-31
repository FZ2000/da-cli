"""The shipped example scripts have to actually run.

``examples/`` is documentation-grade code — people copy-paste it, and
``browse-curated-topics.sh`` shipped with ``--limit 50`` against an endpoint
DeviantArt caps at 24. Every run died on its first topic with HTTP 400,
and under ``set -euo pipefail`` that ended the script. The in-file comment
("--limit 50 keeps each topic's wall-time bounded") shows it was written
but never run.

Nothing here reaches the network: the caps come from
``docs/commands/search.md``, which CI already keeps honest against the
parser, so this ties the examples to the same source of truth.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((REPO_ROOT / "examples").glob("*.sh"))
# The installers were NOT covered here, and that is how a dead
# install_schedule.sh shipped: it called xml_escape nine lines above the
# function's definition, so bash exited 127 at line 49 and every path
# below it — the bundle build, the plist, launchctl load, and the whole
# uninstall subcommand — was unreachable. `bash -n` passes on that,
# because it does not resolve function order.
INSTALLERS = sorted(p for p in REPO_ROOT.glob("install*.sh"))
ALL_SCRIPTS = EXAMPLES + INSTALLERS
SEARCH_DOC = REPO_ROOT / "docs" / "commands" / "search.md"


def _documented_caps() -> dict[str, int]:
    """{"search topic": 24, ...} from the limits table in the guide."""
    caps: dict[str, int] = {}
    for line in SEARCH_DOC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*`(search [a-z]+)`\s*\|\s*(\d+)\s*\|", line)
        if m:
            caps[m.group(1)] = int(m.group(2))
    return caps


def test_the_caps_table_was_found():
    """Guard the guard: a renamed table must not silently disable this."""
    caps = _documented_caps()
    assert caps, f"no limit table parsed from {SEARCH_DOC}"
    assert "search topic" in caps


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_example_is_valid_bash(script: Path):
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"{script.name} is not valid bash:\n{result.stderr}"


@pytest.mark.parametrize("script", EXAMPLES, ids=lambda p: p.name)
def test_example_limits_are_within_the_documented_caps(script: Path):
    """`da search <kind> ... --limit N` must be a limit DA will accept.

    One over the cap is HTTP 400, which for these scripts means the whole
    run ends on its first iteration.
    """
    caps = _documented_caps()
    text = script.read_text(encoding="utf-8")
    for match in re.finditer(r"da\s+(search\s+[a-z]+)\b[^\n|]*?--limit\s+(\d+)", text):
        command, limit = match.group(1), int(match.group(2))
        cap = caps.get(command)
        if cap is None:
            continue
        assert limit <= cap, (
            f"{script.name}: `{command} --limit {limit}` exceeds DA's cap of "
            f"{cap} — the request returns HTTP 400"
        )


@pytest.mark.parametrize("script", EXAMPLES, ids=lambda p: p.name)
def test_example_propagates_failure(script: Path):
    """A wrapper script must not report success when `da` failed.

    post-sync-webhook.sh captured the sync's exit code and then exited
    with curl's. Its own header tells the reader to run it in place of
    `da sync feed`, so a scheduler watching the exit code saw every
    failed sync as a success, as long as the webhook was reachable.
    """
    text = script.read_text(encoding="utf-8")
    if "da sync" not in text and "da diagnose" not in text and "da search" not in text:
        pytest.skip("not a wrapper around a da command")
    assert re.search(r"exit\s+\"?\$", text) or "set -e" in text, (
        f"{script.name} neither exits with a captured status nor uses set -e"
    )


class TestWebhookWrapperExitCodes:
    """Run the wrapper against a stub `da` and check what it reports."""

    SCRIPT = REPO_ROOT / "examples" / "post-sync-webhook.sh"

    def _run(self, tmp_path: Path, diagnose_stdout: str, diagnose_rc: int) -> int:
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "da"
        stub.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  sync) exit 2 ;;\n"
            f"  diagnose) printf '%s' '{diagnose_stdout}'; exit {diagnose_rc} ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        result = subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
            # Inherit PATH rather than hardcoding one: the script shells
            # out to python3, which lives in /usr/local/bin on the
            # official Docker images and /opt/homebrew/bin on this
            # machine. A fixed "/usr/bin:/bin" made the script exit 127
            # on Linux, which looked like the assertion failing.
            env={
                "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
                "HOME": str(tmp_path),
            },
        )
        return result.returncode

    def test_a_failed_sync_is_reported_as_failed(self, tmp_path):
        rc = self._run(tmp_path, '{"overall":{"status":"FAIL"},"findings":[]}', 2)
        assert rc == 2, f"a failed sync reported {rc}"

    def test_it_survives_diagnose_emitting_non_json(self, tmp_path):
        """The one case it must not die in: `da` itself is broken.

        Building the payload with a strict json.loads raised under
        `set -e`, so the script died before firing the webhook — silencing
        the alert in exactly the situation it exists to report.
        """
        rc = self._run(tmp_path, "Traceback: ImportError", 1)
        assert rc == 2, f"expected the sync's status, got {rc}"


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: p.name)
def test_every_function_is_defined_before_it_is_used(script: Path):
    """Bash resolves functions at execution time, not parse time.

    A call above its own `func() {` is a runtime "command not found",
    which under `set -e` kills the script — and `bash -n` does not catch
    it, which is exactly how a dead install_schedule.sh shipped. (This is
    shellcheck's SC2218; shellcheck is not a dependency here, so the one
    rule that matters is enforced directly.)
    """
    text = script.read_text(encoding="utf-8")
    definitions = {
        m.group(1): i
        for i, line in enumerate(text.splitlines())
        if (m := re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", line))
    }
    if not definitions:
        pytest.skip("no shell functions defined")

    problems = []
    for lineno, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for name, defined_at in definitions.items():
            if lineno >= defined_at:
                continue
            # A call, not the definition and not a mention in prose: the
            # name at a command position, or inside $( ).
            if re.search(rf"(^|[;&|(]|\$\()\s*{re.escape(name)}\s", line):
                problems.append(
                    f"line {lineno + 1} calls {name}(), defined at line {defined_at + 1}"
                )
    assert not problems, f"{script.name}: " + "; ".join(problems)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="install_schedule.sh exits 1 on non-Darwin by design (launchctl, .app, TCC)",
)
class TestTheInstallerRuns:
    """install_schedule.sh shipped completely dead; nothing executed it.

    macOS-only, because the script's own first act is to refuse to run
    anywhere else. The define-before-use check above is static text
    analysis and does run everywhere — it is the one that catches the
    defect these were written for, so Linux CI keeps that guard even
    though it cannot execute the script.
    """

    SCRIPT = REPO_ROOT / "install_schedule.sh"

    def _run(self, args: list[str], home: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "HOME": str(home)},
            timeout=60,
        )

    @pytest.fixture
    def fake_home(self, tmp_path: Path) -> Path:
        for sub in ("Library/LaunchAgents", "Library/Logs", "Applications"):
            (tmp_path / sub).mkdir(parents=True)
        return tmp_path

    def test_uninstall_runs_cleanly_on_a_fresh_machine(self, fake_home):
        """`uninstall` with nothing installed must succeed, not exit 127.

        Chosen as the executable path because it does no `launchctl load`
        — running the full install would register a real job in the
        developer's own launchd session pointing at a temp directory.
        """
        result = self._run(["uninstall"], fake_home)

        assert result.returncode == 0, f"exit {result.returncode}\n{result.stdout}{result.stderr}"
        assert "command not found" not in result.stderr

    def test_a_zero_padded_minute_is_accepted(self, fake_home):
        """DA_MINUTE=08 was read as octal; the range check silently
        errored and the script died later in printf.
        """
        result = subprocess.run(
            ["bash", str(self.SCRIPT), "uninstall"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "HOME": str(fake_home), "DA_MINUTE": "08", "DA_HOUR": "09"},
            timeout=60,
        )
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"
        assert "value too great for base" not in result.stderr

    def test_an_out_of_range_minute_is_still_rejected(self, fake_home):
        """The control for the base-10 fix: 99 must still fail."""
        result = subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "HOME": str(fake_home), "DA_MINUTE": "99"},
            timeout=60,
        )
        assert result.returncode != 0
        assert "out of range" in result.stderr
