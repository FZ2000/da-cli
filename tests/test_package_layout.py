"""The package's own structural invariants.

These guard mistakes that a passing test suite would otherwise hide.
"""

from __future__ import annotations

import pathlib
import pkgutil
import sys

import pytest

import dacli

REPO = pathlib.Path(__file__).resolve().parent.parent


def _submodule_names() -> set[str]:
    return {m.name for m in pkgutil.iter_modules(dacli.__path__)}


class TestNoStdlibShadowing:
    """A submodule may not share a name with a stdlib module dacli imports.

    Importing ``dacli.http`` binds ``http`` as an attribute of the
    ``dacli`` package, overwriting the stdlib ``http`` that
    ``dacli/auth.py`` imports for ``http.server`` (it was
    ``dacli/__init__.py`` until that import went the way of the other 28
    the F401 suppression was hiding). The OAuth loopback listener then
    dies with
    ``module 'dacli.http' has no attribute 'server'`` — and only three
    tests caught it, all in one file.
    """

    def test_no_submodule_shadows_a_stdlib_module_we_import(self):
        stdlib = set(sys.stdlib_module_names)
        clashes = sorted(_submodule_names() & stdlib)
        assert not clashes, (
            f"submodule(s) {clashes} shadow stdlib modules of the same name; "
            f"rename them (dacli/http.py had to become dacli/net.py)"
        )


class TestPublicSurface:
    def test_every_all_entry_resolves(self):
        missing = [n for n in dacli.__all__ if not hasattr(dacli, n)]
        assert not missing, f"__all__ lists names the package does not expose: {missing}"

    def test_stdlib_http_server_is_reachable(self):
        """The exact breakage the rename fixed."""
        import http.server

        assert hasattr(http.server, "BaseHTTPRequestHandler")


class TestShippedPackaging:
    """Both install paths must carry the whole package.

    The dev install is editable, so it imports the source tree directly
    and cannot see either fault below. The CI ``artifact`` job builds and
    runs the real thing; these tests are the fast local equivalent, so a
    packaging regression fails in seconds rather than after a push.
    """

    def test_package_discovery_is_a_glob_not_a_list(self):
        """An explicit list omits subpackages added later.

        ``packages = ["dacli"]`` shipped a wheel with no
        ``dacli.commands``: importable at the top level, then
        ``ModuleNotFoundError`` on the first subcommand.
        """
        # tomllib is 3.11+ and this project supports 3.10. The rest of
        # the matrix, the lint job, and the CI `artifact` job all cover
        # this, so skipping the oldest leg costs no real coverage.
        tomllib = pytest.importorskip("tomllib", reason="tomllib is 3.11+ (PEP 680)")
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
        setuptools_cfg = cfg["tool"]["setuptools"]
        assert "packages" not in setuptools_cfg or not isinstance(
            setuptools_cfg.get("packages"), list
        ), (
            "pyproject pins an explicit package list; a subpackage added later "
            "will be silently left out of the wheel. Use "
            "[tool.setuptools.packages.find] with include = ['dacli*']."
        )
        include = setuptools_cfg["packages"]["find"]["include"]
        assert any(pattern.endswith("*") for pattern in include), (
            f"packages.find include={include} matches no subpackages"
        )

    def test_installer_preserves_the_package_tree(self):
        """A flat copy collapses subpackages onto their parents.

        ``find dacli -name '*.py' -exec install {} share/dacli/`` wrote
        ``commands/config.py`` over ``config.py`` and ``commands/index.py``
        over ``index.py``, producing an installed CLI that could not start.
        """
        installer = (REPO / "install.sh").read_text()
        assert '-exec install -m 0644 {} "$SHARE_DIR/dacli/"' not in installer, (
            "install.sh copies every .py into one flat directory, which "
            "overwrites dacli/config.py with dacli/commands/config.py"
        )
