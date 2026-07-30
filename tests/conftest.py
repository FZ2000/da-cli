"""
Shared pytest fixtures for da-cli tests.

Two design choices to call out:

1. We `import dacli` directly (the repo root is on sys.path for the test run).
   No subprocess invocation of the `da` shim — it's a 5-line wrapper, tested
   separately via `test_shim.py` if needed.

2. Network is fully mocked. The fixture `mock_urlopen` patches
   `urllib.request.urlopen` with a callable that returns canned responses.
   Tests should never reach the real DA API (and the lone `--cov-fail-under`
   gate would catch a network-dependent regression by silently 0-ing some
   coverage line).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Make the repo root importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import dacli  # noqa: E402  (intentional import after sys.path mutation)

# Snapshot the *real* _keychain_* helpers before any autouse fixture stubs
# them. The autouse `_no_keychain_autouse` below forces every test onto the
# stubbed path by default so tests never accidentally touch the developer's
# real macOS Keychain. Tests that explicitly exercise the real helpers
# (TestKeychainGet, TestKeychainSet in test_auth_flow.py) opt back in via
# the `real_keychain` fixture below, which restores these snapshots.
_REAL_KEYCHAIN_GET = dacli._keychain_get
_REAL_KEYCHAIN_SET = dacli._keychain_set


def describe_folder(folder: Path, devid: str, image: bytes = b"IMG") -> Path:
    """Write the pair of files a completed sync leaves behind.

    Fixtures used to write ``description.json`` as ``{}``, which no real
    sync ever produces — ``_save_one`` always records the deviationid,
    and every consumer keys on it. Tests built on the shortcut could not
    see a folder being matched to the *wrong* deviation, because none of
    their folders claimed to be any deviation at all.
    """
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "description.json").write_text(json.dumps({"deviationid": devid}), encoding="utf-8")
    (folder / "image.png").write_bytes(image)
    return folder


# ---------------------------------------------------------------------------
# Always-on safety net: every test gets a tmp INDEX_PATH and a fresh
# connection. This is autouse so tests that don't explicitly opt into
# `isolated_paths` (e.g. the direct _save_one tests in test_sync.py)
# can't accidentally write into the developer's real synced-index.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _always_isolate_index(tmp_path_factory, monkeypatch):
    idx_dir = tmp_path_factory.mktemp("index-isolation")
    monkeypatch.setattr(dacli, "INDEX_PATH", idx_dir / "index.db")
    # Close any open connection from a prior test and clear the cache
    # directly. Using monkeypatch on `_INDEX_CONN` is fragile under
    # concurrency tests: monkeypatch records "before" at fixture entry
    # then on teardown sets _INDEX_CONN back to that captured value —
    # which may be a connection a prior test had cached but that the
    # subsequent _index_close() already closed, leaving worker threads
    # in the next test using a closed conn → sqlite3.InterfaceError.
    dacli._index_close()
    # Reset the per-process bootstrap memo so each test gets a fresh
    # bootstrap-on-empty-index check.
    dacli._BOOTSTRAP_CHECKED_THIS_PROCESS = False
    # Same for the corrupt-state warned memo — tests for state-corruption
    # depend on a clean "haven't warned yet" starting point.
    dacli._STATE_CORRUPTION_WARNED = False
    yield
    dacli._index_close()


# ---------------------------------------------------------------------------
# isolated config + state directories
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """
    Redirect CONFIG_DIR/STATE_DIR/CONFIG_PATH/STATE_PATH at the dacli module
    level to a temp location so tests never touch the real user's config.

    Yields a dict with the temp paths so tests can assert against them.
    """
    cfg_dir = tmp_path / "cfg"
    st_dir = tmp_path / "state"
    cfg = cfg_dir / "config.json"
    st = st_dir / "state.json"
    idx = st_dir / "index.db"
    monkeypatch.setattr(dacli, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(dacli, "STATE_DIR", st_dir)
    monkeypatch.setattr(dacli, "CONFIG_PATH", cfg)
    monkeypatch.setattr(dacli, "STATE_PATH", st)
    monkeypatch.setattr(dacli, "INDEX_PATH", idx)
    # These are derived from STATE_DIR at import time, so redirecting
    # STATE_DIR alone leaves them pointing at the real one. Without these
    # two lines any test that touches the TLS path writes a loopback key
    # — or, as happened, a 4-byte stub — into the user's actual
    # ~/.local/state/da-cli, where it then breaks `da auth` for real.
    monkeypatch.setattr(dacli, "LOOPBACK_CERT", st_dir / "loopback-cert.pem")
    monkeypatch.setattr(dacli, "LOOPBACK_KEY", st_dir / "loopback-key.pem")
    # Force a fresh index connection per test so the cached one from a
    # previous test (pointing at a now-deleted tmp dir) is discarded.
    # Direct reset (no monkeypatch on `_INDEX_CONN`) — see autouse fixture
    # above for the rationale.
    dacli._index_close()
    # Wipe any leftover env vars that could pollute load_config
    for k in (
        "DA_CLIENT_ID",
        "DA_CLIENT_SECRET",
        "DA_DESTINATION",
        "DA_REDIRECT_URI",
    ):
        monkeypatch.delenv(k, raising=False)
    yield {"cfg_dir": cfg_dir, "st_dir": st_dir, "cfg": cfg, "st": st, "idx": idx}
    # Clean up the connection so the next test gets a fresh one.
    dacli._index_close()


# ---------------------------------------------------------------------------
# Disable real keychain access during tests — autouse.
# ---------------------------------------------------------------------------
# Tests that don't explicitly request `no_keychain` would otherwise hit the
# developer's real macOS Keychain on darwin: `load_config()` calls
# `_keychain_get(client_secret)` for every test, and `_keychain_set` would
# write `client_secret=test-value` into the real keychain during config
# round-trip tests. Autouse prevents that — the explicit `no_keychain`
# fixture below is now a no-op kept for backward-compat with tests that
# already request it (they get the autouse behaviour either way).
@pytest.fixture(scope="session", autouse=True)
def _no_keychain_in_subprocesses(tmp_path_factory):
    """Put a stub `security` first on PATH for the whole session.

    The in-process fixture below patches `dacli._keychain_*`, which does
    nothing for a spawned subprocess: `da` re-imports the package in a
    fresh interpreter and calls the real functions, which shell out to
    `security` by bare name. Six tests spawn `da`, and two of them
    (tests/test_shim.py) reached the developer's actual login Keychain on
    every single run:

        SECURITY INVOKED: find-generic-password -s da-cli -a client_secret -w
        SECURITY INVOKED: find-generic-password -s da-cli -a client_secret -w

    A read is harmless enough; the same seam is how `da config unset`
    reaches `security delete-generic-password`, which is not.

    Scrubbing HOME would not have helped — the login Keychain is per-user,
    not per-HOME. Shadowing the binary is what actually closes it, and it
    matches what the in-process stub simulates: `security` unavailable, so
    `_keychain_get` returns None and the config-file path is used.
    """
    stub_dir = tmp_path_factory.mktemp("nokeychain-bin")
    stub = stub_dir / "security"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    previous = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{stub_dir}{os.pathsep}{previous}"
    yield stub_dir
    os.environ["PATH"] = previous


@pytest.fixture(autouse=True)
def _no_keychain_autouse(monkeypatch):
    """Force every _keychain_* call to behave as if `security` is missing."""
    monkeypatch.setattr(dacli, "_keychain_get", lambda key: None)
    monkeypatch.setattr(dacli, "_keychain_set", lambda key, value: False)


@pytest.fixture
def no_keychain(monkeypatch):
    """Backward-compat alias for the autouse keychain-stub fixture.

    New tests don't need to request this; the autouse version above
    applies to every test. Kept so existing tests that already declare
    the dependency still work without churn.
    """
    monkeypatch.setattr(dacli, "_keychain_get", lambda key: None)
    monkeypatch.setattr(dacli, "_keychain_set", lambda key, value: False)


@pytest.fixture
def real_keychain(monkeypatch):
    """Opt out of the autouse keychain stub.

    The autouse `_no_keychain_autouse` fixture forces every `_keychain_*`
    call to a no-op stub so tests can't accidentally read or write the
    developer's real macOS Keychain. Tests that explicitly exercise the
    real `_keychain_get` / `_keychain_set` helpers (e.g. TestKeychainGet,
    TestKeychainSet in test_auth_flow.py) request this fixture to restore
    the original implementations for the duration of the test. The test
    is then responsible for mocking `subprocess.run` (the underlying
    mechanism) so no real `security` invocation escapes.
    """
    monkeypatch.setattr(dacli, "_keychain_get", _REAL_KEYCHAIN_GET)
    monkeypatch.setattr(dacli, "_keychain_set", _REAL_KEYCHAIN_SET)


# ---------------------------------------------------------------------------
# HTTP mocking
# ---------------------------------------------------------------------------
class FakeResponse:
    """Minimal stand-in for urllib's HTTPResponse, supporting context-manager use."""

    def __init__(self, payload: bytes, status: int = 200, headers: dict[str, str] | None = None):
        self._payload = payload
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_exc: Any) -> None:
        pass


@pytest.fixture
def mock_urlopen():
    """
    Patch `urllib.request.urlopen` with a configurable mock.

    Yields the mock so tests can set its `side_effect` or `return_value`.
    Default behaviour: returns an empty 200 response.
    """
    with patch("urllib.request.urlopen") as m:
        m.return_value = FakeResponse(b"{}")
        yield m


@pytest.fixture
def json_response_factory():
    """
    Convenience factory for building FakeResponse around a JSON payload.

    Usage:
        def test_x(mock_urlopen, json_response_factory):
            mock_urlopen.return_value = json_response_factory({"hello": "world"})
    """

    def _factory(obj: dict[str, Any] | list[Any], status: int = 200) -> FakeResponse:
        return FakeResponse(json.dumps(obj).encode(), status=status)

    return _factory


# ---------------------------------------------------------------------------
# Sample fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sample_deviation():
    """A representative deviation dict — image, mature, real-shaped fields."""
    return {
        "deviationid": "ABCDEF12-3456-7890-ABCD-EF1234567890",
        "url": "https://www.deviantart.com/sample-artist/art/Sample-Title-12345",
        "title": "Sample / Title : With weird chars!",
        "is_mature": True,
        "is_favourited": False,
        "published_time": "1700000000",
        "stats": {"comments": 3, "favourites": 42},
        "author": {
            "userid": "11111111-2222-3333-4444-555555555555",
            "username": "sample-artist",
        },
        "content": {
            "src": "https://images.example.com/sample.png",
            "height": 800,
            "width": 600,
        },
    }


@pytest.fixture
def sample_metadata():
    """Metadata-endpoint response shape for a single deviation."""
    return {
        "deviationid": "ABCDEF12-3456-7890-ABCD-EF1234567890",
        "title": "Sample / Title : With weird chars!",
        "description": "<p>Hello <strong>world</strong></p>",
        "tags": [{"tag_name": "sample"}, {"tag_name": "test"}],
    }


@pytest.fixture(scope="session")
def real_cert_pair(tmp_path_factory):
    """One genuinely valid self-signed pair, generated once per session.

    The cert tests used to write ``b"CERT"`` / ``b"-----BEGIN ..."``
    stubs. Those bytes are not loadable, so the tests could not tell a
    working pair from a corrupt one — which is precisely the case that
    broke `da auth` in the field.
    """
    d = tmp_path_factory.mktemp("realcert")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-sha256",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.fixture
def authed(isolated_paths, no_keychain):
    """Pre-set client_id + a fresh token so commands skip the refresh path."""
    dacli.set_config_field("client_id", "12345")
    dacli.save_state(
        {
            "access_token": "T",
            "expires_at": time.time() + 3600,
            "refresh_token": "rt",
            "scope": "browse",
        }
    )


@pytest.fixture
def authed_with_destination(authed, isolated_paths, tmp_path):
    """`authed` + a writable destination directory wired into config."""
    dest = tmp_path / "gallery"
    dest.mkdir()
    dacli.set_config_field("destination", str(dest))
    return dest
