"""OAuth 2.1 (PKCE) lifecycle: authorise, refresh, and identity.

Extracted verbatim from the single-file module; see ADR 0007.

Reads through the ``dacli`` package at call time rather than importing:

* ``http_json`` / ``http_post_json`` — stubbed by the suite so no test
  reaches DeviantArt.
* ``LOOPBACK_CERT`` / ``LOOPBACK_KEY`` / ``STATE_DIR`` — repointed at a
  temp dir, so tests never write a TLS key into the real state dir.
* ``load_state`` / ``save_state`` / ``load_config`` — swapped wholesale
  by ``cmd_bench``.
* ``log`` — also swapped by ``cmd_bench --json``; a direct import here
  would leak human-readable lines into machine-readable output.
"""

import argparse
import base64
import contextlib
import hashlib
import hmac
import http.server
import json
import os
import secrets
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import dacli

from .constants import (
    API_BASE,
    AUTH_CONNECTION_TIMEOUT_S,
    AUTH_DEFAULT_PORT,
    AUTH_LISTENER_TIMEOUT_S,
    AUTH_URL,
    LOG_BODY_TRUNCATE,
    LOOPBACK_CERT_KEY_BITS,
    LOOPBACK_CERT_VALIDITY_DAYS,
    REFRESH_TOKEN_CRIT_DAYS,
    REFRESH_TOKEN_TTL_DAYS,
    REFRESH_TOKEN_WARN_DAYS,
    STATE_DIR,
    TOKEN_REFRESH_SKEW_S,
    TOKEN_URL,
)
from .errors import AuthError, ConfigError, HttpError
from .lock import _token_lock
from .net import RETRYABLE_HTTP_CODES


# --------------------------------------------------------------------------
# OAuth 2.1 (PKCE) lifecycle
# --------------------------------------------------------------------------
def access_token(
    cfg: dict[str, str],
    state: dict[str, object],
    *,
    force_refresh: bool = False,
) -> str:
    """Return a valid access token, refreshing when needed.

    `force_refresh=True` skips the cached-token-still-fresh check and
    always exchanges the refresh_token at DA's token endpoint. Used by
    `_authed_http_json` after a 401 to recover from a server-side grant
    revocation (the cached token's local expires_at says it's still
    valid but DA already invalidated it).
    """
    if not force_refresh and _token_is_fresh(state):
        cached = state["access_token"]
        assert isinstance(cached, str)
        return cached

    # Everything below rotates a one-time credential, so only one process
    # may be in here at a time. DeviantArt issues a NEW refresh token on
    # every exchange and consumes the old one; two processes presenting
    # the same one means the loser gets 400 invalid_grant and dies. The
    # collision is routine rather than exotic: examples/diagnose-cron.sh
    # runs `da diagnose` every ten minutes alongside the nightly sync,
    # and diagnose forces a refresh whenever the access token is stale.
    with _token_lock():
        # Re-read under the lock. Waiting for the holder usually means the
        # holder just did this work — adopting their result is both
        # cheaper and safer than rotating again, because every extra
        # rotation is another chance to strand a token.
        current = dacli.load_state()
        if _token_is_fresh(current) and current.get("access_token") != state.get("access_token"):
            _adopt(state, current)
            adopted = current["access_token"]
            assert isinstance(adopted, str)
            dacli.log("another da process had already refreshed; using its token", "debug")
            return adopted

        # The newest refresh token wins. `state` was loaded when this
        # process started and can be hours old; `current` is what is on
        # disk right now, which is what DA will accept.
        refresh_token = current.get("refresh_token") or state.get("refresh_token")
        if not refresh_token:
            raise ConfigError("no refresh_token stored — run `da auth` first")
        if not cfg.get("client_id"):
            raise ConfigError(
                "no client_id configured — set DA_CLIENT_ID or `da config set client_id <ID>`"
            )
        dacli.log("refreshing access token via refresh_token", "warn")
        payload = {
            "grant_type": "refresh_token",
            "client_id": cfg["client_id"],
            "refresh_token": str(refresh_token),
        }
        if cfg.get("client_secret"):
            payload["client_secret"] = cfg["client_secret"]
        try:
            # retries=0, deliberately. A refresh token is single-use: if
            # the first attempt reached DA and the response was lost, DA
            # has already rotated and the replacement exists only in that
            # lost response. Retrying then presents a consumed token and
            # turns "we do not know" into a guaranteed invalid_grant.
            body = dacli.http_post_json(TOKEN_URL, payload, retries=0)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:LOG_BODY_TRUNCATE]
            if e.code in RETRYABLE_HTTP_CODES:
                # DA's auth server having a bad minute is not a rejected
                # grant. Saying "run `da auth`" here costs the user a
                # browser round trip for nothing, and marking the grant
                # rejected then makes the next `sync watched` abort on its
                # first per-artist hiccup. Retrying is not an option
                # either — see retries=0 above — so report and stop.
                raise HttpError(
                    f"DeviantArt's auth server returned HTTP {e.code}: {detail}. "
                    f"This is usually temporary; the next run will retry."
                ) from e
            # A 4xx is DA telling us the grant is gone. Record it, so
            # callers looping over many items can tell "this one failed"
            # from "nothing will work again". `sync watched` had a guard
            # for exactly this and it could never fire: it tested whether
            # refresh_token was still present, and nothing removed it.
            #
            # Marked rather than deleted: `da auth status` needs the token
            # to still be there to report `revoked` instead of `unknown`.
            current["refresh_token_rejected_at"] = time.time()
            dacli.save_state(current)
            raise AuthError(
                f"DeviantArt rejected the refresh token (HTTP {e.code}): {detail}. "
                f"Run `da auth` to sign in again."
            ) from e
        except (urllib.error.URLError, TimeoutError) as e:
            # Not a rejection at all: the grant is probably fine and the
            # network is not. Distinguishable by type so `da auth status`
            # can report `unreachable` rather than sending someone to
            # re-authenticate because their wifi dropped.
            raise HttpError(f"could not reach DeviantArt to refresh the token: {e}") from e
        if "access_token" not in body:
            raise AuthError(f"unexpected refresh response from DeviantArt: {body}")

        # Merge into a freshly-read state rather than writing back the
        # snapshot this process started with, which would silently revert
        # anything another command wrote in the meantime.
        current["access_token"] = body["access_token"]
        current["expires_at"] = time.time() + body.get("expires_in", 3600)
        if body.get("refresh_token"):
            current["refresh_token"] = body["refresh_token"]
            # Reset the 90-day chain clock whenever DA rotates. This is
            # the only place a new refresh_token arrives.
            current["refresh_token_issued_at"] = time.time()
        # A working exchange clears any earlier rejection: the grant is
        # demonstrably live again, and a stale mark would make the next
        # `sync watched` abort on the first per-artist failure.
        current.pop("refresh_token_rejected_at", None)
        dacli.save_state(current)
        _adopt(state, current)

    tok = state["access_token"]
    assert isinstance(tok, str)
    return tok


def _token_is_fresh(state: dict[str, object]) -> bool:
    """Is this state's access token present and not about to expire?"""
    token = state.get("access_token")
    expires_at = state.get("expires_at", 0)
    if not isinstance(token, str) or not token:
        return False
    if not isinstance(expires_at, (int, float)):
        return False
    return time.time() < expires_at - TOKEN_REFRESH_SKEW_S


def _adopt(state: dict[str, object], source: dict[str, object]) -> None:
    """Copy the token fields from ``source`` into the caller's ``state``.

    Callers hold their state dict across the call and read it afterwards
    (``da auth status`` reports on it, the sync loop re-reads
    ``access_token`` per request), so updating it in place is part of the
    contract — returning the token alone would leave them using a value
    that is no longer on disk.
    """
    for key in ("access_token", "expires_at", "refresh_token", "refresh_token_issued_at"):
        if key in source:
            state[key] = source[key]


def authed_http_json(
    url: str,
    cfg: dict[str, str],
    state: dict[str, object],
    **kwargs: object,
) -> dict[str, object]:
    """Wrap dacli.http_json with on-401-refresh-and-retry-once logic.

    The cached access_token in state may be locally valid (expires_at
    in the future) but server-side revoked — happens after a re-auth
    rotates the refresh_token chain, or DA invalidates a grant. When
    the call raises 401, force a fresh refresh and try once more. If
    the retry still 401s, the refresh_token is itself stale and the
    user needs to run `da auth` interactively.
    """
    token = access_token(cfg, state)
    try:
        return dacli.http_json(url, token=token, **kwargs)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        dacli.log("access_token rejected by DA (HTTP 401); forcing refresh and retrying", "warn")
        token = access_token(cfg, state, force_refresh=True)
        try:
            return dacli.http_json(url, token=token, **kwargs)
        except urllib.error.HTTPError as e2:
            if e2.code == 401:
                dacli.log(
                    "still 401 after forced refresh — refresh_token itself is "
                    "rejected. Run `da auth` to re-issue.",
                    "error",
                )
                sys.exit(2)
            raise


def _is_loopback(host: str) -> bool:
    """Can the local listener serve a callback on this host?

    ``::1`` is deliberately absent. It is a loopback address, but
    ``_ReusableServer`` binds ``("127.0.0.1", port)`` — AF_INET only — and
    the generated cert's SAN covers ``DNS:localhost,IP:127.0.0.1``. So
    accepting ``::1`` here took the listener path and then never received
    the redirect: the browser got ECONNREFUSED and `da auth` burned its
    full five-minute timeout before blaming the redirect_uri whitelist.

    Returning False sends ``https://[::1]:8765/`` down the ``--paste``
    path instead, which works. Binding dual-stack and extending the SAN
    would be the richer fix; refusing to claim support we do not have is
    the honest one.
    """
    return host in ("localhost", "127.0.0.1")


# Derived once at import, exactly as in the single-file module. Tests patch
# these on the package (dacli.LOOPBACK_CERT = tmp), so every read below goes
# through dacli.* — but the definitions themselves must be plain module
# globals for the re-export in __init__ to bind.
LOOPBACK_CERT = STATE_DIR / "loopback-cert.pem"
LOOPBACK_KEY = STATE_DIR / "loopback-key.pem"


def _cert_pair_loads(cert: Path, key: Path) -> bool:
    """Can this pair actually terminate TLS?

    Exactly the operation the listener performs, so anything that would
    fail there fails here first, while it is still cheap to recover.
    """
    try:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    except (OSError, ValueError):  # ssl.SSLError is an OSError
        return False
    return True


def _lock_down(*paths: Path) -> None:
    """Force 0600 on files that must not be readable by other local users.

    Tolerant of a missing file: callers reach here on paths they have just
    verified, and racing an operator's `rm` is not worth an exception.
    """
    for path in paths:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def _ensure_self_signed_cert() -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating a self-signed pair on first
    use. Required because DA's developer dashboard rejects HTTP entries in
    the OAuth Redirect URI Whitelist — only HTTPS values are accepted —
    so the loopback listener has to speak TLS. The cert is self-signed
    and only used to terminate the localhost callback; the browser will
    show a one-time warning the user clicks through. Stored in dacli.STATE_DIR
    with 0600 perms.
    """
    if dacli.LOOPBACK_CERT.exists() and dacli.LOOPBACK_KEY.exists():
        if _cert_pair_loads(dacli.LOOPBACK_CERT, dacli.LOOPBACK_KEY):
            # Re-assert the mode every time, not just at generation. A
            # pair written by an interrupted run is fully valid, so this
            # branch is where such a key is used forever — and it used to
            # return without ever checking its permissions.
            _lock_down(dacli.LOOPBACK_KEY, dacli.LOOPBACK_CERT)
            return dacli.LOOPBACK_CERT, dacli.LOOPBACK_KEY
        # Existing but unusable: openssl killed part-way, a full disk, a
        # half-restored backup. Trusting mere existence meant `da auth`
        # failed forever with a bare "[SSL] PEM lib" and no way to know
        # deleting two files would fix it. Replace them instead.
        dacli.log(
            f"loopback cert/key exist but cannot be loaded ({dacli.LOOPBACK_CERT.parent}) "
            f"— regenerating",
            "warn",
        )
    dacli.STATE_DIR.mkdir(parents=True, exist_ok=True)
    # A private key must never exist world-readable, not even briefly.
    # openssl creates -keyout under the ambient umask and the chmod
    # afterwards is too late: LibreSSL 3.3.6 — the openssl macOS actually
    # ships — writes it 0644, and STATE_DIR is 0755, so any local user
    # could read it in the window. Worse, a run killed between the write
    # and the chmod leaves a VALID pair, so every later `da auth` takes
    # the early-return path above and the 0644 key persists forever.
    #
    # Generating inside a 0700 directory closes the window instead of
    # narrowing it: the key is unreachable by other users from the moment
    # it exists, whatever mode openssl chose.
    staging = Path(tempfile.mkdtemp(dir=dacli.STATE_DIR, prefix=".cert-staging-"))
    os.chmod(staging, 0o700)
    staged_key = staging / "key.pem"
    staged_cert = staging / "cert.pem"
    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                f"rsa:{LOOPBACK_CERT_KEY_BITS}",
                "-keyout",
                str(staged_key),
                "-out",
                str(staged_cert),
                "-days",
                str(LOOPBACK_CERT_VALIDITY_DAYS),
                "-nodes",
                "-sha256",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        shutil.rmtree(staging, ignore_errors=True)
        dacli.log("openssl not found on PATH — required to generate the loopback TLS cert", "error")
        dacli.log(
            "on macOS it ships with the OS; otherwise install it (e.g. `brew install openssl`)",
            "error",
        )
        sys.exit(2)
    except subprocess.CalledProcessError as e:
        shutil.rmtree(staging, ignore_errors=True)
        dacli.log(
            f"failed to generate self-signed cert: {e.stderr.decode()[:LOG_BODY_TRUNCATE]}", "error"
        )
        sys.exit(2)
    try:
        if not (staged_key.exists() and staged_cert.exists()):
            # openssl exited 0 and wrote nothing. Reported here rather
            # than left to raise FileNotFoundError from the chmod below,
            # because the actionable message is the whole point of
            # checking: the same "exited 0, produced nothing usable" case
            # is already handled after the move.
            dacli.log(
                "openssl reported success but produced no cert/key — "
                "check your openssl installation, or use `da auth --paste`",
                "error",
            )
            sys.exit(2)
        # chmod BEFORE the move: the key is already 0600 by the time it
        # has a name another user could reach.
        os.chmod(staged_key, 0o600)
        os.chmod(staged_cert, 0o600)
        staged_key.replace(dacli.LOOPBACK_KEY)
        staged_cert.replace(dacli.LOOPBACK_CERT)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if not _cert_pair_loads(dacli.LOOPBACK_CERT, dacli.LOOPBACK_KEY):
        # openssl exited 0 but produced something unusable. Say so here
        # rather than letting it surface as "[SSL] PEM lib" from inside
        # the listener, where the cause is invisible.
        dacli.log(
            f"generated a loopback cert that cannot be loaded — delete "
            f"{dacli.LOOPBACK_CERT} and {dacli.LOOPBACK_KEY} and retry, "
            f"or use `da auth --paste`",
            "error",
        )
        sys.exit(2)
    return dacli.LOOPBACK_CERT, dacli.LOOPBACK_KEY


def _capture_code_via_listener(
    port: int,
    auth_link: str,
    *,
    expected_state: str,
    expected_path: str,
    https: bool = False,
) -> tuple[str | None, str | None]:
    """Wait for the OAuth redirect; return ``(code, error)``.

    Exactly one of the two is set, or both are None on timeout.

    ``expected_state`` and ``expected_path`` are what make the captured
    code trustworthy, and this function used to receive neither. It
    accepted a ``code`` from *any* request carrying one, on any path — so
    during the five-minute window anything able to reach the listener
    could hand us a code of its own choosing. That is not limited to
    other local processes: the user has just been told to click through
    the self-signed warning for this exact origin, and browsers cache
    that exception for the session, so an ``<img src>`` on any page they
    visit reaches us too.

    A code we did not ask for is not merely useless. We would exchange it
    with our own verifier and, if it was minted without a
    ``code_challenge``, could end up storing tokens for somebody else's
    DeviantArt account. Worse, accepting it ends the wait and closes the
    listener, so the genuine callback arriving milliseconds later finds a
    dead port and the user is told their whitelist is wrong.

    ``state`` (RFC 6749 §10.12) is the control that settles all of it:
    only a redirect carrying the value we generated this run can be ours.
    """
    captured: dict[str, str | None] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(parsed.query)
            # The path must be the one we registered. Browsers also fetch
            # /favicon.ico within ~100ms of the redirect, which is the
            # other reason not to treat every request as the callback.
            path_ok = parsed.path == expected_path
            # compare_digest, not ==: the timing difference is not a
            # practical attack here, but a constant-time compare is the
            # correct default for a secret and costs nothing.
            state_ok = hmac.compare_digest(q.get("state", [""])[0], expected_state)

            if path_ok and state_ok and "code" in q:
                captured["code"] = q["code"][0]
                body = b"<h1>Authorized.</h1><p>You can close this tab and return to the CLI.</p>"
            elif path_ok and state_ok and "error" in q:
                # DA's own words are far better than our guess. Without
                # this the user saw "Authorized." after clicking Decline,
                # then waited out the whole timeout, then got told their
                # redirect_uri whitelist was wrong.
                captured["error"] = q["error"][0]
                captured["error_description"] = q.get("error_description", [""])[0]
                body = b"<h1>Not authorized.</h1><p>Return to the CLI for details.</p>"
            else:
                # Anything else: a favicon fetch, a port scan, or a code
                # somebody else is trying to give us. Say nothing useful
                # and, crucially, do not stop waiting.
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>Not found.</h1>")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: object) -> None:
            pass

    tls_ctx: ssl.SSLContext | None = None
    if https:
        cert, key = _ensure_self_signed_cert()
        tls_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls_ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))

    class _ReusableServer(socketserver.ThreadingTCPServer):
        # Threading, not the plain TCPServer this used to be. Serving one
        # connection at a time means a client that opens a socket and then
        # says nothing holds the only slot, and the real OAuth redirect
        # never gets accepted — `da auth` sits out its full timeout and
        # fails. Browsers do exactly this: speculative preconnects, and
        # the /favicon.ico fetch the do_GET comment above already
        # documents.
        allow_reuse_address = True
        # Never let a stalled connection keep the process alive.
        daemon_threads = True

        def get_request(self) -> tuple[socket.socket, object]:
            sock, addr = self.socket.accept()
            # Bound the handshake and the request read. Without a timeout
            # a silent client occupies its thread until the flow ends.
            sock.settimeout(AUTH_CONNECTION_TIMEOUT_S)
            return sock, addr

        def finish_request(self, request: object, client_address: object) -> None:
            # The TLS handshake happens HERE, which is already inside the
            # per-connection worker thread. It must not happen anywhere
            # earlier: wrapping the listening socket (what this used to
            # do) runs the handshake inside accept(), and doing it in
            # get_request runs it in the accept loop. Both put a blocking
            # handshake back on the accept path, where one client that
            # opens a socket and never sends a ClientHello stalls every
            # later connection — including the real OAuth callback.
            if tls_ctx is None:
                self.RequestHandlerClass(request, client_address, self)
                return
            try:
                tls_sock = tls_ctx.wrap_socket(request, server_side=True)
            except OSError:
                # Abandoned or malformed handshake. wrap_socket has
                # already closed the socket it detached from `request`.
                return
            try:
                self.RequestHandlerClass(tls_sock, client_address, self)
            finally:
                # `request` was detached by wrap_socket, so the server's
                # own shutdown_request can no longer close the fd.
                with contextlib.suppress(OSError):
                    tls_sock.close()

        def handle_error(self, request: object, client_address: object) -> None:
            # Default prints a traceback. A port scan or an abandoned
            # browser tab is not something to show someone mid-login.
            pass

    try:
        srv = _ReusableServer(("127.0.0.1", port), _Handler)
    except OSError as exc:
        # allow_reuse_address only covers TIME_WAIT, not a live listener.
        dacli.log(
            f"cannot listen on 127.0.0.1:{port} ({exc.strerror or exc}). "
            f"Another process is using the port — stop it, point "
            f"redirect_uri at a free port, or re-run with `da auth --paste`.",
            "error",
        )
        sys.exit(2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    dacli.log(f"opening {auth_link}\n(if it doesn't open, copy/paste the URL into your browser)")
    if https:
        dacli.log(
            "note: the local listener uses a SELF-SIGNED cert, so your browser "
            "will warn you about the connection. Click through (Advanced → "
            "'Proceed to localhost (unsafe)' on Chrome, similar on others). "
            "It's safe — the cert is only on your machine, only used to "
            "capture the localhost callback."
        )
    with contextlib.suppress(Exception):
        webbrowser.open(auth_link)

    dacli.log(f"waiting for browser redirect ({AUTH_LISTENER_TIMEOUT_S // 60} minute timeout)...")
    deadline = time.time() + AUTH_LISTENER_TIMEOUT_S
    try:
        while not captured and time.time() < deadline:
            time.sleep(0.2)
    finally:
        srv.shutdown()
        # shutdown() stops serve_forever but doesn't close the socket.
        # Without server_close() the listening fd lingers until process
        # exit — fine for the daemon thread but the port stays bound,
        # which can cause a confusing "Address already in use" if `da auth`
        # is rerun in quick succession.
        srv.server_close()
    if captured.get("error"):
        detail = captured.get("error_description") or ""
        return None, f"{captured['error']}{': ' + detail if detail else ''}"
    return captured.get("code"), None


def _capture_code_via_paste() -> str | None:
    """Manual fallback for redirect URIs we can't listen on (e.g. running
    over SSH, no localhost binding). User authorizes in browser, lands on
    the configured URI (a page they control), copies the URL from the
    address bar, and pastes it back here. We extract the `code`. Only
    works when the redirect URI is whitelisted on the DA OAuth app.
    """
    dacli.log("after authorizing, paste the URL you were redirected to (Ctrl+C to abort):")
    try:
        pasted = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not pasted:
        return None
    # urllib.parse.urlparse doesn't raise ValueError on malformed URLs in
    # modern Python; it returns a ParseResult with empty fields, and the
    # `code` lookup below yields None. Treat empty input the same way.
    q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    return q.get("code", [None])[0]


def _cmd_auth_status() -> None:
    """Report whether the credentials actually work, and for how long.

    This asks DeviantArt. The local calculation from
    ``refresh_token_issued_at`` and DA's 90-day ceiling describes the
    clock, not whether the grant is still honoured, and the two come
    apart — a token revoked from the website, or superseded by a re-auth
    elsewhere, reads as perfectly healthy by the calendar. Reporting that
    as ``ok`` is the silent-cron-failure mode this command exists to
    catch, so the unreliable answer is not offered at all.

    Validation goes through ``/placebo``, the same endpoint ``whoami``
    uses, rather than forcing a token exchange, so the refresh token is
    rotated only when the access token has genuinely expired.

    Be aware of what that does *not* promise. An access token lives an
    hour and is refreshed 60s early, so anything polling on roughly an
    hourly cadence refreshes — and therefore rotates — on almost every
    call. (An earlier version of this docstring claimed the opposite.)
    Rotation is safe to do often now that ``access_token`` serialises it
    across processes, but a monitor that only needs liveness is cheaper
    on a longer interval.

    ``da diagnose`` still reports the TTL without network, for when that
    is what you want.

    Exits 0 on ok, 1 on warn, 2 on crit/unknown/revoked/unreachable —
    same policy as ``da diagnose``.
    """
    state = dacli.load_state()
    issued_at = state.get("refresh_token_issued_at")
    if not isinstance(issued_at, (int, float)) or issued_at <= 0:
        result = {"state": "unknown", "days_remaining": None, "issued_at_iso": None}
        print(json.dumps(result))
        sys.exit(2)
    days_remaining = REFRESH_TOKEN_TTL_DAYS - (time.time() - issued_at) / 86400.0
    if days_remaining > REFRESH_TOKEN_WARN_DAYS:
        auth_state = "ok"
        exit_code = 0
    elif days_remaining > REFRESH_TOKEN_CRIT_DAYS:
        auth_state = "warn"
        exit_code = 1
    else:
        auth_state = "crit"
        exit_code = 2
    payload: dict[str, object] = {
        "state": auth_state,
        "days_remaining": round(days_remaining, 1),
        "issued_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(issued_at)),
    }

    try:
        token = dacli.access_token(dacli.load_config(), state)
        placebo = dacli.http_json(f"{API_BASE}/placebo", token=token)
        if placebo.get("status") != "success":
            raise urllib.error.HTTPError(f"{API_BASE}/placebo", 401, "rejected", {}, None)
    except AuthError as e:
        # DA said no. Distinct from HttpError below, which is the network
        # saying no — this branch used to catch both, because access_token
        # collapsed them into one sys.exit(2), so an offline laptop was
        # reported as a revoked credential and the user was sent to
        # re-authenticate for nothing.
        payload["state"] = "revoked"
        payload["days_remaining"] = 0.0
        payload["error"] = str(e)
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(2)
    except urllib.error.HTTPError as e:
        # The /placebo call itself, i.e. DA rejecting the ACCESS token.
        payload["state"] = "revoked"
        payload["days_remaining"] = 0.0
        payload["error"] = f"HTTP {e.code}"
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(2)
    except (HttpError, urllib.error.URLError) as e:
        # No network is not a dead token. HttpError covers the refresh
        # leg (unreachable, or a 5xx from DA's auth server); URLError
        # covers the /placebo leg. Both mean "we could not ask", which is
        # not an answer about the credential.
        reason = getattr(e, "reason", None)
        payload["state"] = "unreachable"
        payload["error"] = str(reason if reason is not None else e)
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(2)

    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(exit_code)


def cmd_auth(args: argparse.Namespace) -> None:
    """Run the OAuth 2.1 PKCE login flow, or report token-chain TTL if
    ``--status`` is set.

    ``da auth status`` is a read-only JSON emitter suitable for
    cron / launchd / monitoring: it reads ``refresh_token_issued_at``
    from state.json, computes remaining days against
    ``REFRESH_TOKEN_TTL_DAYS``, and emits::

        {"state": "ok"|"warn"|"crit"|"unknown"|"revoked"|"unreachable",
         "days_remaining": <float or null>,
         "issued_at_iso": "<ISO 8601 UTC or null>"}

    The credentials are always confirmed with DeviantArt first, so
    ``ok``/``warn``/``crit`` mean the grant works AND has that much of
    its 90 days left. ``revoked`` means DeviantArt rejected it whatever
    the clock says; ``unreachable`` means the network was down, which is
    not the same thing and must not send anyone to re-authenticate.
    ``unknown`` means no ``refresh_token_issued_at`` in state.
    """
    if getattr(args, "auth_cmd", None) == "status":
        _cmd_auth_status()
        return

    cfg = dacli.load_config()
    if not cfg.get("client_id"):
        dacli.log("set DA_CLIENT_ID env var or `da config set client_id <ID>` first", "error")
        sys.exit(2)

    # Default to HTTPS loopback because DA's developer dashboard rejects
    # plain-HTTP entries from the OAuth Redirect URI Whitelist. The
    # listener generates a self-signed cert on first run.
    redirect_uri = (
        args.redirect_uri or cfg.get("redirect_uri") or f"https://localhost:{AUTH_DEFAULT_PORT}/"
    )
    scope = args.scope

    try:
        parsed = urllib.parse.urlparse(redirect_uri)
        host = parsed.hostname or ""
    except ValueError:
        dacli.log(f"can't parse redirect_uri={redirect_uri!r}", "error")
        sys.exit(2)
    if not parsed.scheme or not host:
        dacli.log(f"can't parse redirect_uri={redirect_uri!r}", "error")
        sys.exit(2)

    # Auto-detect: loopback URIs use the listener; everything else uses
    # paste-back (useful when running over SSH or in any context where
    # the local socket can't be reached from the browser).
    #
    # Note: DA does NOT require HTTPS for the Authorization Code + PKCE
    # grant — the loopback listener with http://localhost:PORT/ is the
    # canonical and recommended pattern. (HTTPS is only mandated for the
    # legacy Implicit grant, which we don't use.) Whitelist match is
    # byte-exact: the URI sent here must be byte-identical to what's in
    # the OAuth app's "Redirect URI Whitelist" — trailing slash, port,
    # case, and scheme all matter.
    use_paste = bool(getattr(args, "paste", False)) or not _is_loopback(host)
    if not use_paste:
        try:
            port = parsed.port or AUTH_DEFAULT_PORT
        except ValueError:
            dacli.log(f"can't parse port from redirect_uri={redirect_uri!r}", "error")
            sys.exit(2)

    verifier = base64.urlsafe_b64encode(os.urandom(40)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    # RFC 6749 §10.12. Without it the listener has no way to tell our own
    # redirect from any other request that happens to carry a `code`, and
    # it accepted all of them — see _capture_code_via_listener.
    state = secrets.token_urlsafe(32)
    auth_link = (
        f"{AUTH_URL}?response_type=code&client_id={cfg['client_id']}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        f"&scope={urllib.parse.quote(scope)}"
        f"&state={state}"
        f"&code_challenge={challenge}&code_challenge_method=S256"
    )

    auth_error: str | None = None
    if use_paste:
        dacli.log(f"open this URL in your browser and authorize:\n  {auth_link}")
        dacli.log(f"DA will redirect you to {redirect_uri} (a page you own — likely 404).")
        code = _capture_code_via_paste()
    else:
        code, auth_error = _capture_code_via_listener(
            port,
            auth_link,
            expected_state=state,
            # The path DA will redirect to, from the same string we sent
            # as redirect_uri — so the two cannot drift. "" means "/",
            # which is what a bare https://localhost:8765 resolves to.
            expected_path=parsed.path or "/",
            https=parsed.scheme == "https",
        )

    if auth_error:
        # DeviantArt told us exactly what went wrong; repeating its own
        # words beats guessing at the whitelist.
        dacli.log(f"DeviantArt declined the authorization: {auth_error}", "error")
        if auth_error.startswith("access_denied"):
            dacli.log("you clicked Decline — re-run `da auth` to try again", "error")
        sys.exit(2)

    if not code:
        dacli.log("did not receive an auth code", "error")
        dacli.log(
            f"common cause: redirect_uri={redirect_uri!r} is not in your DA app's "
            f"whitelist. DA matches the whitelist BYTE-EXACTLY — trailing slash, "
            f"port, case, and scheme all matter. If your browser landed on the DA "
            f"home page instead of the redirect URI, that's the symptom of a "
            f"whitelist miss. Fix at https://www.deviantart.com/developers/ → "
            f"Apps & Keys → OAuth2 Redirect URI Whitelist; add EXACTLY: "
            f"{redirect_uri}",
            "error",
        )
        sys.exit(2)

    payload = {
        "grant_type": "authorization_code",
        "client_id": cfg["client_id"],
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if cfg.get("client_secret"):
        payload["client_secret"] = cfg["client_secret"]

    try:
        # retries=0 for the same reason the refresh uses it: an
        # authorization code is single-use. If the first POST reached DA
        # and the response was lost, DA has already consumed the code and
        # minted a token pair that exists only in that lost response —
        # replaying then guarantees invalid_grant and buries the real
        # cause. The two exchanges disagreed on this for no stated reason.
        body = dacli.http_post_json(TOKEN_URL, payload, retries=0)
    except urllib.error.HTTPError as e:
        dacli.log(
            f"token exchange failed: HTTP {e.code} {e.read().decode()[:LOG_BODY_TRUNCATE]}", "error"
        )
        sys.exit(2)
    except (urllib.error.URLError, TimeoutError) as e:
        # Previously fell through to main()'s transport advice, which says
        # "the next run resumes where this one stopped; nothing has been
        # lost" — untrue for `da auth`, whose code is now spent.
        dacli.log(f"could not reach DeviantArt to exchange the code: {e}", "error")
        dacli.log("the authorization code is single-use — re-run `da auth`", "error")
        sys.exit(2)
    if "access_token" not in body:
        dacli.log(f"token exchange returned: {body}", "error")
        sys.exit(2)

    state = dacli.load_state()
    state["access_token"] = body["access_token"]
    state["refresh_token"] = body.get("refresh_token", state.get("refresh_token"))
    # Track when this refresh_token chain was issued so `da diagnose` can
    # warn before DA's hard 3-month expiry. DA doesn't return an `iat` for
    # refresh tokens; we record the local time at issue. If the chain
    # rotates (DA occasionally hands back a new refresh_token on refresh),
    # `access_token()` resets this so the 90-day window follows the chain.
    if body.get("refresh_token"):
        state["refresh_token_issued_at"] = time.time()
    state["expires_at"] = time.time() + body.get("expires_in", 3600)
    state["scope"] = body.get("scope", scope)
    # A completed login clears any earlier rejection. access_token set
    # that mark and told the user to run `da auth`; not clearing it here
    # meant the very next `sync watched` still treated the fresh grant as
    # dead and aborted on its first per-artist failure, for the whole hour
    # until the new access token expired.
    state.pop("refresh_token_rejected_at", None)
    dacli.save_state(state)
    dacli.log(f"authenticated. scope={state['scope']}. tokens stored in {dacli.STATE_PATH}.")


def cmd_refresh(args: argparse.Namespace) -> None:
    """Force a refresh of the access_token (clears the cached one first
    so `access_token` is forced to call the token endpoint), then
    print a one-line confirmation.
    """
    cfg = dacli.load_config()
    state = dacli.load_state()
    # force_refresh instead of clearing the cached token first: if the
    # token endpoint is unreachable, the existing (still valid) access
    # token must survive rather than having been erased from state.json.
    access_token(cfg, state, force_refresh=True)
    dacli.log("ok — access token refreshed.")


def cmd_whoami(args: argparse.Namespace) -> None:
    """Verify the access token via /placebo, then optionally show identity.

    The /user/whoami call needs the `user` scope; if the token only has
    `browse`, we warn and degrade gracefully (the placebo call still
    confirms the token is live).
    """
    cfg = dacli.load_config()
    state = dacli.load_state()
    # authed_http_json, not http_json: this is the command whose job is
    # verifying the token, and it was the one command that could not
    # recover from the failure it exists to detect. A token that is
    # locally fresh but server-side revoked — which is what happens after
    # a re-auth elsewhere rotates the chain — made `da sync` self-heal
    # (it routes through here) while `da whoami` sent the user through a
    # whole browser flow that one refresh would have made unnecessary.
    placebo = authed_http_json(f"{API_BASE}/placebo", cfg, state)
    if placebo.get("status") != "success":
        dacli.log(f"placebo failed: {placebo}", "error")
        sys.exit(2)
    print("token: valid (placebo OK)")
    if state.get("scope"):
        print(f"scope: {state['scope']}")
    if state.get("expires_at"):
        rem = int(state["expires_at"] - time.time())
        print(f"access_token expires in: {rem}s")
    try:
        profile = authed_http_json(f"{API_BASE}/user/whoami?mature_content=true", cfg, state)
        print(f"@{profile.get('username')}  userid={profile.get('userid')}")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            dacli.log(
                "/user/whoami needs `user` scope — current token only has the listed scope", "warn"
            )
            dacli.log('(re-run `da auth --scope "user browse"` to broaden)', "warn")
        else:
            raise
