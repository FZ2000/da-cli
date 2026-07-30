"""API response-shape tests via VCR.py cassette replay.

Record once, replay forever. Catches DeviantArt API shape drift
(renamed fields, dropped required keys) deterministically in CI with
zero network and zero credentials.

Run with::

    pytest -m integration_cassette

Record mode (developer runs locally after an API shape change)::

    VCR_RECORD=1 pytest -m integration_cassette --no-cov

During replay, tests pass a dummy token — VCR filters the
``Authorization`` header from both the recorded request and the replay
request before matching, so any string works. The cassette matches on
method + scheme + host + path + query only.

**Token scrubbing is critical.** VCR's ``vcr_config`` fixture filters
``authorization`` headers and ``access_token`` query params before
writing cassettes. Never commit a cassette containing a real token.
"""

from __future__ import annotations

import os

import pytest

import dacli

pytestmark = [pytest.mark.integration, pytest.mark.integration_cassette]

API = dacli.API_BASE


@pytest.fixture(scope="module")
def anonymous_token() -> str:
    """Token for cassette tests.

    During **replay** (default — no ``VCR_RECORD``): returns a dummy
    string. VCR filters the ``Authorization`` header from both the
    recorded request and the replay request before matching, so any
    non-empty token works. This makes cassette tests fully
    credential-free and network-free.

    During **recording** (``VCR_RECORD=1``): fetches a real
    ``client_credentials`` token from DA so the cassette captures a
    genuine response.
    """
    if not os.environ.get("VCR_RECORD"):
        return "cassette-replay-dummy-token"

    from tests.integration.conftest import _read_client_id, _read_client_secret

    client_id = _read_client_id()
    client_secret = _read_client_secret()
    if not client_id or not client_secret:
        pytest.skip("Set DA_CLIENT_ID + DA_CLIENT_SECRET to record cassettes")

    body = dacli.http_post_json(
        dacli.TOKEN_URL,
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    tok = body.get("access_token")
    if not tok:
        pytest.fail(f"client_credentials grant failed: {body}")
    return str(tok)


class TestDailyDeviationsResponseShape:
    """Asserts the OpenAPI contract for ``/browse/dailydeviations``.
    When DA changes the shape, this test fails loudly. Re-record after
    updating the assertion."""

    @pytest.mark.vcr
    def test_response_contains_required_deviation_fields(self, anonymous_token: str) -> None:
        body = dacli.http_json(f"{API}/browse/dailydeviations?limit=2", token=anonymous_token)
        assert "results" in body
        assert isinstance(body["results"], list)
        assert "has_more" in body
        if body["results"]:
            d = body["results"][0]
            for key in ("deviationid", "is_deleted", "printid"):
                assert key in d, f"deviation missing required key {key!r}"


class TestTopicsResponseShape:
    @pytest.mark.vcr
    def test_response_contains_topic_name_and_canonical_name(self, anonymous_token: str) -> None:
        body = dacli.http_json(f"{API}/browse/topics?limit=3", token=anonymous_token)
        assert isinstance(body["results"], list)
        assert body["results"], "topics should never be empty"
        t = body["results"][0]
        for key in ("name", "canonical_name"):
            assert key in t, f"topic missing required key {key!r}"
        assert "has_more" in body


class TestTagSearchResponseShape:
    @pytest.mark.vcr
    def test_response_contains_tag_name(self, anonymous_token: str) -> None:
        body = dacli.http_json(f"{API}/browse/tags/search?tag_name=nat", token=anonymous_token)
        assert isinstance(body.get("results"), list)
        if body["results"]:
            assert "tag_name" in body["results"][0]
