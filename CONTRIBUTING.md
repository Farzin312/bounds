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

> **After `git pull`, re-run `pip install -e .`** to refresh `bounds --version`. This repo derives its
> version from git tags via `setuptools-scm`, and that version is computed at **(re)build time**, not on
> every `git pull` — so an editable install keeps reporting the version from when it was last built until
> you reinstall. (No version string is ever hand-edited; see "Source of Truth & Versioning" in
> [CLAUDE.md](CLAUDE.md).)

## Project Structure

```
bounds/
├── .github/workflows/      # CI/CD workflows
├── src/bounds/
│   ├── cache/              # Binary SQLite extraction cache (cache.db)
│   ├── extract/            # Tree-sitter language adapters (+ scan.py shared helpers)
│   ├── manifest/           # Manifest loader + schema validation
│   ├── validate/           # Validation engine + checks + propagation
│   ├── cli.py              # Click CLI entry point (all commands)
│   ├── config.py           # Global constants + role/criticality registries
│   ├── errors.py           # Error codes
│   ├── gitutil.py          # Git helpers
│   ├── ignore.py           # .boundsignore matcher + @generated detection
│   ├── models.py           # Data model dataclasses
│   ├── output.py           # JSON / human / CI rendering
│   ├── discover.py         # Bootstrap discovery
│   ├── calibrate.py        # Manifest↔source reconciliation
│   ├── agentsync.py        # Cross-agent config generation
│   └── ciconfig.py         # CI config generation
├── tests/                  # Pytest test suite (10 files; CI reports the live count)
├── docs/                   # Deep-dive documentation (linked from the README entrance)
├── ARCHITECTURE.md         # Engineering contract
├── CONTRIBUTING.md         # This file
└── README.md               # Lean visual entrance — overview + quickstart, links into docs/
```

## Repository Hygiene — How the Repo Scales and Stays Clean

Bounds is a tool *about* keeping architecture legible; the repository has to model that. As the
project grows, keep it navigable by holding these invariants. A change that violates one should be
fixed in the same PR, not deferred.

**1. One module, one concern.** Each file under `src/bounds/` owns a single responsibility and is
declared as (or part of) a subsystem in `.bounds/manifests/`. New functionality is a new module with
a clear boundary — not a grab-bag appended to `cli.py` or `engine.py`. `cli.py` is wiring only;
command logic lives in its own module (see `discover.py`, `calibrate.py`, `agentsync.py`,
`ciconfig.py`). If you can't name the subsystem a file belongs to, it isn't scoped yet.

**2. The repo dogfoods itself — keep it green.** Bounds models its own architecture in `.bounds/`.
After any change to `src/bounds/**`, run `bounds validate` (and `bounds calibrate` to see what
drifted) and update the affected manifest in the same PR. `bounds validate --quick` runs in CI; a PR
that leaves the project's own manifests stale will not be merged. New cross-subsystem imports must be
reflected as `exposes`/`consumes` edges — that's the architecture staying honest.

**3. One source of truth — no drift.** Every fact lives in exactly one place. Agent-config text lives
in `agentsync.py` (the generator), never also in a `templates/` copy. The architecture map lives in
the manifests (queried via `bounds describe`), mirrored as prose in `ARCHITECTURE.md`. Scope/status
lives in the README "Roadmap" section + GitHub Milestones. If you find the same fact in two files,
collapse it to one and link.

**4. Commit only what others need; generate the rest.** A clone should be lean. Commit source,
tests, docs, and the canonical agent contract (`AGENTS.md`) + dev memory (`CLAUDE.md`). Do **not**
commit anything a tool regenerates locally: the binary cache (`.bounds/cache.db`), per-tool agent
configs (`GEMINI.md`, `.cursor/`, `.windsurf/`, `.aider.conf.yml`, `.github/copilot-instructions.md`,
`.claude/commands/bounds.md` — all produced by `bounds agent --sync`), build artifacts, or
`bounds ci --install` output. These are in `.gitignore`; if you add a new generator, gitignore its
output in the same PR. The test for "should this be committed?" is: *would a fresh cloner need it, or
can they regenerate it with one command?*

**5. Lean root.** The repository root is the first thing a visitor reads — keep it to source dirs,
governance docs, packaging, and the canonical agent files. New top-level files need a real
justification; prefer a subdirectory (`docs/`, `assets/`, `benchmarks/`) over another root entry.

**6. Stable contracts only grow.** Error codes in `errors.py` and the JSON output shapes are a public
contract: add, never rename or repurpose. The same applies to the manifest schema (version it; old
schemas are supported forever).

**7. Comments serve the reader.** Module/function docstrings say *why this exists* and the one
non-obvious rule, briefly. No process noise (no review/TODO chatter in committed code), no comments
that merely restate the code. The "why" comments (determinism, context-armor, the token rationale)
are the ones worth keeping.

## Coding Standards

> **The binding, reviewable checklist lives in [docs/coding-standards.md](docs/coding-standards.md).**
> It encodes the invariants every structural change must hold — determinism, fail-soft /
> report-hard, zero-LLM on the structural path, resource bounds, the additive-only output contract,
> the single-source severity table, the "one home per concept" DRY rule, and tests-for-new-behavior.
> Walk its **(blocking)** sections when you review or self-review a PR. The conventions below are the
> day-to-day style layer on top of it.

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

> **Read [docs/testing.md](docs/testing.md)** for the full guide: the invariants every change must
> preserve (determinism, fail-soft, JSON-first, stable error codes, posix paths), how to write a
> regression test, the `xfail(strict=True)` pattern for known-but-unfixed bugs, and — importantly —
> **how to tell an intended behavior change from a regression and update the right baseline**
> (`config.STATE_VERSION` bumps, `calibrate --dump-baseline`, append-only `errors.py`).

### Writing Tests

- Tests live in `tests/` and are grouped by feature area (10 files; run the full suite — CI reports the count):
  `test_extract.py`, `test_validate.py`, `test_schema_flex.py` (roles/criticality),
  `test_cache_sqlite.py`, `test_discover.py`, `test_calibrate.py`, `test_agentsync.py`,
  `test_ciconfig.py`, `test_cli.py`, and `test_commands_cli.py`.
- Use `pytest` fixtures from `tests/conftest.py` for temporary projects: `sample_project`
  (multi-subsystem TS+Py), `py_project` (minimal Python project), `git_sample_project` /
  `git_init` (git-backed variants for quick-mode tests).
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
   tree-sitter tree for exported symbols and import references. Build the result via
   `base.make_result(...)` so both the content and structure hashes are computed consistently.
4. Register the adapter in `src/bounds/extract/registry.py`.
5. Add the tree-sitter grammar to `pyproject.toml` dependencies.
6. Write tests in `tests/test_extract.py`.

## Release Process & Source of Truth

GitHub is the single source of truth for the codebase and its version.

1. **Automatic Versioning.** This repository uses `setuptools-scm`. The version is derived dynamically from git tags and the commit count. **Never** re-add a static `version =` string to `pyproject.toml`.
2. **Update CHANGELOG.md.** Document the changes for the new version.
3. **Tag the release.** To cut a formal release, create a git tag: `git tag -a v0.x.y -m "Release v0.x.y" && git push origin v0.x.y`.
4. **Automatic Build.** The [release workflow](.github/workflows/release.yml) detects the tag, builds the sdist/wheel, and publishes to PyPI using the tag as the version.
5. **Update local CLI.** To update your own `bounds` installation to the latest version on GitHub, run: `pipx install --force git+https://github.com/Farzin312/bounds.git`.

## Documentation

When contributing documentation changes:

- **README.md** — the **lean visual entrance**: overview, why-use, quickstart, and the visuals. It
  stays short and links into `docs/` for depth — don't grow it back into a manual. No emoji.
- **docs/** — the deep-dive guides (`why-bounds`, `team-workflow`, `how-it-works`, `token-economics`,
  `use-cases`, `cli-reference`, `ai-agents`, `languages-and-platforms`, `install`, `comparison`),
  indexed by `docs/README.md`. New long-form content goes here and is linked from the README, not
  pasted into it. Honesty rules apply (compliance is advisory; the cache deters naive dumps, not
  access; Python + TS only today; `--deep` stubbed).
- **ARCHITECTURE.md** — engineering contract. Every module signature, dataclass field, error code,
  and JSON shape is binding.
- **README "Roadmap" section + GitHub Milestones** — scope and phasing (shipped vs deferred).
- **SECURITY.md** — security principles (7), vulnerability disclosure policy, install channels.
- **CHANGELOG.md** — version history. Keep a summary of changes per release.
- **Benchmarks** — raw data in `benchmarks/v0.1.0/` with full methodology, commands, and
  measurements.
- **Terminology** — use `exposes`, `paths`, `consumes`, `consumed_by` (computed), `namespace`.
  Not `provides`, `files`, `consumed_by` (declared), `owns.files`.

## Questions?

Open a [Discussion](https://github.com/Farzin312/bounds/discussions) or file
an issue. We're friendly.
