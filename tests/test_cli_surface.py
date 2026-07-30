"""Small correctness defects on the CLI's own surface.

Four unrelated things, each one the code being less careful than the
comment next to it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import dacli

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestMaskSecretDoesNotRevealShortSecrets:
    """The first-4/last-4 form is only masking when the input is long."""

    def test_a_nine_character_secret_is_not_shown(self):
        """It rendered as SSSS...SSSS — 8 of 9 characters, 89% of it.

        A real DeviantArt client_secret is 32 hex characters, so nothing
        in practice was exposed. But the function is generic, the failure
        was silent, and the docstring said "shorter than 8" while the code
        tested `<= 8`.
        """
        masked = dacli.mask_secret("123456789")
        assert masked == "*****", f"revealed {masked!r} from a 9-character secret"

    @pytest.mark.parametrize("length", [1, 8, 9, 12, 16, 23])
    def test_nothing_below_the_threshold_leaks_any_characters(self, length):
        secret = "S" * length
        masked = dacli.mask_secret(secret)
        assert masked == "*****", f"len={length} rendered as {masked!r}"

    def test_a_real_length_secret_still_shows_its_edges(self):
        """The control: masking must stay useful for identifying which
        secret you are looking at. A real DA client_secret is 32 hex.
        """
        masked = dacli.mask_secret("NOT-A-REAL-SECRET-FOR-TESTS-0000")
        assert masked == "NOT-...0000"

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_and_none_pass_through(self, value):
        assert dacli.mask_secret(value) == value


class TestSafeFilenameRefusesOptionLikeNames:
    """A DA title becomes a directory name in the user's gallery."""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("-rf", "rf"),
            ("--help", "help"),
            ("--exclude=*", "exclude"),
            ("-", "untitled"),
            ("--", "untitled"),
        ],
    )
    def test_a_leading_dash_is_stripped(self, title, expected):
        """`tar`, `rsync`, `find` and `rm` all parse a leading dash as an
        option unless the invocation is `--`-terminated. Titles are
        attacker-influenced content — the same reasoning the existing `..`
        guard applies.
        """
        assert dacli.safe_filename(title) == expected

    @pytest.mark.parametrize("title", ["Sunset", "a-b-c", "x_-_y", "2024-06-01"])
    def test_dashes_elsewhere_are_untouched(self, title):
        """The control: only a LEADING dash is a problem. Stripping all of
        them would mangle ordinary titles and collapse distinct ones.
        """
        assert dacli.safe_filename(title) == title

    @pytest.mark.parametrize("evil", ["..", ".", "...", "../..", "/etc/passwd", "\x00"])
    def test_the_traversal_guard_still_holds(self, evil, tmp_path):
        """The control for the control: the older guard must survive.

        Asserted as "the folder resolves inside the destination", which is
        the property that matters. My first version asserted `".." not in
        result`, which is too strict — "../.." sanitises to ".._..", a
        single component containing no separator, and is harmless.
        """
        result = dacli.safe_filename(evil)
        assert "/" not in result
        assert (tmp_path / result).resolve().is_relative_to(tmp_path.resolve())


class TestConfigOverrideIsAbsolute:
    def test_a_relative_config_is_made_absolute(self, tmp_path):
        """cwd under launchd is the scheduler's, not the author's.

        A relative --config in a plist therefore reads AND CREATES a
        different file than intended — silently, because _atomic_write
        mkdir-p's the parent. Checked through a subprocess run from a
        different directory, which is the condition that matters.
        """
        workdir = tmp_path / "elsewhere"
        workdir.mkdir()
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "da"), "--config", "rel.json", "config", "path"],
            capture_output=True,
            text=True,
            check=False,
            cwd=workdir,
            env={**os.environ, "HOME": str(tmp_path), "NO_COLOR": "1"},
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        reported = next(
            line.split(":", 1)[1].strip()
            for line in result.stdout.splitlines()
            if line.startswith("config:")
        )
        assert Path(reported).is_absolute(), f"--config reported a relative path: {reported}"
        assert reported == str(workdir / "rel.json")

    def test_config_dir_still_follows_config_path(self, tmp_path, monkeypatch):
        """CONFIG_DIR is NOT a dead write, contrary to the audit note.

        tests/test_util.py reads dacli.CONFIG_DIR to check that --config
        keeps the two consistent, so the assignment is load-bearing. Pinned
        here too, since a future cleanup would otherwise remove it on the
        strength of a grep that missed the test.
        """
        override = tmp_path / "nested" / "custom.json"
        dacli.main(["--config", str(override), "config", "path"])
        assert override == dacli.CONFIG_PATH
        assert override.parent == dacli.CONFIG_DIR


class TestContradictoryFlagsAreRejected:
    def test_user_and_via_feed_cannot_both_be_given(self):
        """--via-feed says it skips the friends endpoint "entirely".

        `if args.user:` silently won, so passing both queried
        /user/friends anyway and only honoured --via-feed if that happened
        to return 403.
        """
        with pytest.raises(SystemExit) as exc:
            dacli.build_parser().parse_args(["sync", "watched", "--user", "alice", "--via-feed"])
        assert exc.value.code == 2

    @pytest.mark.parametrize(
        "args",
        [
            ["sync", "watched", "--user", "alice"],
            ["sync", "watched", "--via-feed"],
            ["sync", "watched"],
        ],
    )
    def test_either_alone_is_still_accepted(self, args):
        """The control: the group must not break the working invocations."""
        parsed = dacli.build_parser().parse_args(args)
        assert parsed.func is not None


class TestEveryJsonFlagIsDocumented:
    def test_no_json_flag_has_an_empty_help(self):
        """Four had no help text, so `da search topics --help` printed a
        bare `--json` while its sibling printed the shared description.
        """
        import argparse

        bare: list[str] = []

        def walk(parser: argparse.ArgumentParser, path: str) -> None:
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for name, sub in action.choices.items():
                        walk(sub, f"{path} {name}")
                elif "--json" in action.option_strings and not action.help:
                    bare.append(path)

        walk(dacli.build_parser(), "da")
        assert bare == [], f"--json has no help on: {sorted(set(bare))}"
