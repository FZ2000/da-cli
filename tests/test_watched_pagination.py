"""`_list_watched_via_friends` trusted the server's `has_more` absolutely.

It was the only paginated walk in the codebase with no bound of any kind:
the two sync walks stop on their time budget, `_discover_watched_via_feed`
stops at `max_deviations`, and all three break out when a page comes back
empty. This one had `while True:` and no empty-page guard, so a server
that keeps saying "more" pages forever — and `sync watched` is the
scheduled path, so forever means an unattended job burning one request
per second until someone notices.

Every test in the first two classes hangs against the unfixed function
rather than failing, so each one bounds the stub and asserts that the
bound was never reached.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import dacli
from dacli.constants import FRIENDS_PAGE_CAP, FRIENDS_PAGE_MAX


class _Bounded:
    """An http_json stub that raises instead of letting the walk run away.

    Without the bound a regression here does not fail the suite, it hangs
    it, which in CI reads as an infrastructure problem rather than a bug.
    """

    def __init__(self, body: dict, limit: int):
        self.body = body
        self.limit = limit
        self.calls = 0
        self.offsets: list[int] = []
        self.urls: list[str] = []

    def __call__(self, url: str, **kw: object) -> dict:
        self.calls += 1
        self.urls.append(url)
        for part in url.split("?", 1)[-1].split("&"):
            if part.startswith("offset="):
                self.offsets.append(int(part.split("=", 1)[1]))
        if self.calls > self.limit:
            raise AssertionError(
                f"still paginating after {self.calls} pages — the walk is unbounded"
            )
        return self.body


class TestALyingHasMoreCannotPageForever:
    def test_an_empty_page_ends_the_walk(self):
        """`{"results": [], "has_more": true}` is the cheap way to get here.

        Any offset past the end of a list can produce it, and so can a
        transient server fault. The three sibling walks all break on an
        empty page for exactly this reason; this one read `has_more` and
        went back for more nothing, forever.
        """
        stub = _Bounded({"results": [], "has_more": True}, limit=25)
        with patch.object(dacli, "http_json", side_effect=stub), patch("time.sleep"):
            names = dacli._list_watched_via_friends("tok", "me", True, delay=0)

        assert names == []
        assert stub.calls == 1, f"an empty page should end the walk, not continue it ({stub.calls})"

    def test_repeating_pages_stop_at_the_page_cap(self):
        """The empty-page guard does not cover this one.

        A server that returns the same non-empty page with `has_more`
        forever gets past it — results are truthy every time. Only the
        page cap stops this, which is why it is a separate defence and
        not a redundant one.
        """
        page = {
            "results": [{"user": {"username": f"u{i}"}} for i in range(FRIENDS_PAGE_CAP)],
            "has_more": True,
        }
        stub = _Bounded(page, limit=FRIENDS_PAGE_MAX + 5)
        with patch.object(dacli, "http_json", side_effect=stub), patch("time.sleep"):
            names = dacli._list_watched_via_friends("tok", "me", True, delay=0)

        assert stub.calls == FRIENDS_PAGE_MAX, (
            f"walked {stub.calls} pages; the cap is {FRIENDS_PAGE_MAX}"
        )
        assert len(names) == FRIENDS_PAGE_CAP, "the repeated page should dedupe to one page's worth"

    def test_hitting_the_cap_says_so(self, capsys):
        """Silently returning a truncated list would be worse than hanging.

        `sync watched` then syncs a subset and reports success, so the
        user has no way to tell a short answer from a complete one.
        """
        page = {"results": [{"user": {"username": "alice"}}], "has_more": True}
        with (
            patch.object(dacli, "http_json", side_effect=_Bounded(page, FRIENDS_PAGE_MAX + 5)),
            patch("time.sleep"),
        ):
            dacli._list_watched_via_friends("tok", "me", True, delay=0)

        err = capsys.readouterr().err
        assert "still reported more" in err and "may be missing" in err, (
            f"the cap was hit with no warning on stderr: {err!r}"
        )

    def test_a_normal_walk_logs_no_warning(self, capsys):
        """The control for the warning: it must not fire on a clean walk."""
        pages = iter(
            [
                {"results": [{"user": {"username": "alice"}}], "has_more": True},
                {"results": [{"user": {"username": "bob"}}], "has_more": False},
            ]
        )
        with patch.object(dacli, "http_json", side_effect=lambda *a, **k: next(pages)):
            with patch("time.sleep"):
                dacli._list_watched_via_friends("tok", "me", True, delay=0)

        assert "still reported more" not in capsys.readouterr().err


class _CountingName(str):
    """A username that records every `==` performed against it.

    Membership in a list is a scan, so it costs one `__eq__` per element
    already collected; membership in a set costs a `__hash__` and only
    falls through to `__eq__` on a hash collision. That difference is
    deterministic, which makes it testable — unlike wall-clock timing.
    """

    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return str.__eq__(self, other)

    def __hash__(self) -> int:
        return str.__hash__(self)


class TestDedupDoesNotRescanTheAccumulator:
    def test_membership_is_not_a_linear_scan(self):
        """`u not in watched` against the list it was appending to.

        Quadratic, and invisible at the sizes a normal account reaches —
        which is why it survived. Asserted as comparison count rather
        than elapsed time so it cannot flake on a loaded runner.
        """
        pages_of, per_page = 10, 50
        total = pages_of * per_page
        pages = [
            {
                "results": [
                    {"user": {"username": _CountingName(f"u{p * per_page + i}")}}
                    for i in range(per_page)
                ],
                "has_more": p < pages_of - 1,
            }
            for p in range(pages_of)
        ]
        it = iter(pages)
        _CountingName.comparisons = 0
        with patch.object(dacli, "http_json", side_effect=lambda *a, **k: next(it)):
            with patch("time.sleep"):
                names = dacli._list_watched_via_friends("tok", "me", True, delay=0)

        assert len(names) == total
        # A scan of the accumulator costs ~total^2/2 (~125k here); hashing
        # costs a handful. Anything at or above `total` is a scan.
        assert _CountingName.comparisons < total, (
            f"{_CountingName.comparisons} string comparisons for {total} usernames — "
            "dedup is scanning the accumulator instead of hashing"
        )


class TestTheWalkItselfStillWorks:
    """Controls. The bug was in the loop's exits, not its job."""

    def test_two_pages_are_concatenated_in_order(self):
        pages = iter(
            [
                {
                    "results": [{"user": {"username": f"u{i}"}} for i in range(FRIENDS_PAGE_CAP)],
                    "has_more": True,
                },
                {
                    "results": [
                        {"user": {"username": f"u{i}"}}
                        for i in range(FRIENDS_PAGE_CAP, FRIENDS_PAGE_CAP + 10)
                    ],
                    "has_more": False,
                },
            ]
        )
        with patch.object(dacli, "http_json", side_effect=lambda *a, **k: next(pages)):
            with patch("time.sleep"):
                names = dacli._list_watched_via_friends("tok", "me", mature=True, delay=0)

        assert len(names) == FRIENDS_PAGE_CAP + 10
        assert names[0] == "u0"
        assert names[-1] == f"u{FRIENDS_PAGE_CAP + 9}"

    def test_duplicates_collapse_and_first_position_wins(self):
        page = {
            "results": [
                {"user": {"username": "alice"}},
                {"user": {"username": "bob"}},
                {"user": {"username": "alice"}},
            ],
            "has_more": False,
        }
        with patch.object(dacli, "http_json", return_value=page):
            with patch("time.sleep"):
                names = dacli._list_watched_via_friends("tok", "me", True, delay=0)
        assert names == ["alice", "bob"]

    @pytest.mark.parametrize("missing", [{}, {"user": {}}, {"user": {"username": ""}}])
    def test_a_result_without_a_username_is_skipped(self, missing):
        page = {"results": [missing, {"user": {"username": "alice"}}], "has_more": False}
        with patch.object(dacli, "http_json", return_value=page):
            with patch("time.sleep"):
                names = dacli._list_watched_via_friends("tok", "me", True, delay=0)
        assert names == ["alice"]

    def test_the_offset_step_matches_the_requested_limit(self):
        """These were two separate hardcoded 50s.

        Changing one without the other silently skips or re-reads a page,
        and nothing downstream would notice — the walk just returns a
        wrong list. Both now come from FRIENDS_PAGE_CAP; this pins that
        they agree.
        """
        pages = iter(
            [
                {"results": [{"user": {"username": "a"}}], "has_more": True},
                {"results": [{"user": {"username": "b"}}], "has_more": True},
                {"results": [{"user": {"username": "c"}}], "has_more": False},
            ]
        )
        stub_urls: list[str] = []

        def spy(url: str, **kw: object) -> dict:
            stub_urls.append(url)
            return next(pages)

        with patch.object(dacli, "http_json", side_effect=spy), patch("time.sleep"):
            dacli._list_watched_via_friends("tok", "me", True, delay=0)

        offsets = [int(u.split("offset=")[1].split("&")[0]) for u in stub_urls]
        assert offsets == [0, FRIENDS_PAGE_CAP, FRIENDS_PAGE_CAP * 2]
        assert all(f"limit={FRIENDS_PAGE_CAP}" in u for u in stub_urls)

    def test_no_sleep_after_the_final_page(self):
        """The rate-limit pause belongs between requests, not after the
        last one — it is dead wall-clock on every `sync watched`.
        """
        pages = iter(
            [
                {"results": [{"user": {"username": "alice"}}], "has_more": True},
                {"results": [{"user": {"username": "bob"}}], "has_more": False},
            ]
        )
        with patch.object(dacli, "http_json", side_effect=lambda *a, **k: next(pages)):
            with patch("time.sleep") as slept:
                dacli._list_watched_via_friends("tok", "me", True, delay=1.0)
        assert slept.call_count == 1, (
            f"slept {slept.call_count} times for 2 pages; expected 1 (between them)"
        )
