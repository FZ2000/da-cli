"""Correctness details in the command layer that no test could reach.

Three of these were invisible to the suite by construction:

* Four modules used ``urllib`` submodules they never imported, and worked
  only because ``dacli/__init__`` happened to import them first. Import
  order is not a contract, and in ``user.py`` the unresolvable name sat
  *inside* an ``except`` clause — so the AttributeError would replace the
  HTTP error it was meant to handle.
* ``search user`` built its own ``urlopen`` call, bypassing the HTTP
  layer the whole suite stubs. Nothing in ``tests/`` could observe it, so
  a regression there was undetectable.
* Two of the eight ``--json`` emitters escaped non-ASCII while the other
  six did not, so a script consuming both saw two encodings of the same
  DeviantArt title.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import dacli

REPO_ROOT = Path(__file__).resolve().parent.parent
DACLI = REPO_ROOT / "dacli"


def _urllib_submodules(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(used, imported) urllib submodule names for one parsed module."""
    used = {
        node.value.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "urllib"
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "urllib" and len(parts) > 1:
                    imported.add(parts[1])
    return used, imported


@pytest.mark.parametrize(
    "module", sorted(DACLI.rglob("*.py")), ids=lambda p: str(p.relative_to(DACLI))
)
def test_every_urllib_submodule_used_is_imported(module: Path):
    """`import urllib` does not make `urllib.error` resolvable.

    Submodules bind only when something imports them. These modules
    worked because `dacli/__init__` imports urllib.request first, which
    is an accident of import order rather than a guarantee — and one that
    changes the moment a module is imported on its own.
    """
    used, imported = _urllib_submodules(ast.parse(module.read_text(encoding="utf-8")))
    missing = used - imported
    assert not missing, (
        f"{module.relative_to(REPO_ROOT)} uses "
        f"{', '.join('urllib.' + m for m in sorted(missing))} without importing it"
    )


class TestSearchUserGoesThroughTheHttpLayer:
    """It must be stubbable, retryable and loggable like every other call."""

    def test_it_calls_http_post_json(self, monkeypatch, capsys):
        calls: list[tuple[str, object]] = []

        def fake_post(url, form, **kwargs):
            calls.append((url, form))
            return {"results": [{"username": "alice", "userid": "U-1", "type": "regular"}]}

        monkeypatch.setattr(dacli, "http_post_json", fake_post)
        monkeypatch.setattr(dacli, "load_config", dict)
        monkeypatch.setattr(dacli, "load_state", dict)
        monkeypatch.setattr(dacli, "access_token", lambda cfg, state: "TOKEN")

        import argparse

        dacli.cmd_search_user(argparse.Namespace(query=["alice"], json=False))

        assert len(calls) == 1, "the command bypassed the stubbed HTTP layer"
        assert "user/whois" in calls[0][0]
        assert "@alice" in capsys.readouterr().out

    def test_multiple_usernames_repeat_the_bracket_key(self):
        """`usernames[]=a&usernames[]=b` is the shape DA requires.

        The old hand-built body existed because urlencode was believed
        unable to produce it; `doseq=True` does exactly that.
        """
        req = dacli.net._request(
            "https://example.invalid/user/whois",
            method="POST",
            form={"usernames[]": ["alice", "bob"]},
        )
        assert req.data == b"usernames%5B%5D=alice&usernames%5B%5D=bob"

    def test_scalar_form_values_encode_unchanged(self):
        """The control: doseq must not alter how existing callers encode.

        The token exchange goes through this same builder, so a change in
        its body shape would break authentication outright.
        """
        req = dacli.net._request(
            "https://example.invalid/token",
            method="POST",
            form={"grant_type": "refresh_token", "client_id": "12345"},
        )
        assert req.data == b"grant_type=refresh_token&client_id=12345"


class TestJsonOutputIsConsistent:
    def test_every_json_emitter_keeps_unicode(self):
        """All eight `--json` paths must agree on how they encode a title.

        Two escaped non-ASCII while six did not, so a script reading both
        got `\\u6708` from one command and `月` from another for the same
        deviation.
        """
        offenders: list[str] = []
        for module in sorted((DACLI / "commands").glob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # Only what is PRINTED. json.dumps into _atomic_write is a
                # file, where ASCII escaping is a valid on-disk encoding
                # and changing it would rewrite config.json's format for
                # no benefit.
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                ):
                    continue
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Attribute)
                        and arg.func.attr == "dumps"
                        and "ensure_ascii" not in {kw.arg for kw in arg.keywords}
                    ):
                        offenders.append(f"{module.name}:{arg.lineno}")
        assert not offenders, (
            f"json.dumps for user-facing output without ensure_ascii=False: {', '.join(offenders)}"
        )

    def test_a_cjk_title_survives_a_json_dump(self):
        """What the flag actually buys, stated as behaviour."""
        rendered = json.dumps({"title": "月の幻想"}, indent=2, ensure_ascii=False)
        assert "月の幻想" in rendered


class TestApiDataIsReadDefensively:
    def test_a_tag_without_tag_name_does_not_crash_the_printout(self, monkeypatch, capsys):
        """Bare `t['tag_name']` raised KeyError partway through printing.

        Every other field on this command uses .get(), so a malformed tag
        entry aborted the command after several lines had already reached
        stdout — leaving the user with half a record and a traceback.
        """
        monkeypatch.setattr(dacli, "load_config", dict)
        monkeypatch.setattr(dacli, "load_state", dict)
        monkeypatch.setattr(dacli, "access_token", lambda cfg, state: "TOKEN")
        monkeypatch.setattr(
            dacli,
            "http_json",
            lambda url, **kw: {
                "metadata": [
                    {
                        "deviationid": "D-1",
                        "title": "T",
                        "author": {"username": "alice"},
                        "tags": [{"tag_name": "nature"}, {"unexpected": "shape"}],
                    }
                ]
            },
        )

        import argparse

        dacli.cmd_deviation_show(argparse.Namespace(deviationid="D-1", json=False))

        out = capsys.readouterr().out
        assert "nature" in out
        assert "tags:" in out


class TestTheErrorHierarchyDescribesItselfHonestly:
    """The docstring must keep matching what the code does.

    This class previously pinned the opposite claim — that the four
    subclasses were declared but unraised, and said so in their
    docstrings. Three of them are raised now, so those assertions flipped
    and failed, which is exactly what they were for: the docs stopped
    being true loudly instead of quietly.
    """

    RAISED = ("ConfigError", "AuthError", "HttpError")
    RESERVED = ("SyncError",)

    def test_the_worked_example_is_actually_supported(self):
        """`DacliError`'s docstring shows callers catching by category.

        It used to show that while nothing raised any of them, so the
        example could not work. It has to keep working now.
        """
        doc = dacli.DacliError.__doc__ or ""
        assert "except dacli.AuthError" in doc
        assert "main()" in doc, "the docstring should still state the CLI's exit-code contract"

    @pytest.mark.parametrize("name", RAISED)
    def test_the_live_exceptions_are_raised_somewhere(self, name):
        """Each of these is claimed as usable; something must raise it."""
        sites = self._raise_sites()
        assert name in sites, (
            f"{name} is documented as raised by da-cli but nothing raises it — "
            f"either raise it or mark it reserved"
        )

    @pytest.mark.parametrize("name", RESERVED)
    def test_the_reserved_exceptions_say_so(self, name):
        exc = getattr(dacli, name)
        assert "Reserved" in (exc.__doc__ or ""), (
            f"{name} is documented as if it were raised somewhere"
        )
        assert name not in self._raise_sites(), (
            f"{name} is raised now — drop 'Reserved' from its docstring"
        )

    @staticmethod
    def _raise_sites() -> dict[str, list[str]]:
        """{exception name: ["module:line", ...]} across dacli/."""
        found: dict[str, list[str]] = {}
        for module in sorted(DACLI.rglob("*.py")):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                    name = getattr(node.exc.func, "id", None) or getattr(
                        node.exc.func, "attr", None
                    )
                    if name in {"ConfigError", "AuthError", "HttpError", "SyncError"}:
                        found.setdefault(name, []).append(f"{module.name}:{node.lineno}")
        return found
