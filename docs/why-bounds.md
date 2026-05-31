# Why Bounds

*The honest value model: drift control for agent-heavy development, with token savings as a real but secondary benefit.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

## The real problem Bounds solves: drift

When AI agents do most of the editing, the danger is rarely one catastrophic mistake. It's the slow accumulation of locally-correct changes that are globally wrong. Each edit looks fine on its own — a new export here, a quietly-added cross-module import there — but the architecture you designed erodes one reasonable-looking diff at a time. Nobody decided to couple `billing` to `auth`'s internals; it just happened across fifteen PRs, and by the time a human notices, the boundary is gone.

Bounds exists to catch that erosion **before it merges**. You declare each subsystem's boundary once, in a few lines of YAML, and Bounds validates the code against that declaration on every commit — in both directions. A boundary violation becomes a failing CI check with a deterministic fix suggestion, not a surprise you discover during a future refactor.

That is the defensible value: **drift control**. Token savings are real and worth having (see below), but they are the secondary benefit. The primary one is keeping a multi-agent codebase from quietly losing its shape.

## How it concretely helps an AI agent

Bounds is a plain CLI that emits JSON, so any agent that can run a shell command can use it. Four concrete wins:

| Win | Command | Why it matters |
|-----|---------|----------------|
| A cheap, verified map | `bounds describe <name>` | The agent reads a compact subsystem contract (a few hundred tokens for a small subsystem; cost scales with its exposed API) instead of opening a dozen source files (thousands of tokens) to reconstruct what a part does and what it touches. |
| A trust signal | every `describe` expose carries `verified: true/false` | `verified: true` means tree-sitter confirmed the symbol actually exists in source — the agent can rely on the manifest without re-reading the file. |
| Blast radius **before** editing | `bounds impact <name>` | The agent sees the transitive consumer set and the exact interfaces each consumer relies on *before* writing the change — so it doesn't blindly break `api/` and `frontend/`. This is the biggest single agent win: fewer blind breakages. |
| A deterministic post-edit self-check | `bounds validate --quick` | After editing, the agent runs a fast, zero-LLM structural check and reads `validation_status` to know whether its change drifted from the declared boundary. |

The pattern: pull a verified slice of architecture into context with one CLI call, reason about reach before acting, then verify structure after acting — all deterministically, all without burning tokens on raw source.

## Token savings (the secondary benefit)

An agent's only real cost is tokens into context. A Bounds contract is `O(symbols exposed)` — a subsystem with 50 internal functions and 5 exports is still ~5 expose entries, so the contract stays flat as a subsystem's *internals* grow (it scales with how many symbols it *exposes*, not its line count) — while reading source is `O(files)` and grows with the codebase. So the token win *widens* as a subsystem's implementation grows behind a stable public API.

The numbers in the README are **measured on this repo only** — a single data point, estimated at roughly 4 chars/token, not a cross-repo study. Treat them as illustrative, not a guaranteed average. As one example from this repo, `bounds describe models` returned the verified public surface in ~400 tokens versus ~2,900 tokens for the full source file. The shape of the win is reliable; the exact ratio depends on your code.

## Who it's for, and when it's worth it

Bounds pays off when:

- you have **many subsystems** with real boundaries worth protecting,
- **multiple agents (or people)** are editing concurrently, and
- you operate with **large context budgets** where token-lean retrieval and self-checking matter.

It is **not** worth the overhead when:

- the repo is **tiny** — a few files an agent can just read,
- nobody will **keep the manifests fresh** — a neglected manifest is worse than none, because a stale contract that still validates can mislead.

If you can't commit to the freshness loop (see [./team-workflow.md](./team-workflow.md)), Bounds is not for you yet.

## What Bounds does NOT do

Being explicit about the edges is part of why you can trust the parts that work.

| Bounds does **not** | What that means |
|---------------------|-----------------|
| Execute your code | Pure static tree-sitter parsing — no runtime, no behavior inferred from execution. |
| Produce semantics without `--deep` | The Tier-3 LLM enrichment tier is **stubbed** in the MVP. The structural path is intentionally LLM-free; `--deep` is opt-in and not yet implemented. |
| Infer intent on its own | It validates **human-declared** manifests; it does not invent what a subsystem *should* be. You declare the boundary. |
| Replace exploratory code graphs | It **complements** them — use a code graph to explore "what's in this codebase?", use Bounds to validate "what should the architecture look like?". |
| Guarantee agent reasoning or task success | It cuts token load and validates structure. It makes **no** claim that an agent will reason well or finish the task. |
| Auto-update your manifests | Drift is detected and proposed; you apply fixes with `bounds calibrate --apply`. Nothing rewrites a manifest behind your back. |
| Validate every language | **Python and TypeScript/JS** are tree-sitter-verified today. Go/Rust/Java are roadmap. Unsupported files fall back to YAML-only metadata, and *only* for files a manifest names directly — auto-discovered files in an unsupported language are skipped, not validated. |

A note on the cache: `.bounds/cache.db` is a binary SQLite file so a tool that blindly `cat`s a directory gets binary bytes instead of a giant parseable token blob. That is an **accidental-context-burn defense, not access control** — the manifests in `.bounds/manifests/*.yaml` are plain YAML any agent can still read directly. Only the *derived* extraction cache is binary.

And agent compliance is **advisory**: Bounds writes the right instructions into the configs agents already read, but it cannot force an agent to follow them. The one hard enforcement point is **CI**, which runs in your pipeline, not in the agent.

## Current limitations, and how the roadmap addresses them

| Limitation today | Roadmap response |
|------------------|------------------|
| No semantic tier — `--deep` is stubbed | **v0.2**: live LLM enrichment for type signatures and intent summaries (opt-in, never in the structural path). |
| Only Python + TS/JS are verified | **v0.2**: Go and Rust adapters. **v0.3**: Java. |
| No native agent integration beyond CLI + generated configs | **v0.3**: an MCP server (`bounds mcp`) so agents query Bounds through a first-class protocol. |
| Agent compliance is advisory; the loop isn't closed | Closing the agent loop — tighter hook/CI integration and `bounds watch` — so the right behavior is enforced, not merely suggested. See [./team-workflow.md](./team-workflow.md) for how to wire that up today via `bounds ci --install`. |

---

See also: [./team-workflow.md](./team-workflow.md) for the adoption path and freshness discipline, and the [CLI reference](./cli-reference.md) and [AI agents guide](./ai-agents.md) for command-level detail.
