#!/usr/bin/env python3
"""Verify the flag tables in docs/commands/ against the argparse parser.

The per-command guides carry a parameter table for every command:

    | Flag | Type | Default | What it does |

Prose drifts quietly, but a wrong default is worse than vague prose,
because the reader will act on it. This checks three things for every row
of every such table:

* the flag exists on that command's parser
* the documented default matches the parser's real default
* nothing the parser defines is missing from the table

Run directly, or via ``make check-docs``. CI runs it on every push.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import dacli  # noqa: E402  (needs the sys.path line above)

DOCS = REPO / "docs" / "commands"
TABLE_HEADER = re.compile(r"^\|\s*Flag\s*\|")
HEADING = re.compile(r"^#{2,}\s+`?(da [a-z][\w -]*?)`?\s*$")
# `--flag`, `--flag METAVAR`, `-h`, `--help` — take every long option present.
ROW_FLAGS = re.compile(r"`(-{1,2}[\w-]+)")

# Flags whose default is deliberately described rather than printed,
# because the literal value would mislead.
PROSE_DEFAULTS = {
    # resolved at runtime from config/env; the parser default is None
    ("da auth", "--redirect-uri"),
    ("da auth", "--port"),
}


def parser_surface() -> tuple[dict[str, dict[str, object]], set[str]]:
    """{command path: {option string: default}}, plus paths that exist."""
    table: dict[str, dict[str, object]] = {}

    def walk(parser: argparse.ArgumentParser, prefix: str) -> None:
        opts: dict[str, object] = {}
        for action in parser._actions:  # noqa: SLF001 — argparse exposes no public API
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for name, sub in action.choices.items():
                    walk(sub, f"{prefix} {name}")
            for opt in action.option_strings:
                opts[opt] = action.default
        table[prefix] = opts

    walk(dacli.build_parser(), "da")
    return table, set(table)


def _defaults_agree(claimed: str, actual: object) -> bool:
    c = claimed.strip().strip("`*_ ").lower()
    if c in ("—", "-", "", "n/a"):
        # An em dash means "not applicable" — only honest when there is no
        # meaningful default to state.
        return actual in (None, False, argparse.SUPPRESS)
    if actual is None:
        # None means "resolved elsewhere", so a sentence describing where
        # is the honest answer. A bare literal is not: that would be
        # claiming a default the parser does not have.
        # "resolved from config" is an honest answer; "`0`" is not — it
        # claims a default the parser does not have. Requiring several
        # words rather than merely a backtick is what distinguishes them.
        # A backticked literal used to pass here, which let `--offset`
        # keep documenting a default of 0 after it became None.
        looks_like_prose = len(c.replace("`", "").split()) >= 2
        return c in ("none", "unset", "not set") or looks_like_prose
    if isinstance(actual, bool):
        return (str(actual).lower() in c) or (("on" in c) if actual else ("off" in c))
    return str(actual).lower() in c


def main() -> int:
    surface, known = parser_surface()
    problems: list[str] = []
    rows_checked = 0
    tables_checked = 0

    for doc in sorted(DOCS.glob("*.md")):
        rel = doc.relative_to(REPO)
        command: str | None = None
        in_table = False
        documented: set[str] = set()

        lines = doc.read_text().splitlines()
        for lineno, line in enumerate(lines, 1):
            heading = HEADING.match(line)
            if heading:
                # Leaving a command: everything its parser defines should
                # have appeared in the table.
                if command and documented:
                    missing = {
                        o
                        for o in surface[command]
                        if o.startswith("--") and o not in documented and o != "--help"
                    }
                    if missing:
                        problems.append(f"{rel}: `{command}` table omits {sorted(missing)}")
                candidate = heading.group(1).strip()
                command = candidate if candidate in known else None
                documented = set()
                in_table = False
                continue

            if TABLE_HEADER.match(line):
                in_table = True
                tables_checked += 1
                continue
            if in_table and not line.startswith("|"):
                in_table = False
                continue
            if not (in_table and command) or set(line) <= set("|- :"):
                continue

            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            flags = ROW_FLAGS.findall(cells[0])
            if not flags:
                continue
            rows_checked += 1
            claimed_default = cells[2]

            for flag in flags:
                if not flag.startswith("--"):
                    continue
                documented.add(flag)
                if flag == "--help":
                    # argparse uses SUPPRESS; there is no default to state.
                    continue
                if flag not in surface[command]:
                    problems.append(f"{rel}:{lineno}: `{command}` has no {flag}")
                    continue
                if (command, flag) in PROSE_DEFAULTS:
                    continue
                actual = surface[command][flag]
                if not _defaults_agree(claimed_default, actual):
                    problems.append(
                        f"{rel}:{lineno}: `{command} {flag}` documented default "
                        f"{claimed_default!r}, parser says {actual!r}"
                    )

        if command and documented:
            missing = {
                o
                for o in surface[command]
                if o.startswith("--") and o not in documented and o != "--help"
            }
            if missing:
                problems.append(f"{rel}: `{command}` table omits {sorted(missing)}")

    for p in problems:
        print(p)
    if problems:
        print(f"\n{len(problems)} flag-table problem(s).", file=sys.stderr)
        return 1
    print(f"{rows_checked} flag rows across {tables_checked} tables match the parser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
