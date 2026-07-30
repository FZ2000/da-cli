"""`dacli/__init__.py` had a blanket `F401` suppression, and 29 dead imports.

The suppression was there for a real reason — the package re-exports the
names extracted into submodules, so `dacli.X` keeps resolving for callers
and for the test suite, and those imports genuinely are unused *within*
the file. But "unused within the file" and "unused" are different things,
and a blanket ignore cannot tell them apart. Under it, 29 imports sat
there indefinitely: 16 stdlib modules left over from the single-file era,
10 private helpers that every caller reaches through the owning submodule
instead, two constants, and `http.server`, whose consumer moved to
auth.py.

The fix was to say which is which: every deliberate re-export now carries
an explicit `X as X` alias, the form ruff recognises, so F401 can run and
catch the ones that are simply dead. These tests pin the parts ruff cannot
— that the suppression stays gone, and that the names the docs promise
really are on the package.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import dacli

REPO = Path(__file__).resolve().parent.parent
INIT = REPO / "dacli" / "__init__.py"


def _init_tree() -> ast.Module:
    return ast.parse(INIT.read_text())


class TestTheBlanketSuppressionStaysGone:
    def test_f401_is_not_ignored_for_the_package_init(self):
        """A one-word edit to pyproject.toml restores the blind spot.

        It would not fail anything else — the imports still work, the
        suite still passes, and the next dead import goes unnoticed. So
        the absence of the ignore is itself worth asserting.
        """
        # tomllib is 3.11+ and this project supports 3.10, so the check
        # skips on the oldest supported interpreter rather than failing it.
        # Same idiom as tests/test_package_layout.py.
        tomllib = pytest.importorskip("tomllib", reason="tomllib is 3.11+ (PEP 680)")
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
        ignores = cfg["tool"]["ruff"]["lint"]["per-file-ignores"]["dacli/__init__.py"]
        assert "F401" not in ignores, (
            "F401 is suppressed for dacli/__init__.py again. Re-exports should "
            "carry an explicit `X as X` alias instead — that is what lets ruff "
            "distinguish a deliberate re-export from a dead import."
        )

    def test_no_blanket_noqa_on_the_import_block(self):
        """The other way to reintroduce it."""
        offenders = [
            f"{INIT.name}:{i}: {line.strip()}"
            for i, line in enumerate(INIT.read_text().splitlines(), 1)
            if "noqa" in line and "F401" in line
        ]
        assert not offenders, "F401 suppressed inline:\n" + "\n".join(offenders)


class TestEveryImportIsAccountedFor:
    """The rule the suppression used to hide: a name bound by an import in
    the package init must be one of three things — used in the file, part
    of the declared public API, or an explicitly-marked re-export.
    """

    def test_every_from_import_is_used_exported_or_aliased(self):
        tree = _init_tree()
        all_names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "__all__" for t in node.targets
            ):
                all_names = {e.value for e in node.value.elts}
        assert all_names, "__all__ not found — this test is not checking what it thinks"

        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                root = node
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name):
                    used.add(root.id)

        unaccounted = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                explicit_reexport = alias.asname == alias.name
                if explicit_reexport or bound in all_names or bound in used:
                    continue
                unaccounted.append(f"line {node.lineno}: {alias.name}")

        assert not unaccounted, (
            "imported but neither used, exported, nor marked as a re-export:\n  "
            + "\n  ".join(unaccounted)
            + "\nAdd `as <name>` if it is a deliberate re-export, or delete it."
        )

    def test_the_names_deleted_in_this_change_stay_deleted(self):
        """A regression guard with a short shelf life, deliberately.

        Each of these was reachable as `dacli.<name>` and is not any
        more. If something turns out to need one, the fix is to import it
        from the submodule that owns it — not to re-add a package-level
        alias, which is how the pile grew the first time.
        """
        for name in ("atexit", "sqlite3", "threading", "webbrowser", "HOME"):
            assert not hasattr(dacli, name), (
                f"dacli.{name} is back; import it from the module that owns it instead"
            )

    @pytest.mark.parametrize(
        ("name", "owner"),
        [
            ("_is_loopback", "dacli.auth"),
            ("_delays", "dacli.sync"),
            ("_record_sync_summary", "dacli.sync"),
            ("_retry_backoff", "dacli.net"),
        ],
    )
    def test_the_private_helpers_are_still_reachable_where_they_live(self, name, owner):
        """The control on those deletions.

        These were dropped from the package because every caller already
        reaches them through their own module. That has to keep being
        true, or the deletion broke something quietly.
        """
        import importlib

        assert hasattr(importlib.import_module(owner), name)


class TestTheDocumentedConstantsAreOnThePackage:
    """The reason the tunables were kept rather than deleted with the rest.

    docs/reference/configuration.md documents them as constants a reader
    can go and look at, so the re-exports are load-bearing for the docs
    even though no code reads them through the package.
    """

    @staticmethod
    def _documented() -> list[str]:
        doc = (REPO / "docs/reference/configuration.md").read_text()
        section = doc.split("## Defaults that aren't user-settable", 1)[1]
        section = section.split("\n## ", 1)[0]
        names: list[str] = []
        for line in section.splitlines():
            if not line.startswith("|") or line.startswith("|---"):
                continue
            cell = line.split("|")[1]
            names += re.findall(r"`([A-Z][A-Z0-9_]*)`", cell)
        return sorted(set(names))

    def test_the_table_was_actually_parsed(self):
        found = self._documented()
        assert len(found) > 10, f"only parsed {found} — the table format probably moved"

    def test_every_documented_constant_resolves_on_the_package(self):
        missing = [n for n in self._documented() if not hasattr(dacli, n)]
        assert not missing, (
            f"documented in configuration.md but not reachable as dacli.<name>: {missing}"
        )

    def test_every_all_entry_resolves(self):
        """Cheap, and it catches a mis-typed __all__ entry, which would
        otherwise only surface as an AttributeError in someone's
        `from dacli import *`.
        """
        missing = [n for n in dacli.__all__ if not hasattr(dacli, n)]
        assert not missing, f"__all__ names that do not exist: {missing}"
