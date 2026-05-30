# Compact — Roadmap

What's in the box today, and where it's going. See [ARCHITECTURE.md](ARCHITECTURE.md) for the design
and [README.md](README.md) for usage.

---

## MVP — v0.1 (current)

The goal of v0.1 is a **working, zero-LLM, deterministic** structural validator that an AI agent can
call and trust. Everything here is shipped and tested.

**Languages**
- TypeScript / TSX / JavaScript / JSX (tree-sitter-typescript)
- Python (tree-sitter-python)

**Commands**
- `compact init --root` / `compact init --subsystem <name>` — scaffold `.compact/`
- `compact list` — discover subsystems
- `compact describe <name>` — one subsystem as JSON (`--deep` reserved; see below)
- `compact validate` — full validation (`--quick`, `--mode <m>`, `--enforce on|off`, `--base <ref>`)
- `compact preflight` — the 6 pre-PR checks, blocking
- `compact overview` — project health dashboard

**Engine**
- 6 checks: structural drift, boundary compliance, contract compliance, cross-subsystem impact,
  cycle detection, orphan detection
- 5 modes: `quick`, `full`, `preflight`, `hotfix`, `audit`
- Two-hash content-addressable cache (`content_hash` for cache hits, `structure_hash` for propagation)
- `--quick` incremental validation via `git diff` + reference propagation (sub-200ms target)
- JSON-by-default output, `--human` renderer, stable error codes with fix suggestions
- Cross-platform: Linux, macOS, Windows (pathlib + posix-normalized relative paths)

**Explicitly NOT in v0.1** (stubbed or deferred — see below):
- Real LLM enrichment. `describe --deep` returns a Tier-3 *stub* (`{"note": "LLM enrichment not
  enabled"}`); no provider call is made.
- MCP server. Integration today is via the CLI (any agent that can run a shell command). See README.
- Languages beyond TS/JS + Python.
- `migrate` command, watch mode, configurable ignore lists, IDE extensions.

---

## v0.2 — semantic tier + ergonomics

- **Tier 3 for real:** `describe --deep` calls a provider-agnostic LLM to add type signatures and intent
  summaries to declared interfaces. Cached, opt-in, token-metered. Never on a structural path.
- `compact migrate` — schema version upgrades for `.compact/` manifests.
- Configurable ignores / include globs in `root.yaml`.
- More languages: Go, Rust, Java (new `LanguageAdapter` subclasses).
- Undeclared-export hints promoted from info → optional enforcement.

## v0.3 — distribution & ecosystem

- **MCP server wrapper** (`compact mcp`) exposing `list` / `describe` / `validate` as MCP tools, so
  MCP-native agents (Claude Code, Cursor, Windsurf, Codex) can call Compact without shelling out.
- Official **GitHub Action** and **pre-commit hook** for the preflight gate.
- **Homebrew formula** and prebuilt standalone binaries (pipx remains the cross-platform default).
- `compact watch` — re-validate on file change.

## Later / exploratory

- Nested subsystem topology (current model is intentionally flat).
- Web dashboard for `overview` (interactive subsystem graph).
- Language-server integration for in-editor boundary diagnostics.
- Auto-suggest manifests from a first scan (`compact init --infer`).

---

## Non-goals (by design)

- Compact is **not** a linter, type checker, or test runner — it validates *architecture boundaries*,
  not code correctness.
- The structural engine will **never** depend on an LLM. Determinism and zero token cost are the point.
- `.compact/` is **not** auto-discovered by other tooling — access is CLI-only and explicit.
