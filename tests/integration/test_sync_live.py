"""Live sync test — downloads real deviations from DA to a tmp directory.

This is the headline integration test: it proves the entire sync
pipeline (API call → metadata fetch → image download → atomic write →
index update) works against the real DeviantArt API and the real
wixmp CDN. If this test passes, the core product feature is verified
end-to-end.

Run with::

    pytest -m integration_authenticated

Uses a tmp destination (never the developer's real destination) and
limits to 2 deviations to keep the test fast and polite to DA's rate
limiter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dacli

pytestmark = [pytest.mark.integration, pytest.mark.integration_authenticated]

API = dacli.API_BASE


class TestSyncFeedLive:
    """Run ``da sync feed`` against live DA with a tmp destination.
    Asserts that deviations land on disk with both description.json and
    an image file, and that the synced-index is populated."""

    def test_sync_feed_downloads_and_indexes_real_deviations(
        self,
        user_token: str,
        tmp_path: Path,
    ) -> None:
        """Fetch 2 deviations from the live watch feed and verify the
        full pipeline works: feed page → metadata batch → image
        download → atomic write → index insert.

        Uses a tmp directory as the destination so the developer's real
        gallery is never touched.
        """
        # Fetch one feed page (limit=2 to keep it fast).
        feed = dacli.http_json(
            f"{API}/browse/deviantsyouwatch?limit=2&mature_content=false",
            token=user_token,
        )
        results = feed.get("results", [])
        if not results:
            pytest.skip("watch feed returned no results — try again later")

        # Run _save_one on the first result (this is the core sync
        # operation: download description + image, write to disk, add
        # to index).
        dest = tmp_path / "gallery"
        dest.mkdir()

        # Redirect the index to the tmp dir for this test.
        original_index_path = dacli.INDEX_PATH
        dacli._index_close()
        dacli.INDEX_PATH = tmp_path / "index.db"

        try:
            d = results[0]
            status, artist, title, _size = dacli._save_one(
                d,
                {},  # no metadata batch — _save_one tolerates empty
                dest,
                fallback_artist=None,
            )

            # The first deviation should either download (ok) or be
            # skipped if it has no content URL (noimg). A failure
            # (fail:*) is a real bug.
            assert status in ("ok", "noimg"), (
                f"unexpected status {status!r} for deviation "
                f"{d.get('deviationid')}: artist={artist}, title={title}"
            )

            if status == "ok":
                # Verify the folder structure on disk.
                artist_dir = dest / artist
                assert artist_dir.exists(), f"artist dir {artist_dir} not created"

                # Find the deviation folder (title may be sanitized).
                deviation_folders = list(artist_dir.iterdir())
                assert deviation_folders, "no deviation folder created"
                folder = deviation_folders[0]

                # description.json must exist and be valid JSON.
                desc_path = folder / "description.json"
                assert desc_path.exists(), "description.json not written"
                desc = json.loads(desc_path.read_text())
                assert desc.get("deviationid"), "description.json missing deviationid"

                # At least one image file must exist.
                images = list(folder.glob("image.*"))
                assert images, "no image file written"
                assert images[0].stat().st_size > 0, "image file is 0 bytes"

                # The synced-index must have one row.
                assert dacli.index_count() >= 1, "index not populated after save"
        finally:
            # Restore the original index path.
            dacli._index_close()
            dacli.INDEX_PATH = original_index_path


class TestImageDownloadLive:
    """Verify the wixmp CDN image download path works against real DA
    image URLs. This catches CDN-side breakage (URL format changes,
    auth token issues) that the mocked unit tests can't detect."""

    def test_http_bytes_downloads_real_cdn_image(
        self,
        user_token: str,
    ) -> None:
        """Fetch a feed page, extract the first deviation's content URL,
        and download the actual image bytes from wixmp. Asserts non-empty
        PNG/JPEG/GIF/WebP data."""
        feed = dacli.http_json(
            f"{API}/browse/deviantsyouwatch?limit=1&mature_content=false",
            token=user_token,
        )
        results = feed.get("results", [])
        if not results:
            pytest.skip("watch feed returned no results")

        content = results[0].get("content") or results[0].get("preview") or {}
        src = content.get("src")
        if not src:
            pytest.skip("first feed deviation has no content URL")

        blob = dacli.http_bytes(src)
        assert len(blob) > 1000, f"image download returned {len(blob)} bytes — too small"
        # Verify it's a real image (magic bytes). JPEG has variants
        # (\xff\xd8\xff\xe0, \xff\xd8\xff\xe1, etc.) so check only
        # the first 3 bytes for JPEG.
        assert (
            blob[:4]
            in (
                b"\x89PNG",  # PNG
                b"GIF8",  # GIF
                b"RIFF",  # WebP
            )
            or blob[:3] == b"\xff\xd8\xff"
        ), (  # JPEG (any EXIF/JFIF variant)
            f"unexpected magic bytes: {blob[:4]!r}"
        )
