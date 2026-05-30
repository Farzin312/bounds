# CLAUDE.md — project memory for Compact

Working guide for agents and contributors in this repo. **This is not the product doc or the design doc.**
- Product pitch, install, agent integration → [README.md](README.md)
- Engineering contract (modules, data model, error codes, JSON shapes) → [ARCHITECTURE.md](ARCHITECTURE.md)
- Scope & phasing (what's MVP vs later) → [ROADMAP.md](ROADMAP.md)

## What this repo is

Compact is a zero-LLM CLI that extracts a codebase's structural surface (exported symbols + imports)
with tree-sitter and validates it against human-declared subsystem manifests in `.compact/`. Python,
`src/` layout, package `compact`, console entry `compact = compact.cli:main`.

## Dev environment & commands

```bash
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"          # editable install + pytest
compact --help                   # smoke test the CLI
pytest                           # run the test suite
compact validate --human         # run Compact on itself (bootstrap demo)
```

A `.venv/` already exists in this workspace; prefer `.venv/bin/python` / `.venv/bin/pytest`.

## Code conventions (enforced — see ARCHITECTURE.md for the why)

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
  (no `.compact/`, bad YAML, unknown subsystem) raise `CompactError`.
- **Dataclasses.** Every model in `models.py` carries `to_dict()`; manifest-tier models also carry
  `from_dict()`. Cache round-trips go through `FileRecord.from_result`/`to_result`.
- **Hidden `.compact/`.** Created only by `compact init`; discovered only by walking up from CWD
  (`manifest.loader.find_root`). Never auto-load it from other tooling.

## Where things live

`config.py` constants · `errors.py` codes · `models.py` data model · `manifest/` load+schema ·
`extract/` tree-sitter adapters (`registry.get_adapter` dispatches by extension) · `cache/store.py`
state.json · `validate/{engine,propagation,checks}` · `cli.py` commands · `output.py` JSON/human emit.

**Adding a language adapter:** subclass `extract.base.LanguageAdapter` (set `language_name`,
`extensions`, implement `extract`), then register it in `extract/registry.py`. Use
`base.make_result(...)` so both hashes are computed consistently.

## Performance budget

`--quick` must stay sub-200ms on a typical repo: it diffs git, re-extracts only changed files, reuses
the content-hash cache for the rest, and propagates by manifest graph (no tree-sitter on unchanged
files). Don't add full-tree walks to the quick path.
