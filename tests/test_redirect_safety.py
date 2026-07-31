"""The OAuth token must not follow a redirect to another host.

``urllib`` re-sends headers added with ``add_header`` when it follows a
redirect — including across origins. Every authenticated request in this
codebase attached its bearer token that way, so any 30x from DeviantArt
to a third party would have handed that party a live credential.

It is latent rather than active: DA's JSON API answers directly today.
But the image CDN already redirects to ``wixmp.com``, so this is one API
change away from mattering, and the failure would be silent — the
request succeeds, and the token is simply also somewhere else now.

These tests run two real loopback servers, because the guarantee is
about what ``urllib`` does with a header rather than about anything
da-cli computes. Asserting on ``Request`` internals would pass just as
happily with ``add_header``.
"""

from __future__ import annotations

import http.server
import threading

import pytest

import dacli

TOKEN = "SECRET-TOKEN-DO-NOT-LEAK"


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Second hop: records what it was sent and answers with JSON."""

    received: dict[str, str | None] = {}

    def do_GET(self) -> None:
        type(self).received = {
            "authorization": self.headers.get("Authorization"),
            "user_agent": self.headers.get("User-Agent"),
        }
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> tuple[http.server.HTTPServer, int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture
def two_hosts():
    """Origin A redirects to origin B; B records what arrived."""
    _Recorder.received = {}
    server_b, port_b = _serve(_Recorder)

    class _Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            # A different port is a different origin, which is all the
            # cross-host rule turns on.
            self.send_header("Location", f"http://127.0.0.1:{port_b}/second")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    server_a, port_a = _serve(_Redirector)
    yield f"http://127.0.0.1:{port_a}/first"
    server_a.shutdown()
    server_b.shutdown()


class TestTheTokenDoesNotCrossOrigins:
    def test_http_json_does_not_forward_the_bearer_token(self, two_hosts):
        """The leak, as a test: B must never see the Authorization header."""
        dacli.http_json(two_hosts, token=TOKEN)

        assert _Recorder.received["authorization"] is None, (
            f"the token was sent to a second origin: {_Recorder.received['authorization']!r}"
        )

    def test_the_redirect_is_still_followed(self, two_hosts):
        """The control: dropping the header must not break redirects.

        A fix that stopped following redirects altogether would also pass
        the test above, and would break the first time DA introduced one.
        """
        body = dacli.http_json(two_hosts, token=TOKEN)
        assert body == {"ok": True}, "the redirect should still have been followed"

    def test_non_secret_headers_still_travel(self, two_hosts):
        """Only the credential is withheld — the User-Agent still goes.

        This distinguishes the real fix from one that dropped every
        header, which would make da-cli anonymous to DA's CDN and change
        how it is served.
        """
        dacli.http_json(two_hosts, token=TOKEN)
        assert _Recorder.received["user_agent"], "the User-Agent should survive the redirect"
        assert "da-cli" in (_Recorder.received["user_agent"] or "")


class TestTheTokenStillReachesTheIntendedHost:
    def test_a_direct_request_is_authenticated(self):
        """The other control: the token must still be sent when there is
        no redirect. An unredirected header is still sent on the first
        request — this pins that, because sending nothing at all would
        satisfy every test above.
        """
        _Recorder.received = {}
        server, port = _serve(_Recorder)
        try:
            dacli.http_json(f"http://127.0.0.1:{port}/direct", token=TOKEN)
        finally:
            server.shutdown()

        assert _Recorder.received["authorization"] == f"Bearer {TOKEN}"
