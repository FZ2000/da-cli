"""Anonymous integration tests against live DeviantArt.

Uses ``client_credentials`` grant — no user, no browser, no 90-day
token expiry. CI-runnable forever. Covers endpoints whose OpenAPI
security scheme is ``oauth2AuthorizationCodeOrClientCredentials``
(daily deviations, topics, tags, gallery, collections, profile,
deviation metadata, comments, static data).

Run with::

    pytest -m integration_anonymous

Skipped unless ``DA_CLIENT_ID`` + ``DA_CLIENT_SECRET`` are available
(env, config.json, or Keychain). See ``tests/integration/README.md``.
"""

from __future__ import annotations

import urllib.error

import pytest

import dacli

pytestmark = [pytest.mark.integration, pytest.mark.integration_anonymous]

API = dacli.API_BASE

# A well-known DA account guaranteed to exist and have public content.
# Using DA's own account avoids coupling tests to any individual user.
PUBLIC_TEST_ACCOUNT = "deviantart"


# ---------------------------------------------------------------------------
# Liveness + token validity
# ---------------------------------------------------------------------------
class TestPlacebo:
    """``/placebo`` is the cheapest "is DA up + is my token valid" call."""

    def test_placebo_returns_success(self, anonymous_token: str) -> None:
        body = dacli.http_json(f"{API}/placebo", token=anonymous_token)
        assert body.get("status") == "success"


# ---------------------------------------------------------------------------
# Static reference data (shape never changes — ideal smoke targets)
# ---------------------------------------------------------------------------
class TestStaticData:
    def test_data_countries_includes_united_states(self, anonymous_token: str) -> None:
        body = dacli.http_json(f"{API}/data/countries", token=anonymous_token)
        assert "results" in body
        assert isinstance(body["results"], list)
        names = {c.get("name") for c in body["results"]}
        assert "United States" in names

    def test_data_terms_of_service_returns_content(self, anonymous_token: str) -> None:
        body = dacli.http_json(f"{API}/data/tos", token=anonymous_token)
        # DA returns either inline text or a URL to the current ToS.
        text = body.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 10, f"ToS response unexpectedly short: {text!r}"


# ---------------------------------------------------------------------------
# Browse family
# ---------------------------------------------------------------------------
class TestBrowse:
    def test_daily_deviation_returns_results(self, anonymous_token: str) -> None:
        body = dacli.http_json(
            f"{API}/browse/dailydeviations?mature_content=false",
            token=anonymous_token,
        )
        assert "results" in body
        assert isinstance(body["results"], list)
        if body["results"]:
            d = body["results"][0]
            assert "deviationid" in d
            assert "author" in d

    def test_topics_returns_canonical_names(self, anonymous_token: str) -> None:
        body = dacli.http_json(f"{API}/browse/topics?limit=5", token=anonymous_token)
        assert isinstance(body["results"], list)
        assert body["results"], "topics should never be empty"
        t = body["results"][0]
        assert "name" in t
        assert "canonical_name" in t

    def test_top_topics_returns_list(self, anonymous_token: str) -> None:
        body = dacli.http_json(
            f"{API}/browse/toptopics?mature_content=false", token=anonymous_token
        )
        assert isinstance(body["results"], list)
        assert body["results"]

    def test_tag_search_returns_matching_prefix(self, anonymous_token: str) -> None:
        body = dacli.http_json(f"{API}/browse/tags/search?tag_name=nat", token=anonymous_token)
        assert isinstance(body.get("results"), list)
        assert any("nat" in str(r.get("tag_name", "")).lower() for r in body["results"])

    def test_tag_browse_returns_pagination_fields(self, anonymous_token: str) -> None:
        body = dacli.http_json(
            f"{API}/browse/tags?tag=nature&limit=3&mature_content=false",
            token=anonymous_token,
        )
        assert "results" in body
        assert "has_more" in body
        assert "next_offset" in body


# ---------------------------------------------------------------------------
# Gallery family — anonymous with explicit username
# ---------------------------------------------------------------------------
class TestGallery:
    """Confirms ``/gallery/all`` and friends accept
    ``client_credentials`` when ``?username=`` is provided."""

    def test_gallery_all_returns_pagination_with_username(self, anonymous_token: str) -> None:
        body = dacli.http_json(
            f"{API}/gallery/all?username={PUBLIC_TEST_ACCOUNT}&limit=3",
            token=anonymous_token,
        )
        assert "has_more" in body
        assert "next_offset" in body
        assert isinstance(body.get("results"), list)

    def test_gallery_folders_returns_results_with_username(self, anonymous_token: str) -> None:
        body = dacli.http_json(
            f"{API}/gallery/folders?username={PUBLIC_TEST_ACCOUNT}&limit=5",
            token=anonymous_token,
        )
        assert "results" in body
        assert "has_more" in body


# ---------------------------------------------------------------------------
# User family (public lookups — not the authenticated whoami)
# ---------------------------------------------------------------------------
class TestUserProfile:
    def test_user_profile_returns_public_data(self, anonymous_token: str) -> None:
        body = dacli.http_json(f"{API}/user/profile/{PUBLIC_TEST_ACCOUNT}", token=anonymous_token)
        user = body.get("user", {})
        assert user.get("username") == PUBLIC_TEST_ACCOUNT
        assert "profile_url" in body


# ---------------------------------------------------------------------------
# Negative tests: confirm user-only endpoints reject anonymous tokens.
# ---------------------------------------------------------------------------
class TestUserScopedEndpointsRejectAnonymousToken:
    """These endpoints require ``authorization_code`` (user context).
    A ``client_credentials`` token must get 401 or 403, not 200 —
    proving the security boundary is enforced server-side."""

    def test_whoami_rejects_anonymous_token(self, anonymous_token: str) -> None:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            dacli.http_json(f"{API}/user/whoami?mature_content=true", token=anonymous_token)
        assert exc_info.value.code in (401, 403)

    def test_watch_feed_rejects_anonymous_token(self, anonymous_token: str) -> None:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            dacli.http_json(f"{API}/browse/deviantsyouwatch?limit=1", token=anonymous_token)
        assert exc_info.value.code in (401, 403)

    def test_messages_feed_rejects_anonymous_token(self, anonymous_token: str) -> None:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            dacli.http_json(f"{API}/messages/feed", token=anonymous_token)
        assert exc_info.value.code in (401, 403)
