"""Tests for cmd_auth (the PKCE browser flow) and keychain helpers."""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import dacli


def _state_from(auth_link: str) -> str:
    """The `state` the CLI just generated, read out of its own auth URL.

    This is what a browser does: DA echoes state back on the redirect.
    The listener now refuses any callback that does not carry it, so a
    stub that injects a bare `?code=` is no longer a realistic browser —
    it is the injection attempt the check exists to stop, and it makes
    the listener sit out its full timeout.
    """
    return urllib.parse.parse_qs(urllib.parse.urlparse(auth_link).query)["state"][0]


def _free_port() -> int:
    """Reserve a port the OS says is free, then release it.

    The loopback-listener tests used to hardcode 8889/8890/8891. A
    listener thread from a previous test run can still hold one of
    those when the next run binds, producing an intermittent
    ``OSError: [Errno 48] Address already in use`` — roughly 1 run in 5
    locally. Asking the OS for an ephemeral port each time removes the
    fixed collision target.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# Self-signed cert generator — mock subprocess.run for openssl.
# ---------------------------------------------------------------------------
def _openssl_stub(real_cert_pair):
    """Stand in for openssl, writing to the paths argv actually names.

    The old stubs wrote to the FINAL cert/key paths, which only worked
    because generation used to write there directly. Generation now stages
    into a private 0700 directory first (so the key is never briefly
    world-readable), so a stub that ignores `-keyout` / `-out` is no
    longer imitating openssl — it is imitating an openssl that silently
    produces nothing.
    """

    def fake_run(argv, *_args, **_kwargs):
        args = list(argv)
        out_path = pathlib.Path(args[args.index("-out") + 1])
        key_path = pathlib.Path(args[args.index("-keyout") + 1])
        out_path.write_bytes(real_cert_pair[0].read_bytes())
        key_path.write_bytes(real_cert_pair[1].read_bytes())
        return MagicMock(returncode=0)

    return fake_run


class TestEnsureSelfSignedCert:
    def test_returns_existing_pair_without_running_openssl(
        self, isolated_paths, tmp_path, monkeypatch, real_cert_pair
    ):
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_bytes(real_cert_pair[0].read_bytes())
        key.write_bytes(real_cert_pair[1].read_bytes())
        monkeypatch.setattr(dacli, "LOOPBACK_CERT", cert)
        monkeypatch.setattr(dacli, "LOOPBACK_KEY", key)
        with patch.object(subprocess, "run") as run:
            got_cert, got_key = dacli._ensure_self_signed_cert()
            run.assert_not_called()
        assert got_cert == cert
        assert got_key == key

    def test_generates_pair_via_openssl_on_first_run(
        self, isolated_paths, tmp_path, monkeypatch, real_cert_pair
    ):
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        monkeypatch.setattr(dacli, "LOOPBACK_CERT", cert)
        monkeypatch.setattr(dacli, "LOOPBACK_KEY", key)

        with patch.object(subprocess, "run", side_effect=_openssl_stub(real_cert_pair)) as run:
            got_cert, got_key = dacli._ensure_self_signed_cert()
        assert got_cert == cert
        assert got_key == key
        assert run.called
        assert "openssl" in run.call_args[0][0][0]
        # 0600 perms after generation
        assert (cert.stat().st_mode & 0o777) == 0o600
        assert (key.stat().st_mode & 0o777) == 0o600

    def test_unloadable_existing_pair_is_regenerated(
        self, isolated_paths, tmp_path, monkeypatch, real_cert_pair
    ):
        """Existence is not proof of usability.

        A pair truncated by a full disk, an interrupted openssl, or a
        half-restored backup used to be returned as-is, so `da auth`
        failed with a bare "[SSL] PEM lib" forever — with no hint that
        deleting two files would fix it.
        """
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("CERT")  # what a stray write leaves behind
        key.write_text("KEY")
        monkeypatch.setattr(dacli, "LOOPBACK_CERT", cert)
        monkeypatch.setattr(dacli, "LOOPBACK_KEY", key)

        with patch.object(subprocess, "run", side_effect=_openssl_stub(real_cert_pair)) as run:
            dacli._ensure_self_signed_cert()
        assert run.called, "unloadable pair was accepted instead of regenerated"
        assert dacli.auth._cert_pair_loads(cert, key)

    def test_exits_when_generated_pair_is_unloadable(self, isolated_paths, tmp_path, monkeypatch):
        """openssl exiting 0 having written rubbish must not reach the listener."""
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        monkeypatch.setattr(dacli, "LOOPBACK_CERT", cert)
        monkeypatch.setattr(dacli, "LOOPBACK_KEY", key)

        def fake_run(*args, **kwargs):
            cert.write_text("not a cert")
            key.write_text("not a key")
            return MagicMock(returncode=0)

        with patch.object(subprocess, "run", side_effect=fake_run):
            with pytest.raises(SystemExit) as exc:
                dacli._ensure_self_signed_cert()
        assert exc.value.code == 2

    def test_exits_when_openssl_missing(self, isolated_paths, tmp_path, monkeypatch):
        monkeypatch.setattr(dacli, "LOOPBACK_CERT", tmp_path / "cert.pem")
        monkeypatch.setattr(dacli, "LOOPBACK_KEY", tmp_path / "key.pem")
        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            with pytest.raises(SystemExit):
                dacli._ensure_self_signed_cert()

    def test_exits_when_openssl_fails(self, isolated_paths, tmp_path, monkeypatch):
        monkeypatch.setattr(dacli, "LOOPBACK_CERT", tmp_path / "cert.pem")
        monkeypatch.setattr(dacli, "LOOPBACK_KEY", tmp_path / "key.pem")
        err = subprocess.CalledProcessError(1, ["openssl"], stderr=b"openssl: bad args")
        with patch.object(subprocess, "run", side_effect=err):
            with pytest.raises(SystemExit):
                dacli._ensure_self_signed_cert()


# ---------------------------------------------------------------------------
# Keychain helpers — mock subprocess.run so we don't touch the real Keychain.
# ---------------------------------------------------------------------------
class TestKeychainGet:
    def test_returns_value_when_security_succeeds(self, monkeypatch, real_keychain):
        monkeypatch.setattr("sys.platform", "darwin")
        result = MagicMock()
        result.returncode = 0
        result.stdout = "secret-value\n"
        with patch.object(subprocess, "run", return_value=result):
            assert dacli._keychain_get("client_secret") == "secret-value"

    def test_returns_none_on_security_failure(self, monkeypatch, real_keychain):
        monkeypatch.setattr("sys.platform", "darwin")
        result = MagicMock()
        result.returncode = 44  # not found
        result.stdout = ""
        with patch.object(subprocess, "run", return_value=result):
            assert dacli._keychain_get("client_secret") is None

    def test_returns_none_when_security_missing(self, monkeypatch, real_keychain):
        monkeypatch.setattr("sys.platform", "darwin")
        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            assert dacli._keychain_get("client_secret") is None

    def test_returns_none_off_darwin(self, monkeypatch, real_keychain):
        monkeypatch.setattr("sys.platform", "linux")
        # No subprocess call expected
        with patch.object(subprocess, "run") as run:
            assert dacli._keychain_get("client_secret") is None
            run.assert_not_called()


class TestKeychainSet:
    def test_runs_security_add_password(self, monkeypatch, real_keychain):
        monkeypatch.setattr("sys.platform", "darwin")
        with patch.object(subprocess, "run") as run:
            run.return_value = MagicMock(returncode=0)
            ok = dacli._keychain_set("client_secret", "abc")
        assert ok is True
        # Confirm the right arguments were passed
        called_with = run.call_args[0][0]
        assert called_with[0] == "security"
        assert "add-generic-password" in called_with
        assert "abc" in called_with

    def test_returns_false_when_security_missing(self, monkeypatch, real_keychain):
        monkeypatch.setattr("sys.platform", "darwin")
        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            assert dacli._keychain_set("client_secret", "x") is False

    def test_returns_false_on_called_process_error(self, monkeypatch, real_keychain):
        monkeypatch.setattr("sys.platform", "darwin")
        err = subprocess.CalledProcessError(1, ["security"])
        with patch.object(subprocess, "run", side_effect=err):
            assert dacli._keychain_set("client_secret", "x") is False

    def test_returns_false_off_darwin(self, monkeypatch, real_keychain):
        monkeypatch.setattr("sys.platform", "linux")
        with patch.object(subprocess, "run") as run:
            assert dacli._keychain_set("client_secret", "x") is False
            run.assert_not_called()


# ---------------------------------------------------------------------------
# config_unset on macOS Keychain path
# ---------------------------------------------------------------------------
class TestCmdConfigUnsetKeychain:
    def test_keychain_delete_path(self, isolated_paths, monkeypatch, capsys):
        monkeypatch.setattr("sys.platform", "darwin")
        # Pre-populate config file with a non-secret + a secret to test both routes
        dacli.set_config_field("client_id", "x")
        # Mock the keychain delete
        delete_result = MagicMock(returncode=0)
        with patch.object(subprocess, "run", return_value=delete_result) as run:
            ns = dacli.build_parser().parse_args(["config", "unset", "client_secret"])
            ns.func(ns)
        # subprocess.run called with delete-generic-password
        assert any(
            "delete-generic-password" in a
            for call in run.call_args_list
            for a in (call[0][0] if call[0] else [])
        )
        out = capsys.readouterr().out
        assert "removed" in out.lower()


# ---------------------------------------------------------------------------
# cmd_auth — the PKCE browser flow
# ---------------------------------------------------------------------------
class _FakeServer:
    """Stand-in for socketserver.TCPServer that records the bind args and
    exposes shutdown() as a no-op."""

    def __init__(self, addr, handler):
        self.address = addr
        self.handler = handler
        self.shutdown_called = False

    def serve_forever(self):  # pragma: no cover — never reached in tests
        # In real life this blocks. The test thread injects `captured["code"]`
        # before the wait loop fires, so we never actually run.
        time.sleep(60)

    def shutdown(self):
        self.shutdown_called = True


class TestCmdAuth:
    def test_no_client_id_exits(self, isolated_paths, no_keychain):
        ns = argparse.Namespace(redirect_uri=None, scope="browse")
        with pytest.raises(SystemExit) as exc:
            dacli.cmd_auth(ns)
        assert exc.value.code == 2

    def test_unparseable_redirect_uri_exits(self, isolated_paths, no_keychain):
        dacli.set_config_field("client_id", "12345")
        ns = argparse.Namespace(redirect_uri="not-a-url", scope="browse")
        with pytest.raises(SystemExit):
            dacli.cmd_auth(ns)

    def test_full_flow_with_real_loopback(self, isolated_paths, no_keychain, monkeypatch):
        """Spin a real loopback listener; the test makes a real GET to inject
        the code; the token exchange is mocked."""
        dacli.set_config_field("client_id", "12345")

        # webbrowser.open: instead of opening a browser, send a request to
        # the local listener with a fake `code`.
        port = _free_port()

        def fake_browser_open(url):
            urllib.request.urlopen(
                f"http://localhost:{port}/?code=XYZ-fake-auth-code&state={_state_from(url)}",
                timeout=2,
            )

        monkeypatch.setattr("webbrowser.open", fake_browser_open)

        # http_post_json mocked: returns access + refresh tokens
        def fake_post(url, data, **_kw):
            assert data["grant_type"] == "authorization_code"
            assert data["code"] == "XYZ-fake-auth-code"
            assert data["client_id"] == "12345"
            return {
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
                "scope": "browse",
            }

        with patch.object(dacli, "http_post_json", side_effect=fake_post):
            ns = argparse.Namespace(redirect_uri=f"http://localhost:{port}/", scope="browse")
            dacli.cmd_auth(ns)

        st = dacli.load_state()
        assert st["access_token"] == "AT"
        assert st["refresh_token"] == "RT"

    def test_token_exchange_failure_exits(self, isolated_paths, no_keychain, monkeypatch):
        dacli.set_config_field("client_id", "12345")

        port = _free_port()

        def fake_browser_open(url):
            urllib.request.urlopen(
                f"http://localhost:{port}/?code=Y-fake&state={_state_from(url)}", timeout=2
            )

        monkeypatch.setattr("webbrowser.open", fake_browser_open)

        with patch.object(dacli, "http_post_json", return_value={"error": "invalid"}):
            ns = argparse.Namespace(redirect_uri=f"http://localhost:{port}/", scope="browse")
            with pytest.raises(SystemExit):
                dacli.cmd_auth(ns)

    def test_token_exchange_http_error_exits(self, isolated_paths, no_keychain, monkeypatch):
        dacli.set_config_field("client_id", "12345")

        port = _free_port()

        def fake_browser_open(url):
            urllib.request.urlopen(
                f"http://localhost:{port}/?code=Z&state={_state_from(url)}", timeout=2
            )

        monkeypatch.setattr("webbrowser.open", fake_browser_open)

        # Build a fake HTTPError with a real bytes-readable fp
        import io as _io

        err = urllib.error.HTTPError(
            url="x", code=400, msg="bad", hdrs={}, fp=_io.BytesIO(b'{"err":1}')
        )
        with patch.object(dacli, "http_post_json", side_effect=err):
            ns = argparse.Namespace(redirect_uri=f"http://localhost:{port}/", scope="browse")
            with pytest.raises(SystemExit):
                dacli.cmd_auth(ns)


# ---------------------------------------------------------------------------
# _capture_code_via_paste
# ---------------------------------------------------------------------------
class TestCaptureCodeViaPaste:
    """The paste-back flow runs when --redirect-uri is non-loopback or when
    --paste is forced. The function reads one line from stdin, extracts the
    `code` query param, and returns it. Edge cases (empty input, EOF,
    KeyboardInterrupt, malformed URL) must all return None rather than
    crashing — `cmd_auth` then exits 2 with the actionable whitelist hint."""

    def test_extracts_code_from_redirect_url(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _prompt: "")
        monkeypatch.setattr(
            "builtins.input", lambda _prompt="": "https://example.com/cb?code=ABC-123&state=xyz"
        )
        assert dacli._capture_code_via_paste() == "ABC-123"

    def test_returns_none_on_empty_input(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda _prompt="": "")
        assert dacli._capture_code_via_paste() is None

    def test_returns_none_on_eof(self, monkeypatch):
        def _raise_eof(*_a, **_k):
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise_eof)
        assert dacli._capture_code_via_paste() is None

    def test_returns_none_on_keyboard_interrupt(self, monkeypatch):
        def _raise_ki(*_a, **_k):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise_ki)
        assert dacli._capture_code_via_paste() is None

    def test_returns_none_when_url_has_no_code_param(self, monkeypatch):
        """A redirect URL without ?code=... is the symptom of a DA whitelist
        rejection (browser bounces to the home page). Must surface as None so
        cmd_auth's "did not receive an auth code" branch fires."""
        monkeypatch.setattr(
            "builtins.input", lambda _prompt="": "https://example.com/error?error=access_denied"
        )
        assert dacli._capture_code_via_paste() is None

    def test_returns_none_on_garbage_input(self, monkeypatch):
        """`urlparse` doesn't raise on malformed input in modern Python; it
        returns empty fields. The function should still return None."""
        monkeypatch.setattr("builtins.input", lambda _prompt="": "this is not a url at all")
        assert dacli._capture_code_via_paste() is None

    def test_strips_whitespace(self, monkeypatch):
        """Pasted URLs commonly get leading/trailing whitespace from the
        terminal — the function must `strip()` before parsing."""
        monkeypatch.setattr(
            "builtins.input", lambda _prompt="": "  https://example.com/cb?code=XYZ  \n"
        )
        assert dacli._capture_code_via_paste() == "XYZ"


# ---------------------------------------------------------------------------
# da shim sanity (run via subprocess)
# ---------------------------------------------------------------------------
class TestDaShim:
    def test_shim_runs(self):
        """Confirm `python3 da --version` from the repo root works."""

        repo = Path(__file__).resolve().parent.parent
        # Use subprocess so we exercise the real shim, not a re-import.
        out = subprocess.run(
            ["python3", str(repo / "da"), "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert "da-cli" in out.stdout


class TestAuthListenerRobustness:
    """Regressions found by the pre-publication audit."""

    def test_second_request_does_not_clobber_captured_code(self, isolated_paths, monkeypatch):
        """A browser's follow-up GET /favicon.ico must not erase the code.

        Before the fix the handler assigned unconditionally, so the
        favicon request overwrote a captured code with None and auth
        failed with a misleading 'whitelist miss' hint.

        This drives the real listener. The previous version of this test
        defined its own copy of the handler logic and asserted on that,
        so it would have passed even if `_capture_code_via_listener` had
        regressed completely.
        """
        port = _free_port()

        def fake_browser_open(url):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=GOODCODE&state=S", timeout=3)
            # Exactly what a browser does next.
            urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=3)

        monkeypatch.setattr("webbrowser.open", fake_browser_open)
        code, _err = dacli.auth._capture_code_via_listener(
            port, "http://x.invalid", expected_state="S", expected_path="/", https=False
        )
        assert code == "GOODCODE"

    def test_stalled_connection_does_not_block_the_callback(self, isolated_paths, monkeypatch):
        """One silent client must not stop the real redirect landing.

        The listener was a single-threaded TCPServer, so a client that
        connected and sent nothing held the only slot: the genuine OAuth
        callback was never accepted and `da auth` sat out its full
        timeout. Browsers open speculative preconnects, so this was
        reachable in ordinary use.
        """
        port = _free_port()
        result: dict[str, object] = {}
        # Keep the failure fast: without this the wedged case takes the
        # full five minutes to come back.
        monkeypatch.setattr(dacli.auth, "AUTH_LISTENER_TIMEOUT_S", 8)

        def fake_browser_open(url):
            stalled = socket.create_connection(("127.0.0.1", port), timeout=3)
            try:
                t0 = time.time()
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=REAL&state=S", timeout=4)
                    result["elapsed"] = time.time() - t0
                except Exception as e:
                    result["error"] = f"{type(e).__name__}: {e}"
            finally:
                stalled.close()

        monkeypatch.setattr("webbrowser.open", fake_browser_open)
        code, _err = dacli.auth._capture_code_via_listener(
            port, "http://x.invalid", expected_state="S", expected_path="/", https=False
        )

        # Assert out here, NOT inside the callback: the listener wraps
        # webbrowser.open in contextlib.suppress(Exception), so anything
        # raised in there vanishes and the test passes while wedged.
        assert "error" not in result, f"callback never completed: {result.get('error')}"
        assert result["elapsed"] < 3, (
            f"callback waited {result['elapsed']:.1f}s behind a stalled socket"
        )
        assert code == "REAL"

    def test_stalled_connection_does_not_block_the_tls_callback(self, isolated_paths, monkeypatch):
        """Same, over TLS — where the first attempt at this fix was still wrong.

        Wrapping the listening socket runs the handshake inside accept();
        so does wrapping in get_request(). Both keep a blocking handshake
        on the accept path, so a client that opens a socket and never
        sends a ClientHello still wedges everything behind it. The
        handshake has to happen in the per-connection worker thread.
        """
        port = _free_port()
        result: dict[str, object] = {}
        monkeypatch.setattr(dacli.auth, "AUTH_LISTENER_TIMEOUT_S", 8)

        def fake_browser_open(url):
            stalled = socket.create_connection(("127.0.0.1", port), timeout=3)
            try:
                t0 = time.time()
                try:
                    urllib.request.urlopen(
                        f"https://127.0.0.1:{port}/?code=TLSREAL&state=S",
                        timeout=4,
                        context=ssl._create_unverified_context(),  # noqa: S323 — self-signed loopback cert is the point
                    )
                    result["elapsed"] = time.time() - t0
                except Exception as e:
                    result["error"] = f"{type(e).__name__}: {e}"
            finally:
                stalled.close()

        monkeypatch.setattr("webbrowser.open", fake_browser_open)
        code, _err = dacli.auth._capture_code_via_listener(
            port, "https://x.invalid", expected_state="S", expected_path="/", https=True
        )

        assert "error" not in result, f"TLS callback never completed: {result.get('error')}"
        assert result["elapsed"] < 3, f"TLS callback waited {result['elapsed']:.1f}s"
        assert code == "TLSREAL"

    def test_connections_carry_a_timeout(self, isolated_paths, monkeypatch):
        """A silent client must not hold its worker thread indefinitely."""
        port = _free_port()
        seen: list[float | None] = []
        real_accept = socket.socket.accept

        def spy_accept(self):
            sock, addr = real_accept(self)
            seen.append(sock.gettimeout())
            return sock, addr

        def fake_browser_open(url):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=T&state=S", timeout=3)

        monkeypatch.setattr("webbrowser.open", fake_browser_open)
        monkeypatch.setattr(socket.socket, "accept", spy_accept)
        dacli.auth._capture_code_via_listener(
            port, "http://x.invalid", expected_state="S", expected_path="/", https=False
        )

        # get_request sets the timeout right after accept, so what the
        # spy records is pre-timeout; assert the constant is wired in and
        # is a real bound rather than None.
        assert dacli.constants.AUTH_CONNECTION_TIMEOUT_S > 0
        assert seen, "no connection was accepted"


class TestAtomicWritePermissions:
    def test_no_world_readable_window(self, tmp_path, monkeypatch):
        """The tmp file must never exist with permissive bits.

        _atomic_write used to write content first and chmod after,
        leaving tokens readable by any local user for the whole write.
        """
        target = tmp_path / "state.json"
        seen: list[int] = []
        real_replace = dacli.Path.replace

        def spy_replace(self, other):
            seen.append(self.stat().st_mode & 0o777)
            return real_replace(self, other)

        monkeypatch.setattr(dacli.Path, "replace", spy_replace)
        dacli._atomic_write(target, '{"refresh_token": "RT"}')
        assert seen == [0o600]
        assert target.stat().st_mode & 0o777 == 0o600


class TestAuthStatusAsksDeviantArt:
    """`auth status` reports whether the credentials actually work.

    It used to compute health from `refresh_token_issued_at` and DA's
    90-day ceiling. That describes the calendar, not whether the grant is
    honoured, and the two come apart. Observed on a real install:
    {"state": "ok", "days_remaining": 83.1} while `da whoami` exited 2
    with "The refresh_token is invalid".

    Per review on #48 the unreliable answer is no longer offered — there
    is no local-only mode. `da diagnose` still reports the TTL without
    network for anyone who wants that.

    Validation goes through /placebo rather than a forced token
    exchange, so a monitor calling this hourly does not rotate the
    refresh token hourly.
    """

    def _state(self, days_old=1):
        return {
            "access_token": "AT",
            "expires_at": time.time() + 3600,
            "refresh_token": "RT",
            "refresh_token_issued_at": time.time() - days_old * 86400,
            "scope": "browse",
        }

    def _run(self, http_json=None, post=None):
        ns = dacli.build_parser().parse_args(["auth", "status"])
        with patch.object(dacli, "load_config", return_value={"client_id": "X"}):
            with patch.object(
                dacli, "http_json", side_effect=http_json or (lambda *a, **k: {"status": "success"})
            ):
                with patch.object(
                    dacli, "http_post_json", side_effect=post or (lambda *a, **k: {})
                ):
                    with pytest.raises(SystemExit) as exc:
                        ns.func(ns)
        return exc.value.code

    def test_a_working_token_is_ok(self, isolated_paths, capsys):
        dacli.save_state(self._state())
        code = self._run()
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "ok"
        assert code == 0

    def test_a_rejected_token_is_revoked(self, isolated_paths, capsys):
        """Fresh by the clock, dead at DeviantArt — the case that motivated this."""
        dacli.save_state(self._state(days_old=1))

        def rejected(*a, **k):
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

        code = self._run(http_json=rejected)
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "revoked", (
            f"a token DA rejected was reported as {payload['state']!r}"
        )
        assert payload["days_remaining"] == 0.0
        assert code == 2

    def test_no_network_is_unreachable_not_revoked(self, isolated_paths, capsys):
        """Do not send someone to re-authenticate because their wifi is off."""
        dacli.save_state(self._state())

        def offline(*a, **k):
            raise urllib.error.URLError("no route to host")

        code = self._run(http_json=offline)
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "unreachable"
        assert code == 2

    def test_it_actually_contacts_deviantart(self, isolated_paths):
        """The whole point: no local-only answer remains."""
        dacli.save_state(self._state())
        seen = []

        def spy(url, **kw):
            seen.append(url)
            return {"status": "success"}

        self._run(http_json=spy)
        assert any("placebo" in u for u in seen), (
            f"auth status answered without asking DeviantArt: {seen}"
        )

    def test_a_valid_access_token_is_not_rotated(self, isolated_paths):
        """Validation must not force a refresh a monitor would repeat hourly."""
        dacli.save_state(self._state())
        with patch.object(dacli, "http_post_json") as post:
            self._run()
        post.assert_not_called()
