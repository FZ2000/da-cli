"""Seven constants were defined, exported, cited in the docs — and unused.

Each one had its value written out a second time as a bare literal at the
call site it was named for, so the constant documented the behaviour while
the literal produced it. `docs/commands/sync.md` is the sharp end: its flag
tables cite `FEED_PAGE_CAP` and `GALLERY_PAGE_CAP` by name as the source of
the clamp, and `tools/check_doc_references.py` verifies those names still
resolve — but nothing checked that the code consulted them.

Editing the constant is the change a maintainer would make. These tests
make that change take effect, by rebinding the constant where the call
site reads it and asserting the behaviour moves with it. A literal cannot
pass them.

Note the import style: submodules do `from .constants import X`, a value
import, so the binding that matters is the submodule's own — patch
`dacli.sync.FEED_PAGE_CAP`, not `dacli.constants.FEED_PAGE_CAP`.

Every rebind passes `raising=False` on purpose. Against the unwired code
the name is absent from the submodule, and a raising setattr would fail
with AttributeError — which proves only that the import is missing, not
that the literal is still in charge. Creating the attribute instead lets
the unwired code run to completion and fail on the behaviour assertion,
which is the claim actually being made.
"""

from __future__ import annotations

import argparse
import urllib.parse
from unittest.mock import patch

import pytest

import dacli
from dacli.constants import (
    AUTH_DEFAULT_PORT,
    DEST_FREE_SPACE_FAIL_GIB,
    DEST_FREE_SPACE_WARN_GIB,
    FEED_PAGE_CAP,
    GALLERY_PAGE_CAP,
    METADATA_BATCH_SIZE,
    SHORT_FAST_PATH_DELAY_S,
)


def _sync_args(cmd: str, **overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "limit": 24,
        "mature": True,
        "time_budget": 540,
        "delay_api": 0.0,
        "delay_image": 0.0,
        "jitter": 0.0,
        "offset": 0,
    }
    if cmd == "artist":
        base["artist"] = "alice"
    base.update(overrides)
    return argparse.Namespace(**base)


def _limits_of(urls: list[str], endpoint: str) -> list[int]:
    return [
        int(urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["limit"][0])
        for u in urls
        if endpoint in u
    ]


class TestThePageCapsClampTheRequest:
    """`--limit 999` must come back down to whatever the constant says."""

    def test_the_feed_cap_is_the_constant(self, authed_with_destination, monkeypatch):
        monkeypatch.setattr(dacli.sync, "FEED_PAGE_CAP", 7, raising=False)
        urls: list[str] = []

        def spy(url: str, *a: object, **kw: object) -> dict:
            urls.append(url)
            return {"results": [], "has_more": False}

        with patch.object(dacli, "http_json", side_effect=spy), patch("time.sleep"):
            dacli.cmd_sync_feed(_sync_args("feed", limit=999))

        assert _limits_of(urls, "deviantsyouwatch") == [7], (
            f"feed requested {_limits_of(urls, 'deviantsyouwatch')}; "
            "the clamp is not reading FEED_PAGE_CAP"
        )

    def test_the_gallery_cap_is_the_constant(self, authed_with_destination, monkeypatch):
        monkeypatch.setattr(dacli.sync, "GALLERY_PAGE_CAP", 5, raising=False)
        urls: list[str] = []

        def spy(url: str, *a: object, **kw: object) -> dict:
            urls.append(url)
            return {"results": [], "has_more": False}

        with patch.object(dacli, "http_json", side_effect=spy), patch("time.sleep"):
            dacli.cmd_sync_artist(_sync_args("artist", limit=999))

        assert _limits_of(urls, "gallery/all") == [5], (
            f"gallery requested {_limits_of(urls, 'gallery/all')}; "
            "the clamp is not reading GALLERY_PAGE_CAP"
        )

    @pytest.mark.parametrize(
        ("constant", "expected"),
        [("FEED_PAGE_CAP", FEED_PAGE_CAP), ("GALLERY_PAGE_CAP", GALLERY_PAGE_CAP)],
    )
    def test_the_shipped_values_are_what_the_docs_say(self, constant, expected):
        """docs/commands/sync.md states these numbers in prose next to the
        constant name. The reference check only proves the name resolves,
        so a value change would leave the prose quietly wrong.
        """
        import re
        from pathlib import Path

        doc = (Path(__file__).resolve().parent.parent / "docs/commands/sync.md").read_text()
        row = next(line for line in doc.splitlines() if f"`{constant}`" in line)
        assert re.search(rf"\b{expected}\b", row), (
            f"{constant} is {expected} but sync.md's row reads: {row.strip()}"
        )


class TestTheMetadataBatchSizeChunksTheCall:
    def test_chunk_size_is_the_constant(self, authed_with_destination, monkeypatch):
        """/deviation/metadata caps at 50 ids. Requesting more is a 400 for
        the whole page, so this is the constant whose drift is loudest —
        and it was written out twice, once for the step and once for the
        slice, which is two chances to change only one.
        """
        monkeypatch.setattr(dacli.sync, "METADATA_BATCH_SIZE", 3, raising=False)
        devs = [
            {
                "deviationid": f"D{i:03d}",
                "title": f"t{i}",
                "author": {"username": "alice"},
                "content": {"src": "https://cdn.example/x.jpg"},
                "is_downloadable": False,
            }
            for i in range(7)
        ]
        batches: list[int] = []

        def spy(url: str, *a: object, **kw: object) -> dict:
            if "deviation/metadata" in url:
                batches.append(url.count("deviationids[]="))
                return {"metadata": []}
            off = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["offset"][0])
            return {"results": devs, "has_more": False} if off == 0 else {"results": []}

        with (
            patch.object(dacli, "http_json", side_effect=spy),
            patch.object(dacli, "authed_http_json", side_effect=spy),
            patch.object(dacli, "http_bytes", return_value=b"IMG"),
            patch("time.sleep"),
        ):
            dacli.cmd_sync_feed(_sync_args("feed"))

        assert batches == [3, 3, 1], f"7 ids chunked as {batches}, expected [3, 3, 1]"

    def test_the_shipped_value_matches_the_api_cap(self):
        """The control on the value itself: DA rejects >50 outright."""
        assert METADATA_BATCH_SIZE == 50


class TestTheFastPathDelayIsTheConstant:
    def test_an_all_known_page_sleeps_the_short_delay(self, authed_with_destination, monkeypatch):
        """One API call was made, not the usual call-plus-metadata, so the
        full `--delay-api` gap is not warranted. The shortened value was a
        bare 1.0 inside a `min()`.
        """
        monkeypatch.setattr(dacli.sync, "SHORT_FAST_PATH_DELAY_S", 0.125, raising=False)
        dev = {
            "deviationid": "KNOWN-1",
            "title": "t",
            "author": {"username": "alice"},
            "content": {"src": "https://cdn.example/x.jpg"},
        }
        # The fast path is chosen when every id on the page is already
        # known. Reporting that directly keeps this test about the delay;
        # index_has is strict about the file being on disk, and building
        # a fully-synced folder here would only test the index.
        monkeypatch.setattr(dacli.sync, "index_filter_known", set)

        def feed(url: str, *a: object, **kw: object) -> dict:
            # Page 0 is entirely known, which is the fast path. Page 1 is
            # empty so the walk ends without a second fast-path sleep.
            offsets = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("offset")
            if not offsets:
                return {}
            return {"results": [dev], "has_more": True} if offsets[0] == "0" else {"results": []}

        with (
            patch.object(dacli, "http_json", side_effect=feed),
            patch.object(dacli, "authed_http_json", side_effect=feed),
            patch("time.sleep") as slept,
        ):
            dacli.cmd_sync_feed(_sync_args("feed", delay_api=99.0))

        assert 0.125 in [c.args[0] for c in slept.call_args_list], (
            f"slept {[c.args[0] for c in slept.call_args_list]}; "
            "the fast path is not reading SHORT_FAST_PATH_DELAY_S"
        )

    def test_it_is_a_floor_not_an_override(self):
        """The control: `min()`, so an explicitly shorter --delay-api wins.
        Someone reading the constant alone might "fix" it to a plain
        assignment and slow down every fast page.
        """
        assert SHORT_FAST_PATH_DELAY_S == 1.0


class TestTheFreeSpaceThresholdsAreTheConstants:
    @pytest.mark.parametrize(
        ("gib", "level"),
        [(0.5, "fail"), (3.0, "warn"), (99.0, "ok")],
    )
    def test_each_band_is_reported(self, authed_with_destination, gib, level):
        import collections

        usage = collections.namedtuple("usage", "total used free")
        with patch("shutil.disk_usage", return_value=usage(0, 0, int(gib * (1024**3)))):
            findings = dacli.commands.diagnose._diagnose_checks()
        space = [f for f in findings if "free space" in f[2] or "GiB free" in f[2]]
        assert space, "diagnose reported nothing about free space"
        assert space[0][0] == level, f"{gib} GiB reported as {space[0][0]}, expected {level}"

    def test_moving_the_threshold_moves_the_verdict(self, authed_with_destination, monkeypatch):
        """The discriminator. 3 GiB is a warn by default; raise the fail
        threshold above it and the same 3 GiB must become a fail.
        """
        import collections

        usage = collections.namedtuple("usage", "total used free")
        monkeypatch.setattr(
            dacli.commands.diagnose, "DEST_FREE_SPACE_FAIL_GIB", 10.0, raising=False
        )
        monkeypatch.setattr(
            dacli.commands.diagnose, "DEST_FREE_SPACE_WARN_GIB", 20.0, raising=False
        )
        with patch("shutil.disk_usage", return_value=usage(0, 0, 3 * (1024**3))):
            findings = dacli.commands.diagnose._diagnose_checks()
        space = [f for f in findings if "GiB free" in f[2] or "free space" in f[2]]
        assert space[0][0] == "fail", (
            f"3 GiB under a 10 GiB fail threshold reported {space[0][0]}; "
            "the bands are not reading the constants"
        )

    def test_the_shipped_values_match_the_documented_bands(self):
        """docs/commands/maintenance.md states both numbers."""
        from pathlib import Path

        doc = (Path(__file__).resolve().parent.parent / "docs/commands/maintenance.md").read_text()
        assert f"{DEST_FREE_SPACE_WARN_GIB:g} GiB" in doc
        assert f"{DEST_FREE_SPACE_FAIL_GIB:g} GiB" in doc


class TestTheDefaultRedirectPortIsTheConstant:
    """This one was a live NameError waiting to happen.

    Both call sites are only reached by the interactive OAuth flow, which
    every test stubs, so wiring them wrong left the whole suite green.
    Ruff and mypy caught the undefined name; nothing else would have.
    """

    def test_auth_and_diagnose_agree_on_the_default(self, authed_with_destination):
        findings = dacli.commands.diagnose._diagnose_checks()
        uri = next(f[2] for f in findings if f[2].startswith("redirect_uri:"))
        assert f":{AUTH_DEFAULT_PORT}/" in uri, (
            f"diagnose reports {uri!r}, which does not use AUTH_DEFAULT_PORT"
        )

    def test_moving_the_constant_moves_the_reported_default(
        self, authed_with_destination, monkeypatch
    ):
        monkeypatch.setattr(dacli.commands.diagnose, "AUTH_DEFAULT_PORT", 9999, raising=False)
        findings = dacli.commands.diagnose._diagnose_checks()
        uri = next(f[2] for f in findings if f[2].startswith("redirect_uri:"))
        assert ":9999/" in uri, f"diagnose still reports {uri!r} after the constant moved"

    def test_the_configured_value_still_wins(self, authed_with_destination):
        """The control: the constant is a fallback, not an override."""
        dacli.set_config_field("redirect_uri", "https://localhost:1234/")
        findings = dacli.commands.diagnose._diagnose_checks()
        uri = next(f[2] for f in findings if f[2].startswith("redirect_uri:"))
        assert ":1234/" in uri
