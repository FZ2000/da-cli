"""Network integration tests for da-cli.

Lives in a sub-package so the parent ``tests/conftest.py`` autouse
fixtures (``_always_isolate_index``, ``_no_keychain_autouse``) can be
selectively suppressed — network tests need to read the developer's
real Keychain and state.json to acquire tokens.

Three test categories, all skipped unless ``-m integration`` is passed:

* **Anonymous** (``integration_anonymous`` marker) — ``client_credentials``
  grant; no user, no browser, no token expiry. CI-runnable forever.
* **User-scoped** (``integration_authenticated`` marker) — ``refresh_token`` from
  state.json or ``DA_REFRESH_TOKEN`` env; 90-day cadence; read-only.
* **Recording** (``integration_cassette`` marker) — VCR.py cassette
  replay; CI-runnable forever after first recording; catches API
  response-shape drift.

See ``docs/testing.md`` for how to run each layer and configure
credentials.
"""
