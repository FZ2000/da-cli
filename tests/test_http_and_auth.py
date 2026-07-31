"""Tests for the HTTP layer + the OAuth refresh path inside `access_token`."""

from __future__ import annotations

import json
import time
import urllib.error
from unittest.mock import patch

import pytest

import dacli
from tests.conftest import FakeResponse


# ---------------------------------------------------------------------------
# _request
# ---------------------------------------------------------------------------
class TestRequest:
    def test_no_token_no_form_is_get(self):
        req = dacli._request("https://api.example.com/x")
        assert req.method == "GET"
        assert req.full_url == "https://api.example.com/x"
        assert req.headers["User-agent"].startswith("da-cli/")

    def test_token_sets_authorization(self):
        req = dacli._request("https://api.example.com/x", token="abc")
        # get_header, not .headers: the token is added as an
        # *unredirected* header so urllib drops it rather than forwarding
        # it to another host on a 30x. get_header sees both buckets, so
        # this asserts what actually gets sent without pinning which one.
        assert req.get_header("Authorization") == "Bearer abc"

    def test_form_data_sets_content_type_and_body(self):
        req = dacli._request(
            "https://api.example.com/token",
            method="POST",
            form={"a": "1", "b": "two words"},
        )
        assert req.method == "POST"
        assert req.headers["Content-type"] == "application/x-www-form-urlencoded"
        assert req.data == b"a=1&b=two+words"


# ---------------------------------------------------------------------------
# http_json
# ---------------------------------------------------------------------------
class TestHttpJson:
    def test_returns_parsed_json(self, mock_urlopen, json_response_factory):
        mock_urlopen.return_value = json_response_factory({"a": 1})
        out = dacli.http_json("https://api.example.com/x")
        assert out == {"a": 1}

    def test_429_propagates_immediately(self, mock_urlopen):
        err = urllib.error.HTTPError(url="x", code=429, msg="Too Many Requests", hdrs={}, fp=None)
        mock_urlopen.side_effect = err
        with pytest.raises(urllib.error.HTTPError) as info:
            dacli.http_json("https://api.example.com/x", retries=0)
        assert info.value.code == 429

    def test_500_retries_then_raises(self, mock_urlopen):
        err = urllib.error.HTTPError(url="x", code=500, msg="server err", hdrs={}, fp=None)
        mock_urlopen.side_effect = err
        with patch("time.sleep"):
            with pytest.raises(urllib.error.HTTPError):
                dacli.http_json("https://api.example.com/x", retries=2)
        assert mock_urlopen.call_count == 3  # initial + 2 retries

    def test_url_error_retries(self, mock_urlopen, json_response_factory):
        # First call: URLError; second: success
        mock_urlopen.side_effect = [
            urllib.error.URLError("conn refused"),
            json_response_factory({"ok": True}),
        ]
        with patch("time.sleep"):
            out = dacli.http_json("https://api.example.com/x", retries=2)
        assert out == {"ok": True}


# ---------------------------------------------------------------------------
# http_post_json
# ---------------------------------------------------------------------------
class TestHttpPostJson:
    """http_post_json is documented as having the same retry contract as
    http_json — these tests pin that contract. The OAuth token endpoint
    is the most network-fragile call we make, so a regression in
    retry-on-502 here would break `da auth` and `access_token` refresh
    silently."""

    def test_posts_form_and_parses_response(self, mock_urlopen, json_response_factory):
        mock_urlopen.return_value = json_response_factory({"access_token": "T"})
        out = dacli.http_post_json("https://api.example.com/token", {"a": "1"})
        assert out == {"access_token": "T"}
        # Verify we sent a Request, not raw bytes
        sent_req = mock_urlopen.call_args[0][0]
        assert sent_req.method == "POST"
        assert sent_req.data == b"a=1"

    def test_429_fails_fast(self, mock_urlopen):
        """429 is caller-policy (not transport) — http layer must NOT retry."""
        err = urllib.error.HTTPError(url="x", code=429, msg="Too Many Requests", hdrs={}, fp=None)
        mock_urlopen.side_effect = err
        with pytest.raises(urllib.error.HTTPError) as info:
            dacli.http_post_json("https://api.example.com/token", {"a": "1"}, retries=0)
        assert info.value.code == 429

    def test_400_fails_fast(self, mock_urlopen):
        """A 400 (e.g. invalid_grant from the token endpoint) is permanent
        for this request shape — http layer must NOT retry."""
        err = urllib.error.HTTPError(url="x", code=400, msg="invalid_grant", hdrs={}, fp=None)
        mock_urlopen.side_effect = err
        with pytest.raises(urllib.error.HTTPError) as info:
            dacli.http_post_json("https://api.example.com/token", {"a": "1"}, retries=2)
        assert info.value.code == 400
        assert mock_urlopen.call_count == 1

    def test_502_retries_then_raises(self, mock_urlopen):
        """5xx is transient — retry up to `retries` times, then propagate."""
        err = urllib.error.HTTPError(url="x", code=502, msg="Bad Gateway", hdrs={}, fp=None)
        mock_urlopen.side_effect = err
        with patch("time.sleep"):
            with pytest.raises(urllib.error.HTTPError):
                dacli.http_post_json("https://api.example.com/token", {"a": "1"}, retries=2)
        # initial attempt + 2 retries
        assert mock_urlopen.call_count == 3

    def test_503_recovers_on_retry(self, mock_urlopen, json_response_factory):
        """First 503 is transient, second call succeeds — caller gets the body."""
        err = urllib.error.HTTPError(url="x", code=503, msg="Service Unavailable", hdrs={}, fp=None)
        mock_urlopen.side_effect = [err, json_response_factory({"ok": True})]
        with patch("time.sleep"):
            out = dacli.http_post_json("https://api.example.com/token", {"a": "1"}, retries=2)
        assert out == {"ok": True}
        assert mock_urlopen.call_count == 2

    def test_url_error_retries(self, mock_urlopen, json_response_factory):
        """Network/timeout errors retry with the same contract as 5xx."""
        mock_urlopen.side_effect = [
            urllib.error.URLError("conn refused"),
            json_response_factory({"ok": True}),
        ]
        with patch("time.sleep"):
            out = dacli.http_post_json("https://api.example.com/token", {"a": "1"}, retries=2)
        assert out == {"ok": True}

    def test_token_propagates_as_bearer_header(self, mock_urlopen, json_response_factory):
        """The optional `token` argument (used by /user/whois) must land in
        the Authorization header on the POST request."""
        mock_urlopen.return_value = json_response_factory({"results": []})
        dacli.http_post_json("https://api.example.com/user/whois", {"x": "1"}, token="ABCDEF")
        sent_req = mock_urlopen.call_args[0][0]
        assert sent_req.get_header("Authorization") == "Bearer ABCDEF"


# ---------------------------------------------------------------------------
# http_bytes
# ---------------------------------------------------------------------------
class TestHttpBytes:
    def test_returns_raw_bytes(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(b"\x89PNG\r\n\x1a\n...")
        result = dacli.http_bytes("https://cdn.example.com/x.png")
        assert result.startswith(b"\x89PNG")

    def test_uses_browser_user_agent(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse(b"x")
        dacli.http_bytes("https://cdn.example.com/x")
        sent = mock_urlopen.call_args[0][0]
        assert sent.headers["User-agent"].startswith("Mozilla/")

    def test_retries_on_failure(self, mock_urlopen):
        mock_urlopen.side_effect = [TimeoutError(), FakeResponse(b"ok")]
        with patch("time.sleep"):
            out = dacli.http_bytes("https://cdn.example.com/x", retries=2)
        assert out == b"ok"


# ---------------------------------------------------------------------------
# access_token
# ---------------------------------------------------------------------------
class TestAccessToken:
    def test_uses_cached_when_fresh(self, isolated_paths, no_keychain):
        cfg = {"client_id": "x"}
        state = {
            "access_token": "cached-token",
            "expires_at": time.time() + 3600,
            "refresh_token": "rt",
        }
        with patch.object(dacli, "http_post_json") as post:
            tok = dacli.access_token(cfg, state)
        assert tok == "cached-token"
        post.assert_not_called()

    def test_refreshes_when_expired(self, isolated_paths, no_keychain):
        cfg = {"client_id": "x", "client_secret": "s"}
        state = {"access_token": "old", "expires_at": 0, "refresh_token": "rt"}

        def fake_post(url, data, **_kw):
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "rt"
            return {"access_token": "new", "expires_in": 3600, "refresh_token": "rt2"}

        with patch.object(dacli, "http_post_json", side_effect=fake_post):
            tok = dacli.access_token(cfg, state)
        assert tok == "new"
        # state should be persisted with the new token
        on_disk = json.loads(isolated_paths["st"].read_text())
        assert on_disk["access_token"] == "new"
        assert on_disk["refresh_token"] == "rt2"

    def test_no_refresh_token_raises_config_error(self, isolated_paths, no_keychain):
        """Typed, not sys.exit: callers need to know WHY it failed.

        These asserted SystemExit while access_token collapsed every
        failure into exit 2 — which is precisely what made `da auth
        status` report an unreachable network as a revoked credential.
        The CLI still exits 2; see test_the_cli_still_exits_2 below.
        """
        with pytest.raises(dacli.ConfigError, match="refresh_token"):
            dacli.access_token({"client_id": "x"}, {})

    def test_no_client_id_raises_config_error(self, isolated_paths, no_keychain):
        with pytest.raises(dacli.ConfigError, match="client_id"):
            dacli.access_token({}, {"refresh_token": "rt", "expires_at": 0})

    def test_refresh_response_without_token_raises_auth_error(self, isolated_paths, no_keychain):
        cfg = {"client_id": "x"}
        state = {"refresh_token": "rt", "expires_at": 0}
        with (
            patch.object(dacli, "http_post_json", return_value={"error": "invalid_grant"}),
            pytest.raises(dacli.AuthError),
        ):
            dacli.access_token(cfg, state)

    def test_a_5xx_from_the_token_endpoint_is_not_a_rejection(self, isolated_paths, no_keychain):
        """A bad minute on DA's auth server must not read as a dead grant.

        retries=0 means the first 5xx is final, and it used to land in the
        branch that says "run `da auth`" AND writes
        refresh_token_rejected_at — so one server hiccup sent the user
        through a browser flow and made the next `sync watched` abort on
        its first per-artist failure.
        """
        import io

        err = urllib.error.HTTPError(
            url="x", code=502, msg="bad gateway", hdrs={}, fp=io.BytesIO(b"upstream")
        )
        state = {"refresh_token": "rt", "expires_at": 0}
        with (
            patch.object(dacli, "http_post_json", side_effect=err),
            pytest.raises(dacli.HttpError, match="502"),
        ):
            dacli.access_token({"client_id": "x"}, state)

        assert "refresh_token_rejected_at" not in dacli.load_state(), (
            "a 502 must not be recorded as DA rejecting the grant"
        )

    def test_a_4xx_from_the_token_endpoint_is_a_rejection(self, isolated_paths, no_keychain):
        """The control for the 5xx case: a real rejection still marks."""
        import io

        err = urllib.error.HTTPError(
            url="x", code=400, msg="bad request", hdrs={}, fp=io.BytesIO(b"invalid_grant")
        )
        state = {"refresh_token": "rt", "expires_at": 0}
        with (
            patch.object(dacli, "http_post_json", side_effect=err),
            pytest.raises(dacli.AuthError, match="400"),
        ):
            dacli.access_token({"client_id": "x"}, state)

        assert dacli.load_state().get("refresh_token_rejected_at")

    def test_the_cli_still_exits_2(self, isolated_paths, no_keychain, capsys):
        """The contract the shell sees is unchanged.

        main() catches DacliError, so replacing sys.exit with a raise must
        not alter any documented exit code.
        """
        with pytest.raises(SystemExit) as exc:
            dacli.main(["whoami"])

        assert exc.value.code == 2
        assert "Traceback" not in capsys.readouterr().err


class TestVerboseActuallyEmits:
    """`-v` promised debug output and had no producer anywhere.

    `log(msg, "debug")` was supported by output.py and called by nothing
    in dacli/, so the flag was inert: `da -v` printed exactly what `da`
    printed. Requests and retries now emit at debug level.
    """

    def _capture(self, verbose, capsys):
        dacli._configure_output(quiet=False, verbose=verbose, color="never")
        try:
            with patch(
                "urllib.request.urlopen", return_value=FakeResponse(json.dumps({"ok": 1}).encode())
            ):
                dacli.http_json("https://example.invalid/api/v1/thing?limit=2")
        finally:
            dacli._configure_output(quiet=False, verbose=False, color="never")
        return capsys.readouterr()

    def test_quiet_by_default(self, capsys):
        out = self._capture(False, capsys)
        assert "GET " not in (out.out + out.err)

    def test_verbose_logs_the_request(self, capsys):
        out = self._capture(True, capsys)
        combined = out.out + out.err
        assert "GET " in combined, f"-v produced no request line:\n{combined}"
        assert "example.invalid" in combined

    def test_verbose_logs_each_retry(self, capsys):
        dacli._configure_output(quiet=False, verbose=True, color="never")
        attempts = []

        def failing(*a, **kw):
            attempts.append(1)
            raise urllib.error.HTTPError("u", 500, "Server Error", {}, None)

        try:
            with patch("urllib.request.urlopen", side_effect=failing):
                with patch("time.sleep"):
                    with pytest.raises(urllib.error.HTTPError):
                        dacli.http_json("https://example.invalid/x", retries=2)
        finally:
            dacli._configure_output(quiet=False, verbose=False, color="never")
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert combined.count("retry") == 2, f"expected 2 retry lines:\n{combined}"


class TestDebugOutputCannotLeakCredentials:
    """`-v` output is what people paste into bug reports.

    Tokens travel in the Authorization header today, so nothing puts one
    in a URL — but a debug line that leaked one would be worse than no
    debug line, and the cost of the guard is one regex.
    """

    @pytest.mark.parametrize(
        ("url", "secret"),
        [
            ("https://x/api?access_token=SECRET123&limit=5", "SECRET123"),
            ("https://x/oauth2/token?client_secret=SHHH&grant_type=x", "SHHH"),
            ("https://x/callback?code=AUTHCODE&state=1", "AUTHCODE"),
        ],
    )
    def test_secrets_are_redacted(self, url, secret):
        out = dacli.net._redact(url)
        assert secret not in out, f"{secret} survived redaction: {out}"
        assert "<redacted>" in out

    def test_ordinary_parameters_survive(self):
        out = dacli.net._redact("https://x/api?limit=5&offset=24&mature_content=true")
        assert out == "https://x/api?limit=5&offset=24&mature_content=true"
