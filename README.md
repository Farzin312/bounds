<div align="center">

# Bounds

### A verified, CI-enforced contract of what your architecture is *supposed* to be

<img src="assets/demo.svg" alt="Bounds in four commands: discover manifests, describe a subsystem in ~400 tokens, see a change's blast radius, validate for drift" width="760">

**Bounds** turns each subsystem's intended boundary — its public surface and its cross-module
dependencies — into a tiny YAML contract, then uses **tree-sitter (zero LLM)** to verify that
contract against your real source and **fail the build the moment they diverge**.

> *A code graph tells your agent what the code **is**; Bounds tells it what the code is **supposed
> to be** — and fails the build when those diverge. Use a graph to explore, use Bounds to enforce.*

[![CI](https://github.com/Farzin312/bounds/actions/workflows/ci.yml/badge.svg)](https://github.com/Farzin312/bounds/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey.svg)](docs/languages-and-platforms.md)
[![Zero LLM](https://img.shields.io/badge/structural%20validation-zero%20LLM-brightgreen.svg)](docs/how-it-works.md)
[![Clones](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/Farzin312/bounds/traffic-data/clones-badge.json)](https://github.com/Farzin312/bounds/graphs/traffic)

[Quick start](#quick-start) · [Why use it](docs/why-bounds.md) · [How it works](docs/how-it-works.md) · [Token economics](docs/token-economics.md) · [CLI reference](docs/cli-reference.md) · [For AI agents](docs/ai-agents.md) · [Docs](docs/README.md)

</div>

---

> **Who it's for.** A 3–8 person team running multiple coding agents in parallel on a mature
> (50k+ LOC) Python/TypeScript service that's already been burned by agent-driven architectural
> drift. Agents made drift *fast* — no single reviewer can hold the whole shape in their head
> anymore. Bounds gives that architecture a verified contract and a CI gate that fails when the
> code drifts from it.

## The problem

When an AI agent opens your repo, it does the expensive thing: it reads source files — sometimes
dozens — to reconstruct what each part does and how the parts connect. Every file burns tokens, every
wrong guess produces a bad edit, and the agent's mental model has no way to *check* itself when its
picture of the architecture has drifted from the code.

But an agent doesn't need every function in `auth.ts`. It needs *"auth exposes `login`, `verify`,
`register` and talks to the database via `user_repository`."* That fits in five lines of YAML — and a
machine can reason about it **deterministically**.

<div align="center">
<img src="assets/before-after.svg" alt="Without Bounds an agent reads 5-15 source files for thousands of unverified tokens; with Bounds a single describe call returns a tree-sitter-verified contract in about 400 tokens" width="760">
</div>

## What Bounds does

Bounds maintains a hidden `.bounds/` directory of tiny YAML **subsystem manifests** and uses
tree-sitter to validate them against your real source, in both directions.

- **`bounds describe`** — hand an agent a subsystem's exact public surface as JSON, each interface flagged `verified: true/false`.
- **`bounds validate`** — catch drift the moment exports stop matching the manifest. 6 checks, zero LLM.
- **`bounds validate --quick`** — git-diff incremental validation, safe for every commit.
- **`bounds preflight`** — pre-PR checks: drift, boundaries, contracts, cycles, orphans, impact.
- **`bounds impact <name>`** — transitive blast radius: who breaks if this subsystem's surface changes.
- **`bounds discover` / `bounds calibrate`** — onboard an un-bounded repo in one command, then keep manifests honest against tree-sitter reality.
- **`bounds agent --sync`** — wire Bounds into eight coding agents (Claude Code, Codex, Cursor, …) with one command.
- **Deterministic** — same input, same byte-stable output. No network, no flakiness.

## Why use it

- **Catch architecture drift in CI before it merges** — boundary violations and stale contracts become a failing check with a fix suggestion, not a convention nobody follows.
- **Give agents a ~400-token verified contract instead of source** — one cheap CLI call returns a tree-sitter-confirmed public surface, not a dozen files an agent has to read and guess at.
- **Show blast radius before a risky change** — `bounds impact` returns the transitive consumer set and the interfaces each one relies on, so you know the reach before you write the edit.

See [docs/why-bounds.md](docs/why-bounds.md) for the full rationale.

---

## Quick start

```bash
# Install (works today — PyPI/Homebrew pending publish)
pipx install "git+https://github.com/Farzin312/bounds.git"

cd your-project
bounds discover --apply      # auto-generate root.yaml + manifests from your source
bounds describe auth         # one subsystem's verified surface, as JSON
bounds validate --quick      # fast incremental drift check
```

`bounds discover` groups source by directory, tree-sitter-extracts each subsystem's `exposes`, infers
`consumes` from the import graph, and never overwrites existing manifests. See
[docs/install.md](docs/install.md) for all install channels.

> `.bounds/` is hidden and **only** touched by the `bounds` CLI. Its extraction cache is a binary
> SQLite file (`.bounds/cache.db`, **gitignored and regenerated** — never committed) so a tool that
> blindly dumps a directory gets binary bytes, not a parseable token blob. This is an
> *accidental-context-burn* defense, **not** access control — the manifests are plain YAML any agent
> can read.

---

## Scales as your codebase grows

Reading source is **O(files)** — the bigger the codebase, the bigger the read. A Bounds contract is
**O(public API)** — a subsystem with 50 internal functions and 5 exports stays roughly flat.

<div align="center">
<img src="assets/token-scaling.svg" alt="Line chart: reading source climbs steeply as O of files toward tens of thousands of tokens, while a Bounds describe contract stays flat near 400 tokens as O of public API" width="700">
</div>

The token win *widens* with size. See [docs/token-economics.md](docs/token-economics.md) for the
measured numbers (one repo, one data point), the scaling argument, and the context-rot effect.

## Languages & platforms

**Python + TypeScript/JavaScript** are tree-sitter-verified today; runs on **Linux, macOS, and
Windows** (Python 3.10–3.14). Go, Rust, and Java adapters are on the roadmap. See
[docs/languages-and-platforms.md](docs/languages-and-platforms.md).

---

## Documentation

**Start here**
- [why-bounds.md](docs/why-bounds.md) — the rationale: drift control, token-lean retrieval, blast radius.
- [team-workflow.md](docs/team-workflow.md) — how a team adopts Bounds day to day.
- [use-cases.md](docs/use-cases.md) — concrete workflows: pre-PR safety, onboarding, CI enforcement.

**Reference**
- [cli-reference.md](docs/cli-reference.md) — every command and flag.
- [ai-agents.md](docs/ai-agents.md) — `agent --sync`, the canonical contract, advisory compliance.
- [languages-and-platforms.md](docs/languages-and-platforms.md) — language support matrix and cross-platform notes.
- [install.md](docs/install.md) — all install channels and their current status.

**Deep dives**
- [how-it-works.md](docs/how-it-works.md) — three-tier model, validation engine, quick mode, the binary cache.
- [token-economics.md](docs/token-economics.md) — measured token costs, scaling tables, context rot.
- [comparison.md](docs/comparison.md) — Bounds vs. code graphs, and what Bounds deliberately does *not* do.

Full docs index: [docs/README.md](docs/README.md). Engineering contract: [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Contributing & license

Bounds is **MIT-licensed** (c) Farzin Shifat and built to be extended — adding a language adapter is
one class plus a registry entry. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, coding standards,
and PR workflow; [ARCHITECTURE.md](ARCHITECTURE.md) for the engineering contract;
[SECURITY.md](SECURITY.md) for security principles and disclosure; and
[CHANGELOG.md](CHANGELOG.md) for release notes.
