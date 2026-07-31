"""The loopback listener must only accept the redirect it asked for.

``_capture_code_via_listener`` used to take an authorization code from
*any* request carrying one, on any path. It received neither the expected
``state`` nor the expected path, so it was structurally incapable of
telling our own redirect from someone else's request.

That is not a theoretical exposure. During the five-minute window the
user has just been instructed to click through the self-signed warning
for this exact origin, and browsers cache that exception for the session
— so an ``<img src="https://localhost:8765/?code=...">`` on any page they
visit reaches the listener, as does any other local process.

An injected code is worse than useless. Accepting it ends the wait and
closes the listener, so the genuine callback arriving milliseconds later
finds a dead port and the user is told their redirect_uri whitelist is
wrong. And we would exchange the attacker's code with our own verifier —
if theirs was minted without a ``code_challenge``, the exchange can
succeed and store tokens for *their* account.

These tests drive the real listener with real HTTP requests, because the
guarantee is about what the handler does with a live request. Asserting
on the handler's internals would not show that one is refused end to end.
"""

from __future__ import annotations

import contextlib
import pathlib
import threading
import urllib.error
import urllib.request

import pytest

import dacli
from dacli.auth import _capture_code_via_listener

STATE = "the-state-we-generated-this-run"
GOOD_CODE = "our-legitimate-code"


@pytest.fixture
def listener(tmp_path, monkeypatch):
    """Run the real listener in a thread; return (port, result-holder).

    Plain HTTP: TLS is exercised by the auth-flow tests, and adding a
    self-signed handshake here would only obscure what these assert.
    """
    monkeypatch.setattr(dacli, "STATE_DIR", tmp_path)
    monkeypatch.setattr("webbrowser.open", lambda *_a, **_kw: True)
    # Keep the wait short: every test here resolves in milliseconds, and
    # the real 300s would make a failure take five minutes to report.
    monkeypatch.setattr(dacli.auth, "AUTH_LISTENER_TIMEOUT_S", 6)

    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    result: dict[str, object] = {}

    def run() -> None:
        result["value"] = _capture_code_via_listener(
            port,
            "http://example.invalid/auth",
            expected_state=STATE,
            expected_path="/",
            https=False,
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    # Let the socket bind before any test fires a request at it.
    deadline = threading.Event()
    for _ in range(200):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.05):
                break
        except OSError:
            deadline.wait(0.01)
    return port, result, thread


def _get(port: int, path_and_query: str) -> int:
    """Fire one request at the listener; return its status code."""
    try:
        with urllib.request.urlopen(  # loopback, fixed scheme
            f"http://127.0.0.1:{port}{path_and_query}", timeout=5
        ) as response:
            return int(response.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


class TestAnInjectedCodeIsRefused:
    def test_a_code_on_the_wrong_path_is_rejected(self, listener):
        """The probe that demonstrated the hole: any path was accepted."""
        port, result, thread = listener

        assert _get(port, "/totally/other/path?code=INJECTED") == 404

        # And, just as important, the listener is STILL WAITING — a
        # rejected request must not end the flow and strand the real
        # callback on a closed port.
        assert _get(port, f"/?state={STATE}&code={GOOD_CODE}") == 200
        thread.join(timeout=10)
        assert result["value"] == (GOOD_CODE, None)

    def test_a_code_without_our_state_is_rejected(self, listener):
        """Right path, wrong (or absent) state: still not ours."""
        port, result, thread = listener

        assert _get(port, "/?code=INJECTED") == 404
        assert _get(port, "/?state=someone-elses-state&code=INJECTED") == 404

        assert _get(port, f"/?state={STATE}&code={GOOD_CODE}") == 200
        thread.join(timeout=10)
        assert result["value"] == (GOOD_CODE, None)

    def test_a_favicon_fetch_does_not_disturb_the_flow(self, listener):
        """The behaviour the original `if "code" in q` guard protected.

        Browsers request /favicon.ico within ~100ms of the redirect; that
        must neither be captured nor end the wait.
        """
        port, result, thread = listener

        assert _get(port, "/favicon.ico") == 404
        assert _get(port, f"/?state={STATE}&code={GOOD_CODE}") == 200

        thread.join(timeout=10)
        assert result["value"] == (GOOD_CODE, None)


class TestTheGenuineCallbackStillWorks:
    """Controls: refusing everything would satisfy the tests above."""

    def test_the_matching_redirect_is_accepted(self, listener):
        port, result, thread = listener

        assert _get(port, f"/?state={STATE}&code={GOOD_CODE}") == 200

        thread.join(timeout=10)
        assert result["value"] == (GOOD_CODE, None)

    def test_extra_query_parameters_are_tolerated(self, listener):
        """DA may add parameters; only state and code are ours to check."""
        port, result, thread = listener

        assert _get(port, f"/?code={GOOD_CODE}&state={STATE}&scope=user+browse") == 200

        thread.join(timeout=10)
        assert result["value"] == (GOOD_CODE, None)


class TestAnOAuthErrorIsSurfaced:
    def test_access_denied_ends_the_wait_with_the_reason(self, listener):
        """Clicking Decline used to show "Authorized." and then hang.

        do_GET only looked for `code`, so the page claimed success, the
        wait ran its full five minutes, and the user was finally told
        their redirect_uri whitelist was wrong — a cause that was not the
        cause. DA's own error_description was in the query string the
        whole time.
        """
        port, result, thread = listener

        assert (
            _get(port, f"/?state={STATE}&error=access_denied&error_description=user+denied") == 200
        )

        thread.join(timeout=10)
        code, error = result["value"]
        assert code is None
        assert error is not None
        assert "access_denied" in error
        assert "user denied" in error

    def test_an_error_without_our_state_is_ignored(self, listener):
        """An unsolicited error must not be able to abort our login."""
        port, result, thread = listener

        assert _get(port, "/?error=access_denied") == 404
        assert _get(port, f"/?state={STATE}&code={GOOD_CODE}") == 200

        thread.join(timeout=10)
        assert result["value"] == (GOOD_CODE, None)


class TestTheStateItselfIsSound:
    def test_state_is_generated_with_a_csprng_and_is_unpredictable(self):
        """A guessable state is no state at all."""
        import re

        source = (dacli.auth.__file__ or "").replace(".pyc", ".py")
        text = pathlib.Path(source).read_text(encoding="utf-8")
        assert "secrets.token_urlsafe" in text, "state must come from a CSPRNG"
        assert re.search(r"state\s*=\s*secrets\.token_urlsafe\(\s*(\d\d+)\s*\)", text), (
            "state should be at least 32 bytes of entropy"
        )

    def test_state_is_compared_in_constant_time(self):
        source = (dacli.auth.__file__ or "").replace(".pyc", ".py")
        text = pathlib.Path(source).read_text(encoding="utf-8")
        assert "hmac.compare_digest" in text

    def test_the_authorization_url_carries_the_state(self, monkeypatch, tmp_path, capsys):
        """Validating a state we never sent would reject every login."""
        monkeypatch.setattr(dacli, "CONFIG_PATH", tmp_path / "config.json")
        monkeypatch.setattr(dacli, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(dacli, "STATE_DIR", tmp_path)
        monkeypatch.setattr(dacli, "load_config", lambda: {"client_id": "12345"})
        # dacli.auth, not dacli: cmd_auth calls this as a module-local
        # name, so patching the package re-export does not intercept it.
        monkeypatch.setattr(dacli.auth, "_capture_code_via_paste", lambda: None)

        import argparse

        with pytest.raises(SystemExit):
            dacli.cmd_auth(
                argparse.Namespace(
                    paste=True,
                    scope="user browse",
                    redirect_uri="https://localhost:8765/",
                    port=None,
                    auth_cmd=None,
                )
            )

        printed = capsys.readouterr().out
        assert "state=" in printed, "the authorization URL must include state"


class TestTheInjectionThroughThePublicSurface:
    """The same attack via ``cmd_auth``, which both versions expose.

    The direct-listener tests above cannot run against the old code — its
    signature had no ``expected_state`` — so they demonstrate the fix but
    not the hole. This one goes through the public command, so it runs
    unchanged on either side: on the old code the injected bare ``?code=``
    is accepted and exchanged; here it is ignored.
    """

    def test_a_bare_code_never_reaches_the_token_exchange(self, tmp_path, monkeypatch):
        exchanges: list[dict[str, object]] = []

        def record_exchange(url, form, **_kw):
            exchanges.append(dict(form))
            return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

        monkeypatch.setattr(dacli, "CONFIG_PATH", tmp_path / "config.json")
        monkeypatch.setattr(dacli, "STATE_PATH", tmp_path / "state.json")
        monkeypatch.setattr(dacli, "STATE_DIR", tmp_path)
        monkeypatch.setattr(dacli, "load_config", lambda: {"client_id": "12345"})
        monkeypatch.setattr(dacli, "http_post_json", record_exchange)
        # 3s, not 300: the fixed code correctly waits out its timeout here,
        # and a five-minute test would be useless.
        monkeypatch.setattr(dacli.auth, "AUTH_LISTENER_TIMEOUT_S", 3)

        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        def inject_a_foreign_code(_auth_link: str) -> bool:
            """A code we never asked for, with no state — the attack."""
            with contextlib.suppress(Exception):
                _get(port, "/?code=ATTACKERS-CODE")
            return True

        monkeypatch.setattr("webbrowser.open", inject_a_foreign_code)

        import argparse

        with pytest.raises(SystemExit) as exc:
            dacli.cmd_auth(
                argparse.Namespace(
                    paste=False,
                    scope="user browse",
                    redirect_uri=f"http://localhost:{port}/",
                    port=None,
                    auth_cmd=None,
                )
            )

        assert exc.value.code == 2, "an unsolicited code must not complete the login"
        assert exchanges == [], f"a code we never requested was exchanged for tokens: {exchanges}"
        assert not (tmp_path / "state.json").exists(), "tokens were stored from a foreign code"
