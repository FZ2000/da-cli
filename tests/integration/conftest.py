"""Fixtures for network integration tests.

**Isolation principle.** These fixtures never mutate dacli's
module-level globals (``CONFIG_PATH``, ``STATE_PATH``, etc.) and never
undo the parent conftest's autouse stubs. Instead, every fixture reads
what it needs **directly** from the real filesystem paths, and token
refresh is done inline (via ``http_post_json``) so ``save_state()`` is
never called. The developer's real ``config.json``, Keychain, and
destination directory are never opened for write. The one deliberate
exception is ``state.json``: ``user_token`` persists a rotated
refresh_token back to it (see its inline comment) — otherwise DA's
rotation would brick the developer's next ``da`` command.

What the parent conftest's autouse fixtures do (and why we leave them
alone):

* ``_always_isolate_index`` — repoints ``INDEX_PATH`` to a tmp dir.
  Fine: network tests don't touch the index.
* ``_no_keychain_autouse`` — stubs ``_keychain_get``/``_keychain_set``
  to no-ops. Fine: the ``anonymous_token`` fixture reads
  ``client_secret`` from the env var or the real config.json directly,
  bypassing the stubbed ``_keychain_get``.

**Token lifecycle.**

* ``anonymous_token`` (session): one ``client_credentials`` exchange
  per pytest run; DA's TTL is 1 hour.
* ``user_token`` (session): one ``refresh_token`` exchange per run;
  done inline so ``save_state()`` is never called.

**Skip policy.** Every credential-dependent fixture calls
``pytest.skip()`` with an actionable message if creds are absent. In CI,
``user_token`` calls ``pytest.fail()`` so a missed rotation surfaces as
red.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import dacli


# ---------------------------------------------------------------------------
# Auto-skip network tests unless -m integration is given
# ---------------------------------------------------------------------------
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every ``integration``-marked test unless the operator opted in
    via ``-m integration``. Keeps the default ``pytest`` run hermetic."""
    markexpr = config.getoption("-m") or ""
    if "integration" in markexpr:
        return
    skip = pytest.mark.skip(reason="integration tests require `-m integration`")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Self-throttle: sleep between tests to be kind to DA's rate limiter
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _network_throttle(request: pytest.FixtureRequest) -> None:
    """Sleep 1.5 s after every network test. DA has no documented rate
    limit; this is defensive politeness."""
    if "integration" not in request.keywords:
        return
    yield
    time.sleep(1.5)


# ---------------------------------------------------------------------------
# Credential helpers — read directly from real paths, never via dacli's
# module-level globals (which may be repointed by parent fixtures).
# ---------------------------------------------------------------------------
def _real_config_path() -> Path:
    """The developer's real config.json path, computed from HOME (not
    from ``dacli.CONFIG_PATH`` which the parent conftest may have
    repointed to a tmp dir)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "da-cli" / "config.json"


def _real_state_path() -> Path:
    """The developer's real state.json path (same rationale)."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "da-cli" / "state.json"


def _read_client_id() -> str | None:
    """Read client_id from env or the real config.json."""
    cid = os.environ.get("DA_CLIENT_ID")
    if cid:
        return cid
    path = _real_config_path()
    if path.exists():
        try:
            return json.loads(path.read_text()).get("client_id")
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _read_client_secret() -> str | None:
    """Read client_secret from env, real config.json, or real Keychain.

    Bypasses ``dacli._keychain_get`` (which the parent conftest stubs)
    by calling ``security`` directly on macOS. On Linux, falls back to
    the config file.
    """
    secret = os.environ.get("DA_CLIENT_SECRET")
    if secret:
        return secret

    # Try the real config.json (da-cli stores the secret there on
    # non-Keychain platforms, and sometimes on macOS too).
    path = _real_config_path()
    if path.exists():
        try:
            secret = json.loads(path.read_text()).get("client_secret")
            if secret:
                return secret
        except (json.JSONDecodeError, OSError):
            pass

    # Try the real macOS Keychain directly (bypassing dacli's stub).
    if sys.platform == "darwin":
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                dacli.KEYCHAIN_SERVICE,
                "-a",
                "client_secret",
                "-w",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            secret = result.stdout.strip()
            if secret:
                return secret

    return None


# ---------------------------------------------------------------------------
# Anonymous token via client_credentials grant
# ---------------------------------------------------------------------------
_ANONYMOUS_TOKEN_CACHE: dict[str, object] = {}


@pytest.fixture(scope="session")
def anonymous_token() -> str:
    """Session-scoped ``client_credentials`` token, or skip.

    Reads credentials directly from env / real config / real Keychain.
    Does NOT call ``dacli.load_config()`` (which reads from
    ``dacli.CONFIG_PATH`` and may be repointed by the parent conftest).
    Does NOT call ``dacli._keychain_get`` (which is stubbed).

    Note: cassette test files (``test_api_response_shapes.py``) override
    this fixture with a module-scoped dummy-token version for replay.
    """
    if "token" in _ANONYMOUS_TOKEN_CACHE:
        return str(_ANONYMOUS_TOKEN_CACHE["token"])

    client_id = _read_client_id()
    client_secret = _read_client_secret()

    if not client_id or not client_secret:
        pytest.skip(
            "DA_CLIENT_ID/DA_CLIENT_SECRET not configured — see tests/integration/README.md"
        )

    body = dacli.http_post_json(
        dacli.TOKEN_URL,
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    tok = body.get("access_token")
    if not tok:
        pytest.fail(f"client_credentials grant failed: {body}")
    _ANONYMOUS_TOKEN_CACHE["token"] = tok
    _ANONYMOUS_TOKEN_CACHE["expires_at"] = time.time() + float(body.get("expires_in", 3600))
    return str(tok)


# ---------------------------------------------------------------------------
# User-scoped state + token via refresh_token
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def user_state() -> dict[str, object]:
    """Read the developer's REAL ``state.json`` directly (not through
    ``dacli.STATE_PATH`` which the parent conftest may have repointed).

    Returns a plain dict. Tests that exercise the refresh path must
    pass a ``.copy()`` to any function that mutates it.
    """
    # CI path: prefer the DA_REFRESH_TOKEN secret.
    env_refresh = os.environ.get("DA_REFRESH_TOKEN")
    if env_refresh:
        return {
            "refresh_token": env_refresh,
            "refresh_token_issued_at": time.time(),
            "access_token": "",
            "expires_at": 0,
            "scope": os.environ.get("DA_SCOPE", "browse"),
        }

    path = _real_state_path()
    if not path.exists():
        if "CI" in os.environ:
            pytest.fail("CI: DA_REFRESH_TOKEN secret missing — rotation overdue")
        pytest.skip("no state.json — run `da auth` first")

    state: dict[str, object] = json.loads(path.read_text())
    if not state.get("refresh_token"):
        pytest.skip("state.json has no refresh_token")

    # TTL pre-flight.
    issued_at = float(state.get("refresh_token_issued_at", 0))
    days_left = dacli.REFRESH_TOKEN_TTL_DAYS - (time.time() - issued_at) / 86400.0
    if days_left <= 0:
        msg = f"refresh_token expired {-days_left:.1f} days ago — re-run `da auth`"
        if "CI" in os.environ:
            pytest.fail(f"CI: {msg}")
        pytest.skip(msg)
    return state


@pytest.fixture(scope="session")
def user_token(user_state: dict[str, object]) -> str:
    """Live ``access_token`` via one inline refresh exchange.

    The refresh is done inline (via ``http_post_json``) so we control
    exactly what happens to the response. DA may rotate the
    ``refresh_token`` on each refresh — if so, we **must** persist the
    rotated token back to the real ``state.json``. Otherwise the real
    file would have a dead refresh_token and every subsequent ``da``
    command would fail. This is the same lifecycle that
    ``dacli.access_token()`` performs on every call — not a test-only
    side effect.
    """
    client_id = _read_client_id()
    client_secret = _read_client_secret()
    refresh_token = str(user_state.get("refresh_token"))

    if not client_id:
        pytest.skip("no client_id configured")

    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    body = dacli.http_post_json(dacli.TOKEN_URL, payload)
    tok = body.get("access_token")
    if not tok:
        if "CI" in os.environ:
            pytest.fail(f"refresh_token rejected: {body}")
        pytest.skip(f"refresh failed: {body}")

    # Persist the refreshed tokens to the REAL state.json. DA may have
    # rotated the refresh_token; if we don't save the new one, the old
    # one dies and the user's next `da` command fails. This mirrors
    # what dacli.access_token() does on every call — it's the normal
    # OAuth lifecycle, not a test side effect.
    real_state_path = _real_state_path()
    if real_state_path.exists():
        state = json.loads(real_state_path.read_text())
        state["access_token"] = str(body["access_token"])
        state["expires_at"] = time.time() + float(body.get("expires_in", 3600))
        if body.get("refresh_token"):
            state["refresh_token"] = body["refresh_token"]
            state["refresh_token_issued_at"] = time.time()
        real_state_path.write_text(json.dumps(state, indent=2))
        os.chmod(real_state_path, 0o600)

    return str(tok)


# ---------------------------------------------------------------------------
# CLI subprocess environment: tmp HOME mirroring real config + state
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def cli_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, str]:
    """Build a tmp ``HOME`` with copies of the developer's real config +
    state. Subprocess invocations of ``da`` use this HOME via the
    environment dict — the real ``~/.config``, ``~/.local/state``, and
    Keychain are never touched by the subprocess.

    Credentials are passed via ``DA_CLIENT_ID`` / ``DA_CLIENT_SECRET``
    env vars so the subprocess's ``load_config()`` picks them up
    directly — it never needs to read the macOS Keychain (which is
    HOME-relative and would be absent in the tmp HOME).
    """
    home = tmp_path_factory.mktemp("da-home")

    real_state = _real_state_path()
    if not real_state.exists() and not os.environ.get("DA_REFRESH_TOKEN"):
        pytest.skip("no real state.json — run `da auth` first")

    target_state_dir = home / ".local" / "state" / "da-cli"
    target_state_dir.mkdir(parents=True, exist_ok=True)
    if real_state.exists():
        (target_state_dir / "state.json").write_bytes(real_state.read_bytes())
    elif os.environ.get("DA_REFRESH_TOKEN"):
        (target_state_dir / "state.json").write_text(
            json.dumps(
                {
                    "refresh_token": os.environ["DA_REFRESH_TOKEN"],
                    "refresh_token_issued_at": time.time(),
                    "scope": os.environ.get("DA_SCOPE", "browse"),
                }
            )
        )

    # Copy config.json but strip client_secret — the subprocess can't
    # read the real Keychain (HOME-relative path). Instead, credentials
    # are passed via DA_CLIENT_ID / DA_CLIENT_SECRET env vars below,
    # which load_config() reads with higher priority than the file.
    real_config = _real_config_path()
    if real_config.exists():
        target_cfg_dir = home / ".config" / "da-cli"
        target_cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = json.loads(real_config.read_text())
        cfg.pop("client_secret", None)  # force env-var fallback
        (target_cfg_dir / "config.json").write_text(json.dumps(cfg, indent=2))

    env: dict[str, str] = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}

    # Pass credentials via env so the subprocess never needs the Keychain.
    client_id = _read_client_id()
    client_secret = _read_client_secret()
    if client_id:
        env["DA_CLIENT_ID"] = client_id
    if client_secret:
        env["DA_CLIENT_SECRET"] = client_secret

    return env


# ---------------------------------------------------------------------------
# VCR.py cassette config (used only when pytest-vcr is installed)
# ---------------------------------------------------------------------------

# Response headers that leak information about the recording machine
# (CDN edge location → rough geolocation; Date → recording time) without
# being needed for replay. Stripped from every cassette at record time.
_ENVIRONMENT_LEAKING_RESPONSE_HEADERS = frozenset(
    {"date", "via", "x-amz-cf-id", "x-amz-cf-pop", "x-backend", "x-cache"}
)


def _strip_environment_leaking_headers(response: dict) -> dict:
    headers = response.get("headers", {})
    for name in list(headers):
        if name.lower() in _ENVIRONMENT_LEAKING_RESPONSE_HEADERS:
            del headers[name]
    return response


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, object]:
    """Scrub auth and environment details from cassettes before recording.

    To record cassettes (developer runs locally once)::

        VCR_RECORD=1 pytest -m integration_cassette --no-cov

    To replay (CI, default)::

        pytest -m integration_cassette --no-cov
    """
    record_mode = "all" if os.environ.get("VCR_RECORD") else "none"
    return {
        "record_mode": record_mode,
        "filter_headers": ["authorization", "Authorization"],
        "filter_query_parameters": ["access_token", "token"],
        "before_record_response": _strip_environment_leaking_headers,
        "match_on": ["method", "scheme", "host", "path", "query"],
        "cassette_library_dir": str(Path(__file__).parent / "cassettes"),
    }
