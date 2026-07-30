"""401-recovery test — verifies the auto-refresh-on-401 path against
live DA.

``authed_http_json`` wraps every authenticated API call with
on-401-refresh-and-retry-once logic. If DA invalidates the cached
access_token mid-session (e.g. after a re-auth rotates the refresh
chain), the helper forces a fresh token exchange and retries. This
test proves that recovery path works against the real DA API, not
just in mocked unit tests.

Run with::

    pytest -m integration_authenticated
"""

from __future__ import annotations

import pytest

import dacli

pytestmark = [pytest.mark.integration, pytest.mark.integration_authenticated]

API = dacli.API_BASE


class TestAutoRefreshOn401:
    """Verify that ``authed_http_json`` recovers from a server-side
    token rejection by forcing a refresh and retrying."""

    def test_tampered_token_triggers_auto_refresh(
        self,
        user_state: dict[str, object],
    ) -> None:
        """Inject an invalid access_token into state, then call
        ``authed_http_json``. The helper should:

        1. Try the call with the bad token.
        2. Get 401 from DA.
        3. Force a refresh via ``access_token(force_refresh=True)``.
        4. Retry the call with the fresh token.
        5. Return the successful response.

        If step 3 or 4 fails, the test surfaces the exact error.
        """
        from tests.integration.conftest import _read_client_id, _read_client_secret

        client_id = _read_client_id()
        client_secret = _read_client_secret()
        assert client_id, "no client_id available"

        # Build a state copy with a deliberately-invalid access_token.
        # DA will reject it with 401, triggering the refresh path.
        state = user_state.copy()
        state["access_token"] = "DELIBERATELY-INVALID-TOKEN-TO-TRIGGER-401"
        state["expires_at"] = float("inf")  # make it look "not expired" locally

        cfg: dict[str, str] = {"client_id": client_id}
        if client_secret:
            cfg["client_secret"] = client_secret

        # Call authed_http_json against a lightweight endpoint (/placebo).
        # The helper should recover transparently.
        body = dacli.authed_http_json(
            f"{API}/placebo",
            cfg,
            state,
        )
        assert body.get("status") == "success", (
            f"authed_http_json did not recover from 401 — response: {body}"
        )
