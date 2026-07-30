"""Tests for config + state file management, including the secret-priority chain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dacli


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------
class TestLoadConfig:
    def test_empty_returns_empty_dict(self, isolated_paths, no_keychain):
        assert dacli.load_config() == {}

    def test_reads_json_file(self, isolated_paths, no_keychain):
        cfg_path: Path = isolated_paths["cfg"]
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"client_id": "1234", "destination": "/tmp/da"}))
        cfg = dacli.load_config()
        assert cfg["client_id"] == "1234"
        assert cfg["destination"] == "/tmp/da"

    def test_env_overrides_file(self, isolated_paths, no_keychain, monkeypatch):
        cfg_path: Path = isolated_paths["cfg"]
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"client_id": "from_file"}))
        monkeypatch.setenv("DA_CLIENT_ID", "from_env")
        cfg = dacli.load_config()
        assert cfg["client_id"] == "from_env"

    def test_env_provides_destination_and_redirect_uri(
        self, isolated_paths, no_keychain, monkeypatch
    ):
        monkeypatch.setenv("DA_DESTINATION", "/data/da")
        monkeypatch.setenv("DA_REDIRECT_URI", "http://localhost:9999/")
        cfg = dacli.load_config()
        assert cfg["destination"] == "/data/da"
        assert cfg["redirect_uri"] == "http://localhost:9999/"

    def test_keychain_secret_used_when_file_lacks_it(self, isolated_paths, monkeypatch):
        # Force keychain hit
        monkeypatch.setattr(
            dacli, "_keychain_get", lambda key: "kc_value" if key == "client_secret" else None
        )
        cfg = dacli.load_config()
        assert cfg["client_secret"] == "kc_value"

    def test_invalid_json_logs_warning_and_returns_empty(self, isolated_paths, no_keychain, capsys):
        cfg_path: Path = isolated_paths["cfg"]
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("{not valid json")
        cfg = dacli.load_config()
        assert cfg == {}
        # The warning should mention the bad path
        err = capsys.readouterr().err
        assert "invalid JSON" in err


# ---------------------------------------------------------------------------
# set_config_field
# ---------------------------------------------------------------------------
class TestSetConfigField:
    def test_writes_non_secret_to_file(self, isolated_paths, no_keychain):
        dacli.set_config_field("client_id", "12345")
        on_disk = json.loads(isolated_paths["cfg"].read_text())
        assert on_disk["client_id"] == "12345"

    def test_secret_writes_to_keychain_when_available(self, isolated_paths, monkeypatch):
        captured: dict[str, str] = {}

        def fake_set(key: str, value: str) -> bool:
            captured[key] = value
            return True

        monkeypatch.setattr(dacli, "_keychain_set", fake_set)
        dacli.set_config_field("client_secret", "abc")
        assert captured == {"client_secret": "abc"}
        # Should NOT have written to JSON file
        assert not isolated_paths["cfg"].exists() or "client_secret" not in json.loads(
            isolated_paths["cfg"].read_text()
        )

    def test_secret_fallback_to_file_when_keychain_unavailable(self, isolated_paths, no_keychain):
        dacli.set_config_field("client_secret", "fallback")
        on_disk = json.loads(isolated_paths["cfg"].read_text())
        assert on_disk["client_secret"] == "fallback"

    def test_file_mode_is_0600(self, isolated_paths, no_keychain):
        dacli.set_config_field("client_id", "x")
        assert isolated_paths["cfg"].stat().st_mode & 0o777 == 0o600

    def test_preserves_other_fields(self, isolated_paths, no_keychain):
        cfg_path: Path = isolated_paths["cfg"]
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"client_id": "x", "destination": "/y"}))
        dacli.set_config_field("client_id", "z")
        result = json.loads(cfg_path.read_text())
        assert result["client_id"] == "z"
        assert result["destination"] == "/y"


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------
class TestState:
    def test_empty_state(self, isolated_paths):
        assert dacli.load_state() == {}

    def test_round_trip(self, isolated_paths):
        dacli.save_state({"access_token": "T", "expires_at": 123})
        assert dacli.load_state() == {"access_token": "T", "expires_at": 123}

    def test_save_creates_directory(self, isolated_paths):
        # Initial st_dir should not exist; save_state must create it
        assert not isolated_paths["st_dir"].exists()
        dacli.save_state({})
        assert isolated_paths["st_dir"].exists()

    def test_state_file_mode_is_0600(self, isolated_paths):
        dacli.save_state({"x": 1})
        assert isolated_paths["st"].stat().st_mode & 0o777 == 0o600

    def test_corrupt_json_returns_empty(self, isolated_paths, capsys):
        st: Path = isolated_paths["st"]
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text("not json")
        assert dacli.load_state() == {}
        # Operator should have been warned, not silently presented with
        # an empty state. The warning directs them to re-run auth.
        cap = capsys.readouterr()
        msg = (cap.out + cap.err).lower()
        assert "unparsable" in msg or "auth" in msg, f"got: {msg!r}"
        # The bad file is preserved with a .corrupt-<ts> suffix so it can
        # be inspected; the original path is now gone (so the next sync
        # treats state as empty cleanly).
        assert not st.exists()
        backups = list(st.parent.glob(st.name + ".corrupt-*"))
        assert len(backups) == 1, f"expected one backup, got {backups}"

    def test_corrupt_json_warns_only_once(self, isolated_paths, capsys):
        """load_state is called many times per command — without a memo, every
        call would re-warn. Verify the per-process flag deduplicates."""
        st: Path = isolated_paths["st"]
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text("not json")
        dacli.load_state()  # first call → warn + backup
        # Re-create a corrupt file so the *file* exists for the second call;
        # the memo should still suppress the second warning.
        st.write_text("also not json")
        capsys.readouterr()  # clear the first-call output
        dacli.load_state()
        # capsys.readouterr() returns a namedtuple of (out, err); a single
        # call captures both streams since the previous read. Calling it
        # twice silently returns empty strings the second time.
        captured = capsys.readouterr()
        combined = (captured.out + captured.err).lower()
        assert "unparsable" not in combined


class TestNonObjectJsonFiles:
    """Regression: valid JSON of the wrong shape used to crash.

    `[]`, `"x"`, `null` and `42` all parse cleanly and then break every
    downstream .get() with an AttributeError/TypeError. The contract for
    a bad config/state file is warn-and-ignore, not traceback.
    """

    def test_config_list_is_ignored(self, isolated_paths, capsys):
        dacli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.CONFIG_PATH.write_text("[]")
        cfg = dacli.load_config()
        assert cfg == {} or isinstance(cfg, dict)
        assert "JSON object" in capsys.readouterr().err

    def test_config_scalar_is_ignored(self, isolated_paths):
        dacli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.CONFIG_PATH.write_text("42")
        assert isinstance(dacli.load_config(), dict)

    def test_state_null_is_treated_as_corrupt(self, isolated_paths):
        dacli.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.STATE_PATH.write_text("null")
        assert dacli.load_state() == {}

    def test_state_list_is_treated_as_corrupt(self, isolated_paths):
        dacli.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.STATE_PATH.write_text('["not", "a", "dict"]')
        assert dacli.load_state() == {}


class TestSecretPrecedence:
    """env > Keychain > config.json, as documented.

    docs/reference/configuration.md states the order as
    "CLI flag > env var > macOS Keychain (secrets only) > config.json",
    and load_config's own docstring says "SECRET_KEYS are sourced from
    env > keychain". The code did the opposite for the file: it consulted
    the Keychain only when config.json had nothing.

    That makes rotation silently fail. `da config set client_secret NEW`
    writes to the Keychain; an older copy left in config.json keeps
    winning; every request goes out with the secret the user believes
    they just replaced.
    """

    def test_keychain_beats_config_file(self, isolated_paths, monkeypatch):
        dacli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.CONFIG_PATH.write_text(json.dumps({"client_secret": "OLD-FROM-FILE"}))
        monkeypatch.setattr(dacli, "_keychain_get", lambda k: "NEW-FROM-KEYCHAIN")

        assert dacli.load_config()["client_secret"] == "NEW-FROM-KEYCHAIN", (
            "config.json shadowed the Keychain, so rotating the secret has no effect"
        )

    def test_env_beats_keychain(self, isolated_paths, monkeypatch):
        monkeypatch.setenv("DA_CLIENT_SECRET", "FROM-ENV")
        monkeypatch.setattr(dacli, "_keychain_get", lambda k: "FROM-KEYCHAIN")
        assert dacli.load_config()["client_secret"] == "FROM-ENV"

    def test_config_file_used_when_keychain_is_empty(self, isolated_paths, monkeypatch):
        """Linux, or macOS before the secret was ever stored."""
        dacli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.CONFIG_PATH.write_text(json.dumps({"client_secret": "FROM-FILE"}))
        monkeypatch.setattr(dacli, "_keychain_get", lambda k: None)
        assert dacli.load_config()["client_secret"] == "FROM-FILE"

    def test_non_secrets_still_come_from_the_file(self, isolated_paths, monkeypatch):
        dacli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.CONFIG_PATH.write_text(json.dumps({"client_id": "ID-FROM-FILE"}))
        monkeypatch.setattr(dacli, "_keychain_get", lambda k: "SHOULD-NOT-APPLY")
        assert dacli.load_config()["client_id"] == "ID-FROM-FILE"


class TestRotationRemovesTheStaleCopy:
    def test_keychain_write_drops_the_config_file_copy(self, isolated_paths, monkeypatch):
        """Two places holding a secret is one place too many.

        With precedence fixed the leftover is harmless on read, but it is
        still a secret sitting in a file the user believes no longer holds
        one.
        """
        dacli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.CONFIG_PATH.write_text(json.dumps({"client_id": "ID", "client_secret": "OLD"}))
        monkeypatch.setattr(dacli, "_keychain_set", lambda k, v: True)

        dacli.set_config_field("client_secret", "NEW")

        on_disk = json.loads(dacli.CONFIG_PATH.read_text())
        assert "client_secret" not in on_disk, "the old secret is still in config.json"
        assert on_disk["client_id"] == "ID", "unrelated keys must survive"

    def test_fallback_write_still_stores_in_the_file(self, isolated_paths, monkeypatch):
        """When the Keychain is unavailable the file is the only home."""
        monkeypatch.setattr(dacli, "_keychain_set", lambda k, v: False)
        dacli.set_config_field("client_secret", "ONLY-HOME")
        assert json.loads(dacli.CONFIG_PATH.read_text())["client_secret"] == "ONLY-HOME"


class TestConfigFileWrongShape:
    """A config.json holding valid JSON that is not an object.

    `load_config` already warned and ignored it, but `set_config_field`
    assigned straight into whatever `json.loads` returned — so
    `da config set` on a file containing a list raised
    `TypeError: list indices must be integers` and lost the value the
    user was trying to store.
    """

    @pytest.mark.parametrize("content", ['["a", "b"]', '"a string"', "42", "null"])
    def test_set_recovers_and_stores(self, isolated_paths, content):
        dacli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.CONFIG_PATH.write_text(content)
        dacli.set_config_field("client_id", "999")
        assert json.loads(dacli.CONFIG_PATH.read_text()) == {"client_id": "999"}

    def test_it_says_what_it_did(self, isolated_paths, capsys):
        dacli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.CONFIG_PATH.write_text("[1, 2, 3]")
        dacli.set_config_field("client_id", "999")
        combined = capsys.readouterr()
        assert "expected a JSON object" in (combined.out + combined.err), (
            "the file was replaced without telling the user"
        )

    def test_a_normal_object_is_merged_not_replaced(self, isolated_paths):
        dacli.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        dacli.CONFIG_PATH.write_text(json.dumps({"destination": "/keep/me"}))
        dacli.set_config_field("client_id", "999")
        on_disk = json.loads(dacli.CONFIG_PATH.read_text())
        assert on_disk == {"destination": "/keep/me", "client_id": "999"}


class TestDocumentedEnvVarsAreRead:
    """Every env var the configuration reference lists must be wired.

    `DA_JITTER` was in the table and in no code path, so setting it did
    nothing at all.
    """

    @pytest.mark.parametrize(
        ("env", "key", "value"),
        [
            ("DA_CLIENT_ID", "client_id", "12345"),
            ("DA_CLIENT_SECRET", "client_secret", "shh"),
            ("DA_DESTINATION", "destination", "/tmp/art"),
            ("DA_REDIRECT_URI", "redirect_uri", "https://localhost:9999/"),
            ("DA_JITTER", "jitter", "0.4"),
        ],
    )
    def test_env_var_reaches_the_config(self, isolated_paths, monkeypatch, env, key, value):
        monkeypatch.setenv(env, value)
        assert dacli.load_config().get(key) == value, f"{env} is documented but not read"
