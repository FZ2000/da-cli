# Thanks for opening a PR! Please read CONTRIBUTING.md first

## Summary

<!-- What does this change do, in one or two sentences? -->

## Why

<!-- The motivation. What problem does this solve? Link issues if relevant: "Closes #123". -->

## Checklist

- [ ] `ruff check .` passes
- [ ] `ruff format --check .` passes
- [ ] `mypy dacli` passes
- [ ] `pytest -q` passes (coverage stays >= 92%)
- [ ] New functionality has tests
- [ ] No new runtime dependencies introduced (stdlib only — see CONTRIBUTING.md)
- [ ] CHANGELOG.md updated under `[Unreleased]` if user-visible
- [ ] No secrets / tokens / personal data in the diff

## Notes for reviewer

<!-- Anything non-obvious: design tradeoffs, alternative approaches considered,
performance implications, follow-up work this enables. -->
