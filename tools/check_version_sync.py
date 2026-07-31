#!/usr/bin/env python3
"""One version, asserted everywhere it appears.

`dacli.__version__` is the single source of truth. Everything downstream is
supposed to derive from it:

    dacli/constants.py  __version__
        -> pyproject.toml   version = { attr = "dacli.__version__" }
            -> the built wheel and sdist
                -> what PyPI shows
    and, at release time, the git tag `v{__version__}`

That chain is only as good as its weakest hand-maintained link, and there
are several: CITATION.cff carries a literal, CHANGELOG.md needs a matching
section, docs/reference/cli.md embeds it (generated, but the generated file
is committed), and prose in README/docs quotes it in sample output.

Every one of those was wrong at some point before this check existed —
`docs/reference/cli.md` said 0.3.0 while the package said something else,
and the issue-template placeholder had drifted too. A mismatch between the
tag and the package is the one that actually hurts: it puts a wheel on PyPI
reporting a version nobody can find on GitHub, and PyPI will not let you
re-upload the same filename to fix it.

    python3 tools/check_version_sync.py            # files only
    python3 tools/check_version_sync.py --tag v0.1.0   # also check a tag

The release workflow runs the `--tag` form against the pushed tag before it
builds anything.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import dacli  # noqa: E402  (needs the sys.path line above)

VERSION = dacli.__version__


def problems() -> list[str]:
    out: list[str] = []

    # The version must look like a version, or every comparison below is
    # comparing typos.
    if not re.fullmatch(r"\d+\.\d+\.\d+([abrc]|rc|\.post|\.dev)?\d*", VERSION):
        out.append(f"dacli.__version__ is not a PEP 440 release version: {VERSION!r}")

    # pyproject must DERIVE the version, never restate it. A literal here is
    # the classic way the wheel and the package disagree.
    pyproject = (REPO / "pyproject.toml").read_text()
    if not re.search(r'version\s*=\s*\{\s*attr\s*=\s*"dacli\.__version__"\s*\}', pyproject):
        out.append("pyproject.toml does not read the version from dacli.__version__")
    if re.search(rf'^\s*version\s*=\s*"{re.escape(VERSION)}"', pyproject, re.MULTILINE):
        out.append("pyproject.toml hardcodes the version; it must stay dynamic")

    # CITATION.cff is a literal by necessity — GitHub's citation widget reads
    # it statically — so it has to be checked rather than derived.
    cff = (REPO / "CITATION.cff").read_text()
    m = re.search(r'^version:\s*"?([^"\n]+)"?', cff, re.MULTILINE)
    if not m:
        out.append("CITATION.cff has no version field")
    elif m.group(1).strip() != VERSION:
        out.append(f"CITATION.cff version is {m.group(1).strip()!r}, expected {VERSION!r}")

    # A release with no changelog entry is a release nobody can read.
    changelog = (REPO / "CHANGELOG.md").read_text()
    if not re.search(rf"^## \[?{re.escape(VERSION)}\]?[^\n]*$", changelog, re.MULTILINE):
        out.append(f"CHANGELOG.md has no '## {VERSION}' section")

    # Generated, but committed — so it can be stale in exactly the way the
    # generator exists to prevent.
    cli_doc = REPO / "docs" / "reference" / "cli.md"
    if cli_doc.exists():
        found = re.search(r"\(version ([^)]+)\)", cli_doc.read_text())
        if found and found.group(1) != VERSION:
            out.append(
                f"docs/reference/cli.md says version {found.group(1)}; run tools/gen_cli_docs.py"
            )

    # Any other file quoting a DIFFERENT release version in prose. Scoped to
    # `da-cli <x.y.z>` and `v<x.y.z>` so it cannot trip on the pinned versions
    # of third-party tools, which are unrelated and legitimately differ.
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=False
    ).stdout.split()
    if not tracked:
        out.append("git listed no files — not a work tree, so nothing was scanned")
    for rel in tracked:
        if rel.startswith(("tests/integration/cassettes/", "CHANGELOG.md")):
            # Cassettes record a request as it was made at capture time; that
            # is a historical artifact, and the replay matcher ignores the
            # User-Agent header anyway. CHANGELOG legitimately names old ones.
            continue
        p = REPO / rel
        if not p.is_file() or p.suffix in {".png", ".jpg", ".gif", ".db"}:
            continue
        try:
            body = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(body.splitlines(), 1):
            out.extend(
                f"{rel}:{i}: quotes da-cli {other}, but the package is {VERSION}"
                for other in re.findall(r"\bda-cli[ /]v?(\d+\.\d+\.\d+)\b", line)
                if other != VERSION
            )
    return out


def check_tag(tag: str) -> list[str]:
    """The tag and the package must agree, or PyPI gets an unfindable wheel."""
    if tag != f"v{VERSION}":
        return [f"tag {tag!r} does not match dacli.__version__ (expected 'v{VERSION}')"]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", help="also assert this git tag matches the package version")
    args = ap.parse_args()

    found = problems()
    if args.tag:
        found += check_tag(args.tag)

    for f in found:
        print(f"  {f}")
    if found:
        print(f"\n{len(found)} version-sync problem(s).", file=sys.stderr)
        return 1
    scope = f"{VERSION} (tag {args.tag} ok)" if args.tag else VERSION
    print(f"version {scope} is consistent across pyproject, CITATION.cff, CHANGELOG and docs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
