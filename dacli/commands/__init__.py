"""Command handlers, one module per command group.

Each module holds the ``cmd_*`` functions for one top-level command.
They are re-exported from ``dacli/__init__.py`` so ``dacli.cmd_x`` keeps
resolving for callers, the argument parser, and the test suite.

Like every module in the package, these read names the tests patch —
``http_json``, ``load_config``, ``log`` and friends — through the
``dacli`` package at call time rather than importing them. See
:doc:`ADR 0007 <../../docs/explanation/adr/0007-package-layout>`.
"""
