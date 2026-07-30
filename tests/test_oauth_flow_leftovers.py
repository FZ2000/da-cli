"""Four smaller defects in the interactive OAuth flow.

Each is a case of the code claiming something it did not do: ``whoami``
advertised itself as the token check while being the one command unable to
recover from a stale token; ``_is_loopback`` claimed IPv6 support the
listener could not serve; and the authorization-code exchange retried a
single-use credential two functions away from a long comment explaining
why the refresh must not.
"""

from __future__ import annotations

import io
import urllib.error
from unittest.mock import patch

import pytest

import dacli


def _http_error(code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url="x", code=code, msg="m", hdrs={}, fp=io.BytesIO(body))


class TestWhoamiRecoversFromAStaleToken:
    """The command whose job is verifying the token could not repair it."""

    def test_a_401_triggers_a_refresh_and_retry(self, isolated_paths, no_keychain, capsys):
        """A locally-fresh but server-side-revoked token must self-heal.

        That is exactly what happens when a re-auth elsewhere rotates the
        chain. `da sync` recovered because it routes through
        authed_http_json; `da whoami` called http_json directly, so it
        sent the user through a browser flow one refresh would have
        avoided.
        """
        import time

        dacli.save_state(
            {
                "access_token": "STALE",
                "expires_at": time.time() + 3600,  # locally valid
                "refresh_token": "RT",
                "refresh_token_issued_at": time.time(),
            }
        )
        calls: list[str] = []

        def fake_http_json(url, token=None, **_kw):
            calls.append(str(token))
            if token == "STALE":
                raise _http_error(401)
            if "placebo" in url:
                return {"status": "success"}
            return {"username": "alice", "userid": "U-1"}

        with (
            patch.object(dacli, "load_config", return_value={"client_id": "12345"}),
            patch.object(dacli, "http_json", side_effect=fake_http_json),
            patch.object(
                dacli,
                "http_post_json",
                return_value={"access_token": "FRESH", "expires_in": 3600},
            ),
        ):
            dacli.cmd_whoami(None)

        out = capsys.readouterr().out
        assert "@alice" in out, "whoami did not recover from the 401"
        assert "FRESH" in calls, "no refresh was attempted"

    def test_a_healthy_token_makes_no_extra_requests(self, isolated_paths, no_keychain, capsys):
        """The control: routing through the recovering path must not add
        a refresh to the ordinary case.
        """
        import time

        dacli.save_state(
            {"access_token": "GOOD", "expires_at": time.time() + 3600, "refresh_token": "RT"}
        )
        exchanges: list[object] = []

        with (
            patch.object(dacli, "load_config", return_value={"client_id": "12345"}),
            patch.object(
                dacli,
                "http_json",
                side_effect=lambda url, token=None, **kw: (
                    {"status": "success"} if "placebo" in url else {"username": "alice"}
                ),
            ),
            patch.object(dacli, "http_post_json", side_effect=lambda *a, **kw: exchanges.append(a)),
        ):
            dacli.cmd_whoami(None)

        assert exchanges == [], "a healthy token should not be refreshed"
        assert "@alice" in capsys.readouterr().out


class TestLoopbackDetectionMatchesWhatTheListenerBinds:
    def test_ipv6_loopback_is_not_claimed(self):
        """`::1` took the listener path, which binds AF_INET only.

        The browser then got ECONNREFUSED, no callback ever arrived, and
        `da auth` burned its full five-minute timeout before blaming the
        redirect_uri whitelist. Refusing to claim it routes the user to
        `--paste`, which works.
        """
        assert dacli.auth._is_loopback("::1") is False

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
    def test_the_hosts_the_listener_can_serve_are_claimed(self, host):
        """The control: the working cases must keep working."""
        assert dacli.auth._is_loopback(host) is True

    def test_a_remote_host_is_not_loopback(self):
        assert dacli.auth._is_loopback("example.com") is False

    def test_the_cert_san_covers_what_is_claimed(self, isolated_paths, tmp_path, monkeypatch):
        """Whatever _is_loopback accepts, the cert must be valid for.

        Ties the two together so they cannot drift: adding a host here
        without extending the SAN is the shape of the ::1 bug.
        """
        import subprocess

        monkeypatch.setattr(dacli, "STATE_DIR", tmp_path)
        monkeypatch.setattr(dacli, "LOOPBACK_CERT", tmp_path / "c.pem")
        monkeypatch.setattr(dacli, "LOOPBACK_KEY", tmp_path / "k.pem")
        cert, _key = dacli._ensure_self_signed_cert()

        text = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "DNS:localhost" in text
        assert "IP Address:127.0.0.1" in text
        assert "::1" not in text, "the SAN covers ::1 — _is_loopback should accept it again"


class TestTheAuthorizationCodeIsNotReplayed:
    def test_the_exchange_does_not_retry(self, isolated_paths, no_keychain, monkeypatch):
        """An authorization code is single-use, like the refresh token.

        The refresh path passes retries=0 with a long comment explaining
        why replaying a consumed one-time credential turns "we do not
        know" into a guaranteed invalid_grant. This exchange took the
        default of 2 retries, two functions away.
        """
        import argparse

        dacli.set_config_field("client_id", "12345")
        seen: list[object] = []

        def record(url, form, **kwargs):
            seen.append(kwargs.get("retries"))
            return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

        monkeypatch.setattr(dacli.auth, "_capture_code_via_paste", lambda: "THE-CODE")
        monkeypatch.setattr(dacli, "http_post_json", record)

        dacli.cmd_auth(
            argparse.Namespace(
                paste=True,
                scope="user browse",
                redirect_uri="https://localhost:8765/",
                port=None,
                auth_cmd=None,
            )
        )

        assert seen == [0], f"the code exchange was given retries={seen}"

    def test_an_unreachable_token_endpoint_says_the_code_is_spent(
        self, isolated_paths, no_keychain, monkeypatch, capsys
    ):
        """It used to reach main()'s transport advice, which promises "the
        next run resumes where this one stopped; nothing has been lost" —
        untrue for `da auth`, whose code is now consumed.
        """
        import argparse

        dacli.set_config_field("client_id", "12345")
        monkeypatch.setattr(dacli.auth, "_capture_code_via_paste", lambda: "THE-CODE")
        monkeypatch.setattr(
            dacli,
            "http_post_json",
            lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError("unreachable")),
        )

        with pytest.raises(SystemExit) as exc:
            dacli.cmd_auth(
                argparse.Namespace(
                    paste=True,
                    scope="user browse",
                    redirect_uri="https://localhost:8765/",
                    port=None,
                    auth_cmd=None,
                )
            )

        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "single-use" in err, "the user was not told the code is spent"
        assert "nothing has been lost" not in err
