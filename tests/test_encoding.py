"""Every file da-cli reads or writes is UTF-8, whatever the locale says.

``Path.read_text()`` and ``Path.write_text()`` with no ``encoding=`` use
the *locale's* encoding, not UTF-8. That is invisible on a developer
machine, where the locale is already UTF-8 — and it is exactly the kind
of bug that only ever appears on someone else's computer.

It matters here more than in most projects, because the payload is
DeviantArt titles. ``description.json`` is written with
``ensure_ascii=False``, so a Japanese title is stored as real UTF-8
bytes. Under a non-UTF-8 locale that write raises UnicodeEncodeError and
the deviation is lost; a file written earlier under a UTF-8 locale fails
to read back, which makes the collision check treat the folder as
unowned.

CPython hides this most of the time: PEP 538 coerces the C locale to
C.UTF-8, and PEP 540 turns on UTF-8 mode when that fails. The tests
below switch both off, which is what a legacy locale (ja_JP.eucJP,
zh_CN.GBK — both still in use, and both in the population most likely to
own CJK-titled art) does on its own.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# CJK, an em dash and an accent: an ordinary DeviantArt title, and every
# character in it is unencodable in ASCII.
NON_ASCII_TITLE = "月の幻想 — café"

# Switches off both of CPython's UTF-8 safety nets, leaving the locale's
# real encoding in force.
ASCII_LOCALE_ENV = {
    "LC_ALL": "C",
    "LANG": "C",
    "PYTHONCOERCECLOCALE": "0",
    "PYTHONUTF8": "0",
    "PATH": "/usr/bin:/bin",
}


def _run_under_ascii_locale(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run ``script`` in a child whose locale encoding is ASCII.

    The script goes to a *file* rather than ``python -c``. Under an ASCII
    locale the interpreter cannot decode a command line containing
    non-ASCII, so ``-c`` dies with "Unable to decode the command from the
    command line" before running a single statement — the test would then
    fail identically whether or not the bug is present. Source files are
    read as UTF-8 regardless of locale (PEP 3120), so a file does not
    have that problem.
    """
    script_path = tmp_path / "child.py"
    script_path.write_text(script, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={**ASCII_LOCALE_ENV, "TMP_DIR": str(tmp_path)},
        timeout=60,
        check=False,
    )


@pytest.fixture(scope="module")
def ascii_locale_available() -> bool:
    """Whether this machine can actually produce a non-UTF-8 locale.

    Verified rather than assumed: if the child still reports UTF-8, these
    tests would pass against the bug they exist to catch.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
        capture_output=True,
        text=True,
        env=ASCII_LOCALE_ENV,
        timeout=60,
        check=False,
    )
    return "utf" not in probe.stdout.strip().lower()


class TestFileIOIsUTF8RegardlessOfLocale:
    def test_description_json_round_trips_under_an_ascii_locale(
        self, tmp_path, ascii_locale_available
    ):
        """Write a CJK title and read it back with the locale set to ASCII.

        Without ``encoding="utf-8"`` on the write, this raises
        UnicodeEncodeError and the deviation never lands on disk.
        """
        if not ascii_locale_available:
            pytest.skip("this platform coerces every locale to UTF-8")

        script = f"""
import json, os, pathlib, sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from dacli.output import _atomic_write

title = {NON_ASCII_TITLE!r}
path = pathlib.Path(os.environ["TMP_DIR"]) / "description.json"
_atomic_write(path, json.dumps({{"title": title}}, indent=2, ensure_ascii=False), 0o644)
assert json.loads(path.read_text(encoding="utf-8"))["title"] == title
print("ok")
"""
        result = _run_under_ascii_locale(script, tmp_path)
        assert result.returncode == 0, (
            f"writing a non-ASCII title failed under an ASCII locale:\n{result.stderr}"
        )
        assert "ok" in result.stdout

    def test_config_with_a_non_ascii_destination_loads_under_an_ascii_locale(
        self, tmp_path, ascii_locale_available
    ):
        """A config file is UTF-8 even when the locale is not.

        Someone whose home directory has an accent in it writes a
        perfectly ordinary config; reading it back must not depend on
        what LANG happened to be.
        """
        if not ascii_locale_available:
            pytest.skip("this platform coerces every locale to UTF-8")

        cfg_dir = tmp_path / "cfg" / "da-cli"
        cfg_dir.mkdir(parents=True)
        # ensure_ascii=False deliberately. The default escapes the accent
        # to a backslash-u sequence, leaving a pure-ASCII file that reads
        # back fine under any locale — a test that cannot fail. Writing
        # the real UTF-8 bytes is both what a hand-edited config contains
        # and what makes this test able to detect the bug.
        (cfg_dir / "config.json").write_text(
            json.dumps({"destination": "/Users/josé/Art", "client_id": "1234"}, ensure_ascii=False),
            encoding="utf-8",
        )

        script = f"""
import os, pathlib, sys
sys.path.insert(0, {str(REPO_ROOT)!r})
import dacli
from dacli.config import load_config

dacli.CONFIG_PATH = pathlib.Path(os.environ["TMP_DIR"]) / "cfg" / "da-cli" / "config.json"
cfg = load_config()
assert cfg["destination"] == "/Users/jos\\u00e9/Art", cfg["destination"]
print("ok")
"""
        result = _run_under_ascii_locale(script, tmp_path)
        assert result.returncode == 0, (
            f"reading a UTF-8 config failed under an ASCII locale:\n{result.stderr}"
        )
        assert "ok" in result.stdout


class TestNoImplicitLocaleEncodingRemains:
    """A guard against the next call site reintroducing it.

    The two tests above only cover the paths they exercise. This one
    covers every runtime call site at once, including ones added later.
    """

    def test_runtime_text_io_always_names_its_encoding(self):
        offenders = []
        for path in sorted((REPO_ROOT / "dacli").rglob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                reads_with_locale_encoding = ".read_text()" in stripped
                writes_with_locale_encoding = (
                    ".write_text(" in stripped and "encoding=" not in stripped
                )
                if reads_with_locale_encoding or writes_with_locale_encoding:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
        assert not offenders, "text file IO without an explicit encoding:\n" + "\n".join(offenders)
