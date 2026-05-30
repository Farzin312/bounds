<div align="center">

# Bounds

### Architecture contracts your AI agents can trust

**Bounds** is a CLI that turns your codebase's architecture into deterministic, machine-readable
manifests — validated against source with **tree-sitter (zero LLM)**. AI coding agents read a 10-line
subsystem contract instead of 10 source files, and get structural validation they can trust, in
milliseconds, for zero tokens.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey.svg)](#cross-platform-support)
[![Zero LLM](https://img.shields.io/badge/structural%20validation-zero%20LLM-brightgreen.svg)](#how-it-works)

[Quick start](#quick-start) · [Why not another code graph?](#why-not-another-code-graph) · [Token cost comparison](#token-cost-comparison) · [For AI agents](#for-ai-coding-agents) · [How it works](#how-it-works) · [Security](#security) · [Architecture](ARCHITECTURE.md) · [Roadmap](ROADMAP.md)

</div>

---

## The problem

When an AI agent opens your repo, it does the expensive thing: it reads source files — sometimes
dozens — to reconstruct what each part does and how the parts connect. Every file burns tokens.
Every wrong guess produces a bad edit. And the agent's mental model is stale the moment it's built,
with nothing to validate it against.

But an agent doesn't need every function in `auth.ts`. It needs:

> *"auth exposes `login`, `verify`, `register` and talks to the database via `user_repository`."*

That fits in five lines of YAML — and a machine can reason about it **deterministically**.

## What Bounds does

Bounds maintains a hidden `.bounds/` directory of **subsystem boundary manifests**: tiny YAML files
that declare each subsystem's role, its public interfaces (exposes), and its cross-boundary
dependencies (consumes). It then uses **tree-sitter** (never an LLM) to extract the *actual* exported
symbols and imports from your source and **validates the manifests against reality** — in both
directions.

The result is a structural contract an agent can query, and a CI pipeline can enforce:

- **`bounds describe`** — hand an agent a subsystem's exact public surface as JSON, instead of raw files.
- **`bounds validate`** — catch drift the moment exports stop matching the manifest. 6 checks, zero LLM.
- **`bounds validate --quick`** — git-diff incremental validation, safe for every commit.
- **`bounds preflight`** — 6 pre-PR checks: drift, boundaries, contracts, cycles, orphans, cross-subsystem impact.
- **Deterministic** — same input, same bytes out. No tokens, no network, no flakiness.

---

## Why not another code graph?

Bounds exists in a crowded space of code intelligence tools (CodeGraph, tree-sitter-analyzer,
roam-code, CodeSage). Every prior approach shares the same assumption: **parse everything, build a
graph, let the AI figure it out.** This creates three problems Bounds solves:

### 1. Token bloat

A full code graph of a Django codebase can be 50K+ tokens. An agent pays this cost just to find
where `login()` is defined. Bounds gives you the answer in ~1,200 bytes of JSON — one CLI call.

### 2. No intent signal

A graph tells you what IS, not what SHOULD BE. Every symbol is equal. Bounds distinguishes public
contracts from private implementation by design — the developer declares the boundary, and Bounds
enforces it.

### 3. No drift detection

A graph is always correct by definition (it reflects reality). It cannot tell you that someone
added an export without declaring it, or removed an interface another subsystem depends on.
Bounds's validation is the difference between *declared intent* and *extracted reality*.

### In short

Code graphs are too large and noisy for an agent to consume cheaply, they capture what *is* without
any signal of human-declared intent or boundaries, and they drift from the code the moment something
changes with no way to flag it. Bounds targets exactly those three gaps: a tiny per-subsystem
contract instead of a full symbol graph, an explicit declared boundary instead of an undifferentiated
graph, and two-directional validation that catches drift between declared intent and extracted
reality. The two are complementary, not competitive — use a code graph to *explore* ("what's in this
codebase?") and Bounds to *validate* ("what should this architecture look like?"). An agent that
reads the Bounds map first then digs into specific symbols uses any graph tool more efficiently.

<sub>A full side-by-side comparison table is in the [appendix](#appendix-bounds-vs-code-graphs) at the
end of this README.</sub>

---

## Quick start

### Install

```bash
# Recommended: isolated CLI install
pipx install bounds

# Or into the current environment
pip install bounds
```

<details>
<summary><b>Install from source</b></summary>

```bash
pipx install "git+https://github.com/Farzin312/bounds.git"

# Or from a local clone
git clone https://github.com/Farzin312/bounds.git
cd bounds
pip install -e ".[dev]"
```
</details>

<details>
<summary><b>Homebrew (planned — v0.2.0)</b></summary>

```bash
brew install Farzin312/bounds/bounds   # once the tap is published
```

Until then, `pipx` is the cleanest cross-platform path.
</details>

```bash
bounds --help    # verify the install
```

### Initialize a project

```bash
cd your-project
bounds init --root                  # scaffold .bounds/root.yaml
bounds init --subsystem auth        # add .bounds/manifests/auth.yaml
# edit the manifest to declare paths, exposes, consumes...
```

### Explore and validate

```bash
bounds list                         # discover all subsystems           (JSON)
bounds describe auth                # one subsystem's full surface      (JSON)
bounds validate --quick             # fast incremental check            (JSON)
bounds validate --human             # same data, human-readable
bounds preflight                    # 6 pre-PR checks, blocking
bounds overview                     # project health dashboard
```

> `.bounds/` is hidden and **only** touched by the `bounds` CLI — nothing auto-loads it, so it
> never silently inflates an agent's context.

---

## Token Cost Comparison

The core claim: Bounds reduces the context an AI agent needs to understand your codebase from
thousands of tokens to hundreds of bytes.

### Real measured data

```bash
$ .venv/bin/bounds describe models | wc -c
1210 bytes

$ wc -c src/bounds/models.py
8475 bytes

$ wc -c .bounds/manifests/models.yaml
554 bytes
```

To understand the models subsystem's public API (9 exports, consumed by 5 subsystems), an AI
agent reads **1,210 bytes** of structured JSON instead of the full **8,475-byte** source file.
That is an **85.7% reduction** in context.

For the full architecture across all 8 subsystems:

```bash
$ .venv/bin/bounds list | wc -c
1916 bytes
```

**1,916 bytes** describes the complete architecture: 8 subsystems, their roles, criticality,
dependency graph, and interface counts. Without Bounds: grepping 18+ source files and mentally
reconstructing the architecture.

| Scenario | Without Bounds | With Bounds | Savings |
|----------|----------------|-------------|---------|
| Understand one subsystem | Read 1-5 source files (2K-15K tokens) | `bounds describe <name>` (~1,210 bytes) | ~85-99% |
| Map all subsystems | Grep for `class\|def\|export` across codebase | `bounds list` (~1,916 bytes) | Near-infinite |
| Detect architecture drift | Manual code review | `bounds validate` (structured report) | Subjective to deterministic |
| CI gate for boundary violations | No automated option exists | `bounds preflight` | Previously impossible |
| Dependency blast radius | Trace imports manually | `bounds describe` shows `consumed_by` | ~99% time reduction |

---

## Performance

Real wall-clock times (including Python interpreter startup) on M3 Pro, all measurements median of
3 runs:

| Command | Measured | Target | Status |
|---------|----------|--------|--------|
| `bounds validate --quick` | ~353ms | <200ms | Near target (startup overhead included) |
| `bounds validate` (full) | ~207ms | <500ms | Pass |
| `bounds list` | ~250ms | <20ms | Headroom for optimization |
| `bounds describe <name>` | ~307ms | <50ms | Headroom for optimization |

> Measurements include Python interpreter startup (~150ms). The actual validation logic completes
> in ~130-200ms. Run on a warm cache, `--quick` mode re-extracts zero files when nothing has
> changed — pure reference propagation and exit.

---

## How it works

### Three-tier data model

Only the top tier ever costs a token:

| Tier | Source | Cost | What it contains |
|------|--------|------|------------------|
| **Deterministic** | tree-sitter extraction | **Zero LLM** | Exported symbol names, file paths, import statements |
| **Declared** | Human-written YAML | **Zero LLM** | Descriptions, boundary definitions, contract metadata |
| **Semantic** | LLM, on demand (`--deep`, roadmap) | Tokens per use | Type signatures, intent summaries |

The core validation loop runs Tiers 1 + 2 only — never touches an LLM. Tier 3 is enrichment,
triggered when an agent needs deeper context.

### Validation engine

```
Source files ──tree-sitter──> Extracted exports  ──┐
                                                   ├──> Two-directional diff ──> Validation report
YAML manifests ──parse──────> Declared exports  ───┘
                                    +
                              Consumed interfaces
```

The engine checks both directions:
- **Stale manifest**: the manifest claims an export the source doesn't provide.
- **Incomplete manifest**: source exports something the manifest doesn't declare.
- **Cross-subsystem drift**: consumer declares an interface the provider no longer exports.

### Quick mode (incremental)

1. `git diff HEAD~1 --name-only` — find changed files.
2. Re-extract tree-sitter only for changed files.
3. Compare extracted exports against declared per subsystem.
4. **Reference propagation**: for each changed subsystem, check every consumer's declared interfaces
   against current exports — interface-name comparison, zero tree-sitter.

---

## Language support

| Language | Extraction | Describe Merge | Validate | Status |
|----------|-----------|---------------|----------|--------|
| **Python** | Full (functions, classes, decorators) | Yes | Yes | Implemented |
| **TypeScript / JavaScript** | Full (exports, classes, interfaces) | Yes | Yes | Implemented |
| **Go** | Functions, methods, exported symbols | Planned | Planned | v0.2.0 target |
| **Rust** | `pub fn`, `pub struct`, `pub enum`, traits | Planned | Planned | v0.2.0 target |
| **Java** | Classes, interfaces, public methods | Planned | Planned | v0.3.0 target |
| **Fallback** | YAML-only metadata, no tree-sitter | No merge | Data integrity only | Available always |

---

## For AI coding agents

Bounds is a plain CLI that emits JSON, so **any agent that can run a shell command can use it
today**. The universal instruction is the same:

> Prefer `bounds describe <name>` / `bounds list` over reading raw source to understand
> architecture. Output is JSON by default — parse it. Run `bounds validate --quick` after edits
> and treat a non-`fresh` `validation_status` as a signal to update the manifests.

> **Claude Code plugin auto-detection.** Claude Code (and compatible agents) can auto-detect a
> project's `.bounds/` directory and use the `bounds` CLI to load subsystem manifests on demand —
> no manual wiring needed. When the directory is present, the agent reads boundary contracts
> instead of raw source automatically.

### Integration by tool

| Tool | Integration Method | Configuration File | Status |
|------|-------------------|-------------------|--------|
| **Claude Code** | Bash tool + project instructions | `CLAUDE.md` + `.claude/settings.json` | Verified |
| **OpenCode** | Custom tool or slash command | `.opencode/tool/bounds.ts` or `AGENTS.md` | Verified |
| **Codex CLI** | Direct shell commands | `AGENTS.md` | Verified |
| **Cursor** | Project rule (always-on) | `.cursor/rules/bounds.mdc` | Verified |
| **Windsurf** | Workspace rule | `.windsurf/rules/bounds.md` | Verified |
| **Generic agents** | Standing instructions | `AGENTS.md` (cross-tool standard) | Verified |

### Drop-in instruction templates

Ready-to-copy agent-instruction templates live in [`templates/`](templates/):

| File | For | How to use |
|------|-----|------------|
| [`templates/AGENTS.md`](templates/AGENTS.md) | Codex CLI, OpenCode, generic agents | Copy the block into your project's `AGENTS.md` |
| [`templates/CLAUDE.md`](templates/CLAUDE.md) | Claude Code | Append the section to your project's `CLAUDE.md` |
| [`templates/.cursorrules`](templates/.cursorrules) | Cursor | Copy to `.cursorrules`, or adapt into `.cursor/rules/bounds.mdc` |

Each template tells the agent to query `bounds describe` / `bounds list` instead of
reading raw source, and to run `bounds validate --quick` after edits. The instructions are
identical in substance — only the file format differs per tool.

Full integration guides with example configuration files are in
[ARCHITECTURE.md](ARCHITECTURE.md). A native MCP server (`bounds mcp`) is on the
[roadmap](ROADMAP.md) for v0.3.

---

## Security

Bounds is designed with 7 security principles that are enforced from day one:

| # | Principle | Detail |
|---|-----------|--------|
| 1 | **No code execution at install** | Pure Python wheels only — no setup.py scripts, no post-install hooks |
| 2 | **No network at runtime** | Zero telemetry, analytics, API calls, or phone-home. No opt-out toggle needed |
| 3 | **No credential handling** | Never asks for, stores, or transmits API keys, tokens, or secrets |
| 4 | **No eval/exec** | tree-sitter for parsing (safe C bindings), PyYAML for manifests |
| 5 | **Hidden directory safety** | Writes only within `.bounds/` — never outside the project root |
| 6 | **Dependency minimums** | Minimum versions specified, not pinned — users get latest compatible deps |
| 7 | **Signed releases (future)** | sigstore/cosign attestations planned for v0.2.0 |

For the full disclosure policy and distribution integrity details, see [SECURITY.md](SECURITY.md).

---

## Command reference

| Command | What it returns |
|---------|-----------------|
| `bounds init --root` | Scaffolds `.bounds/root.yaml` with project defaults |
| `bounds init --subsystem <name>` | Scaffolds `.bounds/manifests/<name>.yaml`. `--namespace <ns>` tags it |
| `bounds list` | All subsystems with role, criticality, exposes, consumes, consumed_by. `--namespace <ns>` filters |
| `bounds describe <name>` | One subsystem's full manifest as JSON. `--namespace <ns>` describes every subsystem in a group instead |
| `bounds validate` | Full validation — all 6 checks. `--quick`, `--mode <m>`, `--enforce on\|off` |
| `bounds preflight` | 6 pre-PR checks in blocking mode |
| `bounds overview` | Project dashboard: subsystem health, file counts, language breakdown |

`validate` and `preflight` also take file-selection and output toggles (all default off):

| Flag | Effect |
|------|--------|
| `--include-ignored` | Scan files normally excluded by `.boundsignore` |
| `--include-gitignored` | Scan files excluded by `.gitignore` |
| `--follow-symlinks` | Follow external symlinks instead of skipping them with a warning |
| `--fail-on-unowned` | Treat tracked source files outside every subsystem as a blocking error |
| `--ci` | CI plaintext output: one tab-delimited issue per line, for log grepping |

Every command prints **JSON to stdout by default** and accepts `--human` for readable terminal
output. Fatal errors print `{"error": {"code", "message", "fix"}}` and exit 2; blocking failures
exit 1. Error codes are stable — see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Install channels

| Channel | Command | Status |
|---------|---------|--------|
| **pip** | `pip install bounds` | Available |
| **pipx** | `pipx install bounds` | Available |
| **Clone + pip** | `pip install git+https://github.com/Farzin312/bounds.git` | Available |
| **curl** | `curl -sSL https://bounds.dev/install.sh \| bash` | Planned (v0.2.0) |
| **Homebrew** | `brew install bounds` | Planned (v0.2.0) |
| **conda-forge** | `conda install bounds` | Planned (v0.2.0) |
| **Docker** | `docker pull bounds/bounds` | Planned (v0.2.0) |

---

## Cross-platform support

Fully supported on **Linux, macOS, and Windows**, Python **3.10--3.14**. Internally Bounds uses
`pathlib` everywhere and stores POSIX-normalized relative paths, so manifests are identical across
operating systems.

| Platform | Notes |
|----------|-------|
| **Linux** | glibc (`manylinux2014` x86_64/aarch64) and musl/Alpine (`musllinux` x86_64) wheels |
| **macOS** | Apple Silicon (arm64) and Intel (x86_64) wheels — no Xcode required |
| **Windows** | `win_amd64`/`win_arm64` wheels — no Visual C++ Build Tools needed. `--quick` needs Git for Windows on PATH |

Prebuilt tree-sitter grammars ship for every platform — installs never require a C compiler.

---

## Project layout

| File | Purpose |
|------|---------|
| [README.md](README.md) | This file — product pitch, quickstart, agent integration |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Engineering contract: modules, data model, checks, error codes, JSON shapes |
| [ROADMAP.md](ROADMAP.md) | What is in MVP v0.1 vs what is coming (v0.2, v0.3) |
| [CHANGELOG.md](CHANGELOG.md) | Version history and release notes |
| [SECURITY.md](SECURITY.md) | Security principles, vulnerability disclosure, distribution integrity |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, coding standards, testing, PR workflow |
| [CLAUDE.md](CLAUDE.md) | Project memory for agents and contributors working *on* Bounds |
| [templates/](templates/) | Drop-in agent-instruction templates (AGENTS.md, CLAUDE.md, .cursorrules) |
| [benchmarks/](benchmarks/v0.1.0/README.md) | Raw benchmark data, token cost analysis, performance measurements |

---

## Roadmap

| Version | Focus | Key additions |
|---------|-------|--------------|
| **v0.1** (current) | Core engine, zero-LLM validation | Manifests, tree-sitter extraction, 6 checks, cache, quick mode, agent integration |
| **v0.2** | Semantic tier + ergonomics | LLM enrichment (`--deep`), Go/Rust adapters, `bounds migrate`, configurable ignores |
| **v0.3** | Distribution + ecosystem | MCP server, GitHub Action, Homebrew formula, standalone binaries, `bounds watch` |

Full details in [ROADMAP.md](ROADMAP.md).

---

## Contributing

Bounds is MIT-licensed and built to be extended. Adding a language adapter is one class
(`extract.base.LanguageAdapter`) plus a registry entry. See [CONTRIBUTING.md](CONTRIBUTING.md)
and [CLAUDE.md](CLAUDE.md) for development setup, coding standards, testing guide, and PR workflow.
Issues and PRs welcome at [github.com/Farzin312/bounds](https://github.com/Farzin312/bounds).

## Appendix: Bounds vs code graphs

<details>
<summary><b>Full side-by-side comparison</b></summary>

| Dimension | CodeGraph / TSA / roam-code | Bounds |
|-----------|---------------------------|---------|
| Core approach | Extract graph from source | Declare intent in YAML, validate against source |
| What you get | What code exists | What code SHOULD exist |
| Granularity | Functions, classes, imports | Subsystem boundaries, contracts, dependencies |
| Token cost | O(full symbol count) | O(public API count) — 5-20 lines per subsystem |
| Validation | None (graph = truth) | Drift detection, contract compliance, boundary checks |
| LLM dependency | High (embeddings, semantic search) | Zero for structural path |
| CI integration | Analysis step | Gate step — can block PRs |
| Setup time | Server config, embedding indexing | `pip install bounds` + `bounds init` |

**Complementary, not competitive.** Bounds does not compete with CodeGraph — these are tools at
different layers. CodeGraph/TSA/roam-code answer "What's in this codebase?" (detailed, expensive,
exploratory); Bounds answers "What SHOULD this codebase's architecture look like?" (concise,
deterministic, enforceable). The workflow: use CodeGraph to explore, use Bounds to validate. An AI
agent that knows the boundaries (via Bounds) uses CodeGraph more efficiently — it reads the map
first, then digs into specific symbols rather than searching blindly.

</details>

---

## License

[MIT](LICENSE) (c) Farzin Shifat
