"""The loopback private key must never be readable by other local users.

``openssl req -keyout`` creates the key under the ambient umask, and the
``os.chmod`` that followed was too late. The mode openssl chooses is not
the same everywhere — measured on this machine:

    OpenSSL 3.6.1 (homebrew)   ->  0600
    LibreSSL 3.3.6 (macOS's)   ->  0644

and ``STATE_DIR`` is 0755, so on a stock macOS there was a real window
where any local user could read the key.

The window is not the whole problem. A run killed between openssl writing
and the chmod leaves a *valid* pair, so every later ``da auth`` took the
early-return path — which never re-checked the mode — and the 0644 key
persisted indefinitely.

Generation now stages into a private 0700 directory and chmods before
moving into place, so the key is unreachable by other users from the
moment it exists, whatever mode openssl chose.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import dacli

pytestmark = pytest.mark.skipif(
    sys.platform not in ("darwin", "linux"), reason="POSIX permission semantics"
)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """A 0755 state dir — what the real one is, and why this matters."""
    d = tmp_path / "state"
    d.mkdir()
    # 0755 deliberately: this is the mode the real ~/.local/state/da-cli
    # has, and it is why a group/other-readable key there is genuinely
    # reachable by another local user. A private fixture dir would hide
    # the very condition these tests exist to check.
    os.chmod(d, 0o755)  # noqa: S103
    monkeypatch.setattr(dacli, "STATE_DIR", d)
    monkeypatch.setattr(dacli, "LOOPBACK_CERT", d / "loopback-cert.pem")
    monkeypatch.setattr(dacli, "LOOPBACK_KEY", d / "loopback-key.pem")
    return d


def _mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)[-3:]


class TestTheGeneratedKeyIsPrivate:
    def test_the_key_is_0600_after_generation(self, state_dir):
        """Uses the real openssl, whichever one is on PATH."""
        cert, key = dacli._ensure_self_signed_cert()

        assert _mode(key) == "600", f"key is {_mode(key)}, readable by other users"
        assert _mode(cert) == "600"

    def test_no_staging_directory_survives(self, state_dir):
        """A private temp dir left behind is clutter with a key's name on it."""
        dacli._ensure_self_signed_cert()

        leftovers = [
            p.name
            for p in state_dir.iterdir()
            if p.name not in ("loopback-cert.pem", "loopback-key.pem")
        ]
        assert leftovers == [], f"staging left behind: {leftovers}"

    def test_the_key_never_exists_world_readable_at_its_final_path(self, state_dir, monkeypatch):
        """The window itself, not just the end state.

        Samples the key's mode at its FINAL path the instant openssl
        returns — before any chmod. Without staging, openssl has already
        created the file there under its own umask, so on an openssl that
        chooses 0644 (which is the one macOS ships) this sample is
        world-readable. With staging the final path does not exist yet,
        because the key is still inside a 0700 directory.

        Asserting only the end state would not discriminate: the old code
        chmod'd afterwards and so also finished at 0600.
        """
        samples: list[str] = []
        real_run = subprocess.run

        def sample_after_openssl(*args, **kwargs):
            result = real_run(*args, **kwargs)
            key = dacli.LOOPBACK_KEY
            if key.exists():
                samples.append(oct(stat.S_IMODE(key.stat().st_mode))[-3:])
            else:
                samples.append("absent")
            return result

        monkeypatch.setattr(subprocess, "run", sample_after_openssl)
        _cert, key = dacli._ensure_self_signed_cert()

        assert samples, "openssl was never invoked"
        assert samples[0] == "absent", (
            f"the key existed at its final path as {samples[0]} before being locked down — "
            f"readable by any local user in that window"
        )
        assert _mode(key) == "600"


class TestReusingAnExistingPair:
    def test_a_loose_mode_is_tightened_on_reuse(self, state_dir):
        """The persistent case, which the early return never checked.

        A run killed between openssl's write and the chmod leaves a valid
        pair at 0644. Every later `da auth` returned it untouched, so the
        exposure lasted until the file was deleted by hand.
        """
        cert, key = dacli._ensure_self_signed_cert()
        # Simulate the interrupted run.
        os.chmod(key, 0o644)
        os.chmod(cert, 0o644)
        assert _mode(key) == "644"

        again_cert, again_key = dacli._ensure_self_signed_cert()

        assert (again_cert, again_key) == (cert, key), "should reuse, not regenerate"
        assert _mode(again_key) == "600", "reuse must re-assert the mode"

    def test_a_good_pair_is_not_regenerated(self, state_dir):
        """The control: tightening the mode must not mean re-issuing."""
        _cert, key = dacli._ensure_self_signed_cert()
        first = key.read_bytes()

        with pytest.MonkeyPatch.context() as m:
            calls: list[object] = []
            m.setattr(subprocess, "run", lambda *a, **kw: calls.append(a) or None)
            dacli._ensure_self_signed_cert()

        assert calls == [], "openssl was run again for a pair that was already good"
        assert key.read_bytes() == first


class TestOpensslProducingNothing:
    def test_exit_zero_with_no_output_is_reported(self, state_dir, monkeypatch):
        """openssl can exit 0 and write nothing; say so actionably.

        Staging made this reachable in a new way: the chmod/move now
        targets files openssl was supposed to create, so without this
        check the user got a FileNotFoundError traceback instead of a
        sentence telling them what to do.
        """

        def silent_success(*_a, **_kw):
            class R:
                returncode = 0

            return R()

        monkeypatch.setattr(subprocess, "run", silent_success)

        with pytest.raises(SystemExit) as exc:
            dacli._ensure_self_signed_cert()

        assert exc.value.code == 2
