"""Numeric config values, checked when typed and survivable when not.

`da config set jitter 40%` used to be accepted with a cheerful "stored
jitter in ...". The value then sat in config.json until the next
scheduled run, where `_delays` called a bare `float()` on it and raised
an uncaught ValueError — before the first page was fetched, so every
night failed identically, and the message the user eventually saw was a
traceback that never mentioned config.

Two halves, deliberately different:

* **Write** rejects, because the user is present and can retype it.
* **Read** warns and falls back, because a hand-edited config.json or an
  exported ``DA_JITTER=abc`` never passed through the write path, and a
  cron job that dies over a typo is worse than one that carries on.

The asymmetry that gave it away is in the code these replace:
``_concurrency`` already caught ``(TypeError, ValueError)`` and defaulted,
while ``_delays`` — the same kind of value, resolved four lines away —
did not.
"""

from __future__ import annotations

import argparse
import json

import pytest

import dacli


def _args(**overrides: object) -> argparse.Namespace:
    base = {"delay_api": None, "delay_image": None, "jitter": None, "concurrency": None}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestConfigSetRejectsNonNumbers:
    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("jitter", "40%"),
            ("jitter", "abc"),
            ("delay_api", "five"),
            ("delay_image", ""),
            ("concurrency", "4.5"),
            ("concurrency", "many"),
        ],
    )
    def test_rejected_with_exit_2(self, key, value, isolated_paths, capsys):
        with pytest.raises(SystemExit) as exc:
            dacli.set_config_field(key, value)
        assert exc.value.code == 2

        err = capsys.readouterr().err
        assert key in err, "the message must name the key the user mistyped"
        assert repr(value) in err or value in err, "and show what they typed"
        assert not isolated_paths["cfg"].exists(), "nothing should have been written"

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("jitter", "0.2"),
            ("jitter", "0"),
            ("delay_api", "5"),
            ("delay_image", "1.5"),
            ("concurrency", "8"),
        ],
    )
    def test_real_numbers_are_still_accepted(self, key, value, isolated_paths):
        """The control: valid values must not have become harder to set."""
        dacli.set_config_field(key, value)
        stored = json.loads(isolated_paths["cfg"].read_text(encoding="utf-8"))
        assert stored[key] == value

    def test_non_numeric_keys_are_untouched(self, isolated_paths):
        """Only the arithmetic keys are checked — destination is a path."""
        dacli.set_config_field("destination", "~/Pictures/DA")
        stored = json.loads(isolated_paths["cfg"].read_text(encoding="utf-8"))
        assert stored["destination"] == "~/Pictures/DA"


class TestSyncSurvivesAConfigItNeverValidated:
    """The read path: values that reached disk without passing the check."""

    @pytest.mark.parametrize("bad", ["40%", "abc", "", None, [], {}])
    def test_delays_falls_back_instead_of_raising(self, bad):
        """This is the crash. `_delays` must not raise on any of these."""
        api, _img, jit = dacli.sync._delays({"jitter": bad}, _args())
        assert jit == dacli.DEFAULT_JITTER
        assert api == dacli.DEFAULT_DELAY_API

    def test_the_warning_names_the_key_and_the_value(self, capsys):
        dacli.sync._delays({"jitter": "40%"}, _args())
        err = capsys.readouterr().err
        assert "jitter" in err
        assert "40%" in err

    def test_delays_and_concurrency_agree_on_bad_input(self):
        """The asymmetry that caused this: same value, two behaviours.

        `_concurrency` defaulted; `_delays` raised. Whichever is right,
        they must now be the same.
        """
        cfg = {"jitter": "abc", "delay_api": "abc", "concurrency": "abc"}
        assert dacli.sync._concurrency(cfg, _args()) == dacli.DEFAULT_CONCURRENCY
        api, _img, jit = dacli.sync._delays(cfg, _args())
        assert (api, jit) == (dacli.DEFAULT_DELAY_API, dacli.DEFAULT_JITTER)

    @pytest.mark.parametrize("key", ["delay_api", "delay_image", "jitter"])
    def test_negative_values_are_clamped_not_passed_to_sleep(self, key):
        """A negative delay reached time.sleep(), which raises ValueError.

        Worse for delay_image: that sleep ran after the image had been
        committed and indexed, so a fully successful save was reported as
        a failure — see test_save_one_reports_success_once_committed.
        """
        api, img, jit = dacli.sync._delays({key: -2.5}, _args())
        assert min(api, img, jit) >= 0.0

    def test_an_explicit_zero_still_disables_the_delay(self):
        """The control for clamping: 0 is a real request, not a fallback."""
        api, _img, _jit = dacli.sync._delays({}, _args(delay_api=0))
        assert api == 0.0


class TestACommittedSaveIsNeverReportedAsFailed:
    def test_save_one_reports_success_once_committed(self, tmp_path, monkeypatch):
        """A raise after the rename must not turn "ok" into "fail".

        The index write and the rate-limit sleep used to sit inside the
        download's try block. A negative delay_image reaching time.sleep()
        therefore returned fail: for a deviation whose bytes were already
        on disk and already indexed. Because the feed checkpoint only
        advances on a run with zero failures, the same page then re-synced
        forever while the gallery was in fact complete.
        """
        monkeypatch.setattr(dacli, "INDEX_PATH", tmp_path / "index.db")
        monkeypatch.setattr(dacli, "http_bytes", lambda url, **kw: b"REAL-IMAGE-BYTES")
        dest = tmp_path / "dest"
        dest.mkdir()
        deviation = {
            "deviationid": "DEV-1",
            "title": "Moonrise",
            "author": {"username": "bob"},
            "content": {"src": "https://images.example.com/moonrise.png"},
        }

        # -2.5 is what a hand-edited config could still deliver; it is
        # passed directly here so the test pins _save_one, not _delays.
        status, _artist, _title, size = dacli._save_one(
            deviation, {}, dest, image_delay=-2.5, jitter_pct=0.0
        )

        assert status == "ok", f"a committed save reported {status!r}"
        assert size == len(b"REAL-IMAGE-BYTES")
        assert dacli.index_has("DEV-1")
        assert (dest / "bob" / "Moonrise" / "image.png").read_bytes() == b"REAL-IMAGE-BYTES"

    def test_a_real_download_failure_is_still_reported(self, tmp_path, monkeypatch):
        """The control: narrowing the try must not swallow real failures."""
        monkeypatch.setattr(dacli, "INDEX_PATH", tmp_path / "index.db")

        def boom(url, **kw):
            raise OSError("no route to host")

        monkeypatch.setattr(dacli, "http_bytes", boom)
        dest = tmp_path / "dest"
        dest.mkdir()
        deviation = {
            "deviationid": "DEV-2",
            "title": "Sunset",
            "author": {"username": "alice"},
            "content": {"src": "https://images.example.com/sunset.png"},
        }

        status, _artist, _title, _size = dacli._save_one(deviation, {}, dest, image_delay=0)

        assert status == "fail:OSError"
        assert not dacli.index_has("DEV-2"), "a failed download must not be indexed"
