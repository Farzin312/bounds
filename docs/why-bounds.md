# Why Bounds

*The honest value model: verified context for AI agents first, CI drift control second, zero-LLM structural checks throughout.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

## The real problem Bounds solves: agent context quality

When an AI agent starts in a large repo, the default behavior is expensive and weak: read a pile of
files, infer architecture from local evidence, then act on an incomplete mental model. That burns
tokens, increases hallucinated or stale assumptions, and still leaves the agent without a cheap way to
ask, "what is this subsystem supposed to expose?" or "who breaks if I change this table?"

Bounds exists to make the better behavior cheaper. The agent starts with `bounds list`, zooms with
`bounds describe <name>`, checks blast radius with `bounds impact <name>`, and only then reads source.
That is the AI-first value: verified, token-lean context before broad search.

The second value is enforcement. You declare each subsystem's boundary once, in a few lines of YAML,
and Bounds validates the code against that declaration on every commit — in both directions. A
boundary violation becomes a failing CI check with a deterministic fix suggestion, not a surprise you
discover during a future refactor.

## How it concretely helps an AI agent

Bounds is a plain CLI that emits JSON, so any agent that can run a shell command can use it. Four concrete wins:

| Win | Command | Why it matters |
|-----|---------|----------------|
| A cheap, verified map | `bounds describe <name>` | The agent reads a compact subsystem contract (a few hundred tokens for a small subsystem; cost scales with exposed API/table count) instead of opening a dozen source files (thousands of tokens) to reconstruct what a part does and what it touches. |
| A trust signal | every `describe` expose carries `verified: true/false` | `verified: true` means tree-sitter confirmed the symbol actually exists in source — the agent can rely on the manifest without re-reading the file. |
| The current DB surface | `bounds describe <schema-sub>` | For Postgres/Supabase, folds the migrations into the live table + column + policy/RLS surface and a derived **RLS posture** (protected / RLS-without-policy / unprotected). "What columns does `orders` have now, and is it RLS-protected?" is a `CREATE` plus a dozen ordered `ALTER`s an agent gets subtly wrong by reading — Bounds computes it deterministically. |
| Honesty about blind spots | `schema_coverage` on every schema describe | `{complete: true}` means absence is authoritative (a table not listed isn't in the schema); when some DDL couldn't be parsed it flips to `{complete: false, unextracted_files: N}` so an agent never reads a parse gap as "this doesn't exist." |
| Blast radius **before** editing | `bounds impact <name>` | The agent sees the transitive consumer set and the exact interfaces/tables each consumer relies on *before* writing the change — so it doesn't blindly break `api/`, `frontend/`, or a schema consumer. This is the biggest single agent win: fewer blind breakages. |
| A deterministic post-edit self-check | `bounds validate --quick` | After editing, the agent runs a fast, zero-LLM structural check and reads `validation_status` to know whether its change drifted from the declared boundary. |

The pattern: pull a verified slice of architecture into context with one CLI call, reason about reach before acting, then verify structure after acting — all deterministically, all without burning tokens on raw source.

## Token savings

An agent's only real cost is tokens into context, and a Bounds contract is priced by what a subsystem *exposes*, not how big it is — so the token win *widens* as the implementation grows behind a stable public API. The full `O(public API)` vs `O(files)` scaling argument — with a worked example and a measured table — lives in [token-economics.md](token-economics.md).

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
| Validate every language | **Python, TypeScript/JS, SQL migrations, and Prisma schemas** are tree-sitter/parser-verified today. Go/Rust/Java are roadmap. Unsupported files fall back to YAML-only metadata, and *only* for files a manifest names directly — auto-discovered files in an unsupported language are skipped, not validated. |

A note on the cache: `.bounds/cache.db` is a binary SQLite file so a tool that blindly `cat`s a directory gets binary bytes instead of a giant parseable token blob. That is an **accidental-context-burn defense, not access control** — the manifests in `.bounds/manifests/*.yaml` are plain YAML any agent can still read directly. Only the *derived* extraction cache is binary.

And agent compliance is **advisory**: Bounds writes the right instructions into the configs agents already read, but it cannot force an agent to follow them. The one hard enforcement point is **CI**, which runs in your pipeline, not in the agent.

## Current limitations, and how the roadmap addresses them

| Limitation today | Roadmap response |
|------------------|------------------|
| No semantic tier — `--deep` is stubbed | **v0.2**: live LLM enrichment for type signatures and intent summaries (opt-in, never in the structural path). |
| Only Python + TS/JS + SQL + Prisma are verified | **v0.2**: Go and Rust adapters. **v0.3**: Java. |
| tree-sitter-sql can't parse every Postgres construct (`DO $$…$$` table creation, some pg_dump output) | Widen the SQL grammar coverage over time. Until then, **nothing is silently dropped** — `schema_coverage` reports exactly which files it couldn't fully parse (see below), so coverage can only ever *improve* on an already-honest baseline. |
| No native agent integration beyond CLI + generated configs | **v0.3**: an MCP server (`bounds mcp`) so agents query Bounds through a first-class protocol. |
| Agent compliance is advisory; the loop isn't closed | Closing the agent loop — tighter hook/CI integration and `bounds watch` — so the right behavior is enforced, not merely suggested. See [./team-workflow.md](./team-workflow.md) for how to wire that up today via `bounds ci --install`. |

## Schema extraction: what's verified, and the honesty contract

For a SQL/Prisma schema subsystem the migrations *are* the contract — Bounds folds them on every
run rather than storing a list that could go stale. **Verified today** (deterministic, zero-LLM):

- tables + columns across `CREATE`/`ALTER`/`DROP`/`RENAME`, **folded in migration order** so the
  catalog is the *current* surface (a dropped column/table is gone, not lingering);
- functions/RPCs, views, indexes, triggers, types;
- **row-level security** — `CREATE`/`ALTER`/`DROP POLICY` and `ENABLE`/`DISABLE`/`FORCE ROW LEVEL
  SECURITY` — folded the same way (a dropped policy nets out), plus a derived **RLS posture**;
- DDL wrapped in `BEGIN; … COMMIT;` transactions, and tables whose table-level `CONSTRAINT`
  clause the grammar can't parse (recovered best-effort, columns intact).

**Known gaps** (tree-sitter-sql limits): a `DO $$ … $$` block that creates a table dynamically, or
heavily-fragmented pg_dump output. The point is *how Bounds handles them*: **it never silently
drops them.** Every schema describe carries `schema_coverage` — `{complete: true}` when every owned
file extracted (so a table not listed genuinely isn't there), or `{complete: false,
unextracted_files: N}` naming the gap under `--full`. A consumer is told where the map is
incomplete instead of being misled. That honesty is what makes the verified parts trustworthy — and
it means grammar coverage can widen later without ever having quietly given a wrong answer.

---

See also: [./team-workflow.md](./team-workflow.md) for the adoption path and freshness discipline, and the [CLI reference](./cli-reference.md) and [AI agents guide](./ai-agents.md) for command-level detail.
