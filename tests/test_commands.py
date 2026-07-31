"""Tests for the command-body functions: sync feed/artist/watched, search,
user, watch list, deviation show, daily."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
from unittest.mock import patch

import pytest

import dacli

from .conftest import describe_folder as _describe


# ---------------------------------------------------------------------------
# Helpers shared across the tests below
# ---------------------------------------------------------------------------
def _build_sync_args(cmd: str, **overrides: object) -> argparse.Namespace:
    """Default Namespace for sync_* commands. Override per-test."""
    base: dict[str, object] = {
        "limit": 24,
        "mature": True,
        "time_budget": 540,
        "delay_api": 0.0,
        "delay_image": 0.0,
        "jitter": 0.0,
        "offset": 0,
    }
    if cmd == "watched":
        base.update({"user": None, "via_feed": False, "feed_max": 200})
    if cmd == "artist":
        base["artist"] = "alice"
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# cmd_sync_feed
# ---------------------------------------------------------------------------
class TestCmdSyncFeed:
    def test_basic_feed_walk(self, authed_with_destination, sample_deviation):
        """Two deviations on first page, has_more=False — should download both."""
        d1 = dict(sample_deviation, deviationid="DEV-A")
        d2 = dict(sample_deviation, deviationid="DEV-B", title="Other Title")

        feed_response = {"results": [d1, d2], "has_more": False}
        md_response = {
            "metadata": [
                {"deviationid": "DEV-A"},
                {"deviationid": "DEV-B"},
            ]
        }

        # http_json gets called twice: feed page, then metadata
        responses = iter([feed_response, md_response])
        with patch.object(dacli, "http_json", side_effect=lambda *a, **kw: next(responses)):
            with patch.object(dacli, "http_bytes", return_value=b"PNGDATA"):
                with patch("time.sleep"):
                    ns = _build_sync_args("feed")
                    dacli.cmd_sync_feed(ns)

        # Both should be on disk
        artist_dir = authed_with_destination / "sample-artist"
        assert artist_dir.exists()
        title_dirs = list(artist_dir.iterdir())
        assert len(title_dirs) == 2
        for d in title_dirs:
            assert (d / "description.json").exists()
            assert (d / "image.png").exists()

        # state.last_feed_deviationid should have been bookmarked
        st = dacli.load_state()
        assert st["last_feed_deviationid"] == "DEV-A"

    def test_caught_up_stops_at_last_seen(self, authed_with_destination, sample_deviation):
        """When last_feed_deviationid appears in the page, we stop there."""
        # Pre-set last seen
        st = dacli.load_state()
        st["last_feed_deviationid"] = "OLD-DEV"
        dacli.save_state(st)

        d1 = dict(sample_deviation, deviationid="NEW-DEV")
        d2 = dict(sample_deviation, deviationid="OLD-DEV", title="already-have")

        feed = {"results": [d1, d2], "has_more": True}
        md = {"metadata": [{"deviationid": "NEW-DEV"}]}

        responses = iter([feed, md])
        with patch.object(dacli, "http_json", side_effect=lambda *a, **kw: next(responses)):
            with patch.object(dacli, "http_bytes", return_value=b"X"):
                with patch("time.sleep"):
                    dacli.cmd_sync_feed(_build_sync_args("feed"))

        # Only NEW-DEV should be downloaded; OLD-DEV stop boundary
        artist_dir = authed_with_destination / "sample-artist"
        assert any(
            (p / "description.json").read_text().__contains__("NEW-DEV")
            for p in artist_dir.iterdir()
        )

    def test_429_breaks_loop(self, authed_with_destination, sample_deviation, capsys):
        err = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        with patch.object(dacli, "http_json", side_effect=err):
            with patch("time.sleep"):
                dacli.cmd_sync_feed(_build_sync_args("feed"))
        assert "HTTP 429" in capsys.readouterr().out

    def test_empty_feed(self, authed_with_destination):
        with patch.object(dacli, "http_json", return_value={"results": [], "has_more": False}):
            with patch("time.sleep"):
                dacli.cmd_sync_feed(_build_sync_args("feed"))

    def test_jitter_arg_logs_message(self, authed_with_destination, capsys):
        with patch.object(dacli, "http_json", return_value={"results": [], "has_more": False}):
            with patch("time.sleep"):
                dacli.cmd_sync_feed(_build_sync_args("feed", jitter=0.4))
        out = capsys.readouterr().out
        assert "jitter on" in out


# ---------------------------------------------------------------------------
# cmd_sync_artist
# ---------------------------------------------------------------------------
class TestCmdSyncArtist:
    def test_walks_one_page(self, authed_with_destination, sample_deviation):
        d = dict(sample_deviation, author={"username": "alice", "userid": "u1"})
        feed = {"results": [d], "has_more": False}
        md = {"metadata": [{"deviationid": d["deviationid"]}]}

        responses = iter([feed, md])
        with patch.object(dacli, "http_json", side_effect=lambda *a, **kw: next(responses)):
            with patch.object(dacli, "http_bytes", return_value=b"X"):
                with patch("time.sleep"):
                    dacli.cmd_sync_artist(_build_sync_args("artist", artist="alice"))
        assert (authed_with_destination / "alice").exists()

    def test_stops_on_429(self, authed_with_destination, capsys):
        err = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        with patch.object(dacli, "http_json", side_effect=err):
            with patch("time.sleep"):
                dacli.cmd_sync_artist(_build_sync_args("artist", artist="alice"))
        out = capsys.readouterr().out
        assert "alice" in out

    def test_offset_resume_message(self, authed_with_destination, capsys):
        with patch.object(dacli, "http_json", return_value={"results": [], "has_more": False}):
            with patch("time.sleep"):
                dacli.cmd_sync_artist(_build_sync_args("artist", artist="alice"))
        # When stopped reason isn't "gallery complete" + offset > 0 the
        # function emits a resume hint. With our empty page it stops cleanly.
        out = capsys.readouterr().out
        assert "alice" in out


class TestGalleryBackfillCompletes:
    """A gallery larger than one page must eventually finish syncing.

    The early stop assumes "gallery/all is reverse-chronological, so
    everything below a known item is known too". That holds only once the
    gallery has been walked to its end. Before that the index holds an
    arbitrary subset — the newest item from a `sync feed` run, or the
    first page of a walk the time budget cut short — and stopping on page
    0 stranded every older page permanently, because the next run started
    at 0 and stopped in the same place.

    Measured before the fix: a 240-item gallery stayed at 24/240 across
    unlimited full-budget runs. Only a manual --offset made progress, and
    `sync watched` always passes offset=0, so the documented backfill path
    could never complete.
    """

    PAGE = 24

    def _gallery(self, total):
        def dev(i):
            return {
                "deviationid": f"D{i:03}",
                "title": f"t{i:03}",
                "url": "u",
                "is_mature": False,
                "is_favourited": False,
                "published_time": "1700000000",
                "stats": {},
                "author": {"username": "alice", "userid": "U1"},
                "content": {"src": "https://cdn.example/img.png"},
            }

        def fake(url, **_kw):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if "gallery/all" in url:
                off = int(q["offset"][0])
                return {
                    "results": [dev(i) for i in range(off, min(off + self.PAGE, total))],
                    "has_more": off + self.PAGE < total,
                }
            return {"metadata": [{"deviationid": i} for i in q.get("deviationids[]", [])]}

        return fake

    def _seed_partial(self, dest, n):
        """What a truncated first run leaves: the newest page, and a marker."""
        for i in range(n):
            f = _describe(dest / "alice" / f"t{i:03}", f"D{i:03}")
            dacli.index_add(f"D{i:03}", "alice", f"t{i:03}", f, 3)
        st = dacli.load_state()
        st["galleries"] = {"alice": {"complete": False, "offset": n}}
        dacli.save_state(st)

    def _run(self, **overrides):
        with patch.object(dacli, "http_json", side_effect=self._gallery(240)):
            with patch.object(dacli, "http_bytes", return_value=b"IMG"):
                with patch("time.sleep"):
                    dacli.cmd_sync_artist(
                        _build_sync_args("artist", artist="alice", offset=None, **overrides)
                    )

    def test_a_truncated_walk_finishes_on_the_next_run(self, authed_with_destination):
        self._seed_partial(authed_with_destination, self.PAGE)
        assert dacli.index_count() == self.PAGE
        self._run()
        assert dacli.index_count() == 240, (
            f"only {dacli.index_count()}/240 synced; the backfill is still stranded"
        )

    def test_the_gallery_is_marked_complete_afterwards(self, authed_with_destination):
        self._seed_partial(authed_with_destination, self.PAGE)
        self._run()
        assert dacli.load_state()["galleries"]["alice"] == {"complete": True}

    def test_early_stop_returns_once_the_gallery_is_complete(self, authed_with_destination):
        """The optimisation must come back — this is not "always walk everything"."""
        self._seed_partial(authed_with_destination, self.PAGE)
        self._run()  # completes and marks it

        pages = []
        fake = self._gallery(240)

        def counting(url, **kw):
            if "gallery/all" in url:
                pages.append(url)
            return fake(url, **kw)

        with patch.object(dacli, "http_json", side_effect=counting):
            with patch.object(dacli, "http_bytes", return_value=b"IMG"):
                with patch("time.sleep"):
                    dacli.cmd_sync_artist(_build_sync_args("artist", artist="alice", offset=None))
        assert len(pages) == 1, (
            f"a completed gallery re-walked {len(pages)} pages; the early stop is gone"
        )

    def test_explicit_offset_still_wins(self, authed_with_destination):
        self._seed_partial(authed_with_destination, self.PAGE)
        seen = []
        fake = self._gallery(240)

        def spy(url, **kw):
            if "gallery/all" in url:
                seen.append(
                    int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["offset"][0])
                )
            return fake(url, **kw)

        with patch.object(dacli, "http_json", side_effect=spy):
            with patch.object(dacli, "http_bytes", return_value=b"IMG"):
                with patch("time.sleep"):
                    dacli.cmd_sync_artist(_build_sync_args("artist", artist="alice", offset=96))
        assert seen[0] == 96, f"--offset 96 was ignored; started at {seen[0]}"

    def test_watched_lets_each_artist_resume(self, authed_with_destination, monkeypatch):
        """`sync watched` must not pin every artist to offset 0.

        It is the documented path for the initial backfill, and it walks
        each artist through the same code. Passing 0 explicitly would
        suppress the resume for all of them — the one case that needs it
        most, since a run across many artists is the likeliest to be cut
        short by the budget.
        """
        seen = []
        monkeypatch.setattr(dacli, "_cmd_sync_artist_impl", lambda a: seen.append(a.offset))
        monkeypatch.setattr(
            dacli.sync, "_list_watched_via_friends", lambda *a, **k: ["alice", "bob"]
        )
        with patch.object(dacli, "http_json", return_value={"username": "me"}):
            with patch("time.sleep"):
                dacli.cmd_sync_watched(_build_sync_args("watched", user="me"))
        assert seen == [None, None], (
            f"sync watched pinned artists to offset {seen}; each must resume its own walk"
        )

    def test_sync_feed_seeding_does_not_strand_the_backfill(self, authed_with_destination):
        """The everyday trigger: `sync feed` indexes an artist's newest item.

        No truncation involved — one deviation in the index was enough to
        stop `sync artist` on page 0 forever.
        """
        f = _describe(authed_with_destination / "alice" / "t000", "D000")
        dacli.index_add("D000", "alice", "t000", f, 3)

        self._run()
        assert dacli.index_count() == 240, (
            f"only {dacli.index_count()}/240 synced after a feed sync seeded one item"
        )


# ---------------------------------------------------------------------------
# cmd_sync_watched + helpers
# ---------------------------------------------------------------------------
class TestListWatchedViaFriends:
    def test_paginated(self):
        # Two pages: 50 items + 10 items, with `has_more=False` on the second
        page1 = {
            "results": [{"user": {"username": f"u{i}"}} for i in range(50)],
            "has_more": True,
        }
        page2 = {
            "results": [{"user": {"username": f"u{i}"}} for i in range(50, 60)],
            "has_more": False,
        }
        responses = iter([page1, page2])
        with patch.object(dacli, "http_json", side_effect=lambda *a, **kw: next(responses)):
            with patch("time.sleep"):
                names = dacli._list_watched_via_friends("tok", "me", mature=True, delay=0)
        assert len(names) == 60
        assert names[0] == "u0"

    def test_dedupes(self):
        page = {
            "results": [
                {"user": {"username": "alice"}},
                {"user": {"username": "alice"}},  # duplicate
            ],
            "has_more": False,
        }
        with patch.object(dacli, "http_json", return_value=page):
            with patch("time.sleep"):
                names = dacli._list_watched_via_friends("tok", "me", True, delay=0)
        assert names == ["alice"]


class TestDiscoverWatchedViaFeed:
    def test_collects_unique_authors(self):
        feed = {
            "results": [
                {"author": {"username": "alice"}, "deviationid": "1"},
                {"author": {"username": "bob"}, "deviationid": "2"},
                {"author": {"username": "alice"}, "deviationid": "3"},  # dup
            ],
            "has_more": False,
        }
        with patch.object(dacli, "http_json", return_value=feed):
            with patch("time.sleep"):
                names = dacli._discover_watched_via_feed("tok", True, max_deviations=200, delay=0)
        assert names == ["alice", "bob"]

    def test_caps_at_max_deviations(self):
        # has_more=True forever, but feed_max should stop us.
        page = {
            "results": [
                {"author": {"username": f"u{i}"}, "deviationid": f"d{i}"} for i in range(50)
            ],
            "has_more": True,
        }
        with patch.object(dacli, "http_json", return_value=page):
            with patch("time.sleep"):
                names = dacli._discover_watched_via_feed("tok", True, max_deviations=100, delay=0)
        # Since each call returns 50 unique, two iterations = 50+50=100
        # but the cap is 100, and the loop check is before the call, so we
        # may stop after a single iteration. Just assert sane bounds.
        assert 0 < len(names) <= 100

    def test_empty_feed_breaks(self):
        with patch.object(dacli, "http_json", return_value={"results": [], "has_more": False}):
            with patch("time.sleep"):
                names = dacli._discover_watched_via_feed("tok", True, max_deviations=200, delay=0)
        assert names == []


class TestCmdSyncWatched:
    def test_via_feed_flag_uses_feed_discovery(self, authed_with_destination):
        # Discovery returns 1 artist; per-artist sync gets an empty gallery.
        feed_pages = iter(
            [
                {
                    "results": [{"author": {"username": "alice"}, "deviationid": "X"}],
                    "has_more": False,
                },
            ]
        )
        gallery_pages = iter([{"results": [], "has_more": False}])

        def fake_json(url, **_kw):
            if "deviantsyouwatch" in url:
                return next(feed_pages)
            if "/gallery/all" in url:
                return next(gallery_pages)
            return {}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch("time.sleep"):
                dacli.cmd_sync_watched(_build_sync_args("watched", via_feed=True))

    def test_with_user_arg(self, authed_with_destination):
        friends = {"results": [{"user": {"username": "bob"}}], "has_more": False}
        gallery = {"results": [], "has_more": False}

        def fake_json(url, **_kw):
            if "/user/friends/" in url:
                return friends
            if "/gallery/all" in url:
                return gallery
            return {}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch("time.sleep"):
                dacli.cmd_sync_watched(_build_sync_args("watched", user="alice"))

    def test_user_arg_403_falls_back_to_feed(self, authed_with_destination, capsys):
        def fake_json(url, **_kw):
            if "/user/friends/" in url:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            if "deviantsyouwatch" in url:
                return {"results": [], "has_more": False}
            return {}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch("time.sleep"):
                dacli.cmd_sync_watched(_build_sync_args("watched", user="alice"))
        out = capsys.readouterr().out
        assert "lacks `user` scope" in out or "feed" in out

    def test_whoami_403_falls_back_to_feed(self, authed_with_destination, capsys):
        def fake_json(url, **_kw):
            if "/user/whoami" in url:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            if "deviantsyouwatch" in url:
                return {"results": [], "has_more": False}
            return {}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch("time.sleep"):
                dacli.cmd_sync_watched(_build_sync_args("watched"))
        out = capsys.readouterr().out
        assert "feed" in out.lower()

    def test_continues_on_artist_crash(self, authed_with_destination, capsys, monkeypatch):
        # Make discovery yield two artists, the first one raises during sync
        feed = {
            "results": [
                {"author": {"username": "alice"}, "deviationid": "A"},
                {"author": {"username": "bob"}, "deviationid": "B"},
            ],
            "has_more": False,
        }

        def fake_json(url, **_kw):
            if "deviantsyouwatch" in url:
                return feed
            return {"results": [], "has_more": False}

        calls: list[str] = []

        def crashy_sync(args):
            calls.append(args.artist)
            if args.artist == "alice":
                raise RuntimeError("kaboom")

        # `cmd_sync_watched` calls `_cmd_sync_artist_impl` directly (not the
        # locked `cmd_sync_artist` wrapper) so the inner per-artist work
        # doesn't try to re-acquire the `sync` lock the outer command holds.
        monkeypatch.setattr(dacli, "_cmd_sync_artist_impl", crashy_sync)

        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch("time.sleep"):
                with pytest.raises(SystemExit) as exc:
                    dacli.cmd_sync_watched(_build_sync_args("watched", via_feed=True))

        # One artist crashing must not abort the rest of the walk...
        assert calls == ["alice", "bob"]
        # ...but the run as a whole did not do what was asked, so it must
        # not report success. `da sync watched || notify` is the
        # documented pattern; exiting 0 here made a nightly job that
        # failed on every artist indistinguishable from a clean one.
        # 1 = partial (bob succeeded), 2 = nothing succeeded.
        assert exc.value.code == 1

    def test_every_artist_failing_exits_2(self, authed_with_destination, monkeypatch):
        """Total failure is a 2; partial is a 1. Matches `da diagnose`."""
        feed = {
            "results": [
                {"author": {"username": "alice"}, "deviationid": "A"},
                {"author": {"username": "bob"}, "deviationid": "B"},
            ],
            "has_more": False,
        }

        def fake_json(url, **_kw):
            return feed if "deviantsyouwatch" in url else {"results": [], "has_more": False}

        def always_crash(args):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(dacli, "_cmd_sync_artist_impl", always_crash)
        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch("time.sleep"):
                with pytest.raises(SystemExit) as exc:
                    dacli.cmd_sync_watched(_build_sync_args("watched", via_feed=True))
        assert exc.value.code == 2

    def test_time_budget_bounds_the_whole_run_not_each_artist(
        self, authed_with_destination, monkeypatch
    ):
        """`--time-budget N` must mean N seconds total.

        It used to hand the full budget to every artist, so
        ``--time-budget 300`` across 200 watched artists could still be
        running 16 hours later — for a scheduled job, the flag's entire
        purpose defeated.
        """
        artists = [f"a{i}" for i in range(6)]
        budgets: list[int] = []
        clock = {"t": 1000.0}

        def fake_artist(args):
            budgets.append(args.time_budget)
            clock["t"] += 100  # each artist burns 100s

        monkeypatch.setattr(dacli, "_cmd_sync_artist_impl", fake_artist)
        monkeypatch.setattr(dacli.sync.time, "time", lambda: clock["t"])
        monkeypatch.setattr(dacli.sync, "_list_watched_via_friends", lambda *a, **k: artists)

        with patch.object(dacli, "http_json", return_value={"username": "me"}):
            with patch("time.sleep"):
                dacli.cmd_sync_watched(_build_sync_args("watched", user="me", time_budget=300))

        # 300s budget, 100s per artist: three run, the rest are skipped.
        assert len(budgets) == 3, f"expected 3 artists within budget, got {len(budgets)}"
        # Each is handed what remains, never the full 300 again.
        assert budgets == [300, 200, 100]
        assert sum(1 for b in budgets if b == 300) == 1, "budget must not reset per artist"

    def test_budget_exhaustion_is_recorded_not_reported_as_success(
        self, authed_with_destination, monkeypatch
    ):
        """A truncated run must be distinguishable from a finished one."""
        artists = [f"a{i}" for i in range(10)]
        clock = {"t": 1000.0}
        recorded: dict[str, object] = {}

        def fake_artist(args):
            clock["t"] += 100

        monkeypatch.setattr(dacli, "_cmd_sync_artist_impl", fake_artist)
        monkeypatch.setattr(dacli.sync.time, "time", lambda: clock["t"])
        monkeypatch.setattr(dacli.sync, "_list_watched_via_friends", lambda *a, **k: artists)
        monkeypatch.setattr(
            dacli.sync,
            "_record_sync_summary",
            lambda kind, started, totals, reason, **kw: recorded.update(
                kind=kind, totals=totals, reason=reason
            ),
        )

        with patch.object(dacli, "http_json", return_value={"username": "me"}):
            with patch("time.sleep"):
                dacli.cmd_sync_watched(_build_sync_args("watched", user="me", time_budget=250))

        # 250s budget, 100s per artist: three start (with 250, 150 and 50
        # seconds left), the fourth finds too little left to be worth an
        # API call.
        assert recorded["reason"] == dacli.sync.TIME_BUDGET_EXHAUSTED
        totals = recorded["totals"]
        assert totals["artists_done"] == 3
        assert totals["artists_skipped"] == 7
        assert totals["artists_total"] == 10
        # The counts must add up, or `da diagnose` reports a nonsense run.
        assert totals["artists_done"] + totals["artists_skipped"] == totals["artists_total"]


# ---------------------------------------------------------------------------
# Pagination — the core loop of every sync mode.
#
# Nothing exercised a multi-page walk before these. Every existing test
# fed a single page, or set has_more=True and then stopped for a
# different reason (caught up, feed_max). A wrong or missing
# `offset += limit` would re-fetch page 1 forever, or skip whole pages,
# and the entire suite would still pass.
# ---------------------------------------------------------------------------
def _offsets(urls: list[str]) -> list[int]:
    """Offsets actually requested, in order."""
    out = []
    for u in urls:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        if "offset" in q:
            out.append(int(q["offset"][0]))
    return out


class TestFeedPagination:
    def _pages(self, sample_deviation, n_pages, per_page, limit=24):
        """n_pages of feed results, has_more=False only on the last."""
        pages = []
        for p in range(n_pages):
            pages.append(
                {
                    "results": [
                        dict(sample_deviation, deviationid=f"P{p}-D{i}", title=f"t{p}-{i}")
                        for i in range(per_page)
                    ],
                    "has_more": p < n_pages - 1,
                }
            )
        return pages

    def test_walks_every_page_and_advances_offset(self, authed_with_destination, sample_deviation):
        pages = self._pages(sample_deviation, n_pages=3, per_page=2)
        feed_urls: list[str] = []

        def fake_json(url, **_kw):
            if "deviantsyouwatch" in url:
                feed_urls.append(url)
                return pages[len(feed_urls) - 1]
            # metadata for whatever was asked
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            return {"metadata": [{"deviationid": i} for i in q.get("deviationids[]", [])]}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch.object(dacli, "http_bytes", return_value=b"IMG"):
                with patch("time.sleep"):
                    dacli.cmd_sync_feed(_build_sync_args("feed", limit=24))

        assert len(feed_urls) == 3, f"expected 3 feed pages, got {len(feed_urls)}"
        assert _offsets(feed_urls) == [0, 24, 48], "offset did not advance by limit each page"

        # Every deviation from every page reached disk — not just page 1.
        saved = {d.name for artist in authed_with_destination.iterdir() for d in artist.iterdir()}
        assert len(saved) == 6, f"expected 6 deviations across 3 pages, saved {len(saved)}"

    def test_offset_advances_by_the_requested_limit(
        self, authed_with_destination, sample_deviation
    ):
        """A non-default --limit must drive the stride, not the default."""
        pages = self._pages(sample_deviation, n_pages=3, per_page=1)
        feed_urls: list[str] = []

        def fake_json(url, **_kw):
            if "deviantsyouwatch" in url:
                feed_urls.append(url)
                return pages[len(feed_urls) - 1]
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            return {"metadata": [{"deviationid": i} for i in q.get("deviationids[]", [])]}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch.object(dacli, "http_bytes", return_value=b"IMG"):
                with patch("time.sleep"):
                    dacli.cmd_sync_feed(_build_sync_args("feed", limit=10))

        assert _offsets(feed_urls) == [0, 10, 20]

    def test_stops_when_has_more_is_false(self, authed_with_destination, sample_deviation):
        """The walk must not ask for a page past the end."""
        pages = self._pages(sample_deviation, n_pages=2, per_page=1)
        feed_urls: list[str] = []

        def fake_json(url, **_kw):
            if "deviantsyouwatch" in url:
                feed_urls.append(url)
                if len(feed_urls) > len(pages):
                    raise AssertionError(
                        f"asked for page {len(feed_urls)}; only {len(pages)} exist"
                    )
                return pages[len(feed_urls) - 1]
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            return {"metadata": [{"deviationid": i} for i in q.get("deviationids[]", [])]}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch.object(dacli, "http_bytes", return_value=b"IMG"):
                with patch("time.sleep"):
                    dacli.cmd_sync_feed(_build_sync_args("feed"))
        assert len(feed_urls) == 2


class TestArtistPagination:
    def test_walks_every_page_and_advances_offset(self, authed_with_destination, sample_deviation):
        pages = [
            {
                "results": [
                    dict(
                        sample_deviation,
                        deviationid=f"A{p}-{i}",
                        title=f"a{p}-{i}",
                        author={"username": "alice", "userid": "u1"},
                    )
                    for i in range(2)
                ],
                "has_more": p < 2,
            }
            for p in range(3)
        ]
        gallery_urls: list[str] = []

        def fake_json(url, **_kw):
            if "gallery/all" in url:
                gallery_urls.append(url)
                return pages[len(gallery_urls) - 1]
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            return {"metadata": [{"deviationid": i} for i in q.get("deviationids[]", [])]}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch.object(dacli, "http_bytes", return_value=b"IMG"):
                with patch("time.sleep"):
                    dacli.cmd_sync_artist(_build_sync_args("artist", artist="alice", limit=24))

        assert len(gallery_urls) == 3
        assert _offsets(gallery_urls) == [0, 24, 48]
        assert len(list((authed_with_destination / "alice").iterdir())) == 6

    def test_starts_from_the_offset_flag(self, authed_with_destination, sample_deviation):
        """`--offset N` resumes an interrupted walk from N, not from 0."""
        gallery_urls: list[str] = []

        def fake_json(url, **_kw):
            if "gallery/all" in url:
                gallery_urls.append(url)
                return {"results": [], "has_more": False}
            return {"metadata": []}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            with patch("time.sleep"):
                dacli.cmd_sync_artist(_build_sync_args("artist", artist="alice", offset=96))

        assert _offsets(gallery_urls) == [96]


class TestMetadataBatching:
    """`/deviation/metadata` caps at 50 ids per call (METADATA_BATCH_SIZE)."""

    def test_ids_are_chunked_at_fifty(self, authed_with_destination):
        ids = [f"D{i}" for i in range(120)]
        chunks: list[list[str]] = []

        def fake_json(url, **_kw):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            chunks.append(q["deviationids[]"])
            return {"metadata": [{"deviationid": i} for i in q["deviationids[]"]]}

        cfg, state = dacli.load_config(), dacli.load_state()
        with patch.object(dacli, "http_json", side_effect=fake_json):
            md = dacli.sync._fetch_metadata_batch(ids, cfg, state, mature=True)

        assert [len(c) for c in chunks] == [50, 50, 20], (
            "metadata ids were not chunked at 50; DA rejects longer batches"
        )
        # No id lost or duplicated across the chunk boundaries.
        assert [i for c in chunks for i in c] == ids
        assert len(md) == 120

    def test_exactly_fifty_is_one_call(self, authed_with_destination):
        ids = [f"D{i}" for i in range(50)]
        calls = []

        def fake_json(url, **_kw):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            calls.append(len(q["deviationids[]"]))
            return {"metadata": []}

        cfg, state = dacli.load_config(), dacli.load_state()
        with patch.object(dacli, "http_json", side_effect=fake_json):
            dacli.sync._fetch_metadata_batch(ids, cfg, state, mature=True)
        assert calls == [50], "an exact multiple of the cap must not make an empty extra call"


# ---------------------------------------------------------------------------
# Search commands
# ---------------------------------------------------------------------------
class TestSearchPopularDeprecated:
    """`/browse/popular` was retired by DA. Verify the CLI exits cleanly
    with an actionable error pointing at the live alternatives, instead
    of crashing with an HTTPError 404."""

    def test_exits_with_actionable_error(self, authed, capsys):
        ns = dacli.build_parser().parse_args(["search", "popular"])
        with pytest.raises(SystemExit) as exc:
            ns.func(ns)
        assert exc.value.code == 2
        # One read consumes both stdout and stderr; log() routes errors to
        # stderr, so the actionable hint lands there. Assert both streams
        # mention the deprecation and a live alternative.
        captured = capsys.readouterr()
        combined = (captured.out + captured.err).lower()
        assert "retired" in combined or "unavailable" in combined
        assert "topic" in combined or "daily" in combined or "tag" in combined


class TestSearchNewestDeprecated:
    def test_exits_with_actionable_error(self, authed, capsys):
        ns = dacli.build_parser().parse_args(["search", "newest"])
        with pytest.raises(SystemExit) as exc:
            ns.func(ns)
        assert exc.value.code == 2
        captured = capsys.readouterr()
        combined = (captured.out + captured.err).lower()
        assert "retired" in combined or "unavailable" in combined
        assert "topic" in combined or "tag" in combined


class TestSearchTag:
    def test_passes_tag(self, authed, capsys):
        captured: list[str] = []

        def fake_json(url, **_kw):
            captured.append(url)
            return {"results": []}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            ns = dacli.build_parser().parse_args(["search", "tag", "abstract"])
            ns.func(ns)
        assert any("tag=abstract" in u for u in captured)


class TestSearchUser:
    """/user/whois requires POST, not GET. The CLI builds the body manually
    because urlencode doesn't produce DA's `usernames[]=a&usernames[]=b`
    bracket-suffix shape."""

    def test_resolves_via_post(self, authed, capsys, monkeypatch):
        # Capture the request that would be sent
        seen: dict[str, object] = {}

        class _FakeResponse:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def __enter__(self) -> _FakeResponse:
                return self

            def __exit__(self, *_a: object) -> None:
                pass

            def read(self) -> bytes:
                return self._body

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["data"] = req.data
            seen["auth"] = req.get_header("Authorization")
            seen["content_type"] = req.headers.get("Content-type")
            return _FakeResponse(
                json.dumps(
                    {
                        "results": [
                            {"username": "alice", "userid": "U-1", "type": "regular"},
                            {"username": "bob", "userid": "U-2", "type": "regular"},
                        ]
                    }
                ).encode()
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        ns = dacli.build_parser().parse_args(["search", "user", "alice", "bob"])
        ns.func(ns)
        out = capsys.readouterr().out
        assert "@alice" in out
        assert "@bob" in out
        # POST, not GET
        assert seen["method"] == "POST"
        # bracket-suffix encoding
        body_str = (seen["data"] or b"").decode()
        assert "usernames%5B%5D=alice" in body_str
        assert "usernames%5B%5D=bob" in body_str
        # Bearer token attached
        assert seen["auth"] and "Bearer" in seen["auth"]
        assert seen["content_type"] == "application/x-www-form-urlencoded"


class TestSearchTopic:
    def test_passes_topic_and_mature(self, authed, capsys):
        captured: list[str] = []

        def fake_json(url, **_kw):
            captured.append(url)
            return {
                "results": [
                    {"title": "Sample Art", "author": {"username": "artist1"}, "url": "U1"},
                ]
            }

        with patch.object(dacli, "http_json", side_effect=fake_json):
            ns = dacli.build_parser().parse_args(
                ["search", "topic", "digitalart", "--mature", "--limit", "5"]
            )
            ns.func(ns)
        assert any(
            "topic=digitalart" in u and "mature_content=true" in u and "limit=5" in u
            for u in captured
        )
        out = capsys.readouterr().out
        assert "Sample Art" in out

    def test_json_mode(self, authed, capsys):
        with patch.object(dacli, "http_json", return_value={"results": [{"title": "X"}]}):
            ns = dacli.build_parser().parse_args(
                ["search", "topic", "nature", "--mature", "--json"]
            )
            ns.func(ns)
        out = capsys.readouterr().out
        assert '"title": "X"' in out


class TestSearchTopics:
    def test_lists_topics_with_offset(self, authed, capsys):
        captured: list[str] = []

        def fake_json(url, **_kw):
            captured.append(url)
            return {
                "results": [
                    {
                        "canonical_name": "digitalart",
                        "name": "DigitalArt",
                        "example_deviations": [{"title": "ex"}],
                    },
                    {"canonical_name": "nature", "name": "Nature", "example_deviations": []},
                ],
                "has_more": True,
                "next_offset": 12,
            }

        with patch.object(dacli, "http_json", side_effect=fake_json):
            ns = dacli.build_parser().parse_args(
                ["search", "topics", "--limit", "10", "--offset", "10"]
            )
            ns.func(ns)
        assert any("offset=10" in u and "limit=10" in u for u in captured)
        out = capsys.readouterr().out
        assert "digitalart" in out and "nature" in out
        assert "next_offset=12" in out


class TestSearchTopTopics:
    def test_lists_top_topics(self, authed, capsys):
        body = {
            "results": [
                {
                    "canonical_name": "fantasy",
                    "name": "Fantasy",
                    "example_deviations": [{"title": "Black Cat"}],
                },
            ]
        }
        with patch.object(dacli, "http_json", return_value=body):
            ns = dacli.build_parser().parse_args(["search", "toptopics", "--mature"])
            ns.func(ns)
        out = capsys.readouterr().out
        assert "fantasy" in out and "Black Cat" in out


class TestSearchTagSuggest:
    def test_autocompletes(self, authed, capsys):
        captured: list[str] = []

        def fake_json(url, **_kw):
            captured.append(url)
            return {
                "results": [
                    {"tag_name": "nature"},
                    {"tag_name": "naturephotography"},
                ]
            }

        with patch.object(dacli, "http_json", side_effect=fake_json):
            ns = dacli.build_parser().parse_args(["search", "tag-suggest", "nat"])
            ns.func(ns)
        assert any("tag_name=nat" in u for u in captured)
        out = capsys.readouterr().out
        assert "naturephotography" in out

    def test_json_mode(self, authed, capsys):
        with patch.object(dacli, "http_json", return_value={"results": [{"tag_name": "nature"}]}):
            ns = dacli.build_parser().parse_args(["search", "tag-suggest", "na", "--json"])
            ns.func(ns)
        out = capsys.readouterr().out
        assert '"tag_name": "nature"' in out


class TestDeviationMoreLikeThis:
    def test_groups_artist_and_da(self, authed, capsys):
        body = {
            "seed": "AAA-BBB",
            "author": {"username": "seed_author"},
            "more_from_artist": [
                {
                    "deviationid": "111",
                    "title": "Same Artist Pic",
                    "author": {"username": "seed_author"},
                },
            ],
            "more_from_da": [
                {
                    "deviationid": "222",
                    "title": "Other DA Pic",
                    "author": {"username": "other_user"},
                },
            ],
        }
        captured: list[str] = []

        def fake_json(url, **_kw):
            captured.append(url)
            return body

        with patch.object(dacli, "http_json", side_effect=fake_json):
            ns = dacli.build_parser().parse_args(
                ["deviation", "morelikethis", "AAA-BBB", "--mature", "--limit", "3"]
            )
            ns.func(ns)
        assert any("seed=AAA-BBB" in u and "mature_content=true" in u for u in captured)
        out = capsys.readouterr().out
        assert "FROM ARTIST" in out and "FROM DA" in out
        assert "Same Artist Pic" in out and "Other DA Pic" in out

    def test_json_mode(self, authed, capsys):
        with patch.object(
            dacli,
            "http_json",
            return_value={"seed": "ZZZ", "more_from_da": [], "more_from_artist": []},
        ):
            ns = dacli.build_parser().parse_args(
                ["deviation", "morelikethis", "ZZZ", "--mature", "--json"]
            )
            ns.func(ns)
        out = capsys.readouterr().out
        assert '"seed": "ZZZ"' in out


# ---------------------------------------------------------------------------
# user / watch
# ---------------------------------------------------------------------------
class TestUserProfile:
    def test_prints_fields(self, authed, capsys):
        body = {
            "user": {"username": "alice", "userid": "1"},
            "profile_url": "https://example.com/alice",
            "is_watching": True,
            "user_is_artist": True,
            "bio": "I draw things.",
        }
        with patch.object(dacli, "http_json", return_value=body):
            ns = dacli.build_parser().parse_args(["user", "profile", "alice"])
            ns.func(ns)
        out = capsys.readouterr().out
        assert "@alice" in out
        assert "I draw things." in out


class TestWatchList:
    def test_403_exits_with_actionable_message(self, authed, capsys):
        def fake_json(url, **_kw):
            raise urllib.error.HTTPError(url, 403, "f", {}, None)

        with patch.object(dacli, "http_json", side_effect=fake_json):
            ns = dacli.build_parser().parse_args(["watch", "list"])
            with pytest.raises(SystemExit):
                ns.func(ns)
        captured = capsys.readouterr()
        assert "user" in (captured.out + captured.err).lower()

    def test_success_path(self, authed, capsys):
        whoami = {"username": "me"}
        friends = {
            "results": [
                {"user": {"username": "alice", "type": "regular"}},
                {"user": {"username": "bob", "type": "regular"}},
            ],
            "has_more": False,
        }

        def fake_json(url, **_kw):
            if "/whoami" in url:
                return whoami
            return friends

        with patch.object(dacli, "http_json", side_effect=fake_json):
            ns = dacli.build_parser().parse_args(["watch", "list"])
            ns.func(ns)
        out = capsys.readouterr().out
        assert "@alice" in out and "@bob" in out


# ---------------------------------------------------------------------------
# deviation show
# ---------------------------------------------------------------------------
class TestDeviationShow:
    def test_prints_summary(self, authed, capsys):
        body = {
            "metadata": [
                {
                    "deviationid": "X",
                    "title": "Title",
                    "author": {"username": "alice"},
                    "url": "https://x",
                    "is_mature": False,
                    "tags": [{"tag_name": "art"}, {"tag_name": "test"}],
                    "description": "<p>Hello <b>world</b></p>",
                }
            ]
        }
        with patch.object(dacli, "http_json", return_value=body):
            ns = dacli.build_parser().parse_args(["deviation", "show", "X"])
            ns.func(ns)
        out = capsys.readouterr().out
        assert "Title" in out
        assert "@alice" in out
        # HTML stripped
        assert "Hello world" in out
        assert "<p>" not in out

    def test_json_flag(self, authed, capsys):
        body = {"metadata": [{"deviationid": "X", "title": "T", "author": {"username": "a"}}]}
        with patch.object(dacli, "http_json", return_value=body):
            ns = dacli.build_parser().parse_args(["deviation", "show", "X", "--json"])
            ns.func(ns)
        out = capsys.readouterr().out
        # Should be parseable JSON
        parsed = json.loads(out)
        assert parsed["deviationid"] == "X"

    def test_missing_metadata_exits(self, authed):
        with patch.object(dacli, "http_json", return_value={"metadata": []}):
            ns = dacli.build_parser().parse_args(["deviation", "show", "X"])
            with pytest.raises(SystemExit):
                ns.func(ns)


# ---------------------------------------------------------------------------
# daily
# ---------------------------------------------------------------------------
class TestDaily:
    def test_default_no_date(self, authed, capsys):
        body = {
            "results": [{"title": "Pick of the day", "author": {"username": "alice"}, "url": "u"}]
        }
        with patch.object(dacli, "http_json", return_value=body):
            ns = dacli.build_parser().parse_args(["daily"])
            ns.func(ns)
        out = capsys.readouterr().out
        assert "@alice" not in out  # _print_results uses raw `username`
        # Actually _print_results prints "  alice/'Pick of the day' u" form
        assert "alice" in out

    def test_with_date(self, authed, capsys):
        captured: list[str] = []

        def fake_json(url, **_kw):
            captured.append(url)
            return {"results": []}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            ns = dacli.build_parser().parse_args(["daily", "2026-04-26"])
            ns.func(ns)
        assert any("date=2026-04-26" in u for u in captured)


# ---------------------------------------------------------------------------
# config path
# ---------------------------------------------------------------------------
class TestConfigPath:
    def test_prints_paths(self, isolated_paths, capsys):
        ns = dacli.build_parser().parse_args(["config", "path"])
        ns.func(ns)
        out = capsys.readouterr().out
        assert "config:" in out
        assert "state:" in out


# ---------------------------------------------------------------------------
# _ensure_destination
# ---------------------------------------------------------------------------
class TestEnsureDestination:
    def test_no_destination_exits(self, isolated_paths, no_keychain):
        with pytest.raises(SystemExit):
            dacli._ensure_destination({})

    def test_creates_missing_dest_when_parent_exists(self, isolated_paths, no_keychain, tmp_path):
        dest = tmp_path / "subdir"
        result = dacli._ensure_destination({"destination": str(dest)})
        assert result == dest
        assert dest.exists()

    def test_missing_parent_exits(self, isolated_paths, no_keychain, tmp_path):
        # parent of /nonexistent/parent/dest
        with pytest.raises(SystemExit):
            dacli._ensure_destination({"destination": "/definitely/not/here/at/all"})
