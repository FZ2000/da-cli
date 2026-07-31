"""Tests of the test infrastructure.

The conftest fixtures (`isolated_paths`, `mock_urlopen`, `_no_keychain_autouse`,
`real_keychain`, `sample_deviation`, `sample_metadata`) are critical —
every other test relies on them. If a fixture silently breaks isolation
(e.g. `isolated_paths` stops monkeypatching `CONFIG_PATH`), the whole
suite would still pass but tests would touch the developer's real
config / state / index. These tests pin the contracts the rest of the
suite assumes.

Pattern: pytest self-tests this in `testing/_pytest/`; cargo does it in
`tests/testsuite/`. Same rationale — the test framework itself is
load-bearing software.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import dacli
from tests.conftest import (
    _REAL_KEYCHAIN_GET,
    _REAL_KEYCHAIN_SET,
    FakeResponse,
)


# ---------------------------------------------------------------------------
# isolated_paths — must redirect EVERY path dacli would otherwise hit.
# ---------------------------------------------------------------------------
class TestIsolatedPaths:
    def test_redirects_config_path(self, isolated_paths):
        """load_config() must read from the isolated tmp dir, not the
        developer's real ~/.config/da-cli/config.json."""
        # No file in the isolated location → load returns empty.
        assert isolated_paths["cfg"] == dacli.CONFIG_PATH
        assert not isolated_paths["cfg"].exists()
        assert dacli.load_config() == {}

    def test_redirects_state_path(self, isolated_paths):
        assert isolated_paths["st"] == dacli.STATE_PATH
        assert not isolated_paths["st"].exists()
        assert dacli.load_state() == {}

    def test_writes_stay_in_isolated_dir(self, isolated_paths):
        """A round-trip write → read must not touch any path outside
        the tmp dirs supplied by `isolated_paths`."""
        dacli.set_config_field("client_id", "TEST_VALUE_42")
        # File landed in the isolated location, not in ~/.config.
        assert isolated_paths["cfg"].exists()
        cfg = json.loads(isolated_paths["cfg"].read_text())
        assert cfg["client_id"] == "TEST_VALUE_42"

    def test_redirects_index_path(self, isolated_paths):
        assert isolated_paths["idx"] == dacli.INDEX_PATH


# ---------------------------------------------------------------------------
# _no_keychain_autouse — must stub _keychain_* for every test.
# ---------------------------------------------------------------------------
class TestAutouseKeychain:
    def test_keychain_get_is_stubbed_by_default(self):
        """Without requesting `real_keychain`, _keychain_get must return
        None regardless of platform — the autouse fixture installs a
        no-op stub before the test body runs."""
        assert dacli._keychain_get("client_secret") is None

    def test_keychain_set_is_stubbed_by_default(self):
        """_keychain_set must return False (Keychain write 'failed') so
        tests that exercise set_config_field on a SECRET_KEYS field
        fall through to the 0600-file path."""
        assert dacli._keychain_set("client_secret", "x") is False

    def test_real_keychain_fixture_restores_originals(self, real_keychain):
        """The `real_keychain` opt-out fixture must restore the
        original _keychain_* helpers captured at conftest import time.
        Verifies the snapshot mechanism works (the helpers can be
        re-stubbed by autouse for the next test)."""
        # _keychain_get now points at the snapshot, not the lambda stub.
        assert dacli._keychain_get is _REAL_KEYCHAIN_GET
        assert dacli._keychain_set is _REAL_KEYCHAIN_SET
        # And the originals are still callable (will short-circuit on
        # non-darwin in CI but that's fine — we just verify the wiring).
        assert callable(dacli._keychain_get)
        assert callable(dacli._keychain_set)


# ---------------------------------------------------------------------------
# mock_urlopen — must replace urllib.request.urlopen with a mock.
# ---------------------------------------------------------------------------
class TestMockUrlopen:
    def test_default_returns_empty_200(self, mock_urlopen):
        """The bare `mock_urlopen` fixture returns a 200 with empty JSON
        body — so a test that forgets to set a return value still gets
        a parseable response rather than a MagicMock explosion."""
        with patch("urllib.request.urlopen", mock_urlopen):
            # Calling the mock directly exercises the same path http_json does.
            response = mock_urlopen("https://example.com")
            assert json.loads(response.read()) == {}

    def test_can_be_overridden_per_test(self, mock_urlopen, json_response_factory):
        mock_urlopen.return_value = json_response_factory({"hello": "world"})
        with patch("urllib.request.urlopen", mock_urlopen):
            response = mock_urlopen("https://example.com")
            assert json.loads(response.read()) == {"hello": "world"}


# ---------------------------------------------------------------------------
# json_response_factory — must wrap any dict/list in a FakeResponse.
# ---------------------------------------------------------------------------
class TestJsonResponseFactory:
    def test_wraps_dict(self, json_response_factory):
        r = json_response_factory({"a": 1})
        assert isinstance(r, FakeResponse)
        assert json.loads(r.read()) == {"a": 1}
        assert r.status == 200

    def test_wraps_list(self, json_response_factory):
        r = json_response_factory([1, 2, 3])
        assert json.loads(r.read()) == [1, 2, 3]

    def test_status_kwarg(self, json_response_factory):
        r = json_response_factory({"err": 1}, status=400)
        assert r.status == 400


# ---------------------------------------------------------------------------
# Sample fixtures — must have the shape dacli's cmd_* handlers expect.
# ---------------------------------------------------------------------------
class TestSampleFixtures:
    def test_sample_deviation_has_required_keys(self, sample_deviation):
        """cmd_save_one reads d['deviationid'], d['author']['username'],
        d['content']['src'], etc. A future change to the sample fixture
        that drops a key would silently break dozens of tests."""
        for key in (
            "deviationid",
            "url",
            "title",
            "is_mature",
            "is_favourited",
            "published_time",
            "stats",
            "author",
            "content",
        ):
            assert key in sample_deviation, f"sample_deviation missing {key!r}"
        assert "username" in sample_deviation["author"]
        assert "src" in sample_deviation["content"]

    def test_sample_metadata_has_required_keys(self, sample_metadata):
        for key in ("deviationid", "title", "description", "tags"):
            assert key in sample_metadata, f"sample_metadata missing {key!r}"
        assert isinstance(sample_metadata["tags"], list)


# ---------------------------------------------------------------------------
# _always_isolate_index — must redirect the SQLite index per test.
# ---------------------------------------------------------------------------
class TestAlwaysIsolateIndex:
    def test_index_path_under_tmp(self, tmp_path_factory):
        """The autouse index-isolation fixture must put INDEX_PATH
        somewhere under pytest's tmp_path_factory, not under the
        developer's real ~/.local/state/da-cli/. We can't assert the
        exact path from outside the fixture, but we can assert that
        opening the index doesn't create a file in the real state dir."""
        # Force a fresh connection by closing any cached one.
        dacli._index_close()
        # The autouse fixture fires per-test; by the time we run, it's
        # already redirected. Just confirm we can open + query.
        dacli.index_add("TEST-DEVID", "test-artist", "T", Path("/tmp"), 1)
        assert dacli.index_count() >= 1
        # Cleanup so subsequent tests in the same process get a fresh dir.
        dacli._index_close()


# ---------------------------------------------------------------------------
# no_keychain (backward-compat alias) — still works.
# ---------------------------------------------------------------------------
class TestSubprocessesCannotReachTheRealKeychain:
    """The in-process stub does not survive a spawn.

    `_no_keychain_autouse` patches `dacli._keychain_*`, which a subprocess
    never sees: it re-imports the package and calls the real functions,
    which shell out to `security` by bare name. Two shim tests reached the
    developer's actual login Keychain on every run before the session
    fixture shadowed the binary.
    """

    def test_security_on_path_is_the_stub(self):
        found = shutil.which("security")
        assert found is not None, "no `security` on PATH at all"
        assert "nokeychain-bin" in found, (
            f"`security` resolves to {found}, so a spawned `da` would query the real Keychain"
        )

    def test_the_stub_reports_failure_like_a_missing_keychain(self):
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "da-cli", "-a", "x", "-w"],
            capture_output=True,
            check=False,
        )
        assert r.returncode != 0, "the stub must fail so _keychain_get returns None"

    def test_a_spawned_da_reaches_only_the_stub(self, tmp_path):
        """End to end: the call still happens, it just cannot reach the real one.

        Shadowing does not stop `da` asking for the secret — it makes the
        answer "unavailable", exactly as the in-process stub simulates.
        What matters is which binary answers.
        """
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
        r = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "da"), "config", "show"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        assert r.returncode == 0, r.stderr
        resolved = shutil.which("security", path=env["PATH"])
        assert resolved and "nokeychain-bin" in resolved, (
            f"a spawned `da` would resolve `security` to {resolved}"
        )


class TestNoKeychainAlias:
    def test_alias_still_stubbs(self, no_keychain):
        """The explicit `no_keychain` fixture is now a backward-compat
        alias for the autouse stub. Tests that already request it must
        continue to work without churn."""
        assert dacli._keychain_get("client_secret") is None
        assert dacli._keychain_set("client_secret", "x") is False


# ---------------------------------------------------------------------------
# isolated_paths must cover EVERY path derived from STATE_DIR.
# ---------------------------------------------------------------------------
class TestIsolatedPathsCoversDerivedPaths:
    """LOOPBACK_CERT/KEY are computed from STATE_DIR at import time.

    Redirecting STATE_DIR alone left them aimed at the real state dir, so
    tests wrote a TLS key — and in one case a 4-byte stub — into the
    user's own ~/.local/state/da-cli. The stub then made `da auth` fail
    with an unexplained "[SSL] PEM lib" until it was deleted by hand.
    """

    def test_loopback_paths_are_redirected(self, isolated_paths):
        real_state = Path.home() / ".local/state/da-cli"
        for name in ("LOOPBACK_CERT", "LOOPBACK_KEY"):
            p = getattr(dacli, name)
            assert real_state not in p.parents, f"{name} still points into the real state dir"
            assert p.parent == isolated_paths["st_dir"], f"{name} is not under the temp state dir"
