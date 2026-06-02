# Bounds vs code graphs

*Why Bounds isn't another code graph — and why it's complementary to one, not a replacement.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

Bounds exists in a crowded space of code intelligence tools (CodeGraph, tree-sitter-analyzer,
roam-code, CodeSage). Every prior approach shares the same assumption: **parse everything, build a
graph, let the AI figure it out.** That's the right tool for *exploration*. It creates three problems
when an agent needs a cheap, trustworthy answer instead — and those three gaps are what Bounds targets.

> *A code graph tells your agent what the code **is**; Bounds tells it what the code is **supposed to be** — and fails the build when those diverge. Use a graph to explore, use Bounds to enforce.*

---

## The three gaps

The first two are gaps a derived graph *structurally cannot* close — they're not a matter of a bigger
index or a faster parser, but of information the graph never contained. The third (token cost) is one
a graph *can* answer, just expensively. That's the order of what matters.

### 1. No intent signal

A graph tells you what IS, not what SHOULD BE. Every symbol is equal — there is no way to tell a
deliberate public contract apart from an internal helper that happens to be reachable. Bounds
distinguishes public contracts from private implementation by design: the developer declares the
boundary in YAML, and Bounds enforces it against the extracted reality.

### 2. No drift detection

A graph is always correct by definition — it reflects reality. That's also its blind spot: it cannot
tell you that someone added an export without declaring it, or removed an interface another subsystem
depends on. Bounds's validation is exactly the difference between *declared intent* and *extracted
reality*, checked in both directions on every commit.

### 3. Token bloat

A full code graph of a Django codebase can be tens of thousands of tokens. An agent pays this cost
just to find where `login()` is defined. Bounds gives you the answer in a few hundred tokens of JSON —
one CLI call. A Bounds contract is `O(public-surface count)`, not `O(full symbol count)`, so it stays
roughly flat as the implementation behind it grows.

---

## Complementary, not competitive

This is the honest framing: **Bounds does not beat a code graph at exploration, and it isn't trying
to.** These are tools at different layers.

- A code graph (CodeGraph / TSA / roam-code) answers **"What's in this codebase?"** — detailed,
  expensive, exploratory. It's the right tool when you genuinely need to traverse every symbol.
- Bounds answers **"What SHOULD this codebase's architecture look like?"** — concise, deterministic,
  enforceable. It's the right tool when you need a trustworthy contract cheaply, or a CI gate.

The workflow is to use both: **explore with the graph, validate with Bounds.** An agent that reads
the Bounds map first then digs into specific symbols uses any graph tool *more* efficiently — it
reads the boundaries first, then drills into the exact symbols it needs rather than searching blindly.

In short: code graphs are too large and noisy for an agent to consume cheaply, they capture what *is*
without any signal of human-declared intent or boundaries, and they drift from the code the moment
something changes with no way to flag it. Bounds targets exactly those three gaps — a tiny
per-subsystem contract instead of a full symbol graph, an explicit declared boundary instead of an
undifferentiated graph, and two-directional validation that catches drift between declared intent and
extracted reality.

---

## Full side-by-side comparison

| Dimension | CodeGraph / TSA / roam-code | Bounds |
|-----------|---------------------------|---------|
| Core approach | Extract graph from source | Declare intent in YAML, validate against source |
| What you get | What code exists | What code SHOULD exist |
| Granularity | Functions, classes, imports | Subsystem boundaries, contracts, dependencies |
| Token cost | O(full symbol count) | O(public-surface count) — 5–20 lines per subsystem |
| Validation | None (graph = truth) | Drift detection, contract compliance, boundary checks |
| LLM dependency | High (embeddings, semantic search) | Zero for structural path |
| CI integration | Analysis step | Gate step — can block PRs |
| Setup time | Server config, embedding indexing | `pipx install` + `bounds discover` |

The contrast is real but the conclusion is not "Bounds wins." CodeGraph/TSA/roam-code answer "What's
in this codebase?" — detailed, expensive, exploratory. Bounds answers "What SHOULD this codebase's
architecture look like?" — concise, deterministic, enforceable. An AI agent that knows the boundaries
(via Bounds) uses a code graph more efficiently: it reads the map first, then digs into specific
symbols rather than searching blindly.

---

## What Bounds does NOT do

Being explicit about the edges is part of why you can trust the parts that do work:

| Bounds does **not** | What that means |
|---------------------|-----------------|
| Execute your code | Pure static tree-sitter parsing — no runtime, no semantics inferred from behavior. |
| Understand intent on its own | It validates **human-declared** manifests; it does not invent what a subsystem *should* be. |
| Produce semantic summaries without `--deep` | The Tier-3 LLM tier is **stubbed** in the MVP; the structural path is intentionally LLM-free. |
| Replace exploratory code graphs | It **complements** them — graph to explore, Bounds to validate a declared boundary. |
| Guarantee an agent reasons well or finishes the task | It cuts token load and validates structure; it makes no claim about task success. |
| Auto-update your manifests | Drift is detected and proposed; you apply fixes with `calibrate --apply`. |
| Validate every language | Python + TS/JS + SQL migrations are tree-sitter-verified today; Go/Rust/Java are on the roadmap, and other languages fall back to YAML-only (declared files) or are skipped. |
