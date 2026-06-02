# Makefile — Bounds developer & reproduction targets
# ---------------------------------------------------
# Real commands for this repo. A virtualenv is expected at ./.venv
# (see CLAUDE.md). Targets prefer the venv binaries when present and fall
# back to bare `python`/`bounds` on PATH otherwise.
#
# No target performs network code-exec, telemetry, or sudo. `make benchmark`
# only runs read-only `bounds` queries against this repo (dogfooding).

# Prefer the project venv if it exists; otherwise use whatever is on PATH.
VENV   := .venv
PY     := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
PIP    := $(PY) -m pip
PYTEST := $(if $(wildcard $(VENV)/bin/pytest),$(VENV)/bin/pytest,$(PY) -m pytest)
BOUNDS := $(if $(wildcard $(VENV)/bin/bounds),$(VENV)/bin/bounds,bounds)

.DEFAULT_GOAL := help

.PHONY: help install dev test lint validate benchmark oss-bench clean

help: ## List available targets
	@echo "Bounds — make targets:"
	@echo "  make install            Editable install of the package"
	@echo "  make dev                Editable install with dev extras (pytest, etc.)"
	@echo "  make test               Run the test suite (pytest -q)"
	@echo "  make lint               Best-effort lint (ruff if present, else compileall)"
	@echo "  make validate           Dogfood: bounds validate --human"
	@echo "  make benchmark          Coverage + token-economics report on THIS repo (dogfood)"
	@echo "  make oss-bench REPO=DIR Combined coverage + token + command-health report on a cloned repo"
	@echo "  make clean              Remove build/cache artifacts and .bounds/cache.db"
	@echo ""
	@echo "Using PY=$(PY)  BOUNDS=$(BOUNDS)"

install: ## Editable install of the package
	$(PIP) install -e .

dev: ## Editable install with dev extras
	$(PIP) install -e ".[dev]"

test: ## Run the test suite
	$(PYTEST) -q

# Best-effort lint: use ruff if installed; otherwise just byte-compile the
# source as a cheap syntax check. Never fails the build for a missing linter.
lint: ## Best-effort lint (non-failing if ruff absent)
	@if $(PY) -m ruff --version >/dev/null 2>&1; then \
		echo "==> ruff check src tests"; \
		$(PY) -m ruff check src tests; \
	else \
		echo "==> ruff not installed; falling back to compileall (syntax check)"; \
		$(PY) -m compileall -q src; \
	fi

validate: ## Dogfood Bounds on itself
	$(BOUNDS) validate --human

# benchmark: deterministic, model-agnostic dogfood harness. Shells out to
# read-only `bounds` queries against THIS repo and reports BOTH headline value
# props — mapping coverage (authoritative `bounds validate` metric) and token
# savings (Bounds contract vs equivalent source). No wall-clock in its output;
# latency is a de-emphasized, separately-labeled note in benchmarks/results/.
# See benchmarks/README.md for methodology and how to contribute a result.
benchmark: ## Coverage + token-economics report on this repo (dogfood)
	$(PY) benchmarks/run.py

# oss-bench: combined coverage + token-economics + command-surface health report
# for ONE already-cloned third-party repo. Runs oss_bench.py + oss_features.py
# and prints a finished markdown block (closes the hand-assembled-tables hole).
# It WRITES a fresh .bounds/ into the target repo (init + discover); point it at
# a throwaway clone, never your working tree. Usage:
#   make oss-bench REPO=/path/to/clone [NAME=flask LANG=python]
oss-bench: ## Combined coverage + token + command-health report on a cloned repo (REPO=DIR)
	@if [ -z "$(REPO)" ]; then \
		echo "usage: make oss-bench REPO=/path/to/cloned/repo [NAME=flask LANG=python]"; \
		exit 2; \
	fi
	$(PY) benchmarks/oss_report.py --repo "$(REPO)" \
		$(if $(NAME),--name "$(NAME)",) $(if $(LANG),--lang "$(LANG)",)

clean: ## Remove build/cache artifacts and the bounds cache db
	rm -rf build dist .pytest_cache *.egg-info src/*.egg-info
	rm -f .bounds/cache.db
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
