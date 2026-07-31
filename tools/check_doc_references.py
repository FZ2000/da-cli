#!/usr/bin/env python3
"""Verify that code references in the documentation still resolve.

Docs rot in a specific way: a command gets renamed, a flag dropped, a
file moved, and the prose keeps confidently describing the old thing.
Link checkers do not catch it, because these are not links.

This checks every backticked token across all markdown:

* file paths — must exist, either relative to the document or anywhere
  in the repo (docs often refer to `test_sync.py` by bare name)
* ``da`` commands — must exist in ``build_parser()``
* flags — must be defined on some parser
* ``DA_*`` / ``XDG_*`` / ``VCR_*`` environment variables — must be read
  somewhere in the source or the installer

Run it directly, or via ``make check-docs``. CI runs it on every push.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import dacli  # noqa: E402  (needs the sys.path line above)

SKIP_DIRS = (
    ".git/",
    ".venv",
    "node_modules",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    "htmlcov",
    # Agent worktrees and other checkouts nested under the repo. These are
    # copies, not repo content: scanning them multiplied the file count by
    # five and would report a problem against a path that is not the repo.
    ".claude/",
    "dist/",
    "build/",
)

# Tokens that are deliberately not real: template placeholders, and names
# used to say "we did NOT call it this".
ALLOWED = {
    "http.py",  # ADR 0007 explains why the module is net.py
    "SKILL.md",  # changelog records the rename to AGENTS.md
    "logging.yaml",  # hypothetical config in ADR 0003
    "NNNN-kebab-case-title.md",  # the ADR filename template
}

# Flags belonging to other tools that legitimately appear in our docs.
FOREIGN_FLAGS = {
    "--no-cov",
    "--no-verify",
    "--lf",
    "--ff",
    "--fix",
    "--check",
    "--strict",
    "--user",
    "--upgrade",
    "--version",
    "--help",
}


def _repo_files() -> tuple[set[str], dict[str, list[str]]]:
    paths, by_name = set(), {}
    for p in REPO.rglob("*"):
        s = str(p.relative_to(REPO))
        if not p.is_file() or any(x in s for x in SKIP_DIRS):
            continue
        paths.add(s)
        by_name.setdefault(p.name, []).append(s)
    return paths, by_name


WITH_POSITIONALS: set[str] = set()


def _cli_surface() -> tuple[set[str], set[str]]:
    """Every command path and every flag the parser accepts."""

    def walk(parser: argparse.ArgumentParser, prefix: str) -> tuple[set[str], set[str]]:
        cmds, flags = {prefix}, set()
        takes_positional = False
        for action in parser._actions:  # noqa: SLF001 — argparse exposes no public API
            flags |= set(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for name, sub in action.choices.items():
                    c, f = walk(sub, f"{prefix} {name}")
                    cmds |= c
                    flags |= f
            elif not action.option_strings and action.dest not in ("help", "func"):
                # A positional: `da config set KEY VALUE`, `da sync artist NAME`.
                takes_positional = True
        if takes_positional:
            WITH_POSITIONALS.add(prefix)
        return cmds, flags

    return walk(dacli.build_parser(), "da")


def _source_text() -> str:
    parts = [p.read_text() for p in (REPO / "dacli").rglob("*.py")]
    parts += [p.read_text() for p in (REPO / "tests").rglob("*.py")]
    for extra in ("install.sh", "install_schedule.sh"):
        f = REPO / extra
        if f.exists():
            parts.append(f.read_text())
    return "\n".join(parts)


def _check_module_count(problems: list[tuple[str, str, int, str]]) -> int:
    """`install.sh` prints a live module count; the docs quote a number.

    A quoted count is a claim about the package, and it went stale the
    moment it was written: the getting-started transcript omitted the
    `(N modules)` suffix entirely while `install.sh` had been printing it
    for some time. Nothing noticed, because prose is not executed.
    """
    real = sum(1 for _ in (REPO / "dacli").rglob("*.py"))
    found = 0
    for doc in sorted(REPO.rglob("*.md")):
        rel = str(doc.relative_to(REPO))
        if any(x in rel for x in SKIP_DIRS):
            continue
        for lineno, line in enumerate(doc.read_text().splitlines(), 1):
            m = re.search(r"\((\d+) modules\)", line)
            if not m:
                continue
            found += 1
            if int(m.group(1)) != real:
                problems.append(
                    ("module count is stale", rel, lineno, f"says {m.group(1)}, dacli/ has {real}")
                )
    return found


def main() -> int:
    paths, by_name = _repo_files()
    commands, flags = _cli_surface()
    source = _source_text()
    problems: list[tuple[str, str, int, str]] = []
    module_counts = _check_module_count(problems)

    checked = 0
    for doc in sorted(REPO.rglob("*.md")):
        rel = str(doc.relative_to(REPO))
        if any(x in rel for x in SKIP_DIRS):
            continue
        checked += 1
        text = doc.read_text()
        for match in re.finditer(r"`([^`\n]{2,90})`", text):
            token = match.group(1).strip()
            line = text[: match.start()].count("\n") + 1
            if token in ALLOWED:
                continue

            if re.fullmatch(
                r"[\w./-]+\.(py|sh|toml|yaml|yml|cff|md)", token
            ) and not token.startswith("~"):
                if (
                    (doc.parent / token).exists()
                    or token in paths
                    or pathlib.Path(token).name in by_name
                ):
                    continue
                problems.append(("file does not exist", rel, line, token))

            elif re.fullmatch(r"(DA|XDG|VCR)_[A-Z_]+", token):
                if token not in source:
                    problems.append(("env var read nowhere", rel, line, token))

            elif (
                token.startswith("da ")
                and "<" not in token
                # Placeholders, in both the ASCII and typographic
                # spellings — docs use "…" and it is not a command.
                and "..." not in token
                and "…" not in token
            ):
                words = token.split()
                base: list[str] = []
                for w in words:
                    if w.startswith("-"):
                        break
                    base.append(w)
                path = " ".join(base)
                if len(base) > 1 and path not in commands:
                    # The trailing words may be positional arguments rather
                    # than a command name: `da config set client_secret X`,
                    # `da sync artist alice`. Accept them only when the
                    # longest matching prefix is a command that actually
                    # takes positionals — otherwise a genuine typo would
                    # slip through as "an argument to `da`".
                    for cut in range(len(base) - 1, 0, -1):
                        candidate = " ".join(base[:cut])
                        if candidate in commands:
                            if candidate not in WITH_POSITIONALS:
                                problems.append(("no such command", rel, line, path))
                            break
                    else:
                        problems.append(("no such command", rel, line, path))
                problems.extend(
                    ("no such flag", rel, line, w)
                    for w in words
                    if w.startswith("--")
                    and "=" not in w
                    and w not in flags
                    and w not in FOREIGN_FLAGS
                )

    for kind, f, line, token in sorted(set(problems)):
        print(f"{f}:{line}: {kind}: {token}")
    if problems:
        print(f"\n{len(set(problems))} stale documentation reference(s).", file=sys.stderr)
        return 1
    # `checked`, not a fresh rglob: the summary must describe the files this
    # run actually looked at, or a skipped tree inflates the number and the
    # message quietly overstates the coverage.
    print(
        f"all code references in {checked} markdown files resolve "
        f"({module_counts} module-count claim(s) match dacli/)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
