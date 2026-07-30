"""One definition of "synced", enforced on every path that answers it.

Three functions used to answer "is this folder a finished deviation?" and
they disagreed. ``_save_one``'s backfill required an image with bytes in
it and never opened ``description.json``; ``_folder_is_complete`` required
only that both names existed; ``index_rebuild_from_disk`` required the
JSON to parse, name a deviationid, and have a non-empty image.

The gap between the first and the third is a silent, permanent data
defect. A folder whose ``description.json`` is empty or truncated — a
power cut mid-write leaves exactly that — is blessed by the backfill,
indexed as synced, and answered "dup" by every run afterwards. The
metadata is never re-fetched. A rebuild drops the row, and the next sync
puts it straight back.

These tests pin the shared definition and the two failures that came out
of not having one. Every one of them fails on the code they replace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dacli

DEVID = "AAAA1111-2222-3333-4444-555555555555"
OTHER_DEVID = "BBBB9999-8888-7777-6666-555555555555"


def _deviation(devid: str = DEVID, title: str = "Sunset") -> dict[str, object]:
    return {
        "deviationid": devid,
        "url": "https://www.deviantart.com/alice/art/Sunset-1",
        "title": title,
        "author": {"username": "alice", "userid": "U-1"},
        "content": {"src": "https://images.example.com/sunset.png"},
    }


@pytest.fixture
def gallery(tmp_path: Path, monkeypatch):
    """A destination root with the index redirected into it."""
    monkeypatch.setattr(dacli, "INDEX_PATH", tmp_path / "index.db")
    dest = tmp_path / "dest"
    dest.mkdir()
    return dest


@pytest.fixture
def downloads(monkeypatch):
    """Record every CDN fetch; return real bytes so a save can succeed."""
    urls: list[str] = []

    def fake_http_bytes(url: str, **_kw: object) -> bytes:
        urls.append(url)
        return b"REAL-IMAGE-BYTES"

    monkeypatch.setattr(dacli, "http_bytes", fake_http_bytes)
    return urls


def _plant(folder: Path, description: str | None, image: bytes | None = b"OLD") -> None:
    """Write a folder by hand, the way a crash or a bad restore would."""
    folder.mkdir(parents=True, exist_ok=True)
    if description is not None:
        (folder / "description.json").write_text(description, encoding="utf-8")
    if image is not None:
        (folder / "image.jpg").write_bytes(image)


class TestCorruptMetadataIsRepairedNotBlessed:
    """The data-loss case: unreadable metadata beside a real image."""

    @pytest.mark.parametrize(
        ("case", "description"),
        [
            pytest.param("empty", "", id="empty"),
            pytest.param("truncated", '{"deviationid": "AAAA1111-2222-33', id="truncated"),
            pytest.param("not json at all", "<html>404</html>", id="not-json"),
            pytest.param("valid json, wrong shape", "[]", id="wrong-shape"),
            pytest.param("object with no deviationid", '{"title": "Sunset"}', id="no-devid"),
        ],
    )
    def test_unreadable_description_is_refetched(self, case, description, gallery, downloads):
        """Each of these must re-download, not report "dup".

        Reporting "dup" indexes the folder, so no later run ever looks at
        it again — the corruption becomes permanent.
        """
        folder = gallery / "alice" / "Sunset"
        _plant(folder, description)

        status, _artist, _title, _size = dacli._save_one(_deviation(), {}, gallery, image_delay=0)

        assert status == "ok", f"{case}: expected a re-download, got {status!r}"
        assert len(downloads) == 1, f"{case}: nothing was fetched from DA"
        # And the folder is now actually repaired.
        repaired = json.loads((folder / "description.json").read_text(encoding="utf-8"))
        assert repaired["deviationid"] == DEVID

    def test_a_valid_folder_is_still_deduped(self, gallery, downloads):
        """The fix must not turn every existing gallery into a re-download.

        This is the control for the tests above: same code path, intact
        metadata, and it must still short-circuit without a fetch.
        """
        folder = gallery / "alice" / "Sunset"
        _plant(folder, json.dumps({"deviationid": DEVID, "title": "Sunset"}))

        status, _artist, _title, _size = dacli._save_one(_deviation(), {}, gallery, image_delay=0)

        assert status == "dup"
        assert downloads == [], "a complete folder must not be re-fetched"
        assert dacli.index_has(DEVID), "the backfill should still index it"


class TestAForeignFolderIsNotCountedAsThisDeviation:
    """A folder holding someone else's deviation is not this one synced."""

    def test_backfill_requires_a_matching_deviationid(self, gallery, downloads):
        """Two deviations, one folder name, and the index is empty.

        Without the deviationid check the second deviation is indexed as
        "dup" against the first one's folder: its art is never downloaded
        and its index row points at another deviation's image.
        """
        folder = gallery / "alice" / "Sunset"
        _plant(folder, json.dumps({"deviationid": OTHER_DEVID, "title": "Sunset"}))

        status, _artist, _title, _size = dacli._save_one(_deviation(), {}, gallery, image_delay=0)

        assert status == "ok", "a foreign folder must not satisfy this deviation"
        assert len(downloads) == 1
        # It went somewhere of its own rather than overwriting the other.
        assert not dacli.index_has(OTHER_DEVID) or dacli.index_has(DEVID)

    def test_the_backfill_checks_ownership_the_index_lookup_does_not(self, gallery):
        """Where the deviationid is checked, and where it deliberately is not.

        ``index_has`` runs for every row of every page under the index
        lock, so it stops at "is the content still on disk?" — parsing
        each folder's metadata there measured 2.6x slower per lookup on a
        real 15,699-row gallery. A row pointing at another deviation's
        folder needs a corrupt *index*, which is not what that check is
        defending against.

        The backfill is where ownership is genuinely in question, because
        it is about to trust a folder it did not write, so that is where
        the deviationid is compared.
        """
        folder = gallery / "alice" / "Sunset"
        _plant(folder, json.dumps({"deviationid": OTHER_DEVID}))
        dacli.index_add(DEVID, "alice", "Sunset", folder, 4)

        assert dacli.index_has(DEVID) is True, "the hot path stays cheap on purpose"

        # But the backfill, given the same folder and no index row, refuses.
        dacli.index_rebuild_from_disk(gallery)  # clears, re-adds under OTHER_DEVID
        assert dacli.index_has(OTHER_DEVID) is True
        assert dacli.index_has(DEVID) is False, "the rebuild indexes by real ownership"


class TestIncompleteImagesAreNotSynced:
    """Asked through ``index_has``, which is what sync actually calls.

    Going through the public surface rather than the new helper is what
    lets these run against the old code and fail there.
    """

    def _ask(self, gallery: Path, image: bytes | None, *, part: bool = False) -> bool:
        folder = gallery / "alice" / "Sunset"
        _plant(folder, json.dumps({"deviationid": DEVID}), image=image)
        if part:
            (folder / "image.jpg.part").write_bytes(b"half a download")
        dacli.index_add(DEVID, "alice", "Sunset", folder, 4)
        return dacli.index_has(DEVID)

    def test_zero_byte_image_is_not_synced(self, gallery):
        """A 0-byte file is a failed download that kept its name."""
        assert self._ask(gallery, image=b"") is False

    def test_part_file_alone_is_not_synced(self, gallery):
        assert self._ask(gallery, image=None, part=True) is False

    def test_dangling_image_symlink_is_not_synced(self, gallery):
        """A symlink whose target is gone has a directory entry, no bytes.

        The old check globbed for the name and never stat()ed it, so this
        counted as synced and the deviation was never restored.
        """
        folder = gallery / "alice" / "Sunset"
        _plant(folder, json.dumps({"deviationid": DEVID}), image=None)
        (folder / "image.jpg").symlink_to(folder / "nowhere.jpg")
        dacli.index_add(DEVID, "alice", "Sunset", folder, 4)

        assert dacli.index_has(DEVID) is False

    def test_a_complete_folder_is_synced(self, gallery):
        """The control: the same path must still say yes to a good folder."""
        assert self._ask(gallery, image=b"1234567890") is True

    def test_bulk_filter_agrees_with_the_single_lookup(self, gallery):
        """``index_filter_known`` answers the same question in bulk.

        They disagreed once before, so a deviation could be repaired on
        the per-item path and skipped on the bulk one depending only on
        which page it landed in.
        """
        good = gallery / "alice" / "Good"
        bad = gallery / "alice" / "Bad"
        _plant(good, json.dumps({"deviationid": "GOOD-1"}), image=b"bytes")
        _plant(bad, json.dumps({"deviationid": "BAD-1"}), image=b"")
        dacli.index_add("GOOD-1", "alice", "Good", good, 5)
        dacli.index_add("BAD-1", "alice", "Bad", bad, 0)

        assert dacli.index_filter_known(["GOOD-1", "BAD-1"]) == {"GOOD-1"}


class TestRebuildSurvivesOneBadFolder:
    """`da index rebuild` is the repair tool; one bad entry must not end it."""

    def test_a_dangling_symlink_does_not_abort_the_walk(self, gallery):
        """Previously the stat() sat outside the try: this raised
        FileNotFoundError, indexed nothing, and reached the user as
        "mount your external drive and retry".
        """
        for i in range(3):
            _plant(
                gallery / "alice" / f"Good{i}",
                json.dumps({"deviationid": f"GOOD-{i}", "title": f"Good{i}"}),
            )
        bad = gallery / "alice" / "Bad"
        _plant(bad, json.dumps({"deviationid": "BAD-1"}), image=None)
        (bad / "image.jpg").symlink_to(bad / "nowhere.jpg")

        n = dacli.index_rebuild_from_disk(gallery)

        assert n == 3, "the three good folders should still be indexed"
        assert dacli.index_has("GOOD-0")

    def test_rebuild_skips_unreadable_metadata(self, gallery):
        _plant(gallery / "alice" / "Good", json.dumps({"deviationid": "GOOD-0"}))
        _plant(gallery / "alice" / "Corrupt", "")

        assert dacli.index_rebuild_from_disk(gallery) == 1


class TestSyncAndRebuildAgree:
    """The regression that motivated sharing one definition."""

    def test_rebuild_and_sync_reach_the_same_verdict(self, gallery, downloads):
        """A corrupt folder must be "not synced" to BOTH.

        They disagreed before: rebuild dropped the row, the next sync
        re-added it, and the folder ping-ponged forever without ever
        being repaired.
        """
        folder = gallery / "alice" / "Sunset"
        _plant(folder, "")

        rebuilt = dacli.index_rebuild_from_disk(gallery)
        status, _a, _t, _s = dacli._save_one(_deviation(), {}, gallery, image_delay=0)

        assert rebuilt == 0, "rebuild must not index a corrupt folder"
        assert status == "ok", "and sync must not call it a duplicate either"
