# Testing guide

How to run da-cli's tests and, if you need the live ones, how to supply
credentials.

## The short version

```bash
make dev-setup     # once: creates .venv-dev with ruff, mypy, pytest
make check         # what CI runs: lint + format + types + tests
```

That is enough for almost every change. Everything below is detail.

## The three layers

da-cli has three test layers, in increasing order of what they need
from you.

| Layer | Marker | Needs | Runtime | Runs in CI |
| --- | --- | --- | --- | --- |
| Unit and mocked end-to-end | *(none)* | nothing | ~6 s | every push |
| Cassette replay | `integration_cassette` | the `integration` extra | ~5 s | every push |
| Live API | `integration_anonymous`, `integration_authenticated` | DeviantArt credentials | ~1–2 min | when secrets are set |

**Unit and mocked end-to-end** is the default suite. `urllib` is
mocked, so it is hermetic and deterministic. This is the layer to add
to for almost any change.

**Cassette replay** replays real DeviantArt responses recorded to
`tests/integration/cassettes/*.yaml`. No network and no credentials,
but real response bodies — so it catches DeviantArt renaming or
removing a field, which mocks cannot.

**Live API** calls DeviantArt for real. It catches what recordings
cannot: an endpoint that starts rejecting a parameter, a scope that
stops being granted, rate limiting. It needs credentials, so it is
opt-in.

## Running each layer

```bash
pytest                                           # default suite; live tests show as skipped
pytest -m integration_cassette --no-cov          # cassette replay (no credentials)
pytest -m integration_anonymous --no-cov         # live, app credentials only
pytest -m integration_authenticated --no-cov     # live, needs your user token
pytest -m integration --no-cov                   # every live + cassette test
```

Live tests are skipped unless `-m integration...` selects them, so the
bare `pytest` above never touches the network.

Pass `--no-cov` on every command except the bare `pytest`. The 92%
coverage gate is enforced on every run, so a partial selection reports
its tests as passed and then fails the run on coverage.

### Running a subset

The coverage gate (92%) is enforced on every run, so a partial run
fails on coverage even when every test passes. Pass `--no-cov` when
you are iterating:

```bash
pytest tests/test_sync.py --no-cov
pytest tests/test_sync.py::TestSaveOne::test_writes_description --no-cov
pytest -k "checkpoint" --no-cov
```

## Credentials for the live layers

Only the live layers need these. Both read from your existing `da`
configuration first, so if you already use da-cli, they may work with
no setup at all.

### Anonymous tests (`integration_anonymous`)

Need a DeviantArt application's `client_id` and `client_secret` — the
same pair `da config set` uses. They authenticate with the
`client_credentials` grant: no browser, no user, no expiry.

Resolution order:

1. `DA_CLIENT_ID` / `DA_CLIENT_SECRET` environment variables
2. `~/.config/da-cli/config.json`
3. macOS Keychain (macOS only — on Linux use option 1 or 2)

```bash
export DA_CLIENT_ID=12345
export DA_CLIENT_SECRET=your-secret
pytest -m integration_anonymous
```

If none is found, the tests skip with a message naming what is
missing.

### User-scoped tests (`integration_authenticated`)

Need a `refresh_token`, which identifies *you* — these tests read your
watch feed and gallery. Run `da auth` once and they will pick it up
from `~/.local/state/da-cli/state.json` automatically.

Resolution order:

1. `DA_REFRESH_TOKEN` environment variable
2. `refresh_token` in `~/.local/state/da-cli/state.json`

Optionally set `DA_SCOPE` (default `browse`) if your token was issued
with a different scope.

DeviantArt refresh tokens expire **90 days** after they are issued.
When that happens these tests fail with `invalid_grant`; re-run
`da auth`. `da diagnose` warns 14 days ahead.

> **These tests write to your real `state.json`.** DeviantArt rotates
> refresh tokens on use, so the fixture persists the rotated token
> back — otherwise your next `da` command would fail with a token the
> server has already retired. Nothing else of yours is written: config,
> Keychain, and your sync destination are never touched, and all test
> output goes to a temp directory.

## Cassettes

Cassettes are recorded DeviantArt responses, committed under
`tests/integration/cassettes/`. Re-record them when DeviantArt changes
a response shape:

```bash
VCR_RECORD=1 pytest -m integration_cassette --no-cov   # needs live credentials
git diff tests/integration/cassettes/                  # review what changed
```

Read that diff before committing — a changed field is exactly the
signal the layer exists to give you, and it usually means the package
needs a matching change.

Recording scrubs `Authorization` headers and the response headers that
identify the recording machine (CDN edge location, timestamp). Response
*bodies* are kept verbatim, so they contain public DeviantArt content:
usernames, titles, and wixmp CDN URLs.

Those CDN URLs contain `eyJ`-prefixed tokens. They are **not**
credentials — every DeviantArt response embeds them, they are public
and short-lived, and `.gitleaks.toml` allowlists them deliberately. An
`eyJ` in a *request* `Authorization` header or in a `client_secret`
parameter would mean the scrubber failed; in a response body it is
expected.

## Adding tests

Put new tests in the layer that can actually catch the bug:

- Logic, branching, error handling → the default suite. Mock
  `urllib.request.urlopen`, or the `dacli.http_*` helpers.
- "DeviantArt returns a field we depend on" → a cassette test in
  `tests/integration/test_api_response_shapes.py`.
- "DeviantArt still accepts this request" → a live test in
  `tests/integration/`.

Mark live tests with both `@pytest.mark.integration` and the specific
marker (`integration_anonymous` or `integration_authenticated`), or
they will run in the default suite and fail in CI.

## Continuous integration

| Job | Layer | Gates a PR? |
| --- | --- | --- |
| `test` | default suite, Python 3.10–3.14 | yes |
| `integration` | mocked end-to-end, verbose | yes |
| `cassette-replay` | cassette | yes |
| `network-anonymous` | live anonymous | no — needs repository secrets |

`network-anonymous` reads `DA_CLIENT_ID` and `DA_CLIENT_SECRET` from
repository Actions secrets and is non-blocking, because DeviantArt
being down should not block an unrelated PR.
