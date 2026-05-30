# CLAUDE.md — project memory for Bounds

Working guide for agents and contributors in this repo. **This is not the product doc or the design doc.**
- Product pitch, install, agent integration → [README.md](README.md)
- Deep-dive docs (how-it-works, team workflow, CLI reference, agent integration, token economics) → [docs/](docs/README.md)
- Engineering contract (modules, data model, error codes, JSON shapes) → [ARCHITECTURE.md](ARCHITECTURE.md)
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
- **Extensible schema (s-17).** Roles/criticality are built-ins by default but can be overridden in
  `root.yaml` (`roles:`/`criticality:`); resolve via `RootManifest.role_registry()` /
  `criticality_registry()`, never hard-code the enum at a check site.

## Where things live

`config.py` constants · `errors.py` codes · `models.py` data model · `manifest/` load+schema ·
`extract/` tree-sitter adapters (`registry.get_adapter` dispatches by extension; `scan.py` =
shared fs→extraction helpers) · `cache/store.py` SQLite `cache.db` (+ migration/partial-read/inspect) ·
`validate/{engine,propagation,checks}` · `cli.py` command wiring · `discover.py` (s-14) ·
`calibrate.py` (s-16) · `agentsync.py` (s-18) · `ciconfig.py` (s-20) · `output.py` JSON/human emit.

Commands: `list` · `describe` · `validate` · `preflight` · `overview` · `init` · `impact` ·
`discover` · `calibrate` · `agent` · `ci` · `cache`.

**Adding a language adapter:** subclass `extract.base.LanguageAdapter` (set `language_name`,
`extensions`, implement `extract`), then register it in `extract/registry.py`. Use
`base.make_result(...)` so both hashes are computed consistently.

## Performance budget

`--quick` must stay sub-200ms on a typical repo: it diffs git, re-extracts only changed files, reuses
the content-hash cache for the rest, and propagates by manifest graph (no tree-sitter on unchanged
files). Don't add full-tree walks to the quick path.
