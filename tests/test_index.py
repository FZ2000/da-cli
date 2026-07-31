"""Tests for the SQLite-backed synced-deviation index."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import dacli

from .conftest import describe_folder as _describe


# ---------------------------------------------------------------------------
# index_has / index_add / index_filter_known
# ---------------------------------------------------------------------------
class TestIndexBasics:
    def test_empty_index_has_returns_false(self, isolated_paths):
        assert dacli.index_has("anything") is False
        assert dacli.index_count() == 0

    def test_add_then_has(self, isolated_paths, tmp_path):
        # index_has is self-healing — it reads the indexed folder back.
        # Lay down a description.json naming this deviation, the way a
        # real sync writes one, so the row counts as live (not stale).
        _describe(tmp_path, "D-1")
        (tmp_path / "image.png").write_bytes(b"IMG")
        dacli.index_add("D-1", "alice", "Title", tmp_path, 1234)
        assert dacli.index_has("D-1") is True
        assert dacli.index_has("D-2") is False

    def test_filter_known_returns_intersection(self, isolated_paths, tmp_path):
        # Self-healing requires real folders on disk for rows to count
        # as live. Lay down two description.json files at distinct paths.
        f1 = tmp_path / "f1"
        f1.mkdir()
        _describe(f1, "D-1")
        f2 = tmp_path / "f2"
        f2.mkdir()
        _describe(f2, "D-2")
        dacli.index_add("D-1", "a", "t1", f1, 1)
        dacli.index_add("D-2", "a", "t2", f2, 2)
        result = dacli.index_filter_known(["D-1", "D-2", "D-3"])
        assert result == {"D-1", "D-2"}

    def test_filter_known_empty_input(self, isolated_paths):
        assert dacli.index_filter_known([]) == set()

    def test_count_per_artist(self, isolated_paths, tmp_path):
        dacli.index_add("D-1", "alice", "t", tmp_path, 1)
        dacli.index_add("D-2", "alice", "t", tmp_path, 1)
        dacli.index_add("D-3", "bob", "t", tmp_path, 1)
        assert dacli.index_count() == 3
        assert dacli.index_count(artist="alice") == 2
        assert dacli.index_count(artist="bob") == 1
        assert dacli.index_count(artist="nobody") == 0

    def test_add_is_idempotent(self, isolated_paths, tmp_path):
        dacli.index_add("D-1", "alice", "t", tmp_path, 1)
        dacli.index_add("D-1", "alice", "t", tmp_path, 1)  # same id again
        assert dacli.index_count() == 1

    def test_db_file_is_0600(self, isolated_paths, tmp_path):
        dacli.index_add("D-1", "alice", "t", tmp_path, 1)
        # On macOS/Linux, st_mode & 0o777 should reflect our chmod
        mode = isolated_paths["idx"].stat().st_mode & 0o777
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Self-healing: index_has and index_filter_known auto-drop stale rows
# whose folder vanished from disk.
# ---------------------------------------------------------------------------
class TestIndexSelfHealing:
    def test_index_has_drops_stale_row_when_folder_gone(self, isolated_paths, tmp_path):
        """Operator deletes a folder out from under the index. Next
        `index_has` call must report False (not True), and silently
        clean up the row so future syncs redownload."""
        folder = tmp_path / "alice" / "T"
        folder.mkdir(parents=True)
        _describe(folder, "D-1")
        dacli.index_add("D-1", "alice", "T", folder, 5)
        assert dacli.index_has("D-1") is True
        # rm -rf the folder
        import shutil

        shutil.rmtree(folder)
        # Now the index is lying — folder is gone but row is still there.
        # index_has must detect this and self-heal.
        assert dacli.index_has("D-1") is False
        # And the stale row should be GONE from the table — not just
        # silently filtered. Otherwise the index keeps growing forever.
        assert dacli.index_count() == 0

    def test_filter_known_drops_stale_rows_in_bulk(self, isolated_paths, tmp_path):
        """Page-level `index_filter_known` must also self-heal. Lay down
        3 rows, delete 2 folders, run filter — only the live one
        survives, the 2 stale rows are silently dropped."""
        for i in range(3):
            folder = tmp_path / f"a{i}" / "T"
            folder.mkdir(parents=True)
            _describe(folder, f"D-{i}")
            dacli.index_add(f"D-{i}", f"a{i}", "T", folder, 5)
        # Operator removes D-0 and D-2's folders
        import shutil

        shutil.rmtree(tmp_path / "a0")
        shutil.rmtree(tmp_path / "a2")
        # Filter — only D-1 should be reported as known
        live = dacli.index_filter_known(["D-0", "D-1", "D-2"])
        assert live == {"D-1"}
        # Stale rows auto-deleted from the table
        assert dacli.index_count() == 1

    def test_full_artist_wipe_triggers_redownload_on_next_sync(self, isolated_paths, tmp_path):
        """End-to-end: pre-seed an artist's deviations, rm -rf the
        whole artist folder, then call _save_one for one of those
        deviations. With self-healing index_has, _save_one must NOT
        short-circuit — it must proceed to redownload."""
        from unittest.mock import patch

        artist = "TestArtist"
        title = "Sample_Title_With_weird_chars"  # safe_filename'd
        folder = tmp_path / artist / title
        folder.mkdir(parents=True)
        (folder / "description.json").write_text(json.dumps({"deviationid": "DEV-1"}))
        (folder / "image.png").write_bytes(b"IMG")
        (folder / "image.png").write_bytes(b"old-bytes")
        dacli.index_add("DEV-1", artist, title, folder, 9)
        # Whole-artist wipe
        import shutil

        shutil.rmtree(tmp_path / artist)
        assert not folder.exists()

        # Build a deviation dict matching what was indexed and call _save_one
        d = {
            "deviationid": "DEV-1",
            "url": "u",
            "title": "Sample / Title : With weird chars!",
            "is_mature": False,
            "is_favourited": False,
            "published_time": "1",
            "stats": {},
            "author": {"username": artist, "userid": "U1"},
            "content": {"src": "https://cdn/x.png"},
        }
        with patch.object(dacli, "http_bytes", return_value=b"NEW-PNG"):
            with patch("time.sleep"):
                status, _, _, size = dacli._save_one(d, {}, tmp_path)
        # Must redownload — NOT report dup
        assert status == "ok", f"got {status!r} — index_has didn't self-heal"
        assert (folder / "image.png").exists()
        assert (folder / "image.png").read_bytes() == b"NEW-PNG"
        assert size == 7


# ---------------------------------------------------------------------------
# index_rebuild_from_disk + index_bootstrap_if_empty
# ---------------------------------------------------------------------------
class TestIndexRebuild:
    def _make_synthetic_dest(self, dest: Path, n: int = 3) -> None:
        """Lay out N pretend deviation folders with description.json + image.png."""
        for i in range(n):
            folder = dest / f"artist{i % 2}" / f"title{i}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "description.json").write_text(
                json.dumps(
                    {
                        "deviationid": f"D-{i}",
                        "title": f"Title {i}",
                        "author_username": f"artist{i % 2}",
                    }
                )
            )
            (folder / "image.png").write_bytes(b"\x89PNG fake bytes" * (i + 1))

    def test_rebuild_walks_disk(self, isolated_paths, tmp_path):
        dest = tmp_path / "gallery"
        self._make_synthetic_dest(dest, n=4)
        n = dacli.index_rebuild_from_disk(dest)
        assert n == 4
        assert dacli.index_count() == 4
        assert dacli.index_has("D-0") is True
        assert dacli.index_has("D-3") is True

    def test_rebuild_skips_folders_without_image(self, isolated_paths, tmp_path):
        dest = tmp_path / "gallery"
        folder = dest / "artist" / "title"
        folder.mkdir(parents=True)
        (folder / "description.json").write_text(json.dumps({"deviationid": "D-1"}))
        # No image file → not synced yet → don't index
        n = dacli.index_rebuild_from_disk(dest)
        assert n == 0
        assert dacli.index_has("D-1") is False

    def test_rebuild_skips_part_files_only(self, isolated_paths, tmp_path):
        dest = tmp_path / "gallery"
        folder = dest / "artist" / "title"
        folder.mkdir(parents=True)
        (folder / "description.json").write_text(json.dumps({"deviationid": "D-1"}))
        (folder / "image.png.part").write_bytes(b"half-written")
        # Only a .part file → counts as not yet synced
        n = dacli.index_rebuild_from_disk(dest)
        assert n == 0

    def test_rebuild_skips_corrupt_description(self, isolated_paths, tmp_path):
        dest = tmp_path / "gallery"
        folder = dest / "artist" / "title"
        folder.mkdir(parents=True)
        (folder / "description.json").write_text("not-json")
        (folder / "image.png").write_bytes(b"IMG")
        (folder / "image.png").write_bytes(b"x")
        n = dacli.index_rebuild_from_disk(dest)
        assert n == 0  # unparsable → silently skipped

    def test_rebuild_returns_zero_on_missing_dest(self, isolated_paths, tmp_path):
        assert dacli.index_rebuild_from_disk(tmp_path / "nope") == 0

    def test_rebuild_is_idempotent(self, isolated_paths, tmp_path):
        dest = tmp_path / "gallery"
        self._make_synthetic_dest(dest, n=3)
        n1 = dacli.index_rebuild_from_disk(dest)
        n2 = dacli.index_rebuild_from_disk(dest)
        assert n1 == n2 == 3
        assert dacli.index_count() == 3  # not 6

    def test_bootstrap_runs_when_empty(self, isolated_paths, tmp_path, capsys):
        dest = tmp_path / "gallery"
        self._make_synthetic_dest(dest, n=2)
        n = dacli.index_bootstrap_if_empty(dest)
        assert n == 2
        assert "bootstrap" in capsys.readouterr().out.lower()

    def test_bootstrap_skips_when_populated(self, isolated_paths, tmp_path):
        dest = tmp_path / "gallery"
        self._make_synthetic_dest(dest, n=2)
        dacli.index_add("D-99", "x", "y", tmp_path, 1)  # non-empty
        n = dacli.index_bootstrap_if_empty(dest)
        assert n == 0  # didn't run

    def test_rebuild_is_authoritative_drops_stale_rows(self, isolated_paths, tmp_path):
        """`da index rebuild` must DELETE rows whose folder is gone, not
        just INSERT-OR-REPLACE the live ones. Operator runs `rm -rf
        gallery/some-artist` to free space; rebuild must reflect that."""
        dest = tmp_path / "gallery"
        # Set up: 3 folders on disk, 3 rows in index
        self._make_synthetic_dest(dest, n=3)
        dacli.index_rebuild_from_disk(dest)
        assert dacli.index_count() == 3
        # Operator deletes folder 0 from disk
        import shutil

        shutil.rmtree(dest / "artist0")
        # Re-rebuild — D-0 (and D-2 if it shared artist0) must be GONE
        # from the index, not retained as stale.
        new_count = dacli.index_rebuild_from_disk(dest)
        assert new_count < 3
        # _make_synthetic_dest interleaves artists by i%2; with n=3 that's
        # artist0/[D-0,D-2] + artist1/[D-1]. After deleting artist0, only
        # D-1 should remain.
        assert dacli.index_count() == 1
        assert dacli.index_has("D-1") is True
        assert dacli.index_has("D-0") is False
        assert dacli.index_has("D-2") is False

    def test_rebuild_skips_when_dest_missing(self, isolated_paths, tmp_path):
        """When the destination doesn't exist (external drive unplugged),
        rebuild must NOT wipe the index. Returns 0 and leaves rows alone."""
        # Pre-seed
        dacli.index_add("D-survives", "alice", "T", tmp_path, 1)
        assert dacli.index_count() == 1
        # Rebuild against a missing path
        n = dacli.index_rebuild_from_disk(tmp_path / "this-mount-point-is-gone")
        assert n == 0
        # Index untouched
        assert dacli.index_count() == 1

    def test_bootstrap_exits_cleanly_on_corrupt_index(self, isolated_paths, tmp_path, capsys):
        """Operator should get an actionable single-line error, not a
        worker-pool stack trace, when the index DB is corrupt."""
        # Close any cached connection, then write garbage to the index path.
        dacli._index_close()
        dacli.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.INDEX_PATH.write_bytes(b"this is not a sqlite database, just garbage")
        dest = tmp_path / "gallery"
        dest.mkdir()
        with pytest.raises(SystemExit) as exc:
            dacli.index_bootstrap_if_empty(dest)
        assert exc.value.code == 2
        cap = capsys.readouterr()
        msg = (cap.out + cap.err).lower()
        # The message must direct the operator to the recovery action.
        assert "corrupt" in msg or "rebuild" in msg, f"got: {msg!r}"


# ---------------------------------------------------------------------------
# _save_one integration: index hits short-circuit, misses populate
# ---------------------------------------------------------------------------
class TestSaveOneIndexInteraction:
    def test_save_one_hit_via_index_returns_dup(self, isolated_paths, tmp_path, sample_deviation):
        # Pre-populate the index with this deviation's id, AND lay down
        # a description.json at the indexed path — index_has is now
        # self-healing and the row would otherwise be auto-deleted as
        # "stale" before we got the chance to assert dup.
        indexed_path = tmp_path / "indexed-folder"
        indexed_path.mkdir()
        _describe(indexed_path, sample_deviation["deviationid"])
        dacli.index_add(sample_deviation["deviationid"], "x", "y", indexed_path, 1)
        dest = tmp_path / "dest"
        dest.mkdir()
        status, _, _, _ = dacli._save_one(sample_deviation, {}, dest)
        assert status == "dup"
        # No artist/title tree should have been created — the index hit
        # short-circuits before any disk write.
        assert list(dest.iterdir()) == []

    def test_save_one_disk_hit_backfills_index(self, isolated_paths, tmp_path, sample_deviation):
        # Lay out a folder on disk that satisfies has_desc + has_image,
        # but with the index empty. _save_one should treat as dup AND
        # populate the index.
        from dacli import safe_filename

        artist = sample_deviation["author"]["username"]
        title_safe = safe_filename(sample_deviation["title"])
        folder = tmp_path / artist / title_safe
        folder.mkdir(parents=True)
        (folder / "description.json").write_text(
            json.dumps({"deviationid": sample_deviation["deviationid"]})
        )
        (folder / "image.png").write_bytes(b"\x89PNG real")
        assert not dacli.index_has(sample_deviation["deviationid"])
        status, _, _, _ = dacli._save_one(sample_deviation, {}, tmp_path)
        assert status == "dup"
        # Index now knows about it.
        assert dacli.index_has(sample_deviation["deviationid"])

    def test_save_one_ok_inserts_into_index(
        self, isolated_paths, tmp_path, sample_deviation, monkeypatch
    ):
        monkeypatch.setattr(dacli, "http_bytes", lambda url, **_kw: b"\x89PNGbytes")
        monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
        status, _, _, size = dacli._save_one(sample_deviation, {}, tmp_path)
        assert status == "ok"
        assert size > 0
        assert dacli.index_has(sample_deviation["deviationid"])


# ---------------------------------------------------------------------------
# `da index rebuild` and `da index show` CLI commands
# ---------------------------------------------------------------------------
class TestIndexCommands:
    def test_index_rebuild_via_cli(self, isolated_paths, tmp_path):
        dest = tmp_path / "gallery"
        folder = dest / "alice" / "title"
        folder.mkdir(parents=True)
        (folder / "description.json").write_text(
            json.dumps({"deviationid": "D-1", "author_username": "alice", "title": "T"})
        )
        (folder / "image.png").write_bytes(b"x")
        dacli.set_config_field("destination", str(dest))
        ns = dacli.build_parser().parse_args(["index", "rebuild"])
        ns.func(ns)
        assert dacli.index_has("D-1")

    def test_index_show_empty(self, isolated_paths, capsys):
        ns = dacli.build_parser().parse_args(["index", "show"])
        ns.func(ns)
        captured = capsys.readouterr()
        combined = (captured.out + captured.err).lower()
        assert "no index" in combined or "total rows" in combined

    def test_index_show_populated(self, isolated_paths, tmp_path, capsys):
        dacli.index_add("D-1", "alice", "t", tmp_path, 100)
        dacli.index_add("D-2", "bob", "t", tmp_path, 200)
        ns = dacli.build_parser().parse_args(["index", "show"])
        ns.func(ns)
        out = capsys.readouterr().out
        assert "total rows:" in out
        assert "alice" in out
        assert "bob" in out


# ---------------------------------------------------------------------------
# Durability and the on-disk liveness probe.
#
# The index is what makes a repeat sync cheap. Everything below is a way it
# can quietly stop being true while every existing assertion still passes,
# because the suite only ever reads the index back through the same cached
# connection that wrote it, inside one process.
# ---------------------------------------------------------------------------
class TestIndexDurability:
    def test_writes_survive_closing_the_connection(self, isolated_paths, tmp_path):
        """Rows must be committed, not sitting in an open transaction.

        `_index()` opens SQLite with `isolation_level=None` (autocommit),
        and that keyword is the only thing making writes durable — nothing
        in the codebase calls commit(), and both `_index_close()` and the
        atexit hook call close(), which rolls back an open transaction.

        Every other index test reads back through the same cached
        connection, which sees its own uncommitted work, so the guarantee
        was entirely unpinned: drop the keyword and the index silently
        never persists, while the suite stays green.
        """
        folder = tmp_path / "alice" / "T"
        folder.mkdir(parents=True)
        _describe(folder, "D-persist")
        dacli.index_add("D-persist", "alice", "T", folder, 7)
        assert dacli.index_count() == 1

        # Drop the cached connection: the next call reopens the file, so
        # anything that was never committed is gone.
        dacli._index_close()

        assert dacli.index_count() == 1, "index rows did not survive the connection closing"
        assert dacli.index_has("D-persist") is True

    def test_writes_are_visible_to_a_second_connection(self, isolated_paths, tmp_path):
        """The same guarantee, seen from outside — as the next cron run would."""
        import sqlite3

        folder = tmp_path / "bob" / "T"
        folder.mkdir(parents=True)
        _describe(folder, "D-outside")
        dacli.index_add("D-outside", "bob", "T", folder, 3)

        other = sqlite3.connect(dacli.INDEX_PATH)
        try:
            seen = other.execute("SELECT COUNT(*) FROM synced").fetchone()[0]
        finally:
            other.close()
        assert seen == 1, (
            "a separate connection cannot see the row, so it was never committed; "
            "the next `da sync` process would find an empty index"
        )


class TestIndexLivenessProbe:
    """`index_has` must check that the CONTENT is there, not just the folder.

    The self-healing check exists so a deleted download gets re-fetched.
    Existing tests only ever remove the whole folder, so weakening the
    probe to a directory stat — which is the natural way to "simplify" it —
    passes the suite while making the repair unreachable in the cases that
    actually happen: an interrupted delete, a partial restore from backup,
    a sync tool that recreated the tree but not the files.
    """

    def _indexed(self, tmp_path, devid="D-live"):
        folder = tmp_path / "alice" / "T"
        folder.mkdir(parents=True, exist_ok=True)
        _describe(folder, devid)
        dacli.index_add(devid, "alice", "T", folder, 3)
        return folder

    def test_emptied_folder_is_not_a_hit(self, isolated_paths, tmp_path):
        folder = self._indexed(tmp_path)
        (folder / "description.json").unlink()
        (folder / "image.png").unlink()
        assert folder.exists(), "precondition: the directory itself is still there"

        assert dacli.index_has("D-live") is False, (
            "an empty folder counted as synced, so the deviation would never be re-downloaded"
        )

    def test_stale_row_is_dropped_so_the_next_run_is_cheap(self, isolated_paths, tmp_path):
        folder = self._indexed(tmp_path)
        (folder / "description.json").unlink()
        dacli.index_has("D-live")
        assert dacli.index_count() == 0, "the stale row was left behind"


class TestRebuildIgnoresFailedDownloads:
    """A 0-byte image is a failed download, not a synced item.

    `index_rebuild_from_disk` skips them for the same reason `_save_one`
    refuses to record them: indexing one makes every later sync treat the
    deviation as done, so the truncated file is never repaired. Nothing
    tested it — the existing rebuild fixtures always write non-empty bytes.
    """

    def _folder(self, dest, devid, image_bytes):
        folder = dest / "alice" / devid
        folder.mkdir(parents=True)
        (folder / "description.json").write_text(
            json.dumps({"deviationid": devid, "title": devid, "author_username": "alice"})
        )
        (folder / "image.png").write_bytes(image_bytes)
        return folder

    def test_zero_byte_image_is_not_indexed(self, isolated_paths, tmp_path):
        dest = tmp_path / "gallery"
        self._folder(dest, "D-good", b"\x89PNG real")
        self._folder(dest, "D-truncated", b"")

        n = dacli.index_rebuild_from_disk(dest)

        assert dacli.index_has("D-good") is True
        assert dacli.index_has("D-truncated") is False, (
            "a 0-byte image was indexed as synced; the truncated download would never be retried"
        )
        assert n == 1, f"rebuild counted {n} rows, expected 1"

    def test_zero_byte_image_is_retried_on_the_next_sync(self, isolated_paths, tmp_path):
        """The consequence that matters, stated directly."""
        dest = tmp_path / "gallery"
        self._folder(dest, "D-truncated", b"")
        dacli.index_rebuild_from_disk(dest)
        assert dacli.index_has("D-truncated") is False


class TestDeletingAnImageForcesRedownload:
    """The documented repair procedure has to actually repair something.

    docs/reference/files-on-disk.md tells the user: "To force one to be
    re-downloaded, delete the file and run the sync again." It did not
    work. `index_has` only checked description.json, so the row still
    counted as a hit, sync reported "dup", and the deleted image was
    never restored — the one recovery path the docs offer was a no-op.

    Both the per-item probe and the bulk one are covered: they answer the
    same question, and while only one of them was strict the outcome
    depended on whether the deviation happened to share a page with
    others.
    """

    def _synced(self, tmp_path, devid="D-img"):
        folder = tmp_path / "alice" / "T"
        folder.mkdir(parents=True, exist_ok=True)
        _describe(folder, devid)
        dacli.index_add(devid, "alice", "T", folder, 3)
        return folder

    def test_index_has_reports_missing_image(self, isolated_paths, tmp_path):
        folder = self._synced(tmp_path)
        assert dacli.index_has("D-img") is True
        (folder / "image.png").unlink()
        assert dacli.index_has("D-img") is False, (
            "the deviation still counted as synced, so the deleted image "
            "would never be re-downloaded"
        )

    def test_bulk_filter_reports_missing_image(self, isolated_paths, tmp_path):
        folder = self._synced(tmp_path)
        assert dacli.index_filter_known(["D-img"]) == {"D-img"}
        (folder / "image.png").unlink()
        assert dacli.index_filter_known(["D-img"]) == set(), (
            "the bulk path disagreed with index_has about what is on disk"
        )

    def test_a_part_file_does_not_count_as_the_image(self, isolated_paths, tmp_path):
        folder = self._synced(tmp_path)
        (folder / "image.png").unlink()
        (folder / "image.png.part").write_bytes(b"half")
        assert dacli.index_has("D-img") is False, "a half-written download counted as complete"

    def test_description_alone_is_not_synced(self, isolated_paths, tmp_path):
        folder = self._synced(tmp_path)
        (folder / "image.png").unlink()
        assert (folder / "description.json").exists()
        assert dacli.index_has("D-img") is False


class TestRebuildRefusesToWipeOnAMissingDrive:
    """`index rebuild` must not empty the index when the drive is gone.

    `index_rebuild_from_disk` documents the intent — "when dest is
    missing, do NOT wipe ... operator probably wants to remount, not
    discover that all 14k rows just got nuked" — but the guard was only
    `dest.exists()`, and `cmd_index_rebuild` called `_ensure_destination`,
    which `mkdir`s the path first. On macOS an unmounted drive leaves its
    mount point behind, so "not mounted" arrived at the guard looking like
    "empty", and the whole index was dropped.
    """

    def _populated(self, tmp_path):
        dest = tmp_path / "Volumes" / "ArtDrive"
        for i in range(3):
            f = dest / "alice" / f"t{i}"
            f.mkdir(parents=True)
            (f / "description.json").write_text(json.dumps({"deviationid": f"D{i}"}))
            (f / "image.png").write_bytes(b"IMG")
            dacli.index_add(f"D{i}", "alice", f"t{i}", f, 3)
        return dest

    def test_missing_destination_does_not_wipe(self, isolated_paths, tmp_path):
        dest = self._populated(tmp_path)
        assert dacli.index_count() == 3
        shutil.rmtree(dest)  # unmounted; the parent /Volumes remains

        assert dacli.index_rebuild_from_disk(dest) == 0
        assert dacli.index_count() == 3, "the index was wiped for a drive that is merely unmounted"

    def test_empty_destination_does_not_wipe(self, isolated_paths, tmp_path):
        """The directory existing but holding nothing is the same situation."""
        dest = self._populated(tmp_path)
        shutil.rmtree(dest)
        dest.mkdir(parents=True)  # what _ensure_destination used to do

        assert dacli.index_rebuild_from_disk(dest) == 0
        assert dacli.index_count() == 3, "an empty mount point emptied the index"

    def test_rebuild_still_works_when_content_is_there(self, isolated_paths, tmp_path):
        dest = self._populated(tmp_path)
        # Remove one folder: a real, intentional rebuild must still prune it.
        shutil.rmtree(dest / "alice" / "t2")
        assert dacli.index_rebuild_from_disk(dest) == 2
        assert dacli.index_count() == 2

    def test_empty_destination_with_empty_index_is_fine(self, isolated_paths, tmp_path):
        """Nothing to protect, so no refusal — a first run must not error."""
        dest = tmp_path / "gallery"
        dest.mkdir()
        assert dacli.index_rebuild_from_disk(dest) == 0

    def test_the_command_reports_and_exits_2(self, isolated_paths, tmp_path):
        """ "done: 0 deviations indexed" reads like success; a repair job
        watching the exit code would never notice the drive was absent."""
        dest = self._populated(tmp_path)
        shutil.rmtree(dest)
        dacli.set_config_field("client_id", "X")
        dacli.set_config_field("destination", str(dest))
        ns = dacli.build_parser().parse_args(["index", "rebuild"])
        with pytest.raises(SystemExit) as exc:
            ns.func(ns)
        assert exc.value.code == 2
        assert dacli.index_count() == 3

    def test_the_command_does_not_create_the_destination(self, isolated_paths, tmp_path):
        dest = tmp_path / "Volumes" / "ArtDrive"
        dest.parent.mkdir(parents=True)
        dacli.set_config_field("client_id", "X")
        dacli.set_config_field("destination", str(dest))
        ns = dacli.build_parser().parse_args(["index", "rebuild"])
        ns.func(ns)
        assert not dest.exists(), (
            "rebuild created the destination, which is what defeated the guard"
        )
