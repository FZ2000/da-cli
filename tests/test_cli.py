"""Tests for argparse wiring + small CLI commands (`config show`, `whoami` etc.)."""

from __future__ import annotations

import json
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

import dacli


class TestParserWiring:
    def test_top_level_help_runs(self, capsys):
        with pytest.raises(SystemExit) as exc:
            dacli.build_parser().parse_args(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "DeviantArt CLI" in out

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            dacli.build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        assert dacli.__version__ in capsys.readouterr().out

    @pytest.mark.parametrize(
        "argv",
        [
            ["auth"],
            ["auth", "logout"],
            ["whoami"],
            ["refresh"],
            ["config", "show"],
            ["config", "set", "client_id", "12345"],
            ["config", "get", "client_id"],
            ["config", "unset", "client_id"],
            ["config", "path"],
            ["sync", "feed"],
            ["sync", "artist", "alice"],
            ["sync", "watched"],
            ["sync", "watched", "--via-feed"],
            ["search", "popular"],
            ["search", "newest"],
            ["search", "tag", "abstract"],
            ["search", "user", "alice", "bob"],
            ["user", "profile", "alice"],
            ["watch", "list"],
            ["deviation", "show", "DEVID-1"],
            ["daily"],
            ["daily", "2026-04-26"],
        ],
    )
    def test_parses_known_invocation(self, argv):
        # Just confirms the argparse tree accepts these without error.
        ns = dacli.build_parser().parse_args(argv)
        assert hasattr(ns, "func"), f"missing func handler for {argv!r}"

    def test_unknown_command_errors(self, capsys):
        with pytest.raises(SystemExit) as exc:
            dacli.build_parser().parse_args(["wat"])
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# cmd_config_show
# ---------------------------------------------------------------------------
class TestCmdConfigShow:
    def test_masks_secret_in_output(self, isolated_paths, no_keychain, capsys):
        cfg_path: Path = isolated_paths["cfg"]
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps({"client_id": "abc", "client_secret": "NOT-A-REAL-SECRET-FOR-TESTS-0000"})
        )
        ns = dacli.build_parser().parse_args(["config", "show"])
        ns.func(ns)
        out = capsys.readouterr().out
        assert "abc" in out  # client_id visible
        assert "NOT-A-REAL-SECRET-FOR-TESTS-0000" not in out  # raw secret not visible
        assert "NOT-...0000" in out  # masked form: first 4, last 4

    def test_lists_paths(self, isolated_paths, no_keychain, capsys):
        ns = dacli.build_parser().parse_args(["config", "show"])
        ns.func(ns)
        out = capsys.readouterr().out
        assert "config file:" in out
        assert "state file:" in out


# ---------------------------------------------------------------------------
# cmd_config_get / cmd_config_unset
# ---------------------------------------------------------------------------
class TestCmdConfigGet:
    def test_existing_key_prints_value(self, isolated_paths, no_keychain, capsys):
        dacli.set_config_field("client_id", "12345")
        ns = dacli.build_parser().parse_args(["config", "get", "client_id"])
        ns.func(ns)
        assert "12345" in capsys.readouterr().out

    def test_secret_masked_by_default(self, isolated_paths, no_keychain, capsys):
        dacli.set_config_field("client_secret", "NOT-A-REAL-SECRET-FOR-TESTS-0000")
        ns = dacli.build_parser().parse_args(["config", "get", "client_secret"])
        ns.func(ns)
        out = capsys.readouterr().out
        assert "NOT-A-REAL-SECRET-FOR-TESTS-0000" not in out
        assert "NOT-...0000" in out

    def test_secret_unmasked_with_flag_goes_to_stderr(self, isolated_paths, no_keychain, capsys):
        """`--unmask` must write the raw secret to STDERR so piping stdout to
        a file (`da config get client_secret --unmask > cfg.txt`) does NOT
        silently capture the secret. Masked output and non-secret values
        still go to stdout."""
        dacli.set_config_field("client_secret", "NOT-A-REAL-SECRET-FOR-TESTS-0000")
        ns = dacli.build_parser().parse_args(["config", "get", "client_secret", "--unmask"])
        ns.func(ns)
        captured = capsys.readouterr()
        assert "NOT-A-REAL-SECRET-FOR-TESTS-0000" in captured.err
        assert "NOT-A-REAL-SECRET-FOR-TESTS-0000" not in captured.out

    def test_missing_key_exits(self, isolated_paths, no_keychain):
        ns = dacli.build_parser().parse_args(["config", "get", "client_id"])
        with pytest.raises(SystemExit) as exc:
            ns.func(ns)
        assert exc.value.code == 1


class TestCmdConfigUnset:
    def test_removes_from_file(self, isolated_paths, no_keychain):
        dacli.set_config_field("client_id", "x")
        ns = dacli.build_parser().parse_args(["config", "unset", "client_id"])
        ns.func(ns)
        cfg = dacli.load_config()
        assert "client_id" not in cfg

    def test_missing_key_warns_no_crash(self, isolated_paths, no_keychain, capsys):
        ns = dacli.build_parser().parse_args(["config", "unset", "nonexistent"])
        ns.func(ns)
        err = capsys.readouterr().err
        assert "nothing to unset" in err.lower()


# ---------------------------------------------------------------------------
# cmd_auth_logout
# ---------------------------------------------------------------------------
class TestCmdAuthLogout:
    def test_removes_state_file(self, isolated_paths, capsys):
        dacli.save_state({"access_token": "T"})
        assert isolated_paths["st"].exists()
        ns = dacli.build_parser().parse_args(["auth", "logout"])
        ns.func(ns)
        assert not isolated_paths["st"].exists()

    def test_no_state_file_is_quiet(self, isolated_paths, capsys):
        ns = dacli.build_parser().parse_args(["auth", "logout"])
        ns.func(ns)
        out = capsys.readouterr().out
        assert "not logged in" in out


# ---------------------------------------------------------------------------
# cmd_whoami
# ---------------------------------------------------------------------------
class TestCmdWhoami:
    def test_403_on_whoami_does_not_crash(self, isolated_paths, no_keychain, capsys):
        dacli.save_state(
            {
                "access_token": "T",
                "expires_at": time.time() + 3600,
                "refresh_token": "rt",
                "scope": "browse",
            }
        )

        def fake_json(url, **_kw):
            if "/placebo" in url:
                return {"status": "success"}
            if "/user/whoami" in url:
                raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)
            return {}

        with patch.object(dacli, "http_json", side_effect=fake_json):
            ns = dacli.build_parser().parse_args(["whoami"])
            ns.func(ns)
        # Should have warned, not crashed
        captured = capsys.readouterr()
        assert "user" in (captured.out + captured.err).lower()

    def test_placebo_failure_exits(self, isolated_paths, no_keychain):
        dacli.save_state(
            {
                "access_token": "T",
                "expires_at": time.time() + 3600,
                "refresh_token": "rt",
            }
        )
        with patch.object(dacli, "http_json", return_value={"status": "error"}):
            ns = dacli.build_parser().parse_args(["whoami"])
            with pytest.raises(SystemExit):
                ns.func(ns)


# ---------------------------------------------------------------------------
# cmd_refresh
# ---------------------------------------------------------------------------
class TestCmdRefresh:
    def test_force_refresh_invalidates_cached(self, isolated_paths, no_keychain):
        dacli.save_state(
            {"access_token": "old", "expires_at": time.time() + 3600, "refresh_token": "rt"}
        )

        def fake_post(url, data, **_kw):
            return {"access_token": "new", "expires_in": 3600}

        with patch.object(dacli, "http_post_json", side_effect=fake_post):
            ns = dacli.build_parser().parse_args(["refresh"])
            ns.cmd_id = "x"  # not used, just ensure no AttributeError
            # Need client_id for refresh path
            dacli.set_config_field("client_id", "12345")
            ns.func(ns)
        assert dacli.load_state()["access_token"] == "new"


# ---------------------------------------------------------------------------
# main: keyboard interrupt
# ---------------------------------------------------------------------------
class TestMain:
    def test_keyboard_interrupt_exits_130(self, isolated_paths):
        def boom(_args):
            raise KeyboardInterrupt

        # Wire a parser that maps a fake command to `boom`
        def fake_parser():
            import argparse

            p = argparse.ArgumentParser()
            sub = p.add_subparsers(dest="cmd", required=True)
            x = sub.add_parser("x")
            x.set_defaults(func=boom)
            return p

        with patch.object(dacli, "build_parser", fake_parser):
            with pytest.raises(SystemExit) as exc:
                dacli.main(["x"])
        assert exc.value.code == 130
