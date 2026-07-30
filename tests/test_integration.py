"""End-to-end integration tests for the da CLI.

Goal: every user-facing CLI command parses cleanly, calls the right HTTP
endpoint(s) with the right method and parameters, and produces correct
output / side effects. Network is mocked at the lowest sensible layer
(`urllib.request.urlopen`) so we exercise the real HTTP construction
code (URL building, headers, methods, body encoding) without ever
hitting the network.

Each test:
- Builds an argparse Namespace via the real `dacli.build_parser()`
- Invokes the handler the parser routed to
- Captures the URL/method/headers/body of every HTTP request
- Asserts on output (stdout) and side effects (state file, index DB,
  config file, destination filesystem)

These complement the unit tests in test_*.py — there's overlap, but the
purpose here is to validate that the WHOLE command graph is well-formed,
not individual functions.
"""

from __future__ import annotations

import argparse
import json
import threading as threading_module
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import dacli

from .conftest import describe_folder as _describe

# ---------------------------------------------------------------------------
# Shared fixtures: a fully-authed CLI with a writable destination + a fake
# urlopen that records every request and serves canned JSON responses keyed
# by URL substring.
# ---------------------------------------------------------------------------


@pytest.fixture
def authed_with_dest(isolated_paths, no_keychain, tmp_path):
    """Pre-set client_id, valid token, and a writable destination."""
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
    return dest


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_a: object) -> None:
        pass

    def read(self) -> bytes:
        return self._payload


@pytest.fixture
def request_recorder(monkeypatch):
    """Record every request; route responses by URL substring.

    Tests populate `recorder["responses"]` (URL substring → JSON object)
    and `recorder["bytes"]` (URL substring → raw bytes). Anything not
    matched returns `{}`. The fake_urlopen closes over `recorder` and
    reads `recorder["responses"]` at call time, so tests can either
    reassign the whole dict or mutate it — both work.
    """
    recorder: dict[str, Any] = {
        "requests": [],
        "responses": {},
        "bytes": {},
    }

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        method = req.get_method() if hasattr(req, "get_method") else "GET"
        data = req.data if hasattr(req, "data") else None
        headers = dict(req.headers) if hasattr(req, "headers") else {}
        recorder["requests"].append(
            {"url": url, "method": method, "data": data, "headers": headers}
        )

        # Image bytes path
        for fragment, raw in recorder["bytes"].items():
            if fragment in url:
                return _FakeResponse(raw)

        # JSON path: longest-matching URL fragment wins
        responses = recorder["responses"]
        for fragment in sorted(responses, key=len, reverse=True):
            if fragment in url:
                return _FakeResponse(json.dumps(responses[fragment]).encode())
        return _FakeResponse(b"{}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return recorder


# ---------------------------------------------------------------------------
# Argparse wiring: every documented invocation produces a working Namespace
# ---------------------------------------------------------------------------


class TestParserWiringExhaustive:
    """Every public CLI invocation must parse and route to a handler."""

    @pytest.mark.parametrize(
        "argv",
        [
            # Auth
            ["auth"],
            ["auth", "--scope", "browse user"],
            ["auth", "--redirect-uri", "https://example.com/cb"],
            ["auth", "--paste"],
            ["auth", "logout"],
            ["whoami"],
            ["refresh"],
            # Config
            ["config", "show"],
            ["config", "path"],
            ["config", "get", "client_id"],
            ["config", "get", "client_secret", "--unmask"],
            ["config", "set", "client_id", "12345"],
            ["config", "unset", "client_id"],
            # Sync
            ["sync", "feed"],
            ["sync", "feed", "--limit", "10", "--time-budget", "60"],
            ["sync", "feed", "--jitter", "0.4", "--no-mature"],
            ["sync", "feed", "--concurrency", "8"],
            ["sync", "artist", "alice"],
            ["sync", "artist", "alice", "--offset", "24", "--full"],
            ["sync", "artist", "alice", "--concurrency", "1"],
            ["sync", "watched"],
            ["sync", "watched", "--concurrency", "4"],
            ["sync", "watched", "--via-feed", "--feed-max", "1000"],
            ["sync", "watched", "--user", "bob"],
            ["sync", "watched", "--full"],
            # Search (live)
            ["search", "tag", "abstract"],
            ["search", "tag", "abstract", "--limit", "5", "--mature"],
            ["search", "tag", "abstract", "--json"],
            ["search", "user", "alice"],
            ["search", "user", "alice", "bob", "carol"],
            ["search", "topic", "digitalart"],
            ["search", "topic", "nature", "--limit", "3", "--json"],
            ["search", "topics"],
            ["search", "topics", "--offset", "20"],
            ["search", "toptopics"],
            ["search", "tag-suggest", "lat"],
            # Search (deprecated — still parseable)
            ["search", "popular"],
            ["search", "newest"],
            # User
            ["user", "profile", "alice"],
            # Watch
            ["watch", "list"],
            ["watch", "list", "--limit", "10", "--offset", "0"],
            # Deviation
            ["deviation", "show", "DEVID-1"],
            ["deviation", "show", "DEVID-1", "--json"],
            ["deviation", "morelikethis", "DEVID-1"],
            # Daily
            ["daily"],
            ["daily", "2026-04-01"],
            ["daily", "--mature"],
            # Index
            ["index", "show"],
            ["index", "rebuild"],
            # Diagnose
            ["diagnose"],
            ["diagnose", "--json"],
            # Bench
            ["bench"],
            ["bench", "--pages", "5", "--per-page", "10"],
            ["bench", "--concurrency", "8", "--json"],
        ],
    )
    def test_parses(self, argv):
        ns = dacli.build_parser().parse_args(argv)
        assert hasattr(ns, "func"), f"no handler wired for {argv!r}"

    def test_unknown_subcommand_errors(self):
        with pytest.raises(SystemExit):
            dacli.build_parser().parse_args(["nonsense"])

    def test_help_lists_all_subcommands(self, capsys):
        with pytest.raises(SystemExit):
            dacli.build_parser().parse_args(["--help"])
        out = capsys.readouterr().out
        for noun in ["auth", "sync", "search", "config", "watch", "deviation", "daily", "index"]:
            assert noun in out, f"--help missing subcommand {noun!r}"


# ---------------------------------------------------------------------------
# Config: round-trip with the real on-disk format
# ---------------------------------------------------------------------------


class TestConstantsGovernTheCLI:
    """A named default must actually be the default.

    DEFAULT_LIMIT and DEFAULT_CONCURRENCY were exported in __all__ and
    documented as the defaults, while the parser used bare literals — so
    changing the constant changed nothing, and the constant was a comment
    with a type. These pin the wiring.
    """

    def test_sync_page_size_defaults_come_from_the_constant(self):
        parser = dacli.build_parser()
        for argv in (["sync", "feed"], ["sync", "artist", "alice"]):
            ns = parser.parse_args(argv)
            assert ns.limit == dacli.DEFAULT_LIMIT, (
                f"{' '.join(argv)} --limit defaults to {ns.limit}, "
                f"but DEFAULT_LIMIT is {dacli.DEFAULT_LIMIT}"
            )

    def test_bench_concurrency_default_comes_from_the_constant(self):
        ns = dacli.build_parser().parse_args(["bench"])
        assert ns.concurrency == dacli.DEFAULT_CONCURRENCY

    def test_sync_concurrency_default_resolves_to_the_constant(self):
        # sync's --concurrency default is None so config can supply it;
        # with neither set, the resolved value must still be the constant.
        ns = dacli.build_parser().parse_args(["sync", "feed"])
        assert dacli._concurrency({}, ns) == dacli.DEFAULT_CONCURRENCY


class TestConfigRoundTrip:
    def test_set_get_unset_lifecycle(self, isolated_paths, no_keychain, capsys):
        # set
        ns = dacli.build_parser().parse_args(["config", "set", "client_id", "abc"])
        ns.func(ns)
        # get
        ns = dacli.build_parser().parse_args(["config", "get", "client_id"])
        ns.func(ns)
        assert "abc" in capsys.readouterr().out
        # unset
        ns = dacli.build_parser().parse_args(["config", "unset", "client_id"])
        ns.func(ns)
        # get again should now exit(1)
        ns = dacli.build_parser().parse_args(["config", "get", "client_id"])
        with pytest.raises(SystemExit):
            ns.func(ns)

    def test_show_masks_secrets(self, isolated_paths, no_keychain, capsys):
        dacli.set_config_field("client_id", "12345")
        Path(dacli.CONFIG_PATH).write_text(
            json.dumps({"client_id": "12345", "client_secret": "NOT-A-REAL-SECRET-FOR-TESTS-0000"})
        )
        ns = dacli.build_parser().parse_args(["config", "show"])
        ns.func(ns)
        out = capsys.readouterr().out
        assert "1234" in out  # masked prefix
        assert "NOT-A-REAL-SECRET-FOR-TESTS-0000" not in out

    def test_secret_unmasked_with_flag_goes_to_stderr(self, isolated_paths, no_keychain, capsys):
        """`--unmask` writes raw secrets to stderr so stdout is safe to
        pipe to a file. See test_cli.py for the same contract pinned at
        the unit level."""
        dacli.set_config_field("client_secret", "NOT-A-REAL-SECRET-FOR-TESTS-0000")
        ns = dacli.build_parser().parse_args(["config", "get", "client_secret", "--unmask"])
        ns.func(ns)
        captured = capsys.readouterr()
        assert "NOT-A-REAL-SECRET-FOR-TESTS-0000" in captured.err
        assert "NOT-A-REAL-SECRET-FOR-TESTS-0000" not in captured.out


# ---------------------------------------------------------------------------
# Sync feed: full happy-path including index hit and metadata-skip fast path
# ---------------------------------------------------------------------------


def _fake_deviation(devid: str, title: str = "T", author: str = "alice") -> dict:
    return {
        "deviationid": devid,
        "url": f"https://da.example/{devid}",
        "title": title,
        "is_mature": False,
        "is_favourited": False,
        "published_time": "1700000000",
        "stats": {"comments": 0, "favourites": 0},
        "author": {"username": author, "userid": "U1"},
        "content": {"src": "https://cdn.example/img.png"},
    }


class TestSyncFeedIntegration:
    def test_first_run_downloads_and_indexes(self, authed_with_dest, request_recorder):
        d1 = _fake_deviation("D-1")
        d2 = _fake_deviation("D-2", title="Other", author="bob")
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {"results": [d1, d2], "has_more": False},
            "deviation/metadata": {
                "metadata": [
                    {"deviationid": "D-1", "tags": []},
                    {"deviationid": "D-2", "tags": []},
                ]
            },
        }
        request_recorder["bytes"] = {"img.png": b"PNG-BODY"}

        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "feed", "--time-budget", "60"])
            ns.func(ns)

        # Both deviations on disk
        assert (authed_with_dest / "alice" / "T" / "description.json").exists()
        assert (authed_with_dest / "bob" / "Other" / "image.png").read_bytes() == b"PNG-BODY"
        # Index populated
        assert dacli.index_has("D-1")
        assert dacli.index_has("D-2")
        # State checkpoint set
        assert dacli.load_state().get("last_feed_deviationid") == "D-1"

    def test_second_run_with_all_known_skips_metadata(self, authed_with_dest, request_recorder):
        # Pre-seed index — pretend prior run got both. Self-healing
        # index_filter_known stat()s each row's path; lay down real
        # description.json files so the rows count as live.
        for sub, devid in (("alice/T", "D-1"), ("bob/Other", "D-2")):
            _describe(authed_with_dest / sub, devid)
        dacli.index_add("D-1", "alice", "T", authed_with_dest / "alice" / "T", 5)
        dacli.index_add("D-2", "bob", "Other", authed_with_dest / "bob" / "Other", 5)

        d1 = _fake_deviation("D-1")
        d2 = _fake_deviation("D-2")
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {"results": [d1, d2], "has_more": False},
        }

        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "feed", "--time-budget", "60"])
            ns.func(ns)

        # NO metadata fetch should have happened
        urls = [r["url"] for r in request_recorder["requests"]]
        assert any("browse/deviantsyouwatch" in u for u in urls)
        assert not any("deviation/metadata" in u for u in urls), (
            "metadata fetch should be skipped on all-known pages"
        )

    def test_429_breaks_loop_cleanly(self, authed_with_dest, request_recorder, capsys):
        err = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        with patch.object(dacli, "http_json", side_effect=err):
            with patch("time.sleep"):
                ns = dacli.build_parser().parse_args(["sync", "feed", "--time-budget", "60"])
                ns.func(ns)
        assert "HTTP 429" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Sync artist: verify early-stop kicks in at first known deviationid
# ---------------------------------------------------------------------------


class TestSyncDryRun:
    """`da sync feed --dry-run` (and `artist`, `watched`) must:
      (a) report what *would* be downloaded,
      (b) NOT touch the destination directory,
      (c) NOT touch the synced-index.
    Pin all three so the contract is solid before users rely on it
    for preview / capacity-planning workflows.
    """

    def test_feed_dry_run_writes_no_files(self, authed_with_dest, request_recorder, isolated_paths):
        d1 = _fake_deviation("DRY-1")
        d2 = _fake_deviation("DRY-2", title="Other", author="bob")
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {"results": [d1, d2], "has_more": False},
            "deviation/metadata": {"metadata": []},
        }
        request_recorder["bytes"] = {"img.png": b"PNG-BODY"}

        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(
                ["sync", "feed", "--dry-run", "--time-budget", "60"]
            )
            ns.func(ns)

        # No files written.
        assert not (authed_with_dest / "alice").exists()
        assert not (authed_with_dest / "bob").exists()
        # Index untouched.
        assert dacli.index_count() == 0
        # The HTTP layer was called (we still walk the feed) but the
        # image-download endpoint was NOT hit (no point fetching bytes
        # we'd throw away).
        assert "cdn.example" not in " ".join(r["url"] for r in request_recorder["requests"])

    def test_feed_dry_run_does_not_advance_checkpoint(
        self, authed_with_dest, request_recorder, isolated_paths
    ):
        """A dry run must leave last_feed_deviationid alone.

        Regression: --dry-run downloaded nothing but still bookmarked
        the newest feed id, so the next REAL sync trimmed at that id and
        skipped forever exactly the items the user had previewed.
        """
        d1 = _fake_deviation("DRY-TOP")
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {"results": [d1], "has_more": False},
            "deviation/metadata": {"metadata": []},
        }
        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(
                ["sync", "feed", "--dry-run", "--time-budget", "60"]
            )
            ns.func(ns)

        assert "last_feed_deviationid" not in dacli.load_state()

    def test_feed_checkpoint_not_advanced_when_items_failed(
        self, authed_with_dest, request_recorder, isolated_paths
    ):
        """An incomplete run must not bookmark as if it were complete.

        Regression: the checkpoint advanced unconditionally, so a run
        that aborted (429 / time budget) or had per-item failures still
        recorded the newest id — permanently skipping the gap, silently.
        """
        d1 = _fake_deviation("FAIL-TOP")
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {"results": [d1], "has_more": False},
            "deviation/metadata": {"metadata": []},
        }
        # Empty CDN body => "fail:EmptyBody" for this item.
        request_recorder["bytes"] = {}

        with patch("time.sleep"), patch.object(dacli, "http_bytes", return_value=b""):
            ns = dacli.build_parser().parse_args(["sync", "feed", "--time-budget", "60"])
            ns.func(ns)

        assert "last_feed_deviationid" not in dacli.load_state()

    def test_feed_checkpoint_advances_on_clean_complete_run(
        self, authed_with_dest, request_recorder, isolated_paths
    ):
        """The happy path must still bookmark, or every run re-walks."""
        d1 = _fake_deviation("GOOD-TOP")
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {"results": [d1], "has_more": False},
            "deviation/metadata": {"metadata": []},
        }
        request_recorder["bytes"] = {"img.png": b"PNG-BODY"}

        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "feed", "--time-budget", "60"])
            ns.func(ns)

        assert dacli.load_state().get("last_feed_deviationid") == "GOOD-TOP"

    def test_feed_dry_run_reports_dry_status_in_summary(
        self, authed_with_dest, request_recorder, capsys
    ):
        d1 = _fake_deviation("DRY-X")
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {"results": [d1], "has_more": False},
            "deviation/metadata": {"metadata": []},
        }
        request_recorder["bytes"] = {"img.png": b"PNG-BODY"}
        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(
                ["sync", "feed", "--dry-run", "--time-budget", "60"]
            )
            ns.func(ns)
        # capsys.readouterr() returns BOTH streams on first call; subsequent
        # calls return empty. Read once, combine, assert.
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # The dry-run warning surfaces early so the operator knows nothing
        # will be persisted. Goes to stderr (warn level) per log()'s contract.
        assert "DRY RUN" in combined

    def test_feed_dry_run_still_respects_known_index(
        self, authed_with_dest, request_recorder, isolated_paths
    ):
        """A deviation already in the index should report as dup, not dry —
        dry-run is for the unknown ones."""
        # Pre-populate the index with one entry that will be in the feed.
        dacli.index_add("KNOWN-1", "alice", "T", authed_with_dest / "alice" / "T", 1)
        # Lay down the matching folder so self-healing doesn't drop it.
        _describe(authed_with_dest / "alice" / "T", "KNOWN-1")
        d_known = _fake_deviation("KNOWN-1")
        d_new = _fake_deviation("DRY-NEW")
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {
                "results": [d_known, d_new],
                "has_more": False,
            },
            "deviation/metadata": {"metadata": []},
        }
        request_recorder["bytes"] = {"img.png": b"P"}
        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(
                ["sync", "feed", "--dry-run", "--time-budget", "60"]
            )
            ns.func(ns)
        # Index count unchanged.
        assert dacli.index_count() == 1
        # The new deviation's folder was NOT created.
        assert not (authed_with_dest / "alice" / "SampleTitle").exists()


class TestPaginationEndToEnd:
    """Multi-page walks through the full argparse -> handler pipeline.

    Routing is by URL substring, so keying responses on `offset=N` gives
    a different page per request without touching the fixture.

    Nothing covered this before: every other sync test feeds one page.
    A broken `offset += limit` would re-fetch page 1 forever and the
    whole suite would stay green.

    Titles are distinct on purpose. `_fake_deviation` defaults every
    title to "T", which would send all of these down the artist+title
    collision path — a different behaviour, tested elsewhere, and one
    that would make these assertions fail for a reason having nothing
    to do with pagination.
    """

    def test_feed_walk_spans_three_pages(self, authed_with_dest, request_recorder):
        request_recorder["responses"] = {
            "deviantsyouwatch?limit=24&offset=0": {
                "results": [
                    _fake_deviation("P0-A", title="P0-A"),
                    _fake_deviation("P0-B", title="P0-B"),
                ],
                "has_more": True,
            },
            "deviantsyouwatch?limit=24&offset=24": {
                "results": [
                    _fake_deviation("P1-A", title="P1-A"),
                    _fake_deviation("P1-B", title="P1-B"),
                ],
                "has_more": True,
            },
            "deviantsyouwatch?limit=24&offset=48": {
                "results": [_fake_deviation("P2-A", title="P2-A")],
                "has_more": False,
            },
            "deviation/metadata": {"metadata": []},
        }
        request_recorder["bytes"] = {"img.png": b"IMG"}

        # Small budget on purpose: if offset ever stops advancing the
        # walk spins on page 0 until the clock runs out, and this keeps
        # that failure to a few seconds instead of a minute.
        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "feed", "--time-budget", "15"])
            ns.func(ns)

        feed_calls = [r for r in request_recorder["requests"] if "deviantsyouwatch" in r["url"]]
        assert len(feed_calls) == 3, f"expected 3 feed pages, made {len(feed_calls)}"
        # Every deviation from every page is indexed, not just page 1's.
        for devid in ("P0-A", "P0-B", "P1-A", "P1-B", "P2-A"):
            assert dacli.index_has(devid), f"{devid} was never synced"

    def test_artist_walk_spans_three_pages(self, authed_with_dest, request_recorder):
        request_recorder["responses"] = {
            "gallery/all?username=alice&limit=24&offset=0": {
                "results": [_fake_deviation("G0-A", title="G0-A")],
                "has_more": True,
            },
            "gallery/all?username=alice&limit=24&offset=24": {
                "results": [_fake_deviation("G1-A", title="G1-A")],
                "has_more": True,
            },
            "gallery/all?username=alice&limit=24&offset=48": {
                "results": [_fake_deviation("G2-A", title="G2-A")],
                "has_more": False,
            },
            "deviation/metadata": {"metadata": []},
        }
        request_recorder["bytes"] = {"img.png": b"IMG"}

        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "artist", "alice", "--time-budget", "15"])
            ns.func(ns)

        gallery_calls = [r for r in request_recorder["requests"] if "gallery/all" in r["url"]]
        assert len(gallery_calls) == 3
        for devid in ("G0-A", "G1-A", "G2-A"):
            assert dacli.index_has(devid), f"{devid} was never synced"


class TestSyncArtistIntegration:
    def test_early_stop_at_first_known(self, authed_with_dest, request_recorder):
        # Pre-seed: 'D-old' is known, should trigger early stop. Lay down
        # the indexed folder so the self-healing existence check passes.
        old_folder = _describe(authed_with_dest / "alice" / "Old", "D-old")
        dacli.index_add("D-old", "alice", "Old", old_folder, 5)
        # The early stop is only sound once this gallery has been walked
        # to its end; record that, which is what a finished walk does.
        st = dacli.load_state()
        st["galleries"] = {"alice": {"complete": True}}
        dacli.save_state(st)

        d_new = _fake_deviation("D-new", title="New")
        d_old = _fake_deviation("D-old", title="Old")
        request_recorder["responses"] = {
            "gallery/all": {"results": [d_new, d_old], "has_more": True},
            "deviation/metadata": {"metadata": [{"deviationid": "D-new", "tags": []}]},
        }
        request_recorder["bytes"] = {"img.png": b"NEW"}

        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "artist", "alice", "--time-budget", "60"])
            ns.func(ns)

        # Only one gallery page fetched (early stop)
        gallery_calls = [r for r in request_recorder["requests"] if "gallery/all" in r["url"]]
        assert len(gallery_calls) == 1
        # New item saved
        assert dacli.index_has("D-new")

    def test_full_flag_disables_early_stop(self, authed_with_dest, request_recorder):
        # Same setup but with --full. Self-healing requires real folder.
        old_folder = _describe(authed_with_dest / "alice" / "Old", "D-old")
        dacli.index_add("D-old", "alice", "Old", old_folder, 5)

        page1 = {
            "results": [_fake_deviation("D-old", title="Old")],
            "has_more": False,
        }
        request_recorder["responses"] = {
            "gallery/all": page1,
            "deviation/metadata": {"metadata": []},
        }

        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(
                ["sync", "artist", "alice", "--full", "--time-budget", "60"]
            )
            ns.func(ns)

        # Even though everything is known, --full means we still process the page
        # and don't early-stop. Stop reason should be "gallery complete" not "caught up".
        # We can't directly inspect reason but we verify the gallery call happened.
        assert any("gallery/all" in r["url"] for r in request_recorder["requests"])

    def test_self_healed_gap_in_middle_of_page_redownloads(
        self, authed_with_dest, request_recorder
    ):
        """Regression: page = [KNOWN, KNOWN, GAP, KNOWN].

        The GAP is a deviation whose row was self-healed away because
        the operator ``rm -rf``'d the folder. Old early-stop logic cut
        the page at the FIRST known and treated GAP as below the
        boundary, never redownloading. New logic must process all
        unknowns in the page regardless of position, then stop paging.
        """
        # Lay down 3 indexed folders (D-13, D-12, D-11 — newest) and
        # 1 indexed-but-stale (D-9 — folder gone after rm -rf).
        for sub, devid in (("alice/T13", "D-13"), ("alice/T12", "D-12"), ("alice/T11", "D-11")):
            _describe(authed_with_dest / sub, devid, image=b"X")
        dacli.index_add("D-13", "alice", "T13", authed_with_dest / "alice" / "T13", 1)
        dacli.index_add("D-12", "alice", "T12", authed_with_dest / "alice" / "T12", 1)
        dacli.index_add("D-11", "alice", "T11", authed_with_dest / "alice" / "T11", 1)
        # D-9 is in the index but its folder was deleted.
        dacli.index_add("D-9", "alice", "T9", authed_with_dest / "alice" / "T9", 1)
        # NOTE: no folder for T9 → index_filter_known will self-heal D-9
        # away when the page comes back.
        assert dacli.index_count() == 4

        # Gallery returns reverse-chrono [D-13, D-12, D-11, D-9, D-8].
        # D-8 is also unknown (never seen). After self-heal, "known" =
        # {D-13, D-12, D-11}; unknowns = {D-9, D-8}. Both must be
        # downloaded.
        page = {
            "results": [
                _fake_deviation("D-13", title="T13"),
                _fake_deviation("D-12", title="T12"),
                _fake_deviation("D-11", title="T11"),
                _fake_deviation("D-9", title="T9"),
                _fake_deviation("D-8", title="T8"),
            ],
            "has_more": False,
        }
        request_recorder["responses"] = {
            "gallery/all": page,
            "deviation/metadata": {
                "metadata": [
                    {"deviationid": "D-9", "tags": []},
                    {"deviationid": "D-8", "tags": []},
                ]
            },
        }
        request_recorder["bytes"] = {"img.png": b"NEW"}

        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "artist", "alice", "--time-budget", "60"])
            ns.func(ns)

        # Both unknown deviations must now be in the index AND have
        # folders on disk. This is the bug-confirming assertion: under
        # the OLD early-stop-at-first-known logic, D-9 and D-8 would
        # have been ignored (cut at i=0, since D-13 is known).
        assert dacli.index_has("D-9"), "D-9 should have been redownloaded after self-heal"
        assert dacli.index_has("D-8"), "D-8 (genuinely new) should have been downloaded"


# ---------------------------------------------------------------------------
# Search (live endpoints)
# ---------------------------------------------------------------------------


class TestSearchEndpoints:
    def test_tag_hits_browse_tags(self, authed_with_dest, request_recorder):
        request_recorder["responses"] = {
            "browse/tags": {"results": [_fake_deviation("X")], "has_more": False}
        }
        ns = dacli.build_parser().parse_args(["search", "tag", "abstract"])
        ns.func(ns)
        urls = [r["url"] for r in request_recorder["requests"]]
        assert any("browse/tags" in u and "tag=abstract" in u for u in urls)

    def test_topic_hits_browse_topic(self, authed_with_dest, request_recorder):
        request_recorder["responses"] = {"browse/topic": {"results": [], "has_more": False}}
        ns = dacli.build_parser().parse_args(["search", "topic", "digitalart"])
        ns.func(ns)
        urls = [r["url"] for r in request_recorder["requests"]]
        assert any("browse/topic" in u and "digitalart" in u for u in urls)

    def test_user_uses_post_with_bracket_form(self, authed_with_dest, request_recorder):
        request_recorder["responses"] = {
            "user/whois": {"results": [{"username": "alice", "userid": "U1", "type": "regular"}]}
        }
        ns = dacli.build_parser().parse_args(["search", "user", "alice", "bob"])
        ns.func(ns)
        whois_calls = [r for r in request_recorder["requests"] if "user/whois" in r["url"]]
        assert len(whois_calls) == 1
        assert whois_calls[0]["method"] == "POST"
        body = (whois_calls[0]["data"] or b"").decode()
        assert "usernames%5B%5D=alice" in body
        assert "usernames%5B%5D=bob" in body

    def test_popular_is_deprecated(self, authed_with_dest, request_recorder, capsys):
        ns = dacli.build_parser().parse_args(["search", "popular"])
        with pytest.raises(SystemExit) as exc:
            ns.func(ns)
        assert exc.value.code == 2
        # No HTTP call should have been made
        urls = [r["url"] for r in request_recorder["requests"]]
        assert not any("browse/popular" in u for u in urls)

    def test_newest_is_deprecated(self, authed_with_dest, request_recorder):
        ns = dacli.build_parser().parse_args(["search", "newest"])
        with pytest.raises(SystemExit) as exc:
            ns.func(ns)
        assert exc.value.code == 2
        urls = [r["url"] for r in request_recorder["requests"]]
        assert not any("browse/newest" in u for u in urls)

    def test_tag_suggest_hits_endpoint(self, authed_with_dest, request_recorder):
        request_recorder["responses"] = {
            "tags/search": {"results": [{"tag_name": "lattice"}, {"tag_name": "latte"}]}
        }
        ns = dacli.build_parser().parse_args(["search", "tag-suggest", "lat"])
        ns.func(ns)
        urls = [r["url"] for r in request_recorder["requests"]]
        assert any("tags" in u and "lat" in u for u in urls)


# ---------------------------------------------------------------------------
# User / watch / deviation / daily / index
# ---------------------------------------------------------------------------


class TestMiscEndpoints:
    def test_user_profile(self, authed_with_dest, request_recorder, capsys):
        request_recorder["responses"] = {
            "user/profile": {
                "user": {"username": "alice", "userid": "U1"},
                "profile_url": "https://da.example/alice",
                "real_name": "Alice",
                "tagline": "an artist",
                "stats": {"user_deviations": 100},
            }
        }
        ns = dacli.build_parser().parse_args(["user", "profile", "alice"])
        ns.func(ns)
        out = capsys.readouterr().out
        assert "alice" in out

    def test_watch_list_browse_scope_exits_with_hint(self, authed_with_dest, capsys):
        with patch.object(
            dacli,
            "http_json",
            side_effect=urllib.error.HTTPError("u", 403, "f", {}, None),
        ):
            ns = dacli.build_parser().parse_args(["watch", "list"])
            with pytest.raises(SystemExit):
                ns.func(ns)
        err = capsys.readouterr().err
        assert "user" in err.lower() and "scope" in err.lower()

    def test_deviation_show(self, authed_with_dest, request_recorder, capsys):
        request_recorder["responses"] = {
            "deviation/metadata": {
                "metadata": [
                    {
                        "deviationid": "DEV-1",
                        "title": "T",
                        "author": {"username": "alice"},
                        "tags": [{"tag_name": "abstract"}],
                        "description": "<p>hi</p>",
                    }
                ]
            }
        }
        ns = dacli.build_parser().parse_args(["deviation", "show", "DEV-1"])
        ns.func(ns)
        assert "DEV-1" in capsys.readouterr().out

    def test_daily_uses_dailydeviations_endpoint(self, authed_with_dest, request_recorder):
        request_recorder["responses"] = {"browse/dailydeviations": {"results": []}}
        ns = dacli.build_parser().parse_args(["daily", "2026-04-01"])
        ns.func(ns)
        urls = [r["url"] for r in request_recorder["requests"]]
        assert any("dailydeviations" in u for u in urls)
        assert any("date=2026-04-01" in u for u in urls)


class TestIndexCommands:
    def test_show_empty(self, isolated_paths, capsys):
        ns = dacli.build_parser().parse_args(["index", "show"])
        ns.func(ns)
        captured = capsys.readouterr()
        assert "no index" in (captured.out + captured.err).lower() or "0" in captured.out

    def test_rebuild_imports_existing_disk_state(self, authed_with_dest, capsys):
        # Lay out one synced deviation on disk
        folder = authed_with_dest / "alice" / "T"
        folder.mkdir(parents=True)
        (folder / "description.json").write_text(
            json.dumps({"deviationid": "D-X", "author_username": "alice", "title": "T"})
        )
        (folder / "image.png").write_bytes(b"X")
        ns = dacli.build_parser().parse_args(["index", "rebuild"])
        ns.func(ns)
        assert dacli.index_has("D-X")


# ---------------------------------------------------------------------------
# Diagnose: structured findings + exit codes
# ---------------------------------------------------------------------------


class TestDiagnoseChecks:
    """`_diagnose_checks()` returns the structured (level, section, msg) list.
    Tests assert directly on that — easier than parsing the rendered output."""

    def test_empty_config_yields_critical_findings(self, isolated_paths, no_keychain):
        findings = dacli._diagnose_checks()
        sections = [section for level, section, _ in findings]
        levels = [level for level, _, _ in findings]
        assert "config" in sections
        assert "auth" in sections
        assert "fail" in levels  # client_id missing should produce a fail

    def test_full_setup_passes(self, isolated_paths, no_keychain, tmp_path):
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
                "last_sync": {
                    "kind": "feed",
                    "started_at": int(time.time() - 60),
                    "ended_at": int(time.time() - 30),
                    "duration_s": 30.0,
                    "totals": {"ok": 5, "dup": 100, "noimg": 0, "fail": 0},
                    "stop_reason": "feed exhausted",
                },
            }
        )
        findings = dacli._diagnose_checks()
        levels = [level for level, _, _ in findings]
        assert "fail" not in levels  # nothing critical

    def _state_with_last_sync(self, tmp_path, **last_sync):
        """Fully-healthy setup, varying only the last_sync record."""
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir(exist_ok=True)
        dacli.set_config_field("destination", str(dest))
        base = {
            "kind": "feed",
            "started_at": int(time.time() - 60),
            "ended_at": int(time.time() - 30),
            "duration_s": 30.0,
            "totals": {"ok": 5, "dup": 100, "noimg": 0, "fail": 0},
            "stop_reason": "feed exhausted",
        }
        base.update(last_sync)
        dacli.save_state(
            {
                "access_token": "AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "RT",
                "scope": "browse",
                "last_sync": base,
            }
        )
        return {section: (level, msg) for level, section, msg in dacli._diagnose_checks()}

    def test_truncated_run_is_not_reported_as_ok(self, isolated_paths, no_keychain, tmp_path):
        """A run the clock cut short must not look healthy.

        Every last_sync used to be reported "ok" regardless of why it
        stopped, so a nightly job truncated every single night showed a
        clean bill of health while the gallery silently fell behind.
        """
        by_section = self._state_with_last_sync(
            tmp_path, stop_reason=dacli.sync.TIME_BUDGET_EXHAUSTED
        )
        level, msg = by_section["last sync"]
        assert level == "warn", f"truncated run reported as {level!r}"
        assert "time budget" in msg

    def test_clean_run_is_still_ok(self, isolated_paths, no_keychain, tmp_path):
        for reason in ("feed exhausted", "caught up", "feed empty", "gallery complete"):
            by_section = self._state_with_last_sync(tmp_path, stop_reason=reason)
            assert by_section["last sync"][0] == "ok", f"{reason!r} should be ok"

    def test_watched_all_complete_is_ok(self, isolated_paths, no_keychain, tmp_path):
        """sync watched's success reason must be recognised too.

        It is not in TERMINAL_STOP_REASONS (those are page-walk reasons),
        so a separate constant carries it; if the two ever drift, a fully
        successful `sync watched` starts reporting warn forever.
        """
        by_section = self._state_with_last_sync(
            tmp_path,
            kind="watched",
            stop_reason=dacli.constants.WATCHED_ALL_COMPLETE,
            totals={"artists_total": 3, "artists_done": 3, "artists_failed": 0},
        )
        assert by_section["last sync"][0] == "ok"

    def test_http_error_stop_is_not_ok(self, isolated_paths, no_keychain, tmp_path):
        by_section = self._state_with_last_sync(tmp_path, stop_reason="HTTP 429 at offset 120")
        assert by_section["last sync"][0] == "warn"

    def test_per_item_failures_are_not_ok(self, isolated_paths, no_keychain, tmp_path):
        """A clean stop_reason with failed items is still not a clean run."""
        by_section = self._state_with_last_sync(
            tmp_path,
            stop_reason="feed exhausted",
            totals={"ok": 5, "dup": 0, "noimg": 0, "fail": 3},
        )
        assert by_section["last sync"][0] == "warn"

    def test_destination_unwritable_is_critical(self, isolated_paths, no_keychain):
        dacli.set_config_field("client_id", "12345")
        dacli.set_config_field("destination", "/dev/null/does-not-exist")
        findings = dacli._diagnose_checks()
        msgs = [(level, msg) for level, _, msg in findings]
        assert any(level == "fail" and "destination" in msg for level, msg in msgs)

    def test_expired_token_with_live_refresh_yields_ok(
        self, isolated_paths, no_keychain, tmp_path, monkeypatch
    ):
        """When access_token has expired but refresh_token works, diagnose
        must actually exercise the refresh and surface "ok" — not the
        old behavior of warn-without-checking. This is the bug class
        behind silent multi-day cron failures: diagnose said
        "refresh_token available" without ever testing it against DA.
        """
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.save_state(
            {
                "access_token": "expired-AT",
                "expires_at": time.time() - 60,  # already expired
                "refresh_token": "live-RT",
                "scope": "browse",
            }
        )
        # Mock the token endpoint to return a fresh access_token
        monkeypatch.setattr(
            dacli,
            "http_post_json",
            lambda url, payload, **_kw: {
                "access_token": "FRESH-AT",
                "expires_in": 3600,
                "refresh_token": "live-RT",
            },
        )
        findings = dacli._diagnose_checks()
        msgs = [(level, msg) for level, _, msg in findings]
        # auth section should be "ok" with explicit "live" wording
        assert any(
            level == "ok" and "live" in msg.lower()
            for level, msg in msgs
            if "scope" in msg or "refresh" in msg.lower() or "live" in msg.lower()
        ), f"expected ok+live, got {msgs}"

    def test_expired_token_with_dead_refresh_yields_fail(
        self, isolated_paths, no_keychain, tmp_path, monkeypatch
    ):
        """When access_token has expired AND DA rejects the refresh_token
        (HTTP 400 invalid_grant), diagnose must surface fail — not warn
        — so the operator immediately knows to run `da auth`."""
        import urllib.error
        from io import BytesIO

        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.save_state(
            {
                "access_token": "expired-AT",
                "expires_at": time.time() - 60,
                "refresh_token": "dead-RT",
                "scope": "browse",
            }
        )

        def reject(url, payload, **_kw):
            body = b'{"error":"invalid_request","error_description":"refresh_token is invalid"}'
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, BytesIO(body))

        monkeypatch.setattr(dacli, "http_post_json", reject)
        findings = dacli._diagnose_checks()
        msgs = [(level, msg) for level, _, msg in findings]
        assert any(
            level == "fail" and "rejected" in msg.lower() and "auth" in msg.lower()
            for level, msg in msgs
        ), f"expected fail+rejected+auth, got {msgs}"

    def test_http_redirect_uri_yields_warning(self, isolated_paths, no_keychain, tmp_path):
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.set_config_field("redirect_uri", "http://localhost:8765/")
        findings = dacli._diagnose_checks()
        msgs = [(level, msg) for level, _, msg in findings]
        assert any(level == "warn" and "HTTP" in msg for level, msg in msgs)

    def test_refresh_token_ttl_warns_within_14_days(self, isolated_paths, no_keychain, tmp_path):
        """The 90-day refresh_token TTL is DA-imposed; we can't extend it.
        diagnose must surface a WARN finding when the chain has <14 days
        left so the operator isn't surprised by a dead token at 03:00."""
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        # Set issued_at to 80 days ago → 10 days remaining → inside the 14-day warn band.
        ten_days_ago_offset = 80 * 86400  # 80 days elapsed → 10 days of 90 remaining
        dacli.save_state(
            {
                "access_token": "live-AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "live-RT",
                "scope": "browse",
                "refresh_token_issued_at": time.time() - ten_days_ago_offset,
            }
        )
        findings = dacli._diagnose_checks()
        ttl_findings = [(lvl, msg) for lvl, _, msg in findings if "refresh_token chain" in msg]
        assert ttl_findings, f"no TTL finding; got {findings}"
        level, msg = ttl_findings[0]
        assert level == "warn"
        assert "10.0 days" in msg or "9." in msg  # ± a few seconds of test exec

    def test_refresh_token_ttl_fails_within_3_days(self, isolated_paths, no_keychain, tmp_path):
        """Below the 3-day critical floor, diagnose must surface FAIL so
        monitoring cron / launchd alerts pick it up — silent failure at
        03:00 is the canonical silent-cron-failure mode this whole TTL
        surfacing exists to prevent."""
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        # 88 days elapsed → 2 days remaining → inside the 3-day crit band.
        two_days_remaining_offset = 88 * 86400
        dacli.save_state(
            {
                "access_token": "live-AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "live-RT",
                "scope": "browse",
                "refresh_token_issued_at": time.time() - two_days_remaining_offset,
            }
        )
        findings = dacli._diagnose_checks()
        ttl_findings = [(lvl, msg) for lvl, _, msg in findings if "refresh_token chain" in msg]
        assert ttl_findings
        level, msg = ttl_findings[0]
        assert level == "fail"
        assert "2." in msg  # ~2.0 days remaining

    def test_refresh_token_ttl_ok_when_fresh(self, isolated_paths, no_keychain, tmp_path):
        """A freshly-issued chain (1 day ago) must surface OK so the
        operator doesn't get noise on every diagnose run."""
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.save_state(
            {
                "access_token": "live-AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "live-RT",
                "scope": "browse",
                "refresh_token_issued_at": time.time() - 86400,  # 1 day ago
            }
        )
        findings = dacli._diagnose_checks()
        ttl_findings = [(lvl, msg) for lvl, _, msg in findings if "refresh_token chain" in msg]
        assert ttl_findings
        level, msg = ttl_findings[0]
        assert level == "ok"
        assert "89 days remaining" in msg  # 90 - 1 = 89

    def test_refresh_token_ttl_silent_when_no_timestamp(
        self, isolated_paths, no_keychain, tmp_path
    ):
        """Older state.json files (pre-TTL feature) won't have the
        `refresh_token_issued_at` field. Diagnose must not crash and
        must not surface a misleading 0-day-remaining finding — just
        skip the TTL check."""
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.save_state(
            {
                "access_token": "live-AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "live-RT",
                "scope": "browse",
                # Note: no refresh_token_issued_at
            }
        )
        findings = dacli._diagnose_checks()
        ttl_findings = [msg for _, _, msg in findings if "refresh_token chain" in msg]
        assert not ttl_findings, f"TTL finding surfaced without timestamp; got {ttl_findings}"

    def test_low_disk_space_yields_warning(
        self, isolated_paths, no_keychain, tmp_path, monkeypatch
    ):
        """< 5 GiB free on dest → warn; < 1 GiB → fail. Sync that fills the
        disk leaves operators with mysterious .part residue, so we surface
        the situation before they kick off a long sync."""
        import shutil as _shutil

        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))

        # Mock 4 GiB free → warn level, not fail
        Usage = type(
            "Usage",
            (),
            {"total": 100 * 1024**3, "used": 96 * 1024**3, "free": 4 * 1024**3},
        )
        monkeypatch.setattr(_shutil, "disk_usage", lambda p: Usage())
        findings = dacli._diagnose_checks()
        msgs = [(level, msg) for level, _, msg in findings]
        assert any(level == "warn" and "low" in msg.lower() for level, msg in msgs)

    def test_critically_low_disk_space_yields_fail(
        self, isolated_paths, no_keychain, tmp_path, monkeypatch
    ):
        import shutil as _shutil

        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))

        # Mock 0.5 GiB free → fail level
        Usage = type(
            "Usage",
            (),
            {"total": 100 * 1024**3, "used": 99 * 1024**3, "free": 512 * 1024**2},
        )
        monkeypatch.setattr(_shutil, "disk_usage", lambda p: Usage())
        findings = dacli._diagnose_checks()
        msgs = [(level, msg) for level, _, msg in findings]
        assert any(level == "fail" and "free" in msg.lower() for level, msg in msgs)


class TestDiagnoseCommand:
    def test_exits_2_on_critical(self, isolated_paths, no_keychain):
        # No client_id → fail → exit 2
        ns = dacli.build_parser().parse_args(["diagnose"])
        with pytest.raises(SystemExit) as exc:
            ns.func(ns)
        assert exc.value.code == 2

    def test_exits_0_on_clean(self, isolated_paths, no_keychain, tmp_path):
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.set_config_field("redirect_uri", "https://localhost:8765/")
        dacli.save_state(
            {
                "access_token": "AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "RT",
                "scope": "browse",
                "last_sync": {
                    "kind": "feed",
                    "started_at": int(time.time() - 60),
                    "ended_at": int(time.time() - 30),
                    "duration_s": 30.0,
                    "totals": {"ok": 0, "dup": 100, "noimg": 0, "fail": 0},
                    "stop_reason": "feed exhausted",
                },
            }
        )
        # Index has rows
        dacli.index_add("D-1", "alice", "T", tmp_path, 1)
        # Pre-create the loopback cert files so the TLS check passes
        dacli.LOOPBACK_CERT.parent.mkdir(parents=True, exist_ok=True)
        dacli.LOOPBACK_CERT.write_text("CERT")
        dacli.LOOPBACK_KEY.write_text("KEY")
        # Suppress the launchctl call (Linux/CI doesn't have it; macOS would
        # otherwise add a "schedule" warn finding)
        with patch("sys.platform", "linux"):
            ns = dacli.build_parser().parse_args(["diagnose"])
            with pytest.raises(SystemExit) as exc:
                ns.func(ns)
            assert exc.value.code == 0

    def test_prints_structured_output(self, isolated_paths, no_keychain, capsys):
        ns = dacli.build_parser().parse_args(["diagnose"])
        with pytest.raises(SystemExit):
            ns.func(ns)
        out = capsys.readouterr().out
        assert "da-cli diagnose" in out
        assert "OVERALL:" in out
        # Marker symbols for the three levels
        assert "✗" in out  # we expect at least one fail (no client_id)


# ---------------------------------------------------------------------------
# Sync summary persisted to state
# ---------------------------------------------------------------------------


class TestSyncSummary:
    def test_feed_records_summary(self, authed_with_dest, request_recorder):
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {"results": [], "has_more": False},
        }
        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "feed", "--time-budget", "60"])
            ns.func(ns)
        last = dacli.load_state().get("last_sync")
        assert last is not None
        assert last["kind"] == "feed"
        assert last["stop_reason"] == "feed empty"
        assert "duration_s" in last
        assert "totals" in last

    def test_artist_records_summary(self, authed_with_dest, request_recorder):
        request_recorder["responses"] = {
            "gallery/all": {"results": [], "has_more": False},
        }
        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "artist", "alice", "--time-budget", "60"])
            ns.func(ns)
        last = dacli.load_state().get("last_sync")
        assert last is not None
        assert last["kind"] == "artist"
        assert last.get("artist") == "alice"

    def test_summary_overwrites_previous(self, authed_with_dest, request_recorder):
        # First run
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {"results": [], "has_more": False},
        }
        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "feed", "--time-budget", "60"])
            ns.func(ns)
        first = dacli.load_state()["last_sync"]
        time.sleep(0.01)  # force ts difference
        # Second run
        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "artist", "alice", "--time-budget", "60"])
            request_recorder["responses"]["gallery/all"] = {"results": [], "has_more": False}
            ns.func(ns)
        second = dacli.load_state()["last_sync"]
        assert second["kind"] == "artist"
        assert second != first


# ---------------------------------------------------------------------------
# Concurrent image downloads: bounded thread pool, deterministic results,
# index writes serialize correctly under contention.
# ---------------------------------------------------------------------------


class TestConcurrentDownloads:
    """`_save_page_concurrent` dispatches per-deviation save tasks across a
    thread pool. Verify: results returned in input order; respects
    concurrency limit; index ends up with every row even when many
    threads upsert in parallel; concurrency=1 still works (sequential
    fallback)."""

    def _build_page(self, n: int) -> tuple[list[dict], dict]:
        """Build n fake deviations + matching metadata blob."""
        results = [_fake_deviation(f"D-{i}", title=f"T{i}") for i in range(n)]
        md = {f"D-{i}": {"deviationid": f"D-{i}", "tags": []} for i in range(n)}
        return results, md

    def test_results_returned_in_input_order(self, authed_with_dest, tmp_path, monkeypatch):
        results, md = self._build_page(8)
        monkeypatch.setattr(dacli, "http_bytes", lambda url, **_kw: b"PNG")
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        out = dacli._save_page_concurrent(
            results,
            md,
            authed_with_dest,
            fallback_artist=None,
            image_delay=0.0,
            jitter_pct=0.0,
            concurrency=4,
        )
        assert len(out) == 8
        # status, artist, title, size — title from input ordering
        for i, (status, _artist, title, _size) in enumerate(out):
            assert status == "ok"
            assert title == f"T{i}"

    def test_index_complete_under_concurrency(self, authed_with_dest, tmp_path, monkeypatch):
        results, md = self._build_page(16)
        monkeypatch.setattr(dacli, "http_bytes", lambda url, **_kw: b"PNG")
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        dacli._save_page_concurrent(
            results,
            md,
            authed_with_dest,
            fallback_artist=None,
            image_delay=0.0,
            jitter_pct=0.0,
            concurrency=8,
        )
        # All 16 rows should be in the index — verifies the lock
        # serialized writes correctly without dropping any.
        for i in range(16):
            assert dacli.index_has(f"D-{i}"), f"missing D-{i} after concurrent save"

    def test_sequential_fallback_when_concurrency_one(
        self, authed_with_dest, tmp_path, monkeypatch
    ):
        results, md = self._build_page(3)
        call_threads: list[str] = []
        import threading as _th

        def fake_bytes(url, **_kw):
            call_threads.append(_th.current_thread().name)
            return b"PNG"

        monkeypatch.setattr(dacli, "http_bytes", fake_bytes)
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        dacli._save_page_concurrent(
            results,
            md,
            authed_with_dest,
            fallback_artist=None,
            image_delay=0.0,
            jitter_pct=0.0,
            concurrency=1,
        )
        # All calls on the main thread (sequential fallback path)
        assert all(t == "MainThread" for t in call_threads)

    def test_concurrency_actually_parallelizes(self, authed_with_dest, tmp_path, monkeypatch):
        """With concurrency=4, image downloads should run on worker
        threads, not the main thread."""
        results, md = self._build_page(8)
        seen_threads: set[str] = set()
        seen_lock = threading_module.Lock()

        def fake_bytes(url, **_kw):
            with seen_lock:
                seen_threads.add(threading_module.current_thread().name)
            return b"PNG"

        monkeypatch.setattr(dacli, "http_bytes", fake_bytes)
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        dacli._save_page_concurrent(
            results,
            md,
            authed_with_dest,
            fallback_artist=None,
            image_delay=0.0,
            jitter_pct=0.0,
            concurrency=4,
        )
        # At least 2 distinct threads used (likely all 4)
        assert len(seen_threads) >= 2

    def test_concurrency_clamped(self, isolated_paths):
        # Assert against the constants, not against 16 and 1. A literal
        # here pins the value the constant happens to have today rather
        # than the contract, so moving the bound leaves the test green
        # while some other code path keeps the old one.
        cfg = {}
        ns = argparse.Namespace(concurrency=999)
        assert dacli._concurrency(cfg, ns) == dacli.constants.CONCURRENCY_MAX
        ns = argparse.Namespace(concurrency=0)
        assert dacli._concurrency(cfg, ns) == dacli.constants.CONCURRENCY_MIN
        ns = argparse.Namespace(concurrency=None)
        assert dacli._concurrency(cfg, ns) == dacli.DEFAULT_CONCURRENCY


# ---------------------------------------------------------------------------
# Auth lifecycle: cmd_auth_logout + token-refresh path under access_token()
# ---------------------------------------------------------------------------


class TestAuthLifecycle:
    def test_logout_removes_state_file(self, isolated_paths, no_keychain):
        dacli.save_state({"access_token": "T"})
        assert isolated_paths["st"].exists()
        ns = dacli.build_parser().parse_args(["auth", "logout"])
        ns.func(ns)
        assert not isolated_paths["st"].exists()

    def test_refresh_renews_access_token(self, isolated_paths, no_keychain):
        dacli.set_config_field("client_id", "12345")
        dacli.save_state(
            {
                "access_token": "old",
                "expires_at": time.time() + 3600,
                "refresh_token": "rt",
                "scope": "browse",
            }
        )
        with patch.object(
            dacli,
            "http_post_json",
            return_value={"access_token": "new", "expires_in": 3600},
        ):
            ns = dacli.build_parser().parse_args(["refresh"])
            ns.func(ns)
        assert dacli.load_state()["access_token"] == "new"

    def test_access_token_auto_refreshes_when_expired(self, isolated_paths, no_keychain):
        dacli.set_config_field("client_id", "12345")
        dacli.save_state(
            {
                "access_token": "expired",
                "expires_at": time.time() - 60,  # already expired
                "refresh_token": "rt",
                "scope": "browse",
            }
        )
        with patch.object(
            dacli,
            "http_post_json",
            return_value={"access_token": "fresh", "expires_in": 3600},
        ):
            cfg = dacli.load_config()
            state = dacli.load_state()
            tok = dacli.access_token(cfg, state)
        assert tok == "fresh"


# ---------------------------------------------------------------------------
# Whoami: handles 403 without crashing
# ---------------------------------------------------------------------------


class TestWhoami:
    def test_403_logs_warning_does_not_crash(self, isolated_paths, no_keychain, capsys):
        dacli.save_state(
            {
                "access_token": "T",
                "expires_at": time.time() + 3600,
                "refresh_token": "rt",
                "scope": "browse",
            }
        )

        def fake_json(url, **_kw):
            if "/placebo" in url:
                return {"status": "success"}
            if "/user/whoami" in url:
                raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)
            return {}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            ns = dacli.build_parser().parse_args(["whoami"])
            ns.func(ns)
        captured = capsys.readouterr()
        assert "user" in (captured.out + captured.err).lower()


# ---------------------------------------------------------------------------
# Cross-command interaction: full sync round trip mutates index AND state
# atomically.
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_sync_then_index_show_reflects_new_rows(
        self, authed_with_dest, request_recorder, capsys
    ):
        request_recorder["responses"] = {
            "browse/deviantsyouwatch": {
                "results": [_fake_deviation("D-1"), _fake_deviation("D-2")],
                "has_more": False,
            },
            "deviation/metadata": {
                "metadata": [
                    {"deviationid": "D-1", "tags": []},
                    {"deviationid": "D-2", "tags": []},
                ]
            },
        }
        request_recorder["bytes"] = {"img.png": b"X"}

        with patch("time.sleep"):
            ns = dacli.build_parser().parse_args(["sync", "feed", "--time-budget", "60"])
            ns.func(ns)

        capsys.readouterr()  # drain
        ns = dacli.build_parser().parse_args(["index", "show"])
        ns.func(ns)
        out = capsys.readouterr().out
        assert "total rows:  2" in out or "2" in out


# ---------------------------------------------------------------------------
# Diagnose --json: machine-readable output for observability
# ---------------------------------------------------------------------------


class TestDiagnoseJsonMode:
    def test_emits_valid_json(self, isolated_paths, no_keychain, capsys):
        ns = dacli.build_parser().parse_args(["diagnose", "--json"])
        with pytest.raises(SystemExit) as exc:
            ns.func(ns)
        assert exc.value.code == 2  # client_id missing
        out = capsys.readouterr().out
        # Must be a single JSON object — pipeable to jq
        payload = json.loads(out)
        assert payload["overall"]["status"] == "FAIL"
        assert payload["exit_code"] == 2
        assert isinstance(payload["findings"], list)
        assert any(f["section"] == "config" for f in payload["findings"])
        # Schema fields present
        for finding in payload["findings"]:
            assert set(finding) == {"level", "section", "message"}
            assert finding["level"] in ("ok", "warn", "fail")

    def test_json_no_human_decoration(self, isolated_paths, no_keychain, capsys):
        ns = dacli.build_parser().parse_args(["diagnose", "--json"])
        with pytest.raises(SystemExit):
            ns.func(ns)
        out = capsys.readouterr().out
        # In JSON mode, no human-only markers should appear in stdout
        assert "OVERALL:" not in out
        assert "✓" not in out
        assert "✗" not in out
        assert "===" not in out
        # Should parse cleanly
        json.loads(out)

    def test_json_overall_status_when_clean(self, isolated_paths, no_keychain, tmp_path, capsys):
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir()
        dacli.set_config_field("destination", str(dest))
        dacli.set_config_field("redirect_uri", "https://localhost:8765/")
        dacli.save_state(
            {
                "access_token": "AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "RT",
                "scope": "browse",
                "last_sync": {
                    "kind": "feed",
                    "started_at": int(time.time() - 60),
                    "ended_at": int(time.time() - 30),
                    "duration_s": 30.0,
                    "totals": {"ok": 0, "dup": 100, "noimg": 0, "fail": 0},
                    "stop_reason": "feed exhausted",
                },
            }
        )
        dacli.index_add("D-1", "alice", "T", tmp_path, 1)
        # Pre-create cert files so the TLS check passes
        dacli.LOOPBACK_CERT.parent.mkdir(parents=True, exist_ok=True)
        dacli.LOOPBACK_CERT.write_text("CERT")
        dacli.LOOPBACK_KEY.write_text("KEY")
        # Drain stdout from set_config_field calls so capsys returns
        # only the JSON payload from diagnose itself.
        capsys.readouterr()
        with patch("sys.platform", "linux"):
            ns = dacli.build_parser().parse_args(["diagnose", "--json"])
            with pytest.raises(SystemExit) as exc:
                ns.func(ns)
            assert exc.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["overall"]["status"] == "OK"
        assert payload["overall"]["criticals"] == 0
        assert payload["overall"]["warnings"] == 0


# ---------------------------------------------------------------------------
# Bench: synthetic sync benchmark
# ---------------------------------------------------------------------------


class TestBench:
    def test_bench_runs_to_completion(self, isolated_paths, no_keychain, capsys):
        ns = dacli.build_parser().parse_args(
            ["bench", "--pages", "2", "--per-page", "4", "--concurrency", "2"]
        )
        ns.func(ns)
        out = capsys.readouterr().out
        # Human-readable summary fields
        assert "pages          : 2" in out
        assert "per page       : 4" in out
        assert "total items    : 8" in out

    def test_bench_json_emits_metrics(self, isolated_paths, no_keychain, capsys):
        ns = dacli.build_parser().parse_args(
            ["bench", "--pages", "2", "--per-page", "4", "--concurrency", "2", "--json"]
        )
        ns.func(ns)
        out = capsys.readouterr().out
        payload = json.loads(out)
        # Schema
        assert payload["pages"] == 2
        assert payload["per_page"] == 4
        assert payload["concurrency"] == 2
        assert payload["total_items"] == 8
        assert payload["indexed"] == 8  # all items got indexed
        assert payload["elapsed_s"] >= 0
        assert payload["items_per_sec"] > 0
        assert payload["pages_per_sec"] > 0

    def test_bench_does_not_pollute_real_index(self, isolated_paths, no_keychain, capsys):
        # Pre-seed real index with a row
        dacli.index_add("EXISTING", "alice", "T", isolated_paths["idx"].parent, 1)
        before = dacli.index_count()
        ns = dacli.build_parser().parse_args(["bench", "--pages", "1", "--per-page", "2", "--json"])
        ns.func(ns)
        # Bench uses its own tmp index dir; the real isolated index
        # should be unchanged
        after = dacli.index_count()
        assert after == before, "bench leaked rows into the real index"

    def test_bench_concurrency_clamped(self, isolated_paths, no_keychain, capsys):
        # concurrency above 16 should clamp
        ns = dacli.build_parser().parse_args(
            ["bench", "--pages", "1", "--per-page", "2", "--concurrency", "999", "--json"]
        )
        ns.func(ns)
        payload = json.loads(capsys.readouterr().out)
        assert payload["concurrency"] == dacli.constants.CONCURRENCY_MAX

    def test_bench_clamps_exactly_like_sync(self, isolated_paths, no_keychain, capsys):
        """Bench exists to measure the real sync, so its bounds must be the
        real sync's bounds.

        Both used to be written as literals in two places. Raising
        CONCURRENCY_MAX moved sync and left bench behind, and the old
        assertion (`== 16`) stayed green through it — the test cemented the
        drift instead of catching it.
        """
        for requested in (0, 1, 4, 999):
            capsys.readouterr()
            ns = dacli.build_parser().parse_args(
                [
                    "bench",
                    "--pages",
                    "1",
                    "--per-page",
                    "1",
                    "--concurrency",
                    str(requested),
                    "--json",
                ]
            )
            ns.func(ns)
            bench_used = json.loads(capsys.readouterr().out)["concurrency"]
            sync_would_use = dacli._concurrency({}, argparse.Namespace(concurrency=requested))
            assert bench_used == sync_would_use, (
                f"--concurrency {requested}: bench used {bench_used}, "
                f"sync would use {sync_would_use}"
            )

    def test_bench_is_reentrant_in_same_process(self, isolated_paths, no_keychain, capsys):
        """Bench monkeypatches log/load_config/load_state/save_state/urlopen
        and the index path. The teardown restores all of them in a matched
        try/finally. Calling bench twice in the same process — or running
        another command after bench — must NOT inherit any leaked patches.
        Pin the contract."""
        # First bench
        ns1 = dacli.build_parser().parse_args(
            ["bench", "--pages", "1", "--per-page", "2", "--concurrency", "1", "--json"]
        )
        ns1.func(ns1)
        first_payload = json.loads(capsys.readouterr().out)
        assert first_payload["total_items"] == 2

        # Second bench — fresh invocation. If anything leaked (urlopen still
        # patched, INDEX_PATH still pointing at the bench's tmp), this would
        # silently use stale state instead of running fresh.
        ns2 = dacli.build_parser().parse_args(
            ["bench", "--pages", "1", "--per-page", "3", "--concurrency", "1", "--json"]
        )
        ns2.func(ns2)
        second_payload = json.loads(capsys.readouterr().out)
        assert second_payload["total_items"] == 3
        # If bench's tmp index leaked into the real one, this assertion would
        # break (the second bench's "indexed" would carry rows from the first).
        assert second_payload["indexed"] == 3

        # Smoke test: a non-bench command immediately after must work,
        # proving urlopen / load_state / log were all restored.
        ns3 = dacli.build_parser().parse_args(["index", "show"])
        ns3.func(ns3)  # Should not raise — bench teardown restored module state.

    def test_bench_clamps_non_positive_inputs_to_minimums(
        self, isolated_paths, no_keychain, capsys
    ):
        """Negative or zero ``--pages`` / ``--per-page`` / ``--concurrency``
        must be clamped to the documented minimums rather than crashing
        (empty range / zero workers / division by zero).

        ``cmd_bench`` floors pages and per_page at 1, and clamps
        concurrency to [1, 16]. Without that, ``--per-page 0`` would
        generate an empty synthetic feed and ``--concurrency 0`` would
        deadlock the ThreadPoolExecutor.
        """
        ns = dacli.build_parser().parse_args(
            [
                "bench",
                "--pages",
                "0",
                "--per-page",
                "0",
                "--concurrency",
                "-5",
                "--json",
            ]
        )
        ns.func(ns)
        payload = json.loads(capsys.readouterr().out)
        assert payload["pages"] == 1
        assert payload["per_page"] == 1
        assert payload["concurrency"] == 1
        assert payload["total_items"] == 1
        assert payload["indexed"] == 1

    def test_bench_quiet_suppresses_human_log_output(self, isolated_paths, no_keychain, capsys):
        """``--json`` mode must suppress the human-readable ``log()`` banner
        so stdout contains exactly one valid JSON object.

        ``cmd_bench`` patches ``log`` to a no-op when ``--json`` is set;
        if that patch leaked past teardown it would silence real log
        calls in subsequent commands. This test pins the in-bench
        behaviour; re-entrancy of ``log`` itself is covered by the
        ``test_bench_is_reentrant_in_same_process`` test above.
        """
        ns = dacli.build_parser().parse_args(
            ["bench", "--pages", "1", "--per-page", "1", "--concurrency", "1", "--json"]
        )
        ns.func(ns)
        out = capsys.readouterr().out
        payload = json.loads(out)  # raises if any log lines polluted stdout
        assert payload["total_items"] == 1


# ---------------------------------------------------------------------------
# Multi-process command lock: prevent overlapping sync runs (manual + cron)
# ---------------------------------------------------------------------------


class TestCommandLock:
    """`_cmd_lock(name)` is an exclusive cross-process advisory lock used
    by the sync commands to prevent overlapping launchd + manual runs
    from racing on state.json. Same-process recursive acquire fails
    (as expected — that's why cmd_sync_watched calls the unlocked
    `_cmd_sync_artist_impl` directly)."""

    def test_lock_acquire_release(self, isolated_paths):
        with dacli._cmd_lock("integration-test"):
            pass  # acquired and released cleanly
        # Acquiring again after release should work
        with dacli._cmd_lock("integration-test"):
            pass

    def test_second_acquire_raises_command_locked(self, isolated_paths):
        """Same-process double-acquire — this models the cross-process
        race because POSIX flock on macOS treats different fd opens as
        different lockers, even within one process."""
        first = dacli._cmd_lock("integration-test")
        first.__enter__()
        try:
            with pytest.raises(dacli.CommandLockedError):
                second = dacli._cmd_lock("integration-test")
                second.__enter__()
        finally:
            first.__exit__(None, None, None)

    def test_distinct_names_dont_conflict(self, isolated_paths):
        """`sync` and `bench` should be independently lockable."""
        with dacli._cmd_lock("name-A"):
            with dacli._cmd_lock("name-B"):
                pass

    def test_sync_feed_skips_when_locked(
        self, authed_with_dest, request_recorder, capsys, monkeypatch
    ):
        """If another instance holds the sync lock, cmd_sync_feed exits
        cleanly with status 0 (overlapping is normal in cron+manual,
        not a failure)."""
        # Acquire the lock as if another process held it
        held = dacli._cmd_lock("sync")
        held.__enter__()
        try:
            ns = dacli.build_parser().parse_args(["sync", "feed"])
            with pytest.raises(SystemExit) as exc:
                ns.func(ns)
            assert exc.value.code == 0
            out = capsys.readouterr().out
            assert "skipping" in out.lower() or "already running" in out.lower()
        finally:
            held.__exit__(None, None, None)

    def test_sync_artist_skips_when_locked(self, authed_with_dest, request_recorder, capsys):
        held = dacli._cmd_lock("sync")
        held.__enter__()
        try:
            ns = dacli.build_parser().parse_args(["sync", "artist", "alice"])
            with pytest.raises(SystemExit) as exc:
                ns.func(ns)
            assert exc.value.code == 0
        finally:
            held.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# main() — keyboard interrupt path
# ---------------------------------------------------------------------------


class TestAuthStatus:
    """``da auth status`` emits JSON describing the refresh_token chain's
    remaining lifetime. Exit code: 0 (ok), 1 (warn), 2 (crit/unknown).
    Suitable for cron / launchd health checks.

    It confirms with DeviantArt before reporting, so these mock the
    validation call and vary only the age of the token — the TTL
    thresholds are what they are testing.
    """

    @staticmethod
    def _valid_token(*_a, **_kw):
        return {"status": "success"}

    def test_status_unknown_when_no_timestamp(self, isolated_paths, no_keychain, capsys):
        dacli.save_state({"refresh_token": "RT", "scope": "browse"})
        ns = dacli.build_parser().parse_args(["auth", "status"])
        with pytest.raises(SystemExit) as exc:
            ns.func(ns)
        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "unknown"
        assert payload["days_remaining"] is None

    def test_status_ok_when_fresh(self, isolated_paths, no_keychain, capsys):
        dacli.save_state(
            {
                "access_token": "AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "RT",
                "refresh_token_issued_at": time.time() - 86400,  # 1 day ago
                "scope": "browse",
            }
        )
        ns = dacli.build_parser().parse_args(["auth", "status"])
        with patch.object(dacli, "load_config", return_value={"client_id": "X"}):
            with patch.object(dacli, "http_json", side_effect=self._valid_token):
                with pytest.raises(SystemExit) as exc:
                    ns.func(ns)
        assert exc.value.code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "ok"
        assert payload["days_remaining"] > 80

    def test_status_warn_at_10_days(self, isolated_paths, no_keychain, capsys):
        dacli.save_state(
            {
                "access_token": "AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "RT",
                "refresh_token_issued_at": time.time() - 80 * 86400,  # 80d → 10d left
                "scope": "browse",
            }
        )
        ns = dacli.build_parser().parse_args(["auth", "status"])
        with patch.object(dacli, "load_config", return_value={"client_id": "X"}):
            with patch.object(dacli, "http_json", side_effect=self._valid_token):
                with pytest.raises(SystemExit) as exc:
                    ns.func(ns)
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "warn"

    def test_status_crit_at_2_days(self, isolated_paths, no_keychain, capsys):
        dacli.save_state(
            {
                "access_token": "AT",
                "expires_at": time.time() + 3600,
                "refresh_token": "RT",
                "refresh_token_issued_at": time.time() - 88 * 86400,  # 88d → 2d left
                "scope": "browse",
            }
        )
        ns = dacli.build_parser().parse_args(["auth", "status"])
        with patch.object(dacli, "load_config", return_value={"client_id": "X"}):
            with patch.object(dacli, "http_json", side_effect=self._valid_token):
                with pytest.raises(SystemExit) as exc:
                    ns.func(ns)
        assert exc.value.code == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "crit"


class TestMain:
    def test_keyboard_interrupt_exits_130(self, isolated_paths):
        def boom(_args: argparse.Namespace) -> None:
            raise KeyboardInterrupt

        def fake_parser():
            p = argparse.ArgumentParser()
            sub = p.add_subparsers(dest="cmd", required=True)
            x = sub.add_parser("x")
            x.set_defaults(func=boom)
            return p

        with patch.object(dacli, "build_parser", fake_parser):
            with pytest.raises(SystemExit) as exc:
                dacli.main(["x"])
        assert exc.value.code == 130
