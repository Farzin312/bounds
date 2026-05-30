# Contributing to Bounds

Thank you for your interest! Bounds is an open-source tool for AI-native codebase
understanding via subsystem boundary manifests. We welcome contributions of all
kinds — bug reports, feature requests, documentation, and code.

## Code of Conduct

This project adheres to the [Contributor Covenant](https://www.contributor-covenant.org/)
code of conduct. By participating you agree to abide by its terms.

## How to File Issues

- **Bug report**: Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md).
  Include Bounds version, Python version, OS, reproduction steps, and expected vs
  actual behaviour.
- **Feature request**: Use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.md).
  Describe the problem, the proposed solution, and any alternatives you've considered.
- **Security issue**: Do not file a public issue. Email maintainers directly.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/Farzin312/bounds.git
cd bounds

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Verify the CLI works
bounds --help
```

## Project Structure

```
bounds/
├── .github/workflows/      # CI/CD workflows
├── src/bounds/
│   ├── cache/              # Content-addressable extraction cache
│   ├── extract/            # Tree-sitter language adapters
│   ├── manifest/           # Manifest loader + schema validation
│   ├── validate/           # Validation engine + checks
│   ├── cli.py              # Click CLI entry point
│   ├── config.py           # Global constants
│   ├── errors.py           # Error codes
│   ├── gitutil.py          # Git helpers
│   ├── models.py           # Data model dataclasses
│   └── output.py           # JSON / human rendering
├── tests/                  # Pytest test suite
├── ARCHITECTURE.md         # Engineering contract
├── CONTRIBUTING.md         # This file
├── README.md               # Project overview + agent integration
└── ROADMAP.md              # Future direction
```

## Coding Standards

### Python

- **Type hints**: Use `from __future__ import annotations` and annotate all
  function signatures. Prefer `str | None` over `Optional[str]`.
- **Docstrings**: Google-style (one-line or multi-line as appropriate). Where
  useful, include `Args:`, `Returns:`, and `Raises:` sections.
- **Line length**: 88 characters (black-compatible).
- **Imports**: Standard library, third-party, then local. One import per line.
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes,
  `UPPER_CASE` for constants.
- **Error handling**: Use `BoundsError` for expected failure paths. Prefer
  explicit exception types over bare `except:`.

### All Files

- **Newlines**: Unix (LF). Set `git config core.autocrlf input`.
- **No trailing whitespace**.
- **UTF-8 encoding**.

## Testing

Run the full suite before submitting a PR:

```bash
# Run tests in parallel
pytest -v -n auto

# With coverage
coverage run -m pytest && coverage report

# Self-test: validate Bounds on its own manifests
bounds validate --quick
```

All tests must pass. If you add a new feature, include tests.

### Writing Tests

- Tests live in `tests/` and mirror the `src/bounds/` package structure.
- Use `pytest` fixtures from `tests/conftest.py` for temporary projects.
- The `py_project` fixture creates a minimal two-subsystem Python project.
- CLI tests use CliRunner from Click.

## Branching and PR Workflow

1. **Branch**: Create a feature branch from `main`. Name it descriptively:
   `feat/add-go-adapter`, `fix/describe-human-formatter`.
2. **Develop**: Make your changes. Keep them focused on one concern.
3. **Test**: Run the test suite and the self-test (`bounds validate --quick`).
4. **Commit**: Write clear commit messages. Reference the issue number if applicable.
5. **PR**: Open a pull request to `main`. Use the PR template. Link the issue.
   Squash-merge when approved.

## Adding a Language Adapter

Tree-sitter adapters live in `src/bounds/extract/`. To add a new language:

1. Create a file `src/bounds/extract/<language>.py`.
2. Subclass `LanguageAdapter` from `.base`. Set `language_name` and `extensions`.
3. Implement `extract(self, rel_path, source) -> ExtractResult` to walk the
   tree-sitter tree for exported symbols and import references.
4. Register the adapter in `src/bounds/extract/registry.py`.
5. Add the tree-sitter grammar to `pyproject.toml` dependencies.
6. Write tests in `tests/test_extract.py`.

## Release Process

1. Update version in `pyproject.toml`.
2. Update `CHANGELOG.md`.
3. Tag the release: `git tag v0.2.0 && git push origin v0.2.0`.
4. The [release workflow](.github/workflows/release.yml) builds and publishes
   to PyPI automatically.
5. Verify: `pip install bounds==<version>` and `bounds --version`.

## Documentation

When contributing documentation changes:

- **README.md** — product pitch, quickstart, agent integration. Must include real benchmark data
  and competitive positioning. No emoji.
- **ARCHITECTURE.md** — engineering contract. Every module signature, dataclass field, error code,
  and JSON shape is binding.
- **ROADMAP.md** — scope and phasing. What is in vs what is deferred.
- **SECURITY.md** — security principles (7), vulnerability disclosure policy, install channels.
- **CHANGELOG.md** — version history. Keep a summary of changes per release.
- **Benchmarks** — raw data in `benchmarks/v0.1.0/` with full methodology, commands, and
  measurements.
- **Terminology** — use `exposes`, `paths`, `consumes`, `consumed_by` (computed), `namespace`.
  Not `provides`, `files`, `consumed_by` (declared), `owns.files`.

## Questions?

Open a [Discussion](https://github.com/Farzin312/bounds/discussions) or file
an issue. We're friendly.
