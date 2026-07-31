# Developer conveniences. Run `make help` for the menu, or just read the
# targets below — each has a one-line comment. Everything here is a thin
# wrapper around the four commands CI runs (ruff, ruff format, mypy,
# pytest); no hidden behaviour.

.PHONY: help install dev-setup lint format format-check type test test-integration check check-docs smoke bench docs clean

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make \033[36m<target>\033[0m\n\nTargets:\n"} \
	      /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install:  ## Install da into ~/.local (production layout)
	./install.sh

dev-setup:  ## Create .venv-dev and install dev tooling (ruff, mypy, pytest)
	python3 -m venv .venv-dev
	. .venv-dev/bin/activate && pip install --upgrade pip && pip install -e '.[dev]'

lint:  ## Run ruff check
	. .venv-dev/bin/activate && ruff check .

format:  ## Run ruff format (writes)
	. .venv-dev/bin/activate && ruff format .

format-check:  ## Run ruff format --check (no writes)
	. .venv-dev/bin/activate && ruff format --check .

type:  ## Run mypy on the dacli package
	. .venv-dev/bin/activate && mypy dacli

test:  ## Run full pytest suite (coverage gate enforced)
	. .venv-dev/bin/activate && pytest -q

test-integration:  ## Run integration tests verbosely (no coverage)
	. .venv-dev/bin/activate && pytest tests/test_integration.py -v --tb=short --no-cov

check-docs:  ## Verify code references in the docs still resolve
	. .venv-dev/bin/activate && python3 tools/check_doc_references.py
	python3 tools/check_doc_flags.py

docs:  ## Regenerate the generated CLI reference
	. .venv-dev/bin/activate && python3 tools/gen_cli_docs.py

check: lint format-check type test  ## Full pre-push pipeline (what CI runs)

smoke:  ## Run the smoke checks (da --version, da diagnose, da bench)
	. .venv-dev/bin/activate && \
	  python3 da --version && \
	  python3 da --help >/dev/null && \
	  tmp=$$(mktemp -d) && \
	  XDG_CONFIG_HOME=$$tmp/cfg XDG_STATE_HOME=$$tmp/state python3 da diagnose; rc=$$?; \
	  [ $$rc -eq 2 ] || { echo "diagnose should exit 2 on empty config, got $$rc"; exit 1; }; \
	  XDG_CONFIG_HOME=$$tmp/cfg XDG_STATE_HOME=$$tmp/state python3 da bench --pages 5 --json; \
	  rm -rf $$tmp

bench:  ## Run a longer benchmark (50 pages, full output)
	. .venv-dev/bin/activate && python3 da bench --pages 50 --per-page 24 --concurrency 4

clean:  ## Remove caches, coverage, pytest tmp dirs
	rm -rf .ruff_cache .mypy_cache .pytest_cache .coverage coverage.xml htmlcov pytest-*
	rm -rf da_cli.egg-info __pycache__ tests/__pycache__
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
