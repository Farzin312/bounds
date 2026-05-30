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

.PHONY: help install dev test lint validate benchmark clean

help: ## List available targets
	@echo "Bounds — make targets:"
	@echo "  make install    Editable install of the package"
	@echo "  make dev        Editable install with dev extras (pytest, etc.)"
	@echo "  make test       Run the test suite (pytest -q)"
	@echo "  make lint       Best-effort lint (ruff if present, else compileall)"
	@echo "  make validate   Dogfood: bounds validate --human"
	@echo "  make benchmark  Time a few bounds commands against this repo"
	@echo "  make clean      Remove build/cache artifacts and .bounds/cache.db"
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

# benchmark: time a few representative read-only commands against this repo.
# Timing varies run-to-run (interpreter startup dominates) — that's expected.
#
# NOTE: the benchmark doc (benchmarks/<version>/README.md) reports context
# cost in TOKENS, not bytes. The raw `wc -c` byte counts shown there are the
# measured proxy; the doc converts/frames them as token savings. This target
# only reproduces the wall-clock timing portion.
benchmark: ## Time a few bounds commands (timing varies; that's fine)
	@echo "==> Benchmarking Bounds against this repo (timings are approximate)"
	@echo "--- bounds list ---"
	@time $(BOUNDS) list >/dev/null
	@echo "--- bounds describe models ---"
	@time $(BOUNDS) describe models >/dev/null
	@echo "--- bounds validate --quick ---"
	@time $(BOUNDS) validate --quick >/dev/null
	@echo "==> Done. See benchmarks/ for token-cost figures (reported in tokens)."

clean: ## Remove build/cache artifacts and the bounds cache db
	rm -rf build dist .pytest_cache *.egg-info src/*.egg-info
	rm -f .bounds/cache.db
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
