"""Tests for small pure helpers: safe_filename, mask_secret, _atomic_write,
jittered, jitter_sleep."""

from __future__ import annotations

import json
import random
import statistics
from pathlib import Path
from unittest.mock import patch

import pytest

import dacli


# ---------------------------------------------------------------------------
# safe_filename
# ---------------------------------------------------------------------------
class TestSafeFilename:
    def test_strips_unsafe_chars(self):
        assert dacli.safe_filename("a/b\\c:d?e*f") == "a_b_c_d_e_f"

    def test_collapses_runs(self):
        # Multiple consecutive bad chars collapse to a single underscore
        assert dacli.safe_filename("a???b") == "a_b"

    def test_strips_leading_trailing_underscores(self):
        assert dacli.safe_filename("///name///") == "name"

    def test_truncates_long_names(self):
        s = "x" * 200
        assert len(dacli.safe_filename(s, n=100)) == 100

    def test_empty_or_none_returns_untitled(self):
        assert dacli.safe_filename(None) == "untitled"
        assert dacli.safe_filename("") == "untitled"

    def test_only_unsafe_chars_returns_untitled(self):
        assert dacli.safe_filename("!@#$%") == "untitled"

    def test_preserves_dots_dashes_underscores(self):
        assert dacli.safe_filename("a.b-c_d") == "a.b-c_d"


# ---------------------------------------------------------------------------
# mask_secret
# ---------------------------------------------------------------------------
class TestMaskSecret:
    def test_long_secret_shows_prefix_and_suffix(self):
        out = dacli.mask_secret("0123456789abcdef0123456789abcdef")
        assert out.startswith("0123")
        assert out.endswith("cdef")
        assert "..." in out

    def test_short_secret_fully_masked(self):
        assert dacli.mask_secret("abc") == "*****"

    def test_exactly_eight_char_secret_fully_masked(self):
        assert dacli.mask_secret("12345678") == "*****"

    def test_falsy_passthrough(self):
        assert dacli.mask_secret("") == ""
        assert dacli.mask_secret(None) is None


# ---------------------------------------------------------------------------
# _atomic_write
# ---------------------------------------------------------------------------
class TestAtomicWrite:
    def test_creates_file_with_content(self, tmp_path: Path):
        target = tmp_path / "subdir" / "out.json"
        dacli._atomic_write(target, '{"k":"v"}')
        assert target.read_text() == '{"k":"v"}'

    def test_default_mode_is_0600(self, tmp_path: Path):
        target = tmp_path / "out.json"
        dacli._atomic_write(target, "x")
        assert (target.stat().st_mode & 0o777) == 0o600

    def test_custom_mode_applied(self, tmp_path: Path):
        target = tmp_path / "out.json"
        dacli._atomic_write(target, "x", mode=0o644)
        assert (target.stat().st_mode & 0o777) == 0o644

    def test_overwrites_existing(self, tmp_path: Path):
        target = tmp_path / "out.json"
        target.write_text("old")
        dacli._atomic_write(target, "new")
        assert target.read_text() == "new"

    def test_temp_file_cleaned_up_on_success(self, tmp_path: Path):
        target = tmp_path / "out.json"
        dacli._atomic_write(target, "x")
        # No leftover .tmp
        assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# jitter
# ---------------------------------------------------------------------------
class TestJittered:
    def test_zero_pct_returns_base(self):
        assert dacli.jittered(1.5, 0.0) == 1.5
        assert dacli.jittered(10.0, 0.0) == 10.0

    def test_negative_pct_returns_base(self):
        # Defensive — negative pct is treated as 0
        assert dacli.jittered(1.5, -0.5) == 1.5

    def test_pct_above_floor(self):
        # pct=2.0 should clamp to 0.95, so range = base * [0.05, 1.95]
        random.seed(42)
        samples = [dacli.jittered(1.0, 2.0) for _ in range(500)]
        assert min(samples) >= 0.05
        assert max(samples) <= 1.96  # 1.0 * 1.95 + tiny epsilon

    def test_pct_zero_floor(self):
        # tiny base + high pct should be clamped to 0.05 minimum
        random.seed(0)
        for _ in range(50):
            assert dacli.jittered(0.0001, 0.9) >= 0.05

    def test_distribution_is_uniform_around_base(self):
        # 1000 samples at base=10, pct=0.4 should mean ≈ 10 with stdev ~ 10*0.4/√3
        random.seed(123)
        samples = [dacli.jittered(10.0, 0.4) for _ in range(2000)]
        assert 9.5 < statistics.mean(samples) < 10.5
        # Range should span [6, 14]
        assert min(samples) >= 6.0
        assert max(samples) <= 14.0


class TestJitterSleep:
    def test_sleeps_jittered_amount(self):
        with patch("time.sleep") as sleep_mock:
            random.seed(0)
            dacli.jitter_sleep(2.0, 0.0)
        assert sleep_mock.call_count == 1
        called_with = sleep_mock.call_args[0][0]
        assert called_with == 2.0

    def test_with_jitter_calls_sleep_with_perturbed_value(self):
        with patch("time.sleep") as sleep_mock:
            random.seed(7)
            dacli.jitter_sleep(2.0, 0.5)
        called_with = sleep_mock.call_args[0][0]
        assert 1.0 <= called_with <= 3.0


# ---------------------------------------------------------------------------
# mature_content_param
# ---------------------------------------------------------------------------
class TestMatureContentParam:
    """The DA API expects the literal strings 'true' / 'false' (lowercase)
    for the mature_content query param on every browse / search endpoint.
    Centralising this in mature_content_param() means there's one place
    to change if DA ever flips to '0'/'1' or integer encoding. These tests
    pin the current encoding."""

    def test_true_input_returns_string_true(self) -> None:
        assert dacli.mature_content_param(True) == "true"

    def test_false_input_returns_string_false(self) -> None:
        assert dacli.mature_content_param(False) == "false"

    def test_return_type_is_str(self) -> None:
        # Don't let a future refactor accidentally return a bool — that
        # would silently produce 'True'/'False' (Python's str(bool)) in
        # the URL.
        assert isinstance(dacli.mature_content_param(True), str)
        assert isinstance(dacli.mature_content_param(False), str)


# ---------------------------------------------------------------------------
# __all__ shape — pin the public surface so external re-imports are stable.
# ---------------------------------------------------------------------------
class TestPublicSurface:
    """`__all__` declares what `from dacli import *` and external
    re-imports get. Every name in it must be a real attribute on the
    module (a typo here would silently hide a public symbol)."""

    def test_all_entries_resolve_to_real_attributes(self) -> None:
        missing = [n for n in dacli.__all__ if not hasattr(dacli, n)]
        assert missing == [], f"__all__ references missing names: {missing}"

    def test_all_contains_command_locked_error(self) -> None:
        # Renamed from CommandLocked per PEP 8 N818. Pin the new name so
        # downstream callers catching it programmatically don't silently
        # break across the rename.
        assert "CommandLockedError" in dacli.__all__
        assert hasattr(dacli, "CommandLockedError")
        assert issubclass(dacli.CommandLockedError, Exception)

    def test_all_does_not_leak_private_helpers(self) -> None:
        # Leading-underscore names are private; they must never appear
        # in the public surface.
        private_leaked = [n for n in dacli.__all__ if n.startswith("_") and n != "__version__"]
        assert private_leaked == [], f"private names leaked into __all__: {private_leaked}"


# ---------------------------------------------------------------------------
# Exception hierarchy — pin the public surface so wrappers can catch by category.
# ---------------------------------------------------------------------------
class TestExceptionHierarchy:
    """`DacliError` is the umbrella; specific subclasses categorise the
    failure mode so external scripts can `except dacli.AuthError` without
    catching unrelated Python exceptions. These tests pin the hierarchy
    shape — a refactor that accidentally re-parents one of these would
    silently break every `except dacli.DacliError` consumer."""

    def test_command_locked_error_is_dacli_error(self) -> None:
        assert issubclass(dacli.CommandLockedError, dacli.DacliError)

    def test_config_error_is_dacli_error(self) -> None:
        assert issubclass(dacli.ConfigError, dacli.DacliError)

    def test_auth_error_is_dacli_error(self) -> None:
        assert issubclass(dacli.AuthError, dacli.DacliError)

    def test_http_error_is_dacli_error(self) -> None:
        assert issubclass(dacli.HttpError, dacli.DacliError)

    def test_sync_error_is_dacli_error(self) -> None:
        assert issubclass(dacli.SyncError, dacli.DacliError)

    def test_dacli_error_is_exception(self) -> None:
        # The hierarchy is rooted at stdlib Exception (not BaseException
        # directly) so it doesn't accidentally shadow control-flow
        # exceptions like SystemExit / KeyboardInterrupt that the CLI
        # relies on. Exception is itself a BaseException subclass, so we
        # check the *direct* base relationship.
        assert dacli.DacliError.__bases__ == (Exception,)

    def test_catch_by_umbrella_catches_specific(self) -> None:
        # The whole point of the hierarchy: `except DacliError` catches
        # any of the specific subclasses.
        for exc_cls in (
            dacli.ConfigError,
            dacli.AuthError,
            dacli.HttpError,
            dacli.SyncError,
            dacli.CommandLockedError,
        ):
            with pytest.raises(exc_cls):
                raise exc_cls("test")
            # And the umbrella catches it too.
            with pytest.raises(dacli.DacliError):
                raise exc_cls("test")

    def test_all_exceptions_listed_in_all(self) -> None:
        # Public surface: every exception a wrapper might catch must be
        # exported via __all__.
        for name in ("DacliError", "ConfigError", "AuthError", "HttpError", "SyncError"):
            assert name in dacli.__all__


# ---------------------------------------------------------------------------
# _configure_output — translates global CLI flags into module state.
# ---------------------------------------------------------------------------
class TestConfigureOutput:
    """``--quiet`` / ``--verbose`` / ``--color`` set the module-level
    output state that ``log()`` reads. These tests drive the configure
    function directly and verify ``log`` honours the choice — keeps
    the contract tight without invoking the full CLI."""

    def setup_method(self) -> None:
        # Snapshot so each test starts from defaults.
        self._saved = dict(dacli._OUTPUT_STATE)

    def teardown_method(self) -> None:
        dacli._OUTPUT_STATE.clear()
        dacli._OUTPUT_STATE.update(self._saved)

    def test_quiet_suppresses_info_log(self, capsys) -> None:
        dacli._configure_output(quiet=True, verbose=False, color="never")
        dacli.log("info message", "info")
        dacli.log("warn message", "warn")
        captured = capsys.readouterr()
        assert "info message" not in captured.out
        assert "warn message" in captured.err

    def test_verbose_enables_debug_log(self, capsys) -> None:
        # Default verbosity: debug is suppressed on both streams.
        dacli._configure_output(quiet=False, verbose=False, color="never")
        dacli.log("debug A", "debug")
        silent = capsys.readouterr()
        assert "debug A" not in silent.out
        assert "debug A" not in silent.err
        # Verbose: debug surfaces on STDERR, not stdout. This test asserted
        # stdout, which is where the defect lived: net.py logs one debug
        # line per request, so `da -v ... --json` put "GET https://..." in
        # front of the payload and nothing could parse it.
        dacli._configure_output(quiet=False, verbose=True, color="never")
        dacli.log("debug B", "debug")
        loud = capsys.readouterr()
        assert "debug B" in loud.err
        assert "debug B" not in loud.out, "debug output belongs on stderr"

    def test_color_never_omits_ansi_codes(self, capsys) -> None:
        dacli._configure_output(quiet=False, verbose=False, color="never")
        dacli.log("oops", "error")
        captured = capsys.readouterr()
        assert "\033[" not in captured.err
        assert "oops" in captured.err

    def test_color_always_adds_ansi_codes(self, capsys) -> None:
        dacli._configure_output(quiet=False, verbose=False, color="always")
        dacli.log("oops", "error")
        captured = capsys.readouterr()
        assert "\033[31m" in captured.err  # red

    def test_quiet_and_verbose_are_mutually_exclusive_at_parser_level(self) -> None:
        # argparse's mutually_exclusive_group enforces this; pin the
        # contract by trying both and expecting SystemExit (argparse's
        # error path).
        with pytest.raises(SystemExit):
            dacli.build_parser().parse_args(["--quiet", "--verbose", "config", "show"])

    def test_config_flag_repoints_module_paths(self, isolated_paths, tmp_path, capsys) -> None:
        """`da --config PATH ...` must make every read use PATH.

        This drives the real ``main()``. The previous version copied
        main()'s repoint logic into the test body under the comment
        "Simulate main()'s repoint logic" and asserted on the copy — so
        deleting the handling from main() outright would not have failed
        it.
        """
        alt = tmp_path / "alt.json"
        alt.write_text('{"client_id": "ALT", "destination": "/tmp/alt-dest"}')

        dacli.main(["--config", str(alt), "config", "show"])

        out = capsys.readouterr().out
        assert "ALT" in out, f"--config was not honoured by main(); got: {out!r}"
        # And the module-level paths main() rewrote are the ones the
        # rest of the CLI reads through.
        assert alt == dacli.CONFIG_PATH
        assert alt.parent == dacli.CONFIG_DIR
        assert dacli.load_config().get("client_id") == "ALT"

    def test_config_flag_expands_user(self, isolated_paths, tmp_path, monkeypatch) -> None:
        """`--config ~/x.json` must expand, not create a literal "~" dir."""
        monkeypatch.setenv("HOME", str(tmp_path))
        alt = tmp_path / "tilde.json"
        alt.write_text('{"client_id": "TILDE"}')

        dacli.main(["--config", "~/tilde.json", "config", "show"])

        assert alt == dacli.CONFIG_PATH
        assert not (tmp_path / "~").exists()


class TestSafeFilenameDotOnly:
    """Regression: dot-only names are path traversal, not filenames."""

    def test_double_dot_becomes_untitled(self):
        assert dacli.safe_filename("..") == "untitled"

    def test_single_dot_becomes_untitled(self):
        assert dacli.safe_filename(".") == "untitled"

    def test_many_dots_become_untitled(self):
        assert dacli.safe_filename("...") == "untitled"

    def test_dot_prefixed_name_is_preserved(self):
        # Only dot-ONLY names are unsafe; a leading dot is fine.
        assert dacli.safe_filename(".hidden") == ".hidden"


class TestConfigFlagScope:
    """`--config` moves the config file and nothing else.

    Its help text used to call it "useful for multi-account setups",
    which it is not on its own: `main()` rewrites CONFIG_PATH and
    CONFIG_DIR, so two config files still share one state.json, one feed
    checkpoint and one index. Someone following that advice would have
    two accounts writing over each other's tokens.

    These pin the real contract in both directions, so neither the flag
    nor the wording can drift back.
    """

    def test_config_path_moves(self, isolated_paths, tmp_path, capsys):
        alt = tmp_path / "alt.json"
        alt.write_text(json.dumps({"client_id": "ALT"}))
        dacli.main(["--config", str(alt), "config", "show"])
        capsys.readouterr()
        assert alt == dacli.CONFIG_PATH
        assert alt.parent == dacli.CONFIG_DIR

    def test_state_and_index_do_not_move(self, isolated_paths, tmp_path, capsys):
        before = (dacli.STATE_PATH, dacli.INDEX_PATH)
        alt = tmp_path / "alt.json"
        alt.write_text(json.dumps({"client_id": "ALT"}))
        dacli.main(["--config", str(alt), "config", "show"])
        capsys.readouterr()
        assert before == (dacli.STATE_PATH, dacli.INDEX_PATH), (
            "--config moved the state or index; if that is now intended, the help "
            "text and docs/reference/configuration.md must say so"
        )

    def test_the_help_text_does_not_promise_multi_account(self):
        """The wording is the bug this pins, as much as the behaviour."""
        for action in dacli.build_parser()._actions:
            if "--config" in action.option_strings:
                help_text = (action.help or "").lower()
                assert "xdg_state_home" in help_text, (
                    "the --config help must point at the env vars that actually "
                    "give a separate account"
                )
                return
        raise AssertionError("--config not found on the parser")
