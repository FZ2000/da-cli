"""Why a refresh failed decides what the user is told to do.

Both defects here are mine, from the PR that added the cross-process
refresh lock. That PR made ``access_token`` handle transport failures
itself with ``sys.exit(2)`` — and ``_cmd_auth_status`` catches
``SystemExit`` and labels it ``revoked``. So an offline laptop reported a
perfectly good credential as revoked and sent the user through a browser
flow for nothing. It also contradicted the docstring I had written one PR
earlier, which said ``unreachable`` "is not the same thing and must not
send anyone to re-authenticate".

The same collapse hid a second case: a transient 502 from DA's auth
server took the rejection branch, so one server hiccup both advised
``da auth`` and wrote ``refresh_token_rejected_at`` — which then made the
next ``sync watched`` abort on its first benign per-artist failure.

And ``cmd_auth`` never cleared that mark, so even after the user did what
they were told, the next watched sync still treated the fresh grant as
dead for the hour until the new access token expired.

One exit code cannot express three outcomes. These tests pin the three.
"""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

import dacli


def _stale_state() -> dict[str, object]:
    """State whose access token has expired, so a refresh is required."""
    import time

    return {
        "access_token": "STALE",
        "expires_at": time.time() - 10,
        "refresh_token": "RT",
        "refresh_token_issued_at": time.time(),
    }


def _http_error(code: int, body: bytes = b"detail") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="x", code=code, msg="m", hdrs={}, fp=io.BytesIO(body))


class TestAuthStatusDistinguishesWhy:
    """The user-visible half of the regression."""

    def _run(self, isolated_paths, failure) -> tuple[dict[str, object], int]:
        dacli.save_state(_stale_state())
        with (
            patch.object(dacli, "load_config", return_value={"client_id": "12345"}),
            patch.object(dacli, "http_post_json", side_effect=failure),
            pytest.raises(SystemExit) as exc,
        ):
            dacli._cmd_auth_status()
        return exc.value.code

    def test_an_unreachable_network_is_not_revoked(self, isolated_paths, no_keychain, capsys):
        """The regression, exactly.

        Reported {"state": "revoked", "days_remaining": 0.0} for a
        credential nothing had rejected.
        """
        code = self._run(isolated_paths, urllib.error.URLError("Network is unreachable"))
        payload = json.loads(capsys.readouterr().out)

        assert payload["state"] == "unreachable", (
            f"a network failure was reported as {payload['state']!r}"
        )
        assert code == 2

    def test_a_5xx_from_the_auth_server_is_not_revoked(self, isolated_paths, no_keychain, capsys):
        """DA having a bad minute is not DA rejecting the grant."""
        self._run(isolated_paths, _http_error(503))
        payload = json.loads(capsys.readouterr().out)

        assert payload["state"] == "unreachable", f"a 503 was reported as {payload['state']!r}"

    def test_a_real_rejection_is_still_revoked(self, isolated_paths, no_keychain, capsys):
        """The control. Reporting everything as `unreachable` would pass
        the two tests above and hide the failure this command exists for.
        """
        self._run(isolated_paths, _http_error(400, b"invalid_grant"))
        payload = json.loads(capsys.readouterr().out)

        assert payload["state"] == "revoked"
        assert payload["days_remaining"] == 0.0


class TestOnlyARejectionMarksTheGrantDead:
    """The half that breaks `sync watched` a run later."""

    def _refresh(self, failure) -> None:
        with (
            patch.object(dacli, "http_post_json", side_effect=failure),
            pytest.raises(dacli.DacliError),
        ):
            dacli.access_token({"client_id": "12345"}, _stale_state())

    def test_a_network_failure_leaves_no_mark(self, isolated_paths, no_keychain):
        dacli.save_state(_stale_state())
        self._refresh(urllib.error.URLError("Network is unreachable"))
        assert "refresh_token_rejected_at" not in dacli.load_state()

    def test_a_5xx_leaves_no_mark(self, isolated_paths, no_keychain):
        dacli.save_state(_stale_state())
        self._refresh(_http_error(502))
        assert "refresh_token_rejected_at" not in dacli.load_state(), (
            "one bad minute on DA's auth server would abort the next `sync watched`"
        )

    def test_a_4xx_does_mark(self, isolated_paths, no_keychain):
        """The control: `sync watched` needs this signal to exist."""
        dacli.save_state(_stale_state())
        self._refresh(_http_error(400, b"invalid_grant"))
        assert dacli.load_state().get("refresh_token_rejected_at")


class TestReauthClearsTheMark:
    def test_cmd_auth_clears_a_previous_rejection(self, isolated_paths, no_keychain, monkeypatch):
        """`access_token` sets the mark and says "run `da auth`".

        Doing so left it set, because cmd_auth wrote the token fields and
        nothing else. The only clearer was access_token's own success path
        — an hour later, when the brand-new access token first expired. In
        between, `sync watched` aborted on its first benign per-artist
        failure and blamed a credential that had just been replaced.
        """
        import argparse
        import time

        dacli.set_config_field("client_id", "12345")
        dacli.save_state({**_stale_state(), "refresh_token_rejected_at": time.time() - 60})
        assert dacli.load_state().get("refresh_token_rejected_at")

        monkeypatch.setattr(dacli.auth, "_capture_code_via_paste", lambda: "THE-CODE")
        monkeypatch.setattr(
            dacli,
            "http_post_json",
            lambda url, form, **kw: {
                "access_token": "NEW-AT",
                "refresh_token": "NEW-RT",
                "expires_in": 3600,
                "scope": "user browse",
            },
        )

        dacli.cmd_auth(
            argparse.Namespace(
                paste=True,
                scope="user browse",
                redirect_uri="https://localhost:8765/",
                port=None,
                auth_cmd=None,
            )
        )

        state = dacli.load_state()
        assert state["access_token"] == "NEW-AT"
        assert "refresh_token_rejected_at" not in state, (
            "a completed re-auth must clear the mark it told the user to fix"
        )

    def test_a_successful_refresh_also_clears_it(self, isolated_paths, no_keychain):
        """The other route back to health, already covered but worth pinning
        next to the one that was missing.
        """
        import time

        dacli.save_state({**_stale_state(), "refresh_token_rejected_at": time.time() - 60})
        with patch.object(
            dacli,
            "http_post_json",
            return_value={"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600},
        ):
            dacli.access_token({"client_id": "12345"}, dacli.load_state())

        assert "refresh_token_rejected_at" not in dacli.load_state()
