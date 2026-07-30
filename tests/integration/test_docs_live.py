"""Run the documented examples against the live DeviantArt API.

`tests/test_docs_examples.py` executes every example that works without
credentials. The rest reach the network, and this is where they are
verified — nothing else proves that what the guides promise is what
DeviantArt actually returns.

Two credential paths, because they cover different commands:

* ``anonymous_token`` — a ``client_credentials`` grant. Covers every
  public endpoint: search, topics, daily deviations, public profiles,
  single deviations. No user account involved, so it works in CI.
* ``user_token`` — needs a live ``refresh_token``. Covers ``whoami`` and
  ``watch list``, which are about *your* account. Skips when absent.

Opt in with ``-m integration``. Skipped by default: these hit the real
API and are rate-limited.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


# Parsed here rather than imported: the hermetic runner that also parses
# these blocks is still being brought up, and the live layer should not
# wait on it.
def _live_examples() -> list[str]:
    import re

    commands: list[str] = []
    for doc in sorted((REPO_ROOT / "docs" / "commands").glob("*.md")):
        for block in re.finditer(
            r"^```console\n(.*?)^```", doc.read_text(), re.DOTALL | re.MULTILINE
        ):
            for line in block.group(1).splitlines():
                if line.startswith("$ da "):
                    # Documented examples may pipe into head/jq for
                    # readability; only the `da` part is ours to run.
                    commands.append(line[2:].split("|")[0].strip())
    return commands


LIVE_COMMANDS = _live_examples()

pytestmark = pytest.mark.integration


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "da"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=90,
        check=False,
    )


@pytest.fixture(scope="session")
def anon_env(tmp_path_factory, anonymous_token) -> dict[str, str]:
    """A CLI environment holding an app token, isolated from the real one.

    The token goes in as ``access_token`` with an hour of life, so the CLI
    uses it directly and never attempts a refresh — a ``client_credentials``
    grant has no refresh_token, and trying would be a confusing failure.
    """
    import os

    home = tmp_path_factory.mktemp("docs-live-home")
    state_dir = home / ".local" / "state" / "da-cli"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "access_token": anonymous_token,
                "expires_at": time.time() + 3600,
                "scope": "browse",
            }
        )
    )
    cfg_dir = home / ".config" / "da-cli"
    cfg_dir.mkdir(parents=True)
    dest = home / "gallery"
    dest.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"destination": str(dest)}))
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "DA_CLIENT_ID": os.environ.get("DA_CLIENT_ID", ""),
        "DA_CLIENT_SECRET": os.environ.get("DA_CLIENT_SECRET", ""),
        "LANG": "C",
        "LC_ALL": "C",
        "NO_COLOR": "1",
    }


# Commands the app token can reach. Everything else is user-scoped.
PUBLIC = (
    "da search",
    "da daily",
    "da user profile",
    "da deviation",
)
USER_SCOPED = ("da whoami", "da watch", "da refresh", "da sync")

PUBLIC_EXAMPLES = [c for c in LIVE_COMMANDS if c.startswith(PUBLIC)]
USER_EXAMPLES = [c for c in LIVE_COMMANDS if c.startswith(USER_SCOPED)]


class TestDocumentedPublicExamplesWork:
    """Every public-endpoint example in the guides, run against live DA."""

    def test_examples_were_found(self):
        assert PUBLIC_EXAMPLES, "no public live examples parsed from the guides"

    @pytest.mark.parametrize("example", PUBLIC_EXAMPLES, ids=lambda c: c)
    def test_example_succeeds(self, example, anon_env):
        argv = example.split()[1:]
        result = _run(argv, anon_env)
        # Retired commands are documented as exiting 2 with a hint; that is
        # a correct documented outcome, not a failure.
        if "popular" in argv or "newest" in argv:
            assert result.returncode == 2, result.stdout + result.stderr
            assert "retired" in (result.stdout + result.stderr).lower()
            return
        assert result.returncode == 0, (
            f"{example}\nexit={result.returncode}\n{result.stdout}{result.stderr}"
        )
        assert (result.stdout + result.stderr).strip(), f"{example} produced no output"

    @pytest.mark.parametrize(
        "example", [c for c in PUBLIC_EXAMPLES if "--json" in c], ids=lambda c: c
    )
    def test_json_examples_emit_valid_json(self, example, anon_env):
        """A documented --json example must produce parseable JSON.

        The guides tell readers to build on these rather than parse the
        human output. If stdout is not valid JSON the advice is wrong.
        """
        result = _run(example.split()[1:], anon_env)
        assert result.returncode == 0, result.stderr
        json.loads(result.stdout)


class TestDocumentedUserScopedExamplesWork:
    """`whoami` and `watch list` need a real account."""

    def test_examples_were_found(self):
        assert USER_EXAMPLES, "no user-scoped live examples parsed from the guides"

    @pytest.mark.parametrize(
        "example",
        [c for c in USER_EXAMPLES if c.startswith(("da whoami", "da watch"))],
        ids=lambda c: c,
    )
    def test_example_succeeds(self, example, cli_environment):
        result = _run(example.split()[1:], cli_environment)
        assert result.returncode == 0, (
            f"{example}\nexit={result.returncode}\n{result.stdout}{result.stderr}"
        )


class TestDocumentedApiShapes:
    """The guides describe fields; DA has to still be sending them."""

    def test_search_json_has_the_documented_fields(self, anon_env):
        result = _run(["search", "tag", "nature", "--limit", "3", "--json"], anon_env)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        items = payload if isinstance(payload, list) else payload.get("results", [])
        assert items, "no results to check the shape of"
        for item in items:
            assert "deviationid" in item, f"documented field missing: {sorted(item)}"

    def test_topics_are_listable(self, anon_env):
        """`da search topics` is documented as how you find a valid topic."""
        result = _run(["search", "topics"], anon_env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip(), "no topics returned"
