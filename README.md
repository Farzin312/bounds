<div align="center">

# Compact

### Give your AI agent a map of the codebase — not the whole city.

**Compact** is a CLI that turns a codebase's architecture into deterministic, machine-readable
manifests — using **tree-sitter (zero LLM)** for all structural validation. AI coding agents read a 10-line YAML boundary instead of 10 files — and get a structural
**validation** they can actually trust, in milliseconds, for zero tokens.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey.svg)](#cross-platform-support)
[![Zero LLM](https://img.shields.io/badge/structural%20validation-zero%20LLM-brightgreen.svg)](#how-it-works)

[Quick start](#quick-start) · [Agent integration](#agent--editor-integration) · [Commands](#command-reference) · [Architecture](ARCHITECTURE.md) · [Roadmap](ROADMAP.md)

</div>

---

## The problem

When an AI agent opens your repo, it does the expensive thing: it reads source files — sometimes dozens —
to reconstruct what each part does and how the parts connect. Every file is tokens. Every wrong guess is a
bad edit. And the agent's mental model is **stale the moment it's built**, with nothing to check it against.

But an agent doesn't need every line of `auth.ts`. It needs:

> *"`auth` exposes `login`, `verify`, `register`, and talks to the database via `user_repository`."*

That fits in five lines of YAML — and a machine can reason about it **deterministically**.

## What Compact does

Compact maintains a hidden `.compact/` directory of **subsystem boundary manifests**: tiny YAML files that
declare each subsystem's role, its public interfaces, and its cross-boundary dependencies. It then uses
**tree-sitter** (never an LLM) to extract the *actual* exported symbols and imports from your source and
**validates the manifests against reality** — in both directions.

The result is a structural contract an agent can query and a CI pipeline can enforce:

- 🗺️ **`describe`** — hand an agent a subsystem's exact public surface as JSON, instead of raw files.
- ✅ **`validate`** — catch drift the moment exports stop matching the manifest. 6 checks, zero LLM (tree-sitter, deterministic).
- ⚡ **`--quick`** — git-diff incremental validation in **sub-200 ms**, safe for every commit.
- 🔒 **Deterministic & free** — same input, same bytes out. No tokens, no network, no flakiness.
- 🤖 **Agent-native** — JSON by default; `--human` when a person is reading.

```text
                 reads 12 files, burns 40k tokens, guesses          asks compact, reads 1 manifest, 0 tokens
   Agent ────────────────────────────────────────────►  ❌    vs.   Agent ──────────────────────────────►  ✅
                                                                       compact describe auth   (JSON, 10 lines)
```

## How it works

Three tiers, only the top one ever costs a token:

| Tier | Source | Cost | Contents |
|------|--------|------|----------|
| **Deterministic** | tree-sitter extraction | **zero** | exported symbols, imports, file paths |
| **Declared** | human-written YAML | **zero** | descriptions, boundaries, contract metadata |
| **Semantic** | LLM, on demand (roadmap) | tokens/use | type signatures, intent summaries |

A subsystem manifest looks like this (`.compact/manifests/auth.yaml`):

```yaml
name: auth
role: service
criticality: core
description: Login, registration, and token verification.
paths: [src/auth]
exposes:
  - { name: login,    kind: function }
  - { name: verify,   kind: function }
  - { name: register, kind: function }
consumes:
  - { subsystem: database, via: user_repository, interfaces: [find_user, create_user] }
```

`compact validate` extracts the real exports of `src/auth/**` with tree-sitter and checks them against
`exposes`; it confirms `database` actually exposes `find_user`/`create_user`; it flags boundary crossings,
cycles, and orphans. All deterministic. See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full engine.

---

## Quick start

### Install

Compact is a standalone CLI, so **[pipx](https://pipx.pypa.io)** (isolated venv + on your PATH) is the
recommended install. Every dependency — including the tree-sitter grammars — ships as a prebuilt wheel, so
**no C compiler is required** on Linux, macOS, or Windows.

```bash
# Recommended: isolated CLI install
pipx install compact

# Or into the current environment
pip install compact
```

<details>
<summary><b>Install from source (works today, before the first PyPI release)</b></summary>

```bash
# pipx, straight from GitHub
pipx install "git+https://github.com/Farzin312/compact.git"

# or pip from a clone
git clone https://github.com/Farzin312/compact.git
cd compact
pip install .          # add -e for an editable dev install, or .[dev] for tests
```
</details>

<details>
<summary><b>Homebrew (planned — self-hosted tap)</b></summary>

A Homebrew tap is on the [roadmap](ROADMAP.md):

```bash
brew install Farzin312/compact/compact   # once the tap is published
```
Until then, `pipx` is the cleanest cross-platform path.
</details>

Verify the install:

```bash
compact --help
```

### Initialize and explore

```bash
cd your-project
compact init --root                  # scaffold .compact/root.yaml
compact init --subsystem auth        # add .compact/manifests/auth.yaml
# ...edit the manifest to declare paths / exposes / consumes...

compact list                         # discover subsystems        (JSON)
compact describe auth                # one subsystem's surface     (JSON)
compact validate --quick             # fast incremental check      (JSON)
compact validate --human             # same data, human-readable
compact preflight                    # the 6 pre-PR checks, blocking
compact overview                     # project health dashboard
```

> `.compact/` is hidden and **only** touched by the `compact` CLI — nothing auto-loads it, so it never
> silently inflates an agent's context.

## Command reference

| Command | What it returns |
|---------|-----------------|
| `compact init --root` | Scaffolds `.compact/root.yaml`. |
| `compact init --subsystem <name>` | Scaffolds `.compact/manifests/<name>.yaml`. |
| `compact list` | All subsystems with role, criticality, and interface counts. |
| `compact describe <name>` | One subsystem's full manifest as JSON (`--deep` reserves Tier-3 LLM enrichment — roadmap). |
| `compact validate` | Full validation, all 6 checks. `--quick` (git-diff incremental), `--mode <m>`, `--enforce on\|off`, `--base <ref>`. |
| `compact preflight` | The 6 checks in blocking mode — the pre-PR gate. |
| `compact overview` | Roles, criticality spread, dependency edges, cycles, health summary. |

Every command prints **JSON to stdout by default** (parse it directly) and accepts `-H`/`--human` for a
readable rendering of the same data. Fatal errors print `{"error": {"code", "message", "fix"}}` and exit `2`;
blocking validation failures exit `1`. Error codes are stable — see [ARCHITECTURE.md §8](ARCHITECTURE.md).

---

## Agent & editor integration

Compact is a plain CLI that emits JSON, so **any agent that can run a shell command can use it today** — no
plugin protocol required. Below is the verified, current way to wire it into each major tool. A native **MCP
server** (`compact mcp`) is on the [roadmap](ROADMAP.md); where MCP is mentioned, it's the *future* path, and
the working integration today is the rules/memory file shown.

The universal instruction you're giving every agent is the same:

> *Prefer `compact describe <name>` / `compact list` over reading raw source to understand architecture.
> Output is JSON by default — parse it; use `--human` only for people. Run `compact validate --quick` after
> edits and treat a non-`fresh` `validation_status` as a signal to update the manifests.*

### Claude Code

Reference Compact in your project **`CLAUDE.md`** so the agent runs it via the Bash tool, and pre-approve it
in `.claude/settings.json`:

```markdown
<!-- CLAUDE.md -->
## Tooling: Compact
This repo uses `compact` (CLI) for codebase structure — prefer it over reading raw source.
- `compact list` — discover subsystems        - `compact describe <name>` — one subsystem (JSON)
- `compact validate --quick` — pre-commit check - `compact preflight` — pre-PR gate
Output is JSON by default (parse it); pass `--human` only for readable terminal output.
```

```jsonc
// .claude/settings.json — skip approval prompts for compact
{ "permissions": { "allow": ["Bash(compact:*)"] } }
```

For a reusable procedure, add an Agent Skill at `.claude/skills/compact/SKILL.md` (it can even inline live
output with the `` !`compact overview` `` bash-injection syntax). *(MCP via `claude mcp add` is the roadmap path.)*

### OpenCode

Best option — a custom tool the model can call directly (`.opencode/tool/compact.ts`):

```typescript
import { tool } from "@opencode-ai/plugin"

export default tool({
  description: "Get a subsystem's boundary manifest from Compact (JSON)",
  args: { subsystem: tool.schema.string().describe("Subsystem name, e.g. 'auth'") },
  async execute(args) {
    return await Bun.$`compact describe ${args.subsystem}`.text()  // JSON by default
  },
})
```

Lighter option — a slash command at `.opencode/command/compact.md`, or just add the usage note to
**`AGENTS.md`** (OpenCode reads it). *(Once Compact ships its MCP server, add it under the `mcp` key in
`opencode.json`.)*

### Codex CLI

Codex runs shell commands directly, so document Compact in **`AGENTS.md`** (project root, or global
`~/.codex/AGENTS.md` — keep it under the 32 KiB limit):

```markdown
<!-- AGENTS.md -->
## Codebase understanding: use the `compact` CLI
Before reading raw source, run `compact` (emits JSON; add `--human` only for readable output):
- `compact list` · `compact describe <name>` · `compact validate --quick` · `compact preflight`
Parse the JSON rather than opening every file.
```

Ensure `compact` is on `PATH`; Codex's default sandbox already permits in-workspace commands.

### Cursor

Compact is a JSON CLI, **not** an MCP stdio server — so it belongs in a **project rule**, not
`.cursor/mcp.json`. Create `.cursor/rules/compact.mdc`:

```markdown
---
description: Use the Compact CLI to understand subsystem architecture before reading raw source
alwaysApply: true
---
This repo has a `compact` CLI that emits structured JSON about subsystem boundaries (zero-LLM, tree-sitter).
- Discover: `compact list`   - Inspect: `compact describe <name>`
- Validate: `compact validate --quick`   - Pre-PR: `compact preflight`
Output is JSON by default; append `--human` only for readable text. Treat compact output as the source of
truth for cross-boundary contracts.
```

A root `AGENTS.md` works as a plain-markdown alternative. *(The legacy `.cursorrules` file also works but is deprecated.)*

### Windsurf

Add a workspace rule at `.windsurf/rules/compact.md` (max 12,000 chars) with an always-on trigger:

```markdown
---
trigger: always_on
description: Use the Compact CLI to understand subsystem boundaries before reading source.
---
- This repo has a `compact` CLI that emits JSON describing subsystem boundaries.
- Run `compact list` and `compact describe <subsystem>` and parse the JSON (default output).
- Don't pass `--human` when consuming output programmatically — that flag is for people.
- Run `compact validate --quick` after edits; a non-`fresh` `validation_status` means update the manifests.
```

*(When Compact ships its MCP server, register it in `~/.codeium/windsurf/mcp_config.json` under `mcpServers`.)*

### Any other agent (generic)

If your agent can run a shell command, it can use Compact:

1. Install Compact and make sure `compact` is on `PATH` (`pipx install compact`).
2. Add a line to whatever standing-instructions file your agent reads (`AGENTS.md` is the emerging
   cross-tool standard, honored by OpenCode, Codex, Cursor, and Windsurf):
   > *"To understand architecture, run `compact describe <name>` and parse its JSON instead of reading source."*
3. Have it call `compact validate --quick` after edits and react to `validation_status`.

JSON-first output means no scraping — the agent consumes structured data directly.

---

## Cross-platform support

Fully supported on **Linux, macOS, and Windows**, Python **3.10–3.14**. Internally Compact uses `pathlib`
everywhere and stores POSIX-normalized relative paths, so manifests are identical across operating systems.

| Platform | Notes |
|----------|-------|
| **Linux** | glibc (`manylinux2014` x86_64/aarch64) and musl/Alpine (`musllinux` x86_64) wheels. `pipx` via `apt`/`dnf`/`pacman` or `pip install --user pipx`. |
| **macOS** | Apple Silicon (arm64) and Intel (x86_64) wheels — no Xcode needed. `brew install pipx` → `pipx install compact`. |
| **Windows** | `win_amd64`/`win_arm64` wheels — no Visual C++ Build Tools needed. Prefer the python.org installer (for the `py` launcher: `py -m pip install compact`), or use `pipx` so PATH is handled. `--quick` needs **Git for Windows** on PATH. |

> Tip: run `pipx ensurepath` once so the `compact` executable resolves in new shells. The tree-sitter
> grammars are prebuilt wheels on every platform above, so installs never require a compiler.

## CI/CD integration

Compact's exit codes make it a drop-in gate: `0` = clean, `1` = blocking failure, `2` = fatal/usage.

**GitHub Actions** — prove the prebuilt-wheel story with a matrix, then gate on `--quick`:

```yaml
# .github/workflows/compact.yml
jobs:
  structure:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix: { os: [ubuntu-latest, macos-latest, windows-latest], python: ['3.10', '3.12', '3.14'] }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '${{ matrix.python }}', cache: 'pip' }
      - run: pip install compact          # or: pip install .
      - run: compact validate --quick     # non-zero exit fails the job
```

**GitLab CI**:

```yaml
compact:
  image: python:3.12
  script:
    - pip install .
    - compact validate            # job fails on non-zero exit
```

**pre-commit** (`.pre-commit-config.yaml`) — `pass_filenames: false` because Compact scans the repo itself:

```yaml
repos:
  - repo: local
    hooks:
      - id: compact-validate
        name: compact validate
        entry: compact validate --quick
        language: system          # compact must be installed; or use language: python + additional_dependencies: ["compact"]
        pass_filenames: false
```

---

## Project layout

| File | Purpose |
|------|---------|
| **[README.md](README.md)** | This file — product pitch, install, integration. |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | The engineering contract: modules, data model, checks, error codes, JSON shapes. |
| **[ROADMAP.md](ROADMAP.md)** | What's in MVP v0.1 vs. what's coming. |
| **[CLAUDE.md](CLAUDE.md)** | Project memory / conventions for contributors and agents working *on* Compact. |

## Contributing

Compact is MIT-licensed and built to be extended. Adding a language is one class
(`extract.base.LanguageAdapter`) plus a registry entry — see [CLAUDE.md](CLAUDE.md) and
[ARCHITECTURE.md §4](ARCHITECTURE.md). Issues and PRs welcome at
[github.com/Farzin312/compact](https://github.com/Farzin312/compact).

## License

[MIT](LICENSE) © Farzin Shifat
