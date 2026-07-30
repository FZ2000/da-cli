"""Boundary conditions that a wrong operator changes silently.

Every case here was found by mutation testing: change one comparison or
one sign, run the whole suite, and nothing failed. These are the ones
where the mutated behaviour is not a crash but a quiet wrong answer —
a token used past its expiry, a retry budget off by one, a warning that
arrives a day late.
"""

from __future__ import annotations

import pathlib
import time
import urllib.error
from unittest.mock import patch

import pytest

import dacli


class TestAccessTokenRefreshSkew:
    """A token is refreshed BEFORE it expires, by TOKEN_REFRESH_SKEW_S.

    The check is `time.time() < expires_at - TOKEN_REFRESH_SKEW_S`. Flip
    that sign and a token is handed out for a full skew window *past*
    expiry, so every call in that window 401s. Nothing noticed.
    """

    def _state(self, seconds_left):
        return {
            "access_token": "CACHED",
            "expires_at": time.time() + seconds_left,
            "refresh_token": "RT",
        }

    def test_token_well_inside_its_life_is_reused(self, isolated_paths):
        skew = dacli.constants.TOKEN_REFRESH_SKEW_S
        state = self._state(skew * 10)
        with patch.object(dacli, "http_post_json") as post:
            assert dacli.access_token({"client_id": "X"}, state) == "CACHED"
        post.assert_not_called()

    def test_token_inside_the_skew_window_is_refreshed(self, isolated_paths):
        """Still valid by the clock, but too close to expiry to hand out."""
        skew = dacli.constants.TOKEN_REFRESH_SKEW_S
        state = self._state(skew // 2)
        with patch.object(
            dacli,
            "http_post_json",
            return_value={"access_token": "FRESH", "expires_in": 3600},
        ) as post:
            assert dacli.access_token({"client_id": "X"}, state) == "FRESH"
        assert post.called, "a token inside the skew window was handed out unrefreshed"

    def test_expired_token_is_refreshed(self, isolated_paths):
        state = self._state(-60)
        with patch.object(
            dacli,
            "http_post_json",
            return_value={"access_token": "FRESH", "expires_in": 3600},
        ):
            assert dacli.access_token({"client_id": "X"}, state) == "FRESH"

    def test_the_skew_is_the_constant_not_a_literal(self, isolated_paths):
        """Moving TOKEN_REFRESH_SKEW_S must actually move the boundary."""
        state = self._state(120)
        with patch.object(dacli.auth, "TOKEN_REFRESH_SKEW_S", 300):
            with patch.object(
                dacli,
                "http_post_json",
                return_value={"access_token": "FRESH", "expires_in": 3600},
            ) as post:
                dacli.access_token({"client_id": "X"}, state)
        assert post.called, (
            "raising the skew past the token's remaining life did not force a "
            "refresh, so the constant does not govern the check"
        )


class TestLoopbackCertNeedsBothHalves:
    """One file present is not a usable pair.

    Mutation testing flagged the `and` in
    `LOOPBACK_CERT.exists() and LOOPBACK_KEY.exists()` as unkilled. It is
    an EQUIVALENT mutation, not a gap: with `or`, a half-present pair
    enters the branch, `_cert_pair_loads` then fails on the missing half,
    and it regenerates anyway. Verified both ways — openssl is invoked
    either side of the mutation.

    These tests stay because the behaviour is worth pinning regardless of
    which check enforces it: half a pair on disk must produce a working
    pair, not a confusing TLS error deep in the listener.
    """

    def _pair(self, tmp_path, monkeypatch, real_cert_pair):
        cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
        monkeypatch.setattr(dacli, "LOOPBACK_CERT", cert)
        monkeypatch.setattr(dacli, "LOOPBACK_KEY", key)
        return cert, key, real_cert_pair

    def test_cert_without_key_is_regenerated(
        self, isolated_paths, tmp_path, monkeypatch, real_cert_pair
    ):
        cert, _key, (good_cert, good_key) = self._pair(tmp_path, monkeypatch, real_cert_pair)
        cert.write_bytes(good_cert.read_bytes())  # key absent

        def fake_openssl(argv, *_a, **_kw):
            # Write where argv says. Generation now stages into a private
            # 0700 directory before moving into place — so the key is
            # never briefly world-readable — and a stub that ignores
            # -keyout/-out imitates an openssl that produces nothing.
            args = list(argv)
            pathlib.Path(args[args.index("-out") + 1]).write_bytes(good_cert.read_bytes())
            pathlib.Path(args[args.index("-keyout") + 1]).write_bytes(good_key.read_bytes())
            return type("R", (), {"returncode": 0})()

        with patch("subprocess.run", side_effect=fake_openssl) as run:
            dacli._ensure_self_signed_cert()
        assert run.called, "a cert with no key was accepted as a usable pair"

    def test_key_without_cert_is_regenerated(
        self, isolated_paths, tmp_path, monkeypatch, real_cert_pair
    ):
        _cert, key, (good_cert, good_key) = self._pair(tmp_path, monkeypatch, real_cert_pair)
        key.write_bytes(good_key.read_bytes())  # cert absent

        def fake_openssl(argv, *_a, **_kw):
            # Write where argv says. Generation now stages into a private
            # 0700 directory before moving into place — so the key is
            # never briefly world-readable — and a stub that ignores
            # -keyout/-out imitates an openssl that produces nothing.
            args = list(argv)
            pathlib.Path(args[args.index("-out") + 1]).write_bytes(good_cert.read_bytes())
            pathlib.Path(args[args.index("-keyout") + 1]).write_bytes(good_key.read_bytes())
            return type("R", (), {"returncode": 0})()

        with patch("subprocess.run", side_effect=fake_openssl) as run:
            dacli._ensure_self_signed_cert()
        assert run.called, "a key with no cert was accepted as a usable pair"


class TestRetryBudgetIsExact:
    """`retries=N` means N+1 attempts. Not N, not N+2.

    The loop guard is `attempt < retries`. Both `<=` and `<` off-by-one
    variants survived the suite: one silently doubles the load on a
    struggling API, the other gives up early.
    """

    @pytest.mark.parametrize("retries", [0, 1, 2, 3])
    def test_attempt_count_matches_the_budget(self, retries):
        attempts = []

        def always_500(*a, **kw):
            attempts.append(1)
            raise urllib.error.HTTPError("u", 500, "Server Error", {}, None)

        with patch("urllib.request.urlopen", side_effect=always_500):
            with patch("time.sleep"):
                with pytest.raises(urllib.error.HTTPError):
                    dacli.http_json("https://example.invalid/x", retries=retries)
        assert len(attempts) == retries + 1, (
            f"retries={retries} made {len(attempts)} attempts, expected {retries + 1}"
        )

    def test_a_success_stops_retrying(self):
        calls = []

        class _Resp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fail_once(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError("u", 500, "e", {}, None)
            return _Resp()

        with patch("urllib.request.urlopen", side_effect=fail_once):
            with patch("time.sleep"):
                dacli.http_json("https://example.invalid/x", retries=3)
        assert len(calls) == 2, f"kept retrying after success: {len(calls)} calls"


class TestRefreshTokenThresholdsAreInclusive:
    """`da diagnose` warns at exactly the documented day counts.

    The comparisons are `<=` against REFRESH_TOKEN_CRIT_DAYS and
    REFRESH_TOKEN_WARN_DAYS. Turning either into `<` moves the boundary by
    a day — for a 3-day critical window that is a third of the warning.

    The clock is frozen. `days_remaining` is derived from `time.time()`,
    so with a live clock the value lands a hair under the boundary and
    `<` and `<=` agree; the mutation survives. Freezing is what makes the
    boundary exactly the boundary.
    """

    FROZEN = 1_800_000_000.0

    def _levels(self, days_left, tmp_path):
        ttl = dacli.constants.REFRESH_TOKEN_TTL_DAYS
        dacli.set_config_field("client_id", "12345")
        dest = tmp_path / "gallery"
        dest.mkdir(exist_ok=True)
        dacli.set_config_field("destination", str(dest))
        dacli.save_state(
            {
                "access_token": "AT",
                "expires_at": self.FROZEN + 3600,
                "refresh_token": "RT",
                "scope": "browse",
                # days_remaining = ttl - (now - issued)/86400, exactly.
                "refresh_token_issued_at": self.FROZEN - (ttl - days_left) * 86400.0,
            }
        )
        with patch("sys.platform", "linux"):
            with patch("time.time", return_value=self.FROZEN):
                return {section: level for level, section, _ in dacli._diagnose_checks()}

    def test_exactly_at_the_critical_threshold_is_a_failure(
        self, isolated_paths, no_keychain, tmp_path
    ):
        crit = dacli.constants.REFRESH_TOKEN_CRIT_DAYS
        assert self._levels(crit, tmp_path)["auth"] == "fail", (
            f"{crit} days left is documented as critical but was not a failure"
        )

    def test_just_above_the_critical_threshold_is_only_a_warning(
        self, isolated_paths, no_keychain, tmp_path
    ):
        crit = dacli.constants.REFRESH_TOKEN_CRIT_DAYS
        assert self._levels(crit + 0.5, tmp_path)["auth"] == "warn"

    def test_exactly_at_the_warning_threshold_is_a_warning(
        self, isolated_paths, no_keychain, tmp_path
    ):
        warn = dacli.constants.REFRESH_TOKEN_WARN_DAYS
        assert self._levels(warn, tmp_path)["auth"] == "warn", (
            f"{warn} days left is documented as a warning but was not reported as one"
        )

    def test_just_above_the_warning_threshold_is_clean(self, isolated_paths, no_keychain, tmp_path):
        warn = dacli.constants.REFRESH_TOKEN_WARN_DAYS
        assert self._levels(warn + 0.5, tmp_path)["auth"] == "ok"
