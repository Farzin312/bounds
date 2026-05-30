# Changelog

## [0.1.0] — 2026-05-29

### Initial release

Compact brings AI-native codebase understanding via subsystem boundary manifests — with zero-LLM structural validation powered by tree-sitter.

### Features

- **Subsystem manifests** — declare subsystem boundaries, roles, public interfaces, and cross-boundary dependencies in YAML. Hidden `.compact/` directory prevents accidental token burn.
- **Tree-sitter extraction** — deterministic extraction of exported symbols and imports from Python and TypeScript/JavaScript source files. Zero LLM cost for structural operations.
- **Validation engine** — 6 checks covering structural drift, boundary compliance, contract compliance, cross-subsystem impact, cycle detection, and orphan detection.
- **5 validation modes** — `quick` (git-diff incremental, sub-200ms target), `full`, `preflight`, `hotfix`, `audit`.
- **Content-addressable cache** — per-file hashing over tree-sitter output (not raw source), so whitespace-only and comment-only changes don't trigger re-validation.
- **Reference propagation** — when a subsystem changes, all consumers are checked via interface-name comparison (zero tree-sitter, zero LLM).
- **Quick mode** — `compact validate --quick` uses git diff to re-extract only changed files, then propagates impact through the subsystem graph.
- **JSON-by-default CLI** — every command outputs structured JSON. `--human` re-renders the same data for terminal use. `--ci` output for log grepability.
- **Stable error codes** — all error codes live in `errors.py` and are a public contract. Never renumbered, renamed, or repurposed between versions.
- **Commands:** `compact init`, `compact list`, `compact describe`, `compact validate`, `compact preflight`, `compact overview`.
- **Cross-platform support** — Linux, macOS, Windows. Python 3.10 through 3.14. Prebuilt tree-sitter wheels for all platforms — no C compiler required.
- **Agent-first design** — JSON output by default, designed for AI coding agents (Claude Code, OpenCode, Codex CLI, Cursor, Windsurf) to consume directly. Agent integration instructions for every major platform.

### Language support

- Python (tree-sitter-python) — functions, classes, decorated exports
- TypeScript / JavaScript (tree-sitter-typescript) — exports, classes, interfaces, type aliases

### Explicitly not in v0.1

- LLM enrichment (`describe --deep` is stubbed — returns `{"note": "LLM enrichment not enabled"}`)
- MCP server (integration today is CLI-only)
- Languages beyond Python + TypeScript/JavaScript
- `migrate` command, configurable ignore lists, IDE extensions

### Notes

- Compact validates ITSELF (`compact validate --human` on the Compact project) — fully dogfooding from day one.
- All 61+ tests pass with pytest-xdist parallel execution.
