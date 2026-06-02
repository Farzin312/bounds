# CLAUDE.md — project memory for Bounds

Working guide for agents and contributors in this repo. **This is not the product doc or the design doc.**
- Product pitch, install, agent integration → [README.md](README.md)
- Deep-dive docs (how-it-works, team workflow, CLI reference, agent integration, token economics) → [docs/](docs/README.md)
- Engineering contract (modules, data model, error codes, JSON shapes) → [ARCHITECTURE.md](ARCHITECTURE.md)
- **Reviewable coding-standards checklist (the invariants below, as PR-checkable items)** → [docs/coding-standards.md](docs/coding-standards.md)
- Scope & phasing (shipped vs planned) → README "Roadmap" section + GitHub Milestones

## What this repo is

Bounds is a zero-LLM CLI that extracts a codebase's structural surface (exported symbols + imports)
with tree-sitter and validates it against human-declared subsystem manifests in `.bounds/`. Python,
`src/` layout, package `bounds`, console entry `bounds = bounds.cli:main`.

## Python environment

Development runs in a virtualenv — one is **created**, never assumed to pre-exist (a fresh clone has
none). Set it up per [CONTRIBUTING.md](CONTRIBUTING.md#development-setup)
(`python -m venv .venv && pip install -e ".[dev]"`). **If** a `.venv/` is present in the workspace,
invoke tooling through it (`.venv/bin/python`, `.venv/bin/pytest`) rather than `source`-ing it; if it
isn't there yet, create it first. This is contributor/maintainer setup — end users never need it; they
just `pipx install` the `bounds` CLI.

## Binding constraints (enforced — see ARCHITECTURE.md for the why)

- **Zero LLM for structural ops.** Everything in `extract/`, `validate/`, `cache/` is tree-sitter +
  pure Python. The only LLM tier (Tier 3, `describe --deep`) is opt-in and **stubbed** in MVP — never
  add an LLM call to a structural path.
- **Determinism.** No timestamps, `random`, wall-clock, or set-ordering in any hash or serialized
  output. Sort collections at serialization boundaries so JSON is byte-stable across runs.
- **JSON-first.** Every command prints one JSON object to stdout. `--human` re-renders the *same* data —
  it must never expose information the JSON omits. Fatal errors print `{"error":{code,message,fix}}`.
- **Stable error codes.** All codes live in `errors.py`. They are a public contract — never renumber,
  rename, or repurpose an existing code; only add. Severities/exit codes per ARCHITECTURE.md §8.
- **Cross-platform paths.** Use `pathlib` everywhere. Store and compare repo-relative paths in
  **posix form** (`as_posix()`), never OS-specific separators. No `os.sep` string concatenation, no
  hard-coded `/` or `\`. Globbing skips `config.DEFAULT_IGNORES`.
- **Fail soft, report hard.** A single unparsable/unsupported file becomes an `Issue`
  (`E_EXTRACTION_FAILED` / `E_UNSUPPORTED_LANGUAGE`), never a crash. Only genuinely fatal conditions
  (no `.bounds/`, bad YAML, unknown subsystem) raise `BoundsError`.
- **Dataclasses.** Every model in `models.py` carries `to_dict()`; manifest-tier models also carry
  `from_dict()`. Cache round-trips go through `FileRecord.from_result`/`to_result`.
- **Hidden `.bounds/`.** Created only by `bounds init`/`bounds discover`; discovered only by walking up
  from CWD (`manifest.loader.find_root`). Never auto-load it from other tooling.
- **Binary cache = context armor.** The extraction cache is binary SQLite `.bounds/cache.db` (NOT
  JSON) so an agent can't `cat` it into context as a parseable token blob. Only the CLI reads it;
  `updated_at` is written empty (no wall-clock). Manifests stay human-readable YAML on purpose —
  only the *derived* cache is binary.
- **Think in tokens, not bytes.** Value/benchmarks are framed in tokens (an agent's cost is
  tokens-into-context); keep every command's output token-lean and retrieval targeted
  (`describe <name>`/`--namespace`/`impact` over loading the whole map).
- **Extensible schema.** Roles/criticality are built-ins by default but can be overridden in
  `root.yaml` (`roles:`/`criticality:`); resolve via `RootManifest.role_registry()` /
  `criticality_registry()`, never hard-code the enum at a check site.

## Source of Truth & Versioning (Mandatory)

GitHub is the single source of truth. To prevent staleness and ensure `pipx upgrade` works for all users:
- **Automatic Versioning (dynamic CalVer).** This repo uses `setuptools-scm` with a custom scheme (`setup.py:calver`) that yields a clean `YYYY.M.<build>` version (e.g. `2026.6.24`) — git-derived, no `dev`/`+local` suffix. `<build>` is the repo's **total commit count**, so the version strictly increases on every commit (multiple same-day commits get distinct versions) and never regresses, even after a release tag. Never add a static `version =` string; never reintroduce a `dev`/distance-based patch (it would collide same-day or regress after a tag — see `tests/meta/test_versioning.py`).
- **Release process.** Tags are optional under CalVer (the version is always clean from commits alone). To mark a formal release, tag with the current CalVer: `git tag -a $(bounds --version | awk '{print $2}') -m "Release" && git push --tags`.
- **Contributor installs.** Always install using `pip install -e .` (editable) for development.
- **End-user staleness check.** If an agent or user reports a stale `bounds` CLI, the fix is: `pipx install --force git+https://github.com/Farzin312/bounds.git`.

## Where things live

`config.py` constants · `errors.py` codes · `models.py` data model · `manifest/` load+schema ·
`extract/` tree-sitter adapters (`registry.get_adapter` dispatches by extension;
`registry.is_language_file` = the one "is this file language X?" check, never hardcode an extension
list; `scan.py` = the **single home** for fs→extraction helpers — `walk_supported` (the one recursive
source walk; pass `exts=None` to walk *every* file) / `iter_subsystem_files`/`iter_repo_source`/
`extract_file`/`strip_ext`/`in_default_ignores` + `read_source_bytes`/`is_oversized` (the one
size-guard+read mechanism; engine vs `extract_file` differ only in policy) + `resolve_owners` (the one
file→subsystem ownership map — **most-specific declared path/file wins**, `path_specificity` ranks it;
shared by engine + extract_project so validate/where/impact/calibrate never disagree) + `mapping_coverage`
(the one source-coverage metric: mapped % + by-language unmapped breakdown, gitignore-aware, gated off
`--quick`), shared by engine + describe + discover/calibrate; never copy a walk or an owner-assignment;
`base.canonical_columns` = the one schema column dedup/sort; `rawquery.py` = opt-in, advisory raw-SQL
table refs) · `cache/store.py` SQLite `cache.db` (+ migration/partial-read/inspect) ·
`validate/{engine,propagation,checks,schema}` (`checks.resolve_import`/`build_suffix_index` = the one
import resolver; `schema.py` folds SQL migrations into the live surface — `_fold_subsystem_schema` =
tables/columns, `_fold_subsystem_objects` = the one ordered fold for functions/views/indexes/triggers/
types + **policies/RLS** (create/alter/drop, enable/disable/force net out), `schema_objects`/
`schema_rls_posture` read it, `hash_schema_catalog` hashes an already-built catalog without re-folding;
`extract/sql.py` is the one SQL extractor — grammar-native DDL descends into transaction blocks,
Postgres RLS is recovered by comment/string/body-masked regex, table refs canonicalise to the bare
name; bump `config.STATE_VERSION` whenever extraction output changes for unchanged source) ·
`describe.py` Tier-1+2
describe assembly · `locate.py` backs `where`+`impact` · `cli.py` command wiring (arg-parse + one
`go()` per command, no business logic; `_progress(msg)` = the one loading-spinner seam — wrap compute
only, never `output.emit`) · `discover.py` · `calibrate.py` · `agentsync.py` ·
`ciconfig.py` · `gitutil.py` git detection + changed-file diff (backs `--quick`) ·
`ignore.py` `.boundsignore` + generated-code detection ·
`update_check.py`/`upgrade.py` GitHub-release check + `pipx` self-upgrade · `output.py` JSON/human emit.

Commands: `list` · `describe` · `validate` · `preflight` · `overview` · `init` · `impact` ·
`where` · `discover` · `calibrate` · `agent` · `ci` · `cache` · `upgrade` · `upgrade-check`.

**Adding a language adapter:** subclass `extract.base.LanguageAdapter` (set `language_name`,
`extensions`, implement `extract`), then register it in `extract/registry.py`. Use
`base.make_result(...)` so both hashes are computed consistently.

## Performance budget

`--quick` must stay sub-200ms on a typical repo: it diffs git, re-extracts only changed files, reuses
the content-hash cache for the rest, and propagates by manifest graph (no tree-sitter on unchanged
files). Don't add full-tree walks to the quick path.
