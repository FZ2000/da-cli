# Contributing to da-cli

Thanks for your interest. This file documents the local dev workflow,
the lint/test bar, and how to get a change merged.

## Dev environment

The `dacli` package is stdlib-only at runtime — `pip install` is not required to
*use* the CLI, and we never want to add a runtime dependency. Dev
tooling lives under the `dev` extra:

```bash
git clone https://github.com/FZ2000/da-cli.git
cd da-cli
python3 -m venv .venv-dev
. .venv-dev/bin/activate
pip install -e '.[dev]'
```

That installs `pytest`, `pytest-cov`, `ruff`, and `mypy`. No other
runtime libraries should ever be added to `[project.dependencies]`.

## Lint / type / test

The full pipeline must pass before any change is merged:

```bash
ruff check .
ruff format --check .
mypy dacli
pytest -q
```

These match what CI runs (see `.github/workflows/ci.yml`): lint on
Python 3.13; tests across the 3.10–3.14 matrix; integration + smoke
jobs on 3.13.

### Lint

`ruff` is configured strictly in `pyproject.toml` — pycodestyle,
pyflakes, isort, bugbear, comprehensions, pyupgrade, simplify,
ruff-rules, bandit (security), pylint, return, pathlib, tryceratops,
annotations, and pydocstyle. Don't blanket-ignore rules; if a rule is
genuinely wrong for the file, add a focused `# noqa: <code>` with
context.

### Types

`mypy` runs in a pragmatic strict-ish mode: every function in
The `dacli` package must have full type annotations, but JSON-derived `Any`
values don't need to be wrapped in TypedDicts. See `[tool.mypy]` in
`pyproject.toml` for the disabled error codes and rationale.

### Tests

`pytest` enforces ≥92% line+branch coverage on the `dacli` package via
`--cov-fail-under=92`. Don't lower this. New functionality needs new
tests.

Test conventions:

- One file per concern: `test_util.py`, `test_config.py`,
  `test_http_and_auth.py`, `test_sync.py`, `test_cli.py`,
  `test_commands.py`, `test_auth_flow.py`, `test_index.py`,
  `test_faults.py`, `test_integration.py`, `test_shim.py`,
  `test_conftest.py`.
- Integration tests (live DA) live under `tests/integration/`; see
  [tests/integration/README.md](tests/integration/README.md) for setup. They are excluded from the
  default `pytest` run by `tests/integration/conftest.py`, which adds a
  skip marker unless `-m integration` is passed — so they appear as
  skipped rather than being collected and failing.
- Fixtures live in `tests/conftest.py`. The `isolated_paths` fixture
  monkeypatches `dacli.CONFIG_PATH` / `STATE_PATH` to a `tmp_path`
  directory so tests never touch the real config.
- HTTP layer is mocked via `mock_urlopen` (see `conftest.py`).
- The `no_keychain` fixture stubs out `_keychain_get/set` so tests run
  identically on Linux and macOS.
- Live DeviantArt API tests live in `tests/integration/` and are
  skipped by default. See [docs/guides/testing.md](docs/guides/testing.md) for how to
  run them and how to supply credentials.

## Commit style

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```text
<type>(<optional scope>): <subject>

<optional body explaining the why, not the what>
```

Common types: `feat`, `fix`, `refactor`, `docs`, `test`, `ci`, `chore`.

Keep commits small and focused. A change that touches lint config and
adds a new feature should be two commits.

## Releases

Maintainers only:

1. Update `__version__` in `dacli/constants.py`.
2. Update `CHANGELOG.md` (move `Unreleased` items into a new
   versioned section with today's date).
3. Tag: `git tag -a v0.X.Y -m 'da-cli v0.X.Y'`.
4. Push the tag: `git push origin v0.X.Y`.
5. Now that a tag exists, link the heading in `CHANGELOG.md`: make it
   `## [0.X.Y] — <date>` and add a definition at the bottom of the file
   (`[0.X.Y]: <repo>/compare/v0.W.Z...v0.X.Y`, or `/releases/tag/v0.X.Y`
   for the first one). Do this *after* the tag is pushed — the file used
   to carry four such links for tags that were never created, and CI
   could not catch it because the link check runs `--offline`.

## Security

Don't include real `client_secret` / token values in commits, issues,
or test fixtures. The `.gitignore` excludes `config.json`, `state.json`,
`sync.log`, `*.log`, and `*.tmp` — but always double-check
`git diff --staged` before committing.

If you find a vulnerability, see [SECURITY.md](SECURITY.md) for
private-reporting instructions rather than filing a public bug.
