"""Fault-injection tests for the http and sync layers.

These don't replace the unit/integration suites — they specifically
hammer every realistic failure mode (network errors, server errors,
malformed payloads, truncated responses, mid-stream drops) and verify
that:

- Retryable failures (5xx + network/timeout) get retried with the
  documented backoff schedule and eventually succeed if a transient
  failure clears.
- Permanent failures (4xx other than 429, malformed payloads) fail
  fast — no wasted retries, original exception re-raised.
- 429 fails fast (caller policy, not http-layer policy).
- Backoff is exponential with jitter (no thundering herd).
- Image-download failures don't poison concurrent siblings.
- Cross-module recovery: a sync command tolerates per-deviation
  failures within a page and keeps processing.

The retry contract these tests pin down is documented at the top of
`dacli.py` near `RETRYABLE_HTTP_CODES`.
"""

from __future__ import annotations

import argparse
import errno
import itertools
import json
import pathlib
import socket
import ssl
import time
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

import dacli

# ---------------------------------------------------------------------------
# Helpers for building fake HTTP failures
# ---------------------------------------------------------------------------


def _disk_full() -> OSError:
    """A realistic disk-full error: errno set, as the OS would raise it."""
    e = OSError("No space left on device")
    e.errno = errno.ENOSPC
    e.filename = "/Volumes/ArtDrive/gallery"
    return e


def _sync_args(cmd: str, **overrides: object) -> argparse.Namespace:
    """Default Namespace for the sync commands, as test_commands.py builds it."""
    base: dict[str, object] = {
        "limit": 24,
        "mature": True,
        "time_budget": 540,
        "delay_api": 0.0,
        "delay_image": 0.0,
        "jitter": 0.0,
        "offset": None,
        "dry_run": False,
        "full": False,
        "concurrency": None,
    }
    if cmd == "artist":
        base["artist"] = "alice"
    base.update(overrides)
    return argparse.Namespace(**base)


def _http_error(code: int, msg: str = "fault") -> urllib.error.HTTPError:
    """Build an HTTPError with a real bytes-readable body so callers
    that .read() the error don't blow up."""
    return urllib.error.HTTPError(
        url="https://example/", code=code, msg=msg, hdrs={}, fp=BytesIO(b"{}")
    )


def _make_response(payload: bytes, status: int = 200):
    class _R:
        def __init__(self) -> None:
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

        def read(self) -> bytes:
            return payload

    return _R()


# ---------------------------------------------------------------------------
# Retryable HTTP errors: 5xx → retry, eventually succeed or raise
# ---------------------------------------------------------------------------


class TestHttpRetryOn5xx:
    """5xx codes are documented retryable. Verify the loop tries
    `retries + 1` times total before giving up."""

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_recovers_when_5xx_resolves(self, code, monkeypatch):
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            if len(attempts) < 3:  # fail twice, succeed on third
                raise _http_error(code)
            return _make_response(json.dumps({"ok": True}).encode())

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        result = dacli.http_json("https://example/", retries=2, backoff=0.0)
        assert result == {"ok": True}
        assert len(attempts) == 3

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_raises_after_retries_exhausted(self, code, monkeypatch):
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            raise _http_error(code)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        with pytest.raises(urllib.error.HTTPError) as exc:
            dacli.http_json("https://example/", retries=2, backoff=0.0)
        assert exc.value.code == code
        assert len(attempts) == 3  # initial + 2 retries

    def test_backoff_is_exponential(self, monkeypatch):
        attempts = []
        sleeps: list[float] = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            raise _http_error(503)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("time.sleep", sleeps.append)
        with pytest.raises(urllib.error.HTTPError):
            dacli.http_json("https://example/", retries=3, backoff=1.0)
        # Three sleeps for three retries (initial attempt + 3 retries = 4 attempts)
        assert len(sleeps) == 3
        # Exponential: each sleep should be roughly 2x the previous
        # (with ±10% jitter), so each ratio should be in [1.6, 2.5]
        for prev, nxt in itertools.pairwise(sleeps):
            assert 1.4 < (nxt / prev) < 2.6, f"{prev=} {nxt=}"


# ---------------------------------------------------------------------------
# Permanent HTTP errors: fail fast, no retries
# ---------------------------------------------------------------------------


class TestHttpFailFastOn4xx:
    @pytest.mark.parametrize("code", [400, 401, 403, 404, 410, 422])
    def test_4xx_does_not_retry(self, code, monkeypatch):
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            raise _http_error(code)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", sleeps.append)
        with pytest.raises(urllib.error.HTTPError) as exc:
            dacli.http_json("https://example/", retries=2, backoff=0.0)
        assert exc.value.code == code
        assert len(attempts) == 1, "4xx should not retry"
        assert sleeps == [], "no backoff sleep on fail-fast"

    def test_429_does_not_retry(self, monkeypatch):
        """429 is the caller's responsibility (cmd_sync_feed breaks the
        loop on 429). Http layer must not consume retries on it."""
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            raise _http_error(429)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(urllib.error.HTTPError) as exc:
            dacli.http_json("https://example/", retries=2, backoff=0.0)
        assert exc.value.code == 429
        assert len(attempts) == 1


# ---------------------------------------------------------------------------
# Network / transport errors: retry as transient
# ---------------------------------------------------------------------------


class TestHttpRetryOnNetworkErrors:
    @pytest.mark.parametrize(
        "exc",
        [
            urllib.error.URLError("DNS lookup failed"),
            urllib.error.URLError(socket.gaierror("temporary failure")),
            TimeoutError("read timed out"),
        ],
    )
    def test_recovers_after_transient_network_error(self, exc, monkeypatch):
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            if len(attempts) < 2:
                raise exc
            return _make_response(json.dumps({"ok": True}).encode())

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        result = dacli.http_json("https://example/", retries=2, backoff=0.0)
        assert result == {"ok": True}
        assert len(attempts) == 2

    def test_raises_after_persistent_network_error(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("persistent")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        with pytest.raises(urllib.error.URLError):
            dacli.http_json("https://example/", retries=2, backoff=0.0)


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------


class TestHttpMalformedResponses:
    def test_invalid_json_raises_immediately(self, monkeypatch):
        """Bad JSON is a permanent server contract violation — no retry."""
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            return _make_response(b"not json at all <<<")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(json.JSONDecodeError):
            dacli.http_json("https://example/")
        assert len(attempts) == 1

    def test_empty_body_raises(self, monkeypatch):
        def fake_urlopen(req, timeout=None):
            return _make_response(b"")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(json.JSONDecodeError):
            dacli.http_json("https://example/")


# ---------------------------------------------------------------------------
# Image download (http_bytes) — same retry contract as http_json
# ---------------------------------------------------------------------------


class TestHttpBytesRetry:
    @pytest.mark.parametrize("code", [500, 503])
    def test_5xx_image_download_retried(self, code, monkeypatch):
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            if len(attempts) < 2:
                raise _http_error(code)
            return _make_response(b"PNGbytes")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        result = dacli.http_bytes("https://cdn/", retries=2, backoff=0.0)
        assert result == b"PNGbytes"
        assert len(attempts) == 2

    def test_4xx_image_does_not_retry(self, monkeypatch):
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            raise _http_error(404)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(urllib.error.HTTPError):
            dacli.http_bytes("https://cdn/", retries=2, backoff=0.0)
        assert len(attempts) == 1

    def test_network_error_image_retried(self, monkeypatch):
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            if len(attempts) < 2:
                raise urllib.error.URLError("connection reset")
            return _make_response(b"BYTES")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        assert dacli.http_bytes("https://cdn/", retries=2, backoff=0.0) == b"BYTES"


# ---------------------------------------------------------------------------
# authed_http_json: 401 → force-refresh-and-retry-once
# ---------------------------------------------------------------------------


class TestAuthedHttpJson401Recovery:
    """`authed_http_json` wraps http_json with on-401 recovery: when DA
    rejects a locally-fresh access_token (server-side grant revocation),
    force a refresh and retry once. If still 401, refresh_token itself
    is bad and we exit asking for `da auth`."""

    def _make_authed_state(self) -> dict:
        return {
            "access_token": "OLD",
            "expires_at": time.time() + 3600,  # locally fresh
            "refresh_token": "RT-OLD",
            "scope": "browse",
        }

    def test_no_401_no_refresh(self, isolated_paths, monkeypatch):
        cfg = {"client_id": "12345"}
        state = self._make_authed_state()
        # http_json succeeds first try; force_refresh shouldn't fire
        refreshes: list[bool] = []

        def fake_http_post_json(url, form, **_kw):
            refreshes.append(True)
            return {"access_token": "NEW", "expires_in": 3600}

        def fake_urlopen(req, timeout=None):
            return _make_response(json.dumps({"ok": True}).encode())

        monkeypatch.setattr(dacli, "http_post_json", fake_http_post_json)
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = dacli.authed_http_json("https://example/", cfg, state)
        assert result == {"ok": True}
        assert refreshes == []  # no refresh needed
        assert state["access_token"] == "OLD"  # untouched

    def test_401_triggers_force_refresh_then_retry(self, isolated_paths, monkeypatch):
        cfg = {"client_id": "12345"}
        state = self._make_authed_state()
        api_calls: list[str] = []

        def fake_http_post_json(url, form, **_kw):
            return {"access_token": "NEW", "expires_in": 3600}

        def fake_urlopen(req, timeout=None):
            # get_header: the token is an unredirected header (it must not
            # survive a cross-host 30x), which req.headers does not hold.
            tok = req.get_header("Authorization", "")
            api_calls.append(tok)
            if tok == "Bearer OLD":
                raise _http_error(401, "rejected")
            return _make_response(json.dumps({"ok": True}).encode())

        monkeypatch.setattr(dacli, "http_post_json", fake_http_post_json)
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        result = dacli.authed_http_json("https://example/", cfg, state)
        assert result == {"ok": True}
        assert state["access_token"] == "NEW"  # refreshed
        # Two API calls: first with OLD (got 401), retry with NEW
        assert api_calls == ["Bearer OLD", "Bearer NEW"]

    def test_persistent_401_exits_with_re_auth_message(self, isolated_paths, monkeypatch, capsys):
        cfg = {"client_id": "12345"}
        state = self._make_authed_state()

        def fake_http_post_json(url, form, **_kw):
            return {"access_token": "ALSO-BAD", "expires_in": 3600}

        def fake_urlopen(req, timeout=None):
            raise _http_error(401, "rejected")

        monkeypatch.setattr(dacli, "http_post_json", fake_http_post_json)
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(SystemExit) as exc:
            dacli.authed_http_json("https://example/", cfg, state)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "re-issue" in err.lower() or "da auth" in err.lower()

    def test_non_401_passes_through(self, isolated_paths, monkeypatch):
        cfg = {"client_id": "12345"}
        state = self._make_authed_state()

        def fake_urlopen(req, timeout=None):
            raise _http_error(500, "server")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        with pytest.raises(urllib.error.HTTPError) as exc:
            dacli.authed_http_json("https://example/", cfg, state, retries=0)
        assert exc.value.code == 500

    def test_force_refresh_skips_cached_token(self, isolated_paths, monkeypatch):
        """access_token(force_refresh=True) hits the token endpoint
        even when the cached token is still locally valid."""
        cfg = {"client_id": "12345"}
        state = self._make_authed_state()
        refresh_count = [0]

        def fake_http_post_json(url, form, **_kw):
            refresh_count[0] += 1
            return {"access_token": "NEW", "expires_in": 3600}

        monkeypatch.setattr(dacli, "http_post_json", fake_http_post_json)
        # First call without force: returns cached
        assert dacli.access_token(cfg, state) == "OLD"
        assert refresh_count[0] == 0
        # Second call with force: refreshes
        assert dacli.access_token(cfg, state, force_refresh=True) == "NEW"
        assert refresh_count[0] == 1


# ---------------------------------------------------------------------------
# Integrated 401 mid-loop scenario: a sync run gets a 401 mid-loop,
# forces a refresh, and resumes correctly. Covers the cross-cutting
# state mutation: state["access_token"] gets rebound inside
# authed_http_json, the metadata batch picks it up, the loop continues.
# ---------------------------------------------------------------------------


class TestSyncFeed401MidLoop:
    def test_401_then_refresh_then_continue(
        self, isolated_paths, no_keychain, tmp_path, monkeypatch
    ):
        # Set up a working sync environment
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.save_state(
            {
                "access_token": "OLD-TOKEN",
                "expires_at": time.time() + 3600,
                "refresh_token": "RT",
                "scope": "browse",
            }
        )

        # Stage: feed call 1 with OLD-TOKEN → 401
        # Refresh fires → access_token becomes NEW-TOKEN
        # Feed call 2 with NEW-TOKEN → 200 with empty results
        url_call_count: list[int] = []

        def fake_http_post_json(url, form, **_kw):
            return {"access_token": "NEW-TOKEN", "expires_in": 3600}

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            # get_header: the token is an unredirected header (it must not
            # survive a cross-host 30x), which req.headers does not hold.
            tok = req.get_header("Authorization", "")
            url_call_count.append(1)
            if "browse/deviantsyouwatch" in url and tok == "Bearer OLD-TOKEN":
                raise _http_error(401, "rejected")
            # Any other call (refreshed feed, metadata, etc) returns success
            if "browse/deviantsyouwatch" in url:
                return _make_response(json.dumps({"results": [], "has_more": False}).encode())
            return _make_response(b"{}")

        monkeypatch.setattr(dacli, "http_post_json", fake_http_post_json)
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("time.sleep", lambda *_: None)

        ns = dacli.build_parser().parse_args(["sync", "feed", "--time-budget", "60"])
        # Should complete cleanly — 401 triggers refresh, retry succeeds
        ns.func(ns)

        # Verify: access_token in state was updated
        assert dacli.load_state()["access_token"] == "NEW-TOKEN"


# ---------------------------------------------------------------------------
# Cross-process command lock: subprocess-based test that pins down the
# real contract. Two python processes, the first holds the lock for a
# beat, the second tries non-blockingly and is correctly rejected.
# ---------------------------------------------------------------------------


class TestCommandLockCrossProcess:
    def test_two_processes_cannot_both_hold(self, isolated_paths, tmp_path):
        import subprocess
        import sys
        import textwrap

        # Interpolate the repo root (for sys.path) and the isolated state
        # dir directly. An earlier version built the scripts with
        # tmp_path.parent.parent and then str-replaced that prefix with
        # the repo root — but the isolated state dir starts with the same
        # prefix, so the replace mangled it too and both processes locked
        # under <repo_root>/pytest-NNNN/..., littering the repo root with
        # a directory per run. The test still passed (both sides agreed on
        # the wrong path), which is why it went unnoticed.
        repo_root = Path(__file__).resolve().parent.parent
        state_dir = str(isolated_paths["st_dir"])

        holder_script = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(repo_root)!r})
            import dacli
            from pathlib import Path
            dacli.STATE_DIR = Path({state_dir!r})
            with dacli._cmd_lock("xprocess-test"):
                print("HELD", flush=True)
                time.sleep(1.0)
        """)

        challenger_script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(repo_root)!r})
            import dacli
            from pathlib import Path
            dacli.STATE_DIR = Path({state_dir!r})
            try:
                with dacli._cmd_lock("xprocess-test"):
                    print("ACQUIRED")
            except dacli.CommandLockedError as e:
                print(f"REJECTED: {{e}}")
        """)

        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Wait for holder to print "HELD"
        held_signal = holder.stdout.readline().strip() if holder.stdout else ""
        assert held_signal == "HELD", f"holder didn't acquire: {held_signal!r}"

        # Now run challenger — should fail to acquire
        challenger = subprocess.run(
            [sys.executable, "-c", challenger_script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        # Wait for holder to release
        holder.wait(timeout=10)

        out = challenger.stdout.strip()
        assert "REJECTED" in out, f"expected REJECTED, got: {out!r}"


# ---------------------------------------------------------------------------
# Sync resilience: per-deviation failure within a page doesn't poison siblings
# ---------------------------------------------------------------------------


class TestSyncResilienceWithFaults:
    """`_save_one` catches its own exceptions and returns 'fail:X' status.
    `_save_page_concurrent` must collect those and continue, so that
    one bad image doesn't break a 24-image page."""

    def test_partial_image_failure_doesnt_block_siblings(
        self, isolated_paths, no_keychain, tmp_path, monkeypatch
    ):
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.save_state(
            {
                "access_token": "AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "RT",
                "scope": "browse",
            }
        )

        # Build 5 deviations; image fetch raises on D-2 only
        def fake_deviation(devid: str) -> dict:
            return {
                "deviationid": devid,
                "url": f"https://da/{devid}",
                "title": f"T-{devid}",
                "is_mature": False,
                "is_favourited": False,
                "published_time": "1",
                "stats": {"comments": 0, "favourites": 0},
                "author": {"username": "alice", "userid": "U1"},
                "content": {"src": f"https://cdn/{devid}.png"},
            }

        results = [fake_deviation(f"D-{i}") for i in range(5)]
        md = {f"D-{i}": {"deviationid": f"D-{i}", "tags": []} for i in range(5)}

        def fake_bytes(url, **_kw):
            if "D-2" in url:
                raise urllib.error.HTTPError(
                    url=url, code=500, msg="cdn down", hdrs={}, fp=BytesIO(b"{}")
                )
            return b"PNG"

        monkeypatch.setattr(dacli, "http_bytes", fake_bytes)
        monkeypatch.setattr("time.sleep", lambda *_: None)

        out = dacli._save_page_concurrent(
            results,
            md,
            dest,
            fallback_artist=None,
            image_delay=0.0,
            jitter_pct=0.0,
            concurrency=4,
        )

        statuses = [s for s, *_ in out]
        # 4 ok, 1 fail
        assert statuses.count("ok") == 4
        assert any(s.startswith("fail:") for s in statuses)
        # The 4 ok ones are indexed; the failed one is not
        for i in range(5):
            devid = f"D-{i}"
            if i == 2:
                assert not dacli.index_has(devid), f"{devid} should NOT be indexed after image fail"
            else:
                assert dacli.index_has(devid), f"{devid} should be indexed"

    def test_mid_stream_corruption_keeps_page_progressing(
        self, isolated_paths, no_keychain, tmp_path, monkeypatch
    ):
        """Simulate a CDN that returns 0-byte responses for some images
        (DA's CDN does this occasionally on hot-path 401s).

        _save_one must reject the empty body rather than commit it.
        Committing a 0-byte image and indexing it made the deviation
        permanently 'synced' — index_has() ignores size, so the broken
        file was never retried. Failing here means the next run
        re-fetches it."""
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.save_state(
            {"access_token": "AT", "expires_at": time.time() + 3600, "scope": "browse"}
        )

        d1 = {
            "deviationid": "D-empty",
            "url": "u",
            "title": "Empty",
            "is_mature": False,
            "is_favourited": False,
            "published_time": "1",
            "stats": {"comments": 0, "favourites": 0},
            "author": {"username": "alice", "userid": "U1"},
            "content": {"src": "https://cdn/empty.png"},
        }
        monkeypatch.setattr(dacli, "http_bytes", lambda url, **_kw: b"")
        monkeypatch.setattr("time.sleep", lambda *_: None)
        status, _, _, size = dacli._save_one(d1, {}, dest)
        # Empty body is a failed download: not committed, not indexed,
        # so the next sync retries it.
        assert status == "fail:EmptyBody"
        assert size == 0
        assert not dacli.index_has("D-empty")
        assert not list(dest.glob("*/*/image.*"))


# ---------------------------------------------------------------------------
# Disk-full (ENOSPC) during sync writes
#
# Real-world failure mode: a long-running sync fills the destination volume
# mid-page. Both the description.json write and the image .part write can
# raise OSError(ENOSPC). Contract:
#   - _save_one returns "fail:OSError" with artist/title populated
#   - the failed deviation is NOT added to the index
#   - sibling deviations on the same page are unaffected
#   - no half-written final files leak (only .part residue, which the next
#     run will retry)
# ---------------------------------------------------------------------------


class TestDiskFullHandling:
    def test_enospc_on_image_write_returns_fail(self, isolated_paths, tmp_path, monkeypatch):
        dest = tmp_path / "gallery"
        dest.mkdir()
        d = {
            "deviationid": "D-enospc-img",
            "url": "u",
            "title": "T",
            "is_mature": False,
            "is_favourited": False,
            "published_time": "1",
            "stats": {},
            "author": {"username": "alice", "userid": "U1"},
            "content": {"src": "https://cdn/x.png"},
        }
        # http_bytes succeeds; the .part write raises ENOSPC.
        monkeypatch.setattr(dacli, "http_bytes", lambda url, **_kw: b"PNG_BLOB")
        monkeypatch.setattr("time.sleep", lambda *_: None)

        real_write_bytes = Path.write_bytes

        def boom(self, data):
            if self.suffix == ".part":
                raise OSError(28, "No space left on device")
            return real_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", boom)

        status, artist, title, size = dacli._save_one(d, {}, dest)

        assert status == "fail:OSError", f"got {status!r}"
        assert artist == "alice"
        assert title == "T"
        assert size == 0
        assert not dacli.index_has("D-enospc-img"), "ENOSPC must NOT poison the index"
        # Description was written before image attempt — that's expected; the
        # next sync run will see description.json without a final image and
        # retry the image fetch (covered by test_part_file_not_treated_as_complete).
        assert (dest / "alice" / "T" / "description.json").exists()
        assert not (dest / "alice" / "T" / "image.png").exists()

    def test_enospc_on_description_write_returns_fail_with_artist_title(
        self, isolated_paths, tmp_path, monkeypatch
    ):
        dest = tmp_path / "gallery"
        dest.mkdir()
        d = {
            "deviationid": "D-enospc-desc",
            "url": "u",
            "title": "T",
            "is_mature": False,
            "is_favourited": False,
            "published_time": "1",
            "stats": {},
            "author": {"username": "bob", "userid": "U2"},
            "content": {"src": "https://cdn/x.png"},
        }
        # _atomic_write uses Path.write_text. Patch ONLY the .tmp suffix
        # path so other write_text calls in conftest aren't affected.
        real_write_text = Path.write_text

        def boom(self, *a, **kw):
            # Matches the staging file for description.json whatever its
            # exact name: _atomic_write now uses mkstemp, so the tmp is
            # ".description.json.<random>.tmp" rather than a fixed
            # "description.json.tmp" two writers could collide on.
            if self.suffix == ".tmp" and "description.json" in self.name:
                raise OSError(28, "No space left on device")
            return real_write_text(self, *a, **kw)

        monkeypatch.setattr(Path, "write_text", boom)
        monkeypatch.setattr(dacli, "http_bytes", lambda url, **_kw: b"PNG_BLOB")
        monkeypatch.setattr("time.sleep", lambda *_: None)

        status, artist, title, _ = dacli._save_one(d, {}, dest)

        assert status == "fail:OSError"
        # The artist/title are still populated — this is the symmetry the
        # in-_save_one OSError catch is responsible for. Without it, the
        # exception would propagate and _save_page_concurrent's defensive
        # handler would emit empty strings.
        assert artist == "bob"
        assert title == "T"
        assert not dacli.index_has("D-enospc-desc")

    def test_enospc_doesnt_poison_sibling_deviations_in_same_page(
        self, isolated_paths, tmp_path, monkeypatch
    ):
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.save_state(
            {"access_token": "AT", "expires_at": time.time() + 3600, "scope": "browse"}
        )

        results = []
        for i in range(4):
            results.append(
                {
                    "deviationid": f"D-{i}",
                    "url": "u",
                    "title": f"T-{i}",
                    "is_mature": False,
                    "is_favourited": False,
                    "published_time": "1",
                    "stats": {},
                    "author": {"username": "alice", "userid": "U1"},
                    "content": {"src": f"https://cdn/{i}.png"},
                }
            )

        monkeypatch.setattr(dacli, "http_bytes", lambda url, **_kw: b"PNG")
        monkeypatch.setattr("time.sleep", lambda *_: None)

        # Only D-2's image write hits ENOSPC.
        real_write_bytes = Path.write_bytes

        def selective_enospc(self, data):
            if self.suffix == ".part" and "T-2" in str(self):
                raise OSError(28, "No space left on device")
            return real_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", selective_enospc)

        out = dacli._save_page_concurrent(
            results,
            {},
            dest,
            fallback_artist=None,
            image_delay=0.0,
            jitter_pct=0.0,
            concurrency=4,
        )

        statuses = [s for s, *_ in out]
        # 3 ok, 1 fail
        assert statuses.count("ok") == 3
        assert any(s == "fail:OSError" for s in statuses)
        for i in (0, 1, 3):
            assert dacli.index_has(f"D-{i}"), f"sibling D-{i} should be indexed"
        assert not dacli.index_has("D-2")


# ---------------------------------------------------------------------------
# Inter-process partial-write resumption
#
# Pins down the durability story end-to-end: if a sync subprocess is killed
# mid-_save_one — after the .part is staged but before the atomic rename —
# the next sync subprocess must NOT treat the half-written image as
# complete, must redownload it cleanly, and must not leave .part residue.
#
# The single-process variant of this is `test_part_file_not_treated_as_complete`
# in test_sync.py. This test crosses the process boundary, where filesystem
# flush ordering, no shared memory, and a fresh import of dacli all matter.
# ---------------------------------------------------------------------------


class TestInterProcessPartialWriteResumption:
    def test_killed_mid_write_resumes_cleanly(self, isolated_paths, tmp_path):
        import subprocess
        import sys
        import textwrap

        dest = tmp_path / "gallery"
        dest.mkdir()
        repo_root = Path(__file__).resolve().parent.parent

        # Subprocess A: monkey-patches Path.replace to raise so the .part is
        # written + fsynced but the atomic rename never happens. Exits
        # nonzero, simulating a crash after stage and before commit.
        crashing_script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(repo_root)!r})
            from pathlib import Path
            import dacli
            dacli.STATE_DIR = Path({str(isolated_paths["st_dir"])!r})
            dacli.INDEX_PATH = Path({str(isolated_paths["idx"])!r})
            dacli._BOOTSTRAP_CHECKED_THIS_PROCESS = True

            # Force the .part → image rename to fail. Other atomic-writes
            # (description.json's tmp → final) must still work, so we only
            # boom when the source path is the staged image .part file.
            real_replace = Path.replace
            def selective_boom(self, target):
                if self.suffix == ".part":
                    raise RuntimeError("simulated SIGKILL after fsync, before rename")
                return real_replace(self, target)
            Path.replace = selective_boom

            dacli.http_bytes = lambda url, **kw: b"REAL_PNG_BYTES"

            d = {{
                "deviationid": "D-crash-1",
                "url": "u",
                "title": "T",
                "is_mature": False,
                "is_favourited": False,
                "published_time": "1",
                "stats": {{}},
                "author": {{"username": "alice", "userid": "U1"}},
                "content": {{"src": "https://cdn/x.png"}},
            }}
            status, *_ = dacli._save_one(d, {{}}, Path({str(dest)!r}))
            print(f"STATUS={{status}}")
            sys.exit(0 if status.startswith("fail:") else 7)
        """)

        a = subprocess.run(
            [sys.executable, "-c", crashing_script],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert a.returncode == 0, (
            f"crashing process didn't fail-soft: rc={a.returncode} stderr={a.stderr!r}"
        )
        assert "STATUS=fail:RuntimeError" in a.stdout

        # Disk state after the crash: description.json saved, .part lingering,
        # no final image.png, devid NOT indexed.
        folder = dest / "alice" / "T"
        assert (folder / "description.json").exists()
        assert (folder / "image.png.part").exists()
        assert not (folder / "image.png").exists()

        # Subprocess B: fresh process, clean http_bytes, retry the same
        # deviation. Must produce a real image.png, no .part residue, indexed.
        recovery_script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(repo_root)!r})
            from pathlib import Path
            import dacli
            dacli.STATE_DIR = Path({str(isolated_paths["st_dir"])!r})
            dacli.INDEX_PATH = Path({str(isolated_paths["idx"])!r})
            dacli._BOOTSTRAP_CHECKED_THIS_PROCESS = True

            dacli.http_bytes = lambda url, **kw: b"FRESH_PNG_BYTES"

            d = {{
                "deviationid": "D-crash-1",
                "url": "u",
                "title": "T",
                "is_mature": False,
                "is_favourited": False,
                "published_time": "1",
                "stats": {{}},
                "author": {{"username": "alice", "userid": "U1"}},
                "content": {{"src": "https://cdn/x.png"}},
            }}
            status, *_ = dacli._save_one(d, {{}}, Path({str(dest)!r}))
            print(f"STATUS={{status}}")
            sys.exit(0 if status == "ok" else 7)
        """)

        b = subprocess.run(
            [sys.executable, "-c", recovery_script],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert b.returncode == 0, (
            f"recovery failed: rc={b.returncode} out={b.stdout!r} err={b.stderr!r}"
        )
        assert "STATUS=ok" in b.stdout
        assert (folder / "image.png").exists()
        assert (folder / "image.png").read_bytes() == b"FRESH_PNG_BYTES"
        assert not (folder / "image.png.part").exists()


class TestMetadataFailureStopsCleanly:
    """A 429 from /deviation/metadata must not escape as a traceback.

    Both walks wrapped only the page-listing call in an HTTPError
    handler. The metadata call two lines later hits the same rate limiter
    with the same token and was unguarded, so a 429 there propagated out
    through cmd_sync_* (which catches only CommandLockedError) and
    main() (which caught only KeyboardInterrupt) — a stack trace, and no
    sync summary, so `da diagnose` went on describing the run before.
    """

    def _pages(self, sample_deviation):
        def fake(url, **_kw):
            if "metadata" in url:
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
            return {"results": [dict(sample_deviation, deviationid="D-1")], "has_more": False}

        return fake

    def test_feed_records_a_summary(self, authed_with_destination, sample_deviation):
        recorded = {}
        with patch.object(dacli, "http_json", side_effect=self._pages(sample_deviation)):
            with patch.object(
                dacli.sync,
                "_record_sync_summary",
                lambda kind, started, totals, reason, **kw: recorded.update(reason=reason),
            ):
                with patch("time.sleep"):
                    dacli.cmd_sync_feed(_sync_args("feed"))
        assert "429" in recorded.get("reason", ""), (
            f"the walk did not record why it stopped: {recorded}"
        )

    def test_artist_records_a_summary(self, authed_with_destination, sample_deviation):
        recorded = {}
        with patch.object(dacli, "http_json", side_effect=self._pages(sample_deviation)):
            with patch.object(
                dacli.sync,
                "_record_sync_summary",
                lambda kind, started, totals, reason, **kw: recorded.update(reason=reason),
            ):
                with patch("time.sleep"):
                    dacli.cmd_sync_artist(_sync_args("artist", artist="alice"))
        assert "429" in recorded.get("reason", "")


class TestMainTurnsErrorsIntoExitCodes:
    """The exit code half of the contract.

    What each message SAYS is covered by TestHandledFailuresSayWhatToDo,
    which supersedes the per-type tests that used to live here — they
    asserted the same three types with weaker assertions.
    """

    def _run(self, exc):
        parser = dacli.build_parser()
        ns = parser.parse_args(["whoami"])
        ns.func = lambda _a: (_ for _ in ()).throw(exc)
        with patch.object(dacli, "build_parser", return_value=parser):
            with patch.object(parser, "parse_args", return_value=ns):
                with pytest.raises(SystemExit) as exc_info:
                    dacli.main(["whoami"])
        return exc_info.value.code

    def test_keyboard_interrupt_exits_130(self, isolated_paths):
        """Ctrl-C is not a failure, and keeps its own code."""
        assert self._run(KeyboardInterrupt()) == 130


class TestHandledFailuresSayWhatToDo:
    """A caught error must earn its keep by being more useful than the crash.

    Catching an exception and printing `f"{type(e).__name__}: {e}"` is
    strictly worse than letting it through: the same words, minus the
    traceback. Every branch here names an action.
    """

    def _message(self, exc, capsys):
        parser = dacli.build_parser()
        ns = parser.parse_args(["whoami"])
        ns.func = lambda _a: (_ for _ in ()).throw(exc)
        with patch.object(dacli, "build_parser", return_value=parser):
            with patch.object(parser, "parse_args", return_value=ns):
                with pytest.raises(SystemExit) as info:
                    dacli.main(["whoami"])
        out = capsys.readouterr()
        return info.value.code, out.out + out.err

    def _os_error(self, cls, err, filename="/Volumes/ArtDrive/gallery"):
        e = cls("boom")
        e.errno = err
        e.filename = filename
        return e

    @pytest.mark.parametrize(
        ("code", "must_mention"),
        [
            (401, "da auth"),
            (403, "da auth"),
            (429, "--delay-api"),
            (503, "temporary"),
            (500, "temporary"),
            (404, "Check the username"),
        ],
    )
    def test_http_failures_name_an_action(self, isolated_paths, capsys, code, must_mention):
        exit_code, text = self._message(
            urllib.error.HTTPError("u", code, "reason", {}, None), capsys
        )
        assert exit_code == 2, "every handled failure keeps the documented exit code"
        assert must_mention in text, (
            f"HTTP {code} message does not tell the user what to do:\n{text}"
        )

    @pytest.mark.parametrize(
        ("err", "must_mention"),
        [
            (errno.ENOSPC, "Free some space"),
            (errno.EACCES, "Permission denied"),
            (errno.EROFS, "read-only"),
            (errno.ENOENT, "mount it"),
        ],
    )
    def test_filesystem_failures_name_an_action(self, isolated_paths, capsys, err, must_mention):
        exit_code, text = self._message(self._os_error(OSError, err), capsys)
        assert exit_code == 2
        assert must_mention in text, f"errno {err} message is not actionable:\n{text}"

    def test_the_failing_path_is_named(self, isolated_paths, capsys):
        _, text = self._message(self._os_error(OSError, errno.ENOSPC), capsys)
        assert "/Volumes/ArtDrive/gallery" in text, "the message does not say which path filled up"

    def test_network_failure_says_it_will_retry(self, isolated_paths, capsys):
        exit_code, text = self._message(urllib.error.URLError("no route to host"), capsys)
        assert exit_code == 2
        assert "retry" in text and "nothing has been lost" in text

    def test_advice_does_not_bury_itself_under_a_traceback_hint(self, isolated_paths, capsys):
        """When we know what to do, do not point at the traceback instead."""
        _, text = self._message(urllib.error.HTTPError("u", 401, "no", {}, None), capsys)
        assert "re-run with -v" not in text, (
            "the -v hint follows actionable advice, which buries it"
        )

    def test_the_hint_appears_when_there_is_no_advice(self, isolated_paths, capsys):
        """A status we have nothing specific to say about still points somewhere."""
        _, text = self._message(urllib.error.HTTPError("u", 418, "teapot", {}, None), capsys)
        assert "re-run with -v" in text


class TestEveryRealisticFailureIsClassified:
    """Sweep of what the HTTP and filesystem layers actually emit.

    The rule only helps if the line falls in the right place. A timeout
    is the ordinary case — DeviantArt being slow — and it arrives as a
    TimeoutError with no errno, so it fell through to "unrecognised" and
    surfaced as a traceback until this sweep caught it. Presenting routine
    failures as defects teaches people to ignore the tracebacks that
    matter.
    """

    def _outcome(self, exc):
        parser = dacli.build_parser()
        ns = parser.parse_args(["whoami"])
        ns.func = lambda _a: (_ for _ in ()).throw(exc)
        with patch.object(dacli, "build_parser", return_value=parser):
            with patch.object(parser, "parse_args", return_value=ns):
                try:
                    dacli.main(["whoami"])
                except SystemExit:
                    return "handled"
                except BaseException:
                    return "traceback"
        return "none"

    @pytest.mark.parametrize(
        ("label", "exc"),
        [
            ("rate limit", urllib.error.HTTPError("u", 429, "x", {}, None)),
            ("dns failure", urllib.error.URLError("Name or service not known")),
            ("timeout", TimeoutError("timed out")),
            ("connection reset", ConnectionResetError("reset by peer")),
            ("broken pipe", BrokenPipeError("broken pipe")),
            ("tls failure", ssl.SSLError("handshake failed")),
            ("non-json reply", json.JSONDecodeError("bad", "x", 0)),
        ],
    )
    def test_routine_failures_are_explained(self, isolated_paths, capsys, label, exc):
        outcome = self._outcome(exc)
        capsys.readouterr()
        assert outcome == "handled", (
            f"{label} produced a traceback; it is a normal transient failure, "
            f"not a defect in da-cli"
        )

    @pytest.mark.parametrize(
        ("label", "exc"),
        [
            ("logic error", TypeError("unsupported operand")),
            ("missing attribute", AttributeError("'NoneType' has no attribute 'get'")),
            ("missing key", KeyError("deviationid")),
        ],
    )
    def test_defects_still_surface(self, isolated_paths, label, exc):
        assert self._outcome(exc) == "traceback", (
            f"{label} was summarised as a user-facing error, which disguises a "
            f"bug in da-cli as a problem with the user's setup"
        )


class TestUnrecognisedErrorsKeepTheirTraceback:
    """A defect in da-cli must not be dressed up as the user's problem.

    Handled failures get a message because we know what they mean.
    Everything else is most likely a bug here, and a tidy one-liner
    saying "TypeError: unsupported operand" reads like something the user
    misconfigured. The traceback is the honest signal.
    """

    def _run(self, exc):
        parser = dacli.build_parser()
        ns = parser.parse_args(["whoami"])
        ns.func = lambda _a: (_ for _ in ()).throw(exc)
        with patch.object(dacli, "build_parser", return_value=parser):
            with patch.object(parser, "parse_args", return_value=ns):
                dacli.main(["whoami"])

    @pytest.mark.parametrize(
        "exc",
        [
            TypeError("unsupported operand type(s) for +: 'int' and 'str'"),
            AttributeError("'NoneType' object has no attribute 'get'"),
            KeyError("deviationid"),
        ],
    )
    def test_programming_errors_propagate(self, isolated_paths, exc):
        with pytest.raises(type(exc)):
            self._run(exc)

    def test_an_unrecognised_oserror_propagates(self, isolated_paths):
        """errno we have no advice for is more likely a bug than a disk."""
        e = OSError("something odd")
        e.errno = errno.EMLINK  # too many links; nothing sensible to suggest
        with pytest.raises(OSError, match="something odd"):
            self._run(e)

    def test_a_recognised_oserror_does_not(self, isolated_paths, capsys):
        e = OSError("full")
        e.errno = errno.ENOSPC
        with pytest.raises(SystemExit) as info:
            self._run(e)
        assert info.value.code == 2
        capsys.readouterr()


class TestACrashedRunIsNotReportedAsYesterdaysSuccess:
    """A failed sync must not leave `last_sync` describing the last good one.

    The second half of the review question on #47: for an unattended run
    the message on stderr goes to a log nobody is watching, so the only
    thing the user will consult is `da diagnose` — and it read the
    previous run's summary, because a crash never wrote one.

    Measured before the fix: a disk-full during tonight's walk, and
    diagnose still reported `[ok] feed ended 2.0h ago (feed exhausted)
    ok=42`. A green light on a machine that had just failed.
    """

    def _yesterdays_success(self):
        return {
            "kind": "feed",
            "started_at": int(time.time() - 7200),
            "ended_at": int(time.time() - 7100),
            "duration_s": 100.0,
            "totals": {"ok": 42, "dup": 0, "noimg": 0, "fail": 0},
            "stop_reason": "feed exhausted",
        }

    def _crash_a_sync(self, authed_with_destination, exc, argv):
        state = dacli.load_state()
        state["last_sync"] = self._yesterdays_success()
        dacli.save_state(state)

        def boom(*a, **kw):
            raise exc

        with patch.object(dacli, "http_json", side_effect=boom):
            with patch("time.sleep"):
                with pytest.raises(SystemExit):
                    dacli.main(argv)
        return dacli.load_state().get("last_sync", {})

    def test_feed_crash_replaces_the_stale_summary(self, authed_with_destination):
        last = self._crash_a_sync(
            authed_with_destination,
            _disk_full(),
            ["sync", "feed", "--time-budget", "30"],
        )
        assert "failed" in last.get("stop_reason", ""), (
            f"diagnose would still report the previous run: {last.get('stop_reason')!r}"
        )
        assert last["totals"]["ok"] == 0, "a crashed run must not claim yesterday's downloads"

    def test_artist_crash_replaces_the_stale_summary(self, authed_with_destination):
        last = self._crash_a_sync(
            authed_with_destination,
            _disk_full(),
            ["sync", "artist", "alice", "--time-budget", "30"],
        )
        assert "failed" in last.get("stop_reason", "")

    def test_diagnose_stops_saying_ok(self, authed_with_destination):
        self._crash_a_sync(
            authed_with_destination,
            _disk_full(),
            ["sync", "feed", "--time-budget", "30"],
        )
        with patch("sys.platform", "linux"):
            levels = {section: level for level, section, _ in dacli._diagnose_checks()}
        assert levels["last sync"] != "ok", "diagnose gave a green light after a failed run"

    def test_an_interrupted_run_is_also_recorded(self, authed_with_destination):
        """Ctrl-C halfway is not the completed run either."""
        last = self._crash_a_sync(
            authed_with_destination,
            KeyboardInterrupt(),
            ["sync", "feed", "--time-budget", "30"],
        )
        assert "failed" in last.get("stop_reason", "")


class TestBenchCleansUpAfterItself:
    """`da bench` created a temp tree per invocation and removed none.

    3,076 of them, 125 MB, on the machine this was found on — and no test
    could catch it, because the harness leaked the same trees.
    """

    def test_no_temp_tree_survives(self, isolated_paths, no_keychain, capsys, tmp_path):
        import tempfile

        root = pathlib.Path(tempfile.gettempdir())
        before = set(root.glob("da-bench-*"))
        ns = dacli.build_parser().parse_args(["bench", "--pages", "1", "--per-page", "2", "--json"])
        ns.func(ns)
        capsys.readouterr()
        leaked = set(root.glob("da-bench-*")) - before
        assert not leaked, f"bench left {len(leaked)} temp tree(s) behind: {sorted(leaked)[:3]}"
