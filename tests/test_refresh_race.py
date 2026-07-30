"""Only one process may exchange a refresh token at a time.

DeviantArt rotates the refresh token on every use and consumes the old
one. ``access_token`` had no cross-process lock and never re-read
``state.json``: it POSTed the token loaded when the process started, then
wrote its whole stale snapshot back. Two processes therefore presented
the same token, and the loser got 400 invalid_grant and exited 2.

The collision is routine rather than exotic. ``examples/diagnose-cron.sh``
runs ``da diagnose`` every ten minutes alongside the nightly
``da sync feed``, and diagnose forces a refresh whenever the access token
has expired — roughly hourly, since tokens live an hour.

The tests here run **real subprocesses** against a **real local token
endpoint** that enforces single-use rotation the way DA does. In-process
mocks cannot show this: the bug is about two interpreters sharing one
file, and a mock that answers both callers happily would pass against the
broken code.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


class _RotatingTokenEndpoint(http.server.BaseHTTPRequestHandler):
    """A token endpoint that consumes each refresh token exactly once."""

    live_tokens: set[str] = set()
    issued: list[str] = []
    rejected: list[str] = []
    lock = threading.Lock()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        fields = dict(pair.split("=", 1) for pair in raw.split("&") if "=" in pair)
        presented = fields.get("refresh_token", "")

        cls = type(self)
        with cls.lock:
            if presented not in cls.live_tokens:
                # Exactly what DA does with a token that was already used.
                cls.rejected.append(presented)
                body = json.dumps({"error": "invalid_request"}).encode()
                self.send_response(400)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            cls.live_tokens.discard(presented)
            issued = f"RT-{len(cls.issued) + 1}"
            cls.issued.append(issued)
            cls.live_tokens.add(issued)

        body = json.dumps(
            {"access_token": f"AT-{issued}", "expires_in": 3600, "refresh_token": issued}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def token_endpoint():
    cls = _RotatingTokenEndpoint
    cls.live_tokens = {"RT-0"}
    cls.issued = []
    cls.rejected = []
    server = http.server.HTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/token", cls
    server.shutdown()


@pytest.fixture
def home(tmp_path: Path):
    """An isolated XDG home holding an expired access token."""
    state_dir = tmp_path / "state" / "da-cli"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "access_token": "AT-EXPIRED",
                "expires_at": time.time() - 10,  # forces a refresh
                "refresh_token": "RT-0",
                "refresh_token_issued_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    cfg_dir = tmp_path / "cfg" / "da-cli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"client_id": "12345"}), encoding="utf-8")
    return tmp_path


_DRIVER = """
import sys, time
sys.path.insert(0, {repo!r})
import dacli, dacli.constants, dacli.auth
for mod in (dacli, dacli.constants, dacli.auth):
    if hasattr(mod, "TOKEN_URL"):
        mod.TOKEN_URL = {token_url!r}
try:
    tok = dacli.access_token(dacli.load_config(), dacli.load_state())
except dacli.DacliError as e:
    # access_token raises a typed error rather than calling sys.exit, so
    # that `da auth status` can tell "DA rejected the grant" from "the
    # network is down". The CLI's exit code comes from main(); this driver
    # calls the library directly, so it maps the error itself.
    print("ERROR:" + type(e).__name__ + ": " + str(e))
    sys.exit(2)
print("TOKEN:" + tok)
"""


def _spawn(home: Path, token_url: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _DRIVER.format(repo=str(REPO_ROOT), token_url=token_url)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=REPO_ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "cfg"),
            "XDG_STATE_HOME": str(home / "state"),
            "DA_CLIENT_ID": "12345",
            "NO_COLOR": "1",
        },
    )


class TestTwoProcessesRefreshingAtOnce:
    def test_neither_process_is_rejected(self, home, token_endpoint):
        """The race, run for real: both must come away with a live token.

        Before the lock, both processes read the same expired state, both
        POSTed RT-0, and the second was told invalid_request — exit 2 for
        whichever lost, which at 03:00 is the nightly sync.
        """
        token_url, endpoint = token_endpoint

        procs = [_spawn(home, token_url) for _ in range(2)]
        results = [p.communicate(timeout=60) for p in procs]

        for i, ((out, err), proc) in enumerate(zip(results, procs, strict=True)):
            assert proc.returncode == 0, f"process {i} exited {proc.returncode}:\n{err}"
            assert "TOKEN:AT-" in out, f"process {i} produced no token:\n{out}{err}"

        assert endpoint.rejected == [], (
            f"a consumed refresh token was replayed: {endpoint.rejected}"
        )

    def test_the_second_process_adopts_the_first_ones_token(self, home, token_endpoint):
        """Both processes end up holding the *same* token, from one exchange.

        Counting exchanges alone does not discriminate: on the broken
        code exactly one exchange also succeeds, because the loser is
        rejected rather than because it adopted anything. What only the
        fix produces is two live processes agreeing on one token.

        Adoption rather than a second rotation is the point — every extra
        rotation is another chance to strand a token if a response goes
        missing.
        """
        token_url, endpoint = token_endpoint

        procs = [_spawn(home, token_url) for _ in range(2)]
        outputs = [p.communicate(timeout=60)[0] for p in procs]

        tokens = [
            line[6:] for out in outputs for line in out.splitlines() if line.startswith("TOKEN:")
        ]
        assert len(tokens) == 2, f"both processes should have produced a token: {outputs}"
        assert tokens[0] == tokens[1], f"the two processes disagree on the token: {tokens}"
        assert len(endpoint.issued) == 1, (
            f"expected one exchange for two processes, saw {len(endpoint.issued)}"
        )

    def test_the_rotated_token_is_what_lands_on_disk(self, home, token_endpoint):
        """Whatever DA last issued must be what the next run will present."""
        token_url, endpoint = token_endpoint

        procs = [_spawn(home, token_url) for _ in range(2)]
        for p in procs:
            p.communicate(timeout=60)

        on_disk = json.loads((home / "state" / "da-cli" / "state.json").read_text(encoding="utf-8"))
        assert on_disk["refresh_token"] == endpoint.issued[-1]
        assert on_disk["refresh_token"] in endpoint.live_tokens, (
            "state.json holds a refresh token DA has already consumed"
        )


class TestASingleProcessIsUnaffected:
    """Controls: the lock must not change the ordinary path."""

    def test_one_process_still_refreshes(self, home, token_endpoint):
        token_url, endpoint = token_endpoint
        out, err = _spawn(home, token_url).communicate(timeout=60)
        assert "TOKEN:AT-RT-1" in out, f"{out}{err}"
        assert len(endpoint.issued) == 1

    def test_a_fresh_token_is_not_exchanged_at_all(self, home, token_endpoint):
        """A valid access token must not take the lock or hit the network."""
        token_url, endpoint = token_endpoint
        state_path = home / "state" / "da-cli" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["expires_at"] = time.time() + 3600
        state_path.write_text(json.dumps(state), encoding="utf-8")

        out, err = _spawn(home, token_url).communicate(timeout=60)

        assert "TOKEN:AT-EXPIRED" in out, f"{out}{err}"
        assert endpoint.issued == [], "a fresh token should not have been exchanged"


class TestARejectedGrantIsRecorded:
    """`sync watched` needs to tell "this item failed" from "nothing will work"."""

    def test_rejection_is_marked_in_state(self, home, token_endpoint):
        """The mark the dead guard in `sync watched` was missing.

        That guard tested whether refresh_token was still present, and
        nothing ever removed it — so it never fired, and a revoked grant
        produced one doomed token POST per remaining artist.
        """
        token_url, endpoint = token_endpoint
        endpoint.live_tokens = set()  # DA rejects everything

        proc = _spawn(home, token_url)
        _out, _err = proc.communicate(timeout=60)

        assert proc.returncode == 2
        state = json.loads((home / "state" / "da-cli" / "state.json").read_text(encoding="utf-8"))
        assert state.get("refresh_token_rejected_at"), "the rejection was not recorded"
        assert state.get("refresh_token"), "the token itself must stay, for `auth status`"

    def test_a_later_success_clears_the_mark(self, home, token_endpoint):
        """A stale mark would abort the next healthy `sync watched`."""
        token_url, endpoint = token_endpoint
        state_path = home / "state" / "da-cli" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["refresh_token_rejected_at"] = time.time() - 3600
        state_path.write_text(json.dumps(state), encoding="utf-8")

        _spawn(home, token_url).communicate(timeout=60)

        after = json.loads(state_path.read_text(encoding="utf-8"))
        assert "refresh_token_rejected_at" not in after
        assert endpoint.issued, "the refresh should still have happened"
