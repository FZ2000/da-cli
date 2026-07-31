"""Tests for sync-side helpers: _resolve_folder, _save_one, _fetch_metadata_batch."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import dacli

from .conftest import describe_folder as _describe


# ---------------------------------------------------------------------------
# _resolve_folder
# ---------------------------------------------------------------------------
class TestResolveFolder:
    def test_clean_name_returns_base(self, tmp_path: Path, sample_deviation):
        artist, title, folder = dacli._resolve_folder(sample_deviation, tmp_path, None)
        assert artist == "sample-artist"
        assert title == "Sample_Title_With_weird_chars"  # safe_filename'd
        assert folder == tmp_path / artist / title

    def test_no_existing_folder(self, tmp_path: Path, sample_deviation):
        _, _, folder = dacli._resolve_folder(sample_deviation, tmp_path, None)
        assert not folder.exists()

    def test_same_deviationid_returns_same_folder(self, tmp_path: Path, sample_deviation):
        # First save creates folder
        _, _, folder = dacli._resolve_folder(sample_deviation, tmp_path, None)
        folder.mkdir(parents=True)
        (folder / "description.json").write_text(
            json.dumps({"deviationid": sample_deviation["deviationid"]})
        )
        # Second resolve with the same deviationid → same folder, no suffix
        _, _, folder2 = dacli._resolve_folder(sample_deviation, tmp_path, None)
        assert folder == folder2
        assert "--" not in folder2.name

    def test_different_deviationid_collision_gets_suffix(self, tmp_path: Path, sample_deviation):
        # Set up existing folder with a DIFFERENT deviationid
        _artist, title, base = dacli._resolve_folder(sample_deviation, tmp_path, None)
        base.mkdir(parents=True)
        (base / "description.json").write_text(json.dumps({"deviationid": "OTHERID-1234"}))
        # Now resolve our sample (different id) → should get a suffix
        _, _, folder = dacli._resolve_folder(sample_deviation, tmp_path, None)
        assert folder != base
        assert folder.parent == base.parent
        assert folder.name.startswith(title + "--")
        # Suffix is first 8 chars of devid (lowercase, before first dash)
        assert folder.name.endswith("abcdef12")

    def test_corrupt_existing_description_does_not_crash(self, tmp_path: Path, sample_deviation):
        # If the existing description.json is invalid JSON, we cannot tell
        # whether it's the same deviation — fall through to base path.
        _, _, base = dacli._resolve_folder(sample_deviation, tmp_path, None)
        base.mkdir(parents=True)
        (base / "description.json").write_text("not json")
        _, _, folder = dacli._resolve_folder(sample_deviation, tmp_path, None)
        # No collision detected → use base
        assert folder == base

    def test_fallback_artist_when_author_missing(self, tmp_path: Path):
        d = {"deviationid": "X-1", "title": "T"}
        artist, _title, folder = dacli._resolve_folder(d, tmp_path, "fallback-name")
        assert artist == "fallback-name"
        assert folder == tmp_path / "fallback-name" / "T"


# ---------------------------------------------------------------------------
# _save_one
# ---------------------------------------------------------------------------
class TestSaveOne:
    def test_writes_description_and_image(self, tmp_path: Path, sample_deviation, sample_metadata):
        md_by_id = {sample_deviation["deviationid"]: sample_metadata}
        with patch.object(dacli, "http_bytes", return_value=b"\x89PNG_fake"):
            with patch("time.sleep"):
                status, _, _, size = dacli._save_one(sample_deviation, md_by_id, tmp_path)
        assert status == "ok"
        assert size == len(b"\x89PNG_fake")

        # Folder + both files
        _artist, _title, folder = dacli._resolve_folder(sample_deviation, tmp_path, None)
        assert (folder / "description.json").exists()
        # CDN URL ends in .png → image.png
        assert (folder / "image.png").exists()
        assert (folder / "image.png").read_bytes() == b"\x89PNG_fake"

    def test_idempotent_skip(self, tmp_path: Path, sample_deviation, sample_metadata):
        md_by_id = {sample_deviation["deviationid"]: sample_metadata}
        # Run once
        with patch.object(dacli, "http_bytes", return_value=b"X"), patch("time.sleep"):
            dacli._save_one(sample_deviation, md_by_id, tmp_path)
        # Run again — should report dup, skip image fetch
        with patch.object(dacli, "http_bytes") as h, patch("time.sleep"):
            status, _, _, size = dacli._save_one(sample_deviation, md_by_id, tmp_path)
        assert status == "dup"
        assert size == 0
        h.assert_not_called()

    def test_part_file_not_treated_as_complete(
        self, tmp_path: Path, sample_deviation, sample_metadata
    ):
        # Pre-create description.json + a stale .part image (no real image yet)
        _artist, _title, folder = dacli._resolve_folder(sample_deviation, tmp_path, None)
        folder.mkdir(parents=True)
        (folder / "description.json").write_text(
            json.dumps({"deviationid": sample_deviation["deviationid"]})
        )
        (folder / "image.png.part").write_bytes(b"half-written")
        # _save_one should NOT consider this "done"
        md_by_id: dict[str, dict[str, object]] = {sample_deviation["deviationid"]: {}}
        with patch.object(dacli, "http_bytes", return_value=b"PNGfull"), patch("time.sleep"):
            status, _, _, _ = dacli._save_one(sample_deviation, md_by_id, tmp_path)
        assert status == "ok"
        # Final image.png exists, .part should be gone (atomic rename)
        assert (folder / "image.png").exists()
        assert (folder / "image.png").read_bytes() == b"PNGfull"
        assert not (folder / "image.png.part").exists()

    def test_no_image_returns_noimg(self, tmp_path: Path, sample_metadata):
        d = {
            "deviationid": "X-1",
            "title": "Lit",
            "author": {"username": "writer"},
            # no content / no preview → literature
        }
        with patch("time.sleep"):
            status, _, _, _ = dacli._save_one(d, {"X-1": sample_metadata}, tmp_path)
        assert status == "noimg"
        # description still saved
        assert (tmp_path / "writer" / "Lit" / "description.json").exists()

    def test_image_failure_returns_fail(self, tmp_path: Path, sample_deviation):
        with patch.object(dacli, "http_bytes", side_effect=RuntimeError("network gone")):
            with patch("time.sleep"):
                status, _, _, _ = dacli._save_one(sample_deviation, {}, tmp_path)
        assert status.startswith("fail:RuntimeError")

    def test_default_extension_jpg_when_url_unrecognized(self, tmp_path: Path):
        d = {
            "deviationid": "X-1",
            "title": "T",
            "author": {"username": "a"},
            "content": {"src": "https://cdn.example.com/anonymous-blob"},
        }
        with patch.object(dacli, "http_bytes", return_value=b"X"), patch("time.sleep"):
            dacli._save_one(d, {}, tmp_path)
        assert (tmp_path / "a" / "T" / "image.jpg").exists()


# ---------------------------------------------------------------------------
# _fetch_metadata_batch
# ---------------------------------------------------------------------------
class TestFetchMetadataBatch:
    def test_returns_keyed_by_deviationid(self, json_response_factory):
        # _fetch_metadata_batch takes (ids, cfg, state, mature) post-refactor;
        # routes through authed_http_json which we mock at the http_json layer
        # below (cfg/state are only used for the on-401 force_refresh path).
        ids = ["A", "B", "C"]
        cfg = {"client_id": "12345"}
        state = {"access_token": "T", "expires_at": 9999999999, "refresh_token": "RT"}
        with patch.object(
            dacli,
            "http_json",
            return_value={
                "metadata": [
                    {"deviationid": "A", "title": "ta"},
                    {"deviationid": "B", "title": "tb"},
                ]
            },
        ):
            md = dacli._fetch_metadata_batch(ids, cfg, state, mature=True)
        assert md["A"]["title"] == "ta"
        assert md["B"]["title"] == "tb"
        assert "C" not in md  # API may omit some

    def test_chunks_in_50s(self):
        ids = [f"D-{i}" for i in range(120)]
        captured_calls: list[str] = []

        def fake_json(url, **_kw):
            captured_calls.append(url)
            return {"metadata": []}

        cfg = {"client_id": "12345"}
        state = {"access_token": "T", "expires_at": 9999999999, "refresh_token": "RT"}
        with patch.object(dacli, "http_json", side_effect=fake_json):
            dacli._fetch_metadata_batch(ids, cfg, state, mature=False)
        # 120 ids → 50 + 50 + 20 = 3 calls
        assert len(captured_calls) == 3


class TestCollisionSuffixIsItselfUnique:
    """The disambiguating suffix must actually disambiguate.

    Two deviations sharing artist and title get the second one a
    `title--<short>` folder, where `<short>` was the first 8 characters of
    the deviationid before the first dash. Two deviations sharing artist,
    title AND that prefix therefore landed on the SAME suffixed folder:
    the second overwrote the first's description.json and image, and the
    index pointed both ids at one folder.

    With real DeviantArt UUIDs an 8-hex-char collision inside one
    artist+title group is remote, so this is a small bug — but its failure
    mode is silent loss of art the user believes was downloaded, and the
    fix is to widen the suffix rather than hope.
    """

    def _dev(self, devid, title="Sunset", artist="alice"):
        return {"deviationid": devid, "title": title, "author": {"username": artist}}

    def _claim(self, folder, devid):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "description.json").write_text(json.dumps({"deviationid": devid}))

    def test_shared_prefix_gets_distinct_folders(self, tmp_path):
        ids = [f"ABCD1234-{n}{n}{n}{n}-1111-1111-111111111111" for n in (1, 2, 3)]
        folders = []
        for devid in ids:
            _, _, folder = dacli.sync._resolve_folder(self._dev(devid), tmp_path, None)
            self._claim(folder, devid)
            folders.append(folder)
        assert len(set(folders)) == 3, (
            f"three deviations sharing artist, title and id prefix got "
            f"{len(set(folders))} folders: {[f.name for f in folders]}"
        )

    def test_resolution_is_stable(self, tmp_path):
        """Re-running a sync must not move art that is already on disk."""
        ids = [f"ABCD1234-{n}{n}{n}{n}-1111-1111-111111111111" for n in (1, 2)]
        first = []
        for devid in ids:
            _, _, folder = dacli.sync._resolve_folder(self._dev(devid), tmp_path, None)
            self._claim(folder, devid)
            first.append(folder)
        second = [dacli.sync._resolve_folder(self._dev(devid), tmp_path, None)[2] for devid in ids]
        assert first == second

    def test_existing_short_suffix_is_kept(self, tmp_path):
        """Backward compatibility: an install that already has
        `title--abcd1234` for this deviation must keep using it."""
        first_id = "AAAA1111-1111-1111-1111-111111111111"
        second_id = "BBBB2222-2222-2222-2222-222222222222"
        _, _, base = dacli.sync._resolve_folder(self._dev(first_id), tmp_path, None)
        self._claim(base, first_id)

        _, _, folder = dacli.sync._resolve_folder(self._dev(second_id), tmp_path, None)
        assert folder.name == "Sunset--bbbb2222", (
            f"the ordinary one-collision case changed shape: {folder.name}"
        )

    def test_unrelated_titles_are_untouched(self, tmp_path):
        _, _, a = dacli.sync._resolve_folder(
            self._dev("AAAA1111-1111-1111-1111-111111111111", title="One"), tmp_path, None
        )
        _, _, b = dacli.sync._resolve_folder(
            self._dev("BBBB2222-2222-2222-2222-222222222222", title="Two"), tmp_path, None
        )
        assert a.name == "One" and b.name == "Two", "unrelated titles were suffixed"


class TestDestinationIsAbsolute:
    """A relative `destination` poisons every row the index writes.

    index_add stores `str(folder)`. With a relative destination those
    rows are cwd-relative, so they stop resolving the moment the next run
    starts from a different directory — and a launchd job starts from
    "/". The self-healing check then treats every one as missing and
    re-downloads the whole gallery.
    """

    def test_relative_destination_is_resolved(self, isolated_paths, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rel").mkdir()
        resolved = dacli.sync._ensure_destination({"destination": "rel"})
        assert resolved.is_absolute(), f"destination stayed relative: {resolved}"
        assert resolved == (tmp_path / "rel").resolve()

    def test_a_symlinked_destination_is_not_rewritten(self, isolated_paths, tmp_path):
        """Making the path absolute must not also canonicalise it.

        `resolve()` follows symlinks. Every index row written before such
        a rewrite records the pre-resolution path, so the lookup misses
        and the entire gallery downloads again — for anyone whose gallery
        lives behind a symlink, and on macOS `/tmp` is one.
        """
        real = tmp_path / "real-gallery"
        real.mkdir()
        link = tmp_path / "my-art"
        link.symlink_to(real)

        got = dacli.sync._ensure_destination({"destination": str(link)})
        assert str(got) == str(link), (
            f"the configured destination was rewritten to {got}; every index row "
            f"recorded under the old path would miss and re-download"
        )

    def test_an_indexed_deviation_stays_reachable(self, isolated_paths, tmp_path):
        """The consequence, stated end to end."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "art"
        link.symlink_to(real)
        folder = _describe(link / "alice" / "Sunset", "D-1")
        dacli.index_add("D-1", "alice", "Sunset", folder, 3)

        dest = dacli.sync._ensure_destination({"destination": str(link)})
        _, _, lookup = dacli.sync._resolve_folder(
            {"deviationid": "D-2", "title": "Sunset", "author": {"username": "alice"}},
            dest,
            None,
        )
        assert lookup.parent == folder.parent, (
            "a sync would look somewhere the index does not know about"
        )

    def test_absolute_destination_is_unchanged(self, isolated_paths, tmp_path):
        dest = tmp_path / "gallery"
        dest.mkdir()
        assert dacli.sync._ensure_destination({"destination": str(dest)}) == dest.resolve()

    def test_tilde_still_expands(self, isolated_paths, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "art").mkdir()
        assert (
            dacli.sync._ensure_destination({"destination": "~/art"}) == (tmp_path / "art").resolve()
        )
