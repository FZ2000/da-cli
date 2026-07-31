"""Configuration, persisted state, and secret storage.

Extracted verbatim from the single-file module; see ADR 0007.

Everything the test suite patches is read through the ``dacli`` package
at call time rather than imported, because importing would bind the real
value and the patch would not reach this module:

* ``CONFIG_PATH`` / ``CONFIG_DIR`` / ``STATE_PATH`` / ``STATE_DIR`` —
  tests repoint these at a temp directory.
* ``_keychain_get`` / ``_keychain_set`` — tests stub these so the suite
  never touches the real macOS Keychain. This module defines them *and*
  calls them, so the calls go through the package.
* ``_STATE_CORRUPTION_WARNED`` — a once-per-process flag. It used to be
  a module global rebound via ``global``; that rebind would be invisible
  as ``dacli._STATE_CORRUPTION_WARNED``, which is what tests reset, so
  the flag now lives on the package.
"""

import json
import os
import subprocess
import sys
import time

import dacli

from .constants import KEYCHAIN_SERVICE
from .output import _atomic_write

# --------------------------------------------------------------------------
# Config + state + secrets
# --------------------------------------------------------------------------
SECRET_KEYS = {"client_secret"}

# Config keys the sync engine feeds to arithmetic. Values arrive as
# strings from the CLI and the environment and as real numbers from
# config.json, so every consumer coerces — but nothing used to check that
# the coercion could succeed. `da config set jitter 40%` was accepted
# with a cheerful "stored jitter in ...", and the next scheduled run died
# in _delays with an uncaught ValueError before it fetched a single page.
NUMERIC_KEYS: dict[str, type] = {
    "delay_api": float,
    "delay_image": float,
    "jitter": float,
    "concurrency": int,
}


def coerce_number(key: str, value: object, default: float) -> float:
    """A config number, or ``default`` with a warning if it is not one.

    Tolerant on purpose: this is the read path, and it runs inside
    scheduled jobs. ``da config set`` rejects a bad value up front, but a
    hand-edited config.json or an exported ``DA_JITTER=abc`` reaches here
    having never passed through it, and a nightly sync that dies with a
    traceback over a typo is a worse answer than one that warns and uses
    the default.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        dacli.log(f"{key}={value!r} is not a number — using {default}", "warn")
        return default
    if number < 0:
        # Negative delays reached time.sleep(), which raises. Clamping is
        # what a user asking for "no delay" means, and it keeps the value
        # out of arithmetic that assumes a non-negative interval.
        dacli.log(f"{key}={value!r} is negative — using 0", "warn")
        return 0.0
    return number


def _keychain_get(key: str) -> str | None:
    """Return a secret from macOS Keychain, or None if unavailable / missing."""
    if sys.platform != "darwin":
        return None
    try:
        p = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        return p.stdout.strip() if p.returncode == 0 else None
    except FileNotFoundError:
        return None


def _keychain_set(key: str, value: str) -> bool:
    """Store a secret in macOS Keychain. Returns True on success, False if
    we're not on macOS or the ``security`` helper is missing / fails.
    """
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                key,
                "-w",
                value,
                "-U",
            ],
            check=True,
            capture_output=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def load_config() -> dict[str, str]:
    """
    Read config from disk + env + keychain. SECRET_KEYS are sourced from env > keychain
    (never returned from the on-disk JSON unless the user explicitly ran `config set`
    on a non-Darwin platform with no Keychain available, in which case we fall back to
    the JSON file with a warning).
    """
    cfg: dict = {}
    if dacli.CONFIG_PATH.exists():
        loaded: object = {}
        try:
            loaded = json.loads(dacli.CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            dacli.log(f"{dacli.CONFIG_PATH}: invalid JSON ({e}); ignoring", "warn")
        # A file containing `[]`, `"x"`, `null` or `42` parses fine and
        # then breaks every .get() downstream with an AttributeError.
        # Same warn-and-ignore contract as unparsable JSON.
        if isinstance(loaded, dict):
            cfg = loaded
        else:
            dacli.log(f"{dacli.CONFIG_PATH}: expected a JSON object; ignoring", "warn")

    env_map = {
        "DA_CLIENT_ID": "client_id",
        "DA_CLIENT_SECRET": "client_secret",
        "DA_DESTINATION": "destination",
        "DA_REDIRECT_URI": "redirect_uri",
        "DA_JITTER": "jitter",
    }
    from_env: set[str] = set()
    for env_key, cfg_key in env_map.items():
        if v := os.environ.get(env_key):
            cfg[cfg_key] = v
            from_env.add(cfg_key)

    # Secrets: env wins, then the Keychain, then config.json — the order
    # documented in docs/reference/configuration.md and in this function's
    # own docstring.
    #
    # This used to skip the Keychain whenever config.json already had a
    # value, which inverts that order and makes rotation silently fail:
    # `da config set client_secret NEW` writes to the Keychain, an older
    # copy left in config.json keeps winning, and every request goes out
    # with the secret the user thinks they just replaced.
    for k in SECRET_KEYS:
        if k in from_env:
            continue
        v = dacli._keychain_get(k)
        if v:
            cfg[k] = v
    return cfg


def _drop_from_config_file(key: str) -> None:
    """Remove `key` from config.json if present. Quiet when it is not."""
    if not dacli.CONFIG_PATH.exists():
        return
    try:
        cfg = json.loads(dacli.CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(cfg, dict) or key not in cfg:
        return
    del cfg[key]
    _atomic_write(dacli.CONFIG_PATH, json.dumps(cfg, indent=2), 0o600)
    dacli.log(f"removed the older {key} from {dacli.CONFIG_PATH}")


def set_config_field(key: str, value: str) -> None:
    """
    For secrets, prefer Keychain on macOS. For non-secrets, write to config.json
    (0600). On non-Darwin or when Keychain write fails, fall back to config.json
    for secrets too (and warn).

    Numeric keys are checked here rather than where they are used. This is
    the one moment the user is present and can retype the value; the
    alternative is what used to happen, where `da config set jitter 40%`
    reported success and the failure surfaced hours later, in a scheduled
    run, as a traceback with no mention of config at all.
    """
    expected = NUMERIC_KEYS.get(key)
    if expected is not None:
        try:
            expected(value)
        except (TypeError, ValueError):
            kind = "a whole number" if expected is int else "a number"
            dacli.log(f"{key} must be {kind} — got {value!r}", "error")
            sys.exit(2)
    if key in SECRET_KEYS and dacli._keychain_set(key, value):
        dacli.log(f"stored {key} in macOS Keychain (service={KEYCHAIN_SERVICE})")
        # Drop any older copy from config.json. The Keychain now wins on
        # read, so a leftover would be dead weight — but it would still be
        # a secret sitting in a file the user believes no longer holds one.
        _drop_from_config_file(key)
        return
    cfg: dict = {}
    if dacli.CONFIG_PATH.exists():
        # Typed as object, not dict: json.loads returns whatever is in the
        # file. Annotating the result as a dict is what let `cfg[key] = value`
        # raise TypeError on a config.json holding a list.
        loaded: object
        try:
            loaded = json.loads(dacli.CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            cfg = loaded
        else:
            # Valid JSON, wrong shape — a list, a string, a bare number.
            # load_config already warns and ignores it; without this the
            # write raised and the value the user asked to store was lost.
            dacli.log(f"{dacli.CONFIG_PATH}: expected a JSON object; replacing it", "warn")
    cfg[key] = value
    _atomic_write(dacli.CONFIG_PATH, json.dumps(cfg, indent=2), 0o600)
    if key in SECRET_KEYS:
        dacli.log(
            f"stored {key} in {dacli.CONFIG_PATH} (Keychain unavailable). "
            f"Permissions are 0600 — the file is still secret-bearing; "
            f"avoid syncing this directory to cloud storage.",
            "warn",
        )
    else:
        dacli.log(f"stored {key} in {dacli.CONFIG_PATH}")


# Seed value. The re-export in __init__ binds this into the dacli
# namespace; from then on load_state() reads and writes
# dacli._STATE_CORRUPTION_WARNED, which is what the test suite resets.
_STATE_CORRUPTION_WARNED = False


def load_state() -> dict[str, object]:
    if not dacli.STATE_PATH.exists():
        return {}
    try:
        loaded = json.loads(dacli.STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
        # Valid JSON of the wrong shape is as unusable as a corrupt
        # file — fall through to the same backup-and-warn path.
        raise json.JSONDecodeError("state.json is not a JSON object", "", 0)
    except json.JSONDecodeError:
        # state.json is corrupt — most likely a SIGKILL during save_state's
        # tmp→rename gap, or a partial write on a power cut. Returning
        # silently {} loses the refresh_token + sync checkpoints without
        # the operator noticing; they'd think they're being asked to
        # re-auth out of nowhere. Warn once and stash the bad file with a
        # timestamp so investigation is possible.
        if not dacli._STATE_CORRUPTION_WARNED:
            dacli._STATE_CORRUPTION_WARNED = True
            # Keep the original suffix so the backup is recognizable as JSON
            # (state.json → state.json.corrupt-1234, not state.corrupt-1234).
            backup = dacli.STATE_PATH.with_name(
                dacli.STATE_PATH.name + f".corrupt-{int(time.time())}"
            )
            try:
                dacli.STATE_PATH.rename(backup)
                dacli.log(
                    f"state.json was unparsable; moved to {backup.name}. "
                    f"Run `da auth` to re-issue tokens.",
                    "warn",
                )
            except OSError as e:
                dacli.log(f"state.json was unparsable and could not be backed up: {e}", "warn")
        return {}


def save_state(state: dict[str, object]) -> None:
    _atomic_write(dacli.STATE_PATH, json.dumps(state, indent=2), 0o600)
