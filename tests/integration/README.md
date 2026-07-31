# Live DeviantArt API tests

These tests call the real DeviantArt API. They are skipped unless you
opt in with `-m integration`, so a plain `pytest` never touches the
network.

**Full instructions — including how to supply credentials — are in
[docs/testing.md](../../docs/guides/testing.md).** This file covers only what
is specific to this directory.

## Quick start

```bash
pip install -e '.[dev,integration]'   # adds vcrpy + pytest-vcr

pytest -m integration_cassette --no-cov        # no credentials, no network
pytest -m integration_anonymous --no-cov       # needs client_id + client_secret
pytest -m integration_authenticated --no-cov   # needs a refresh_token
pytest -m integration --no-cov                 # all of the above

`--no-cov` is required: the 92% coverage gate applies to every run, so
a partial selection passes its tests and then fails on coverage.
```

## What is in here

| File | Layer | Needs |
| --- | --- | --- |
| `test_api_response_shapes.py` | cassette replay | nothing |
| `test_anonymous_endpoints.py` | live | `client_id` + `client_secret` |
| `test_cli_all_commands.py` | live, via subprocess | `client_id` + `client_secret` |
| `test_cli_subprocess.py` | live, via subprocess | `client_id` + `client_secret` |
| `test_user_scoped_endpoints.py` | live | `refresh_token` |
| `test_sync_live.py` | live | `refresh_token` |
| `test_auto_refresh_live.py` | live | `refresh_token` |
| `cassettes/*.yaml` | recorded responses | — |
| `conftest.py` | fixtures + token handling | — |

## Notes specific to these tests

**They are read-only against DeviantArt.** Nothing here posts, edits,
favourites, comments, or deletes. Keep it that way — a test that
mutates a real account has no safe failure mode.

**They self-throttle.** A 1.5 s sleep runs after every test, so a full
live run takes a couple of minutes. That is deliberate politeness
toward an API with no documented rate limit.

**`test_auto_refresh_live.py` deliberately corrupts its token** to
force a 401 and prove the auto-refresh path recovers. A failure there
means recovery is broken, not that your credentials are wrong.

**The `user_token` fixture writes your rotated refresh token back to
`~/.local/state/da-cli/state.json`.** This is intentional — DeviantArt
rotates refresh tokens on use, and discarding the new one would break
your next `da` command. Nothing else of yours is written.

## What these tests do not cover

Write operations, the browser-based `da auth` flow (it needs a human),
and `da config set` / `unset` (they would mutate your real config).
