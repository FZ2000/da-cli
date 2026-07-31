"""User-scoped integration tests against live DeviantArt.

Uses ``refresh_token`` from the developer's ``state.json`` (or the
``DA_REFRESH_TOKEN`` CI secret). 90-day cadence — re-run ``da auth``
when the token expires (``da diagnose`` warns at 14 days remaining).

Run with::

    pytest -m integration_authenticated

The ``user_token`` fixture does one inline refresh per session and
persists any rotated refresh_token back to the real ``state.json``
(same lifecycle as ``da refresh``). Test bodies are read-only — they
never call ``save_state()``, ``set_config_field()``, or any sync
command.

Skipped unless a valid ``refresh_token`` is available. In CI, fails
loudly (not skip) on ``invalid_grant`` so a missed rotation surfaces
as red.
"""

from __future__ import annotations

import pytest

import dacli

pytestmark = [pytest.mark.integration, pytest.mark.integration_authenticated]

API = dacli.API_BASE


# ---------------------------------------------------------------------------
# Token liveness
# ---------------------------------------------------------------------------
class TestRefreshTokenValidity:
    def test_whoami_returns_authenticated_user(self, user_token: str) -> None:
        """``/user/whoami`` is the canonical "who am I" call. Works only
        with a user-scoped token. If this passes, the refresh_token chain
        is alive end-to-end — the ``user_token`` fixture already proved
        the refresh succeeded; this proves the resulting token is
        accepted by a user-scoped endpoint."""
        body = dacli.http_json(f"{API}/user/whoami?mature_content=true", token=user_token)
        assert "userid" in body
        assert "username" in body
        assert isinstance(body["username"], str)


# ---------------------------------------------------------------------------
# Watch feed (the primary data source for ``da sync feed``)
# ---------------------------------------------------------------------------
class TestWatchFeed:
    def test_watch_feed_returns_results(self, user_token: str) -> None:
        """The watch feed is the primary data source for
        ``da sync feed``. If this call works, the entire feed-sync
        path's first API call works."""
        body = dacli.http_json(
            f"{API}/browse/deviantsyouwatch?limit=3&mature_content=false",
            token=user_token,
        )
        assert "next_offset" in body
        assert isinstance(body.get("results"), list)
