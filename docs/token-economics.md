# Token economics

*The cost argument: why a verified contract beats reading source, why the win widens with codebase size, and an honest account of how the numbers were measured.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

An AI agent's only real cost is **tokens into context**, so that is the only unit that matters here. The core claim: Bounds replaces reading a subsystem's source with a compact, tree-sitter-verified contract whose cost tracks how many symbols the subsystem *exposes* — a few hundred tokens for a small, well-factored subsystem, and still far below re-reading the full source for a large one.

## Token cost comparison

> **Estimate basis (read this first).** Token figures are estimates derived from byte size at **~4 chars/token** — a standard rough rule for JSON/source, **not** exact tokenizer counts. They come from **one codebase (this repo): a single data point, not a cross-repo corpus study.** The byte numbers are real and reproducible via `benchmarks/run.py`; treat the *ratio* as illustrative, not a guaranteed average.

### Measured on this repo

To understand the `models` subsystem's public API (10 exports, consumed by 5 subsystems) — a small, well-factored subsystem:

| Read this | Size | Token estimate |
|-----------|------|----------------|
| `bounds describe models` (verified JSON contract) | ~1,620 bytes | **~400 tokens** |
| `src/bounds/models.py` (the full source file) | ~11,660 bytes | **~2,900 tokens** |

The agent gets the verified public surface for **~400 tokens** instead of **~2,900 tokens** of source — and in real cases a subsystem spans several files, so the source side is usually far larger. This is one small subsystem on one repo: a contract's size tracks the number of symbols a subsystem exposes, so a wide-API subsystem costs proportionally more (see the measured range below).

For the whole-system map across all 8 subsystems:

| Read this | Size | Token estimate |
|-----------|------|----------------|
| `bounds list` (every subsystem: role, criticality, graph, interface counts) | 2,633 bytes | **~660 tokens** |

`bounds list` is the cheap whole-system map: **~660 tokens** for the complete architecture instead of grepping a dozen-plus source files and mentally reconstructing it.

| Scenario | Without Bounds | With Bounds | Token savings |
|----------|----------------|-------------|---------------|
| Understand one subsystem | Read 1–15 source files (thousands of tokens) | `bounds describe <name>` (a few hundred tokens for a small subsystem; scales with its exposed API) | ~85–99% for well-factored subsystems |
| Map all subsystems | Grep `class\|def\|export` across the tree | `bounds list` (~660 tokens) | Near-total |
| Dependency blast radius | Trace imports by hand | `bounds impact <name>` (transitive consumers + relied-on interfaces) | ~99% |
| Detect architecture drift | Manual code review | `bounds validate` (structured report, 0 LLM) | Subjective → deterministic |
| CI gate for boundary violations | No automated option | `bounds preflight --ci` | Previously impossible |

These percentages follow from the single-repo measurements above; the same caveat applies.

### Verified across real OSS repos (not just this repo)

To answer the obvious "but you measured your own repo" objection, the same deterministic harness was run on real, third-party projects cloned at a cited commit — [`benchmarks/oss_run.py`](../benchmarks/oss_run.py) shallow-clones each repo, runs `bounds discover`, and measures tokens (Bounds output vs the equivalent source). Full table and methodology: [`benchmarks/results/oss-token-economics.md`](../benchmarks/results/oss-token-economics.md).

| Repo | Commit | `bounds list` | All source | Map reduction | `bounds describe` | Subsystem source | API reduction |
|------|--------|-------------:|-----------:|--------------:|------------------:|-----------------:|--------------:|
| click (Python) | `c480210` | 205 | 208,242 | **99.9%** | 5,971 (`click`) | 103,392 | **94.2%** |
| axios (TypeScript) | `4306df2` | 966 | 558,868 | **99.8%** | 156 (`lib-adapters`) | 18,172 | **99.1%** |

- **Map reduction** is the whole-system `bounds list` figure (orient on the entire repo) vs reading every subsystem's source — that's the 99.9% / 99.8% headline.
- **API reduction** is one subsystem's `bounds describe` contract vs reading *that* subsystem's source; it tracks subsystem size, so a small subsystem (axios `lib-adapters`, 99.1%) shows a larger % than a big one (click `click`, 94.2%). The whole-map `list` reduction is the stable headline; per-`describe` reductions vary with the exposed surface.
- The axios (TypeScript) edges only resolve correctly because of the dotted-filename + tsconfig path-alias resolver fixes on this branch.

Same estimate basis as above (~4 chars/token; tiktoken not installed in the recorded run). The numbers are **reproducible**: re-clone each repo at the cited commit and re-run `python benchmarks/oss_run.py`.

### Capability head-to-head (same model, with vs without)

Tokens are only half the story — does the agent actually answer the architecture question *correctly*? On click @ `c480210`, the same model (**Claude Opus 4.8**) was asked: "what is the public API, and what depends on it?"

- **With Bounds** (`bounds describe click` → `bounds impact click`): **~6,149 tokens**, and the public surface is tree-sitter-verified against source (183 exported symbols; consumers identified) rather than inferred.
- **Without Bounds** (read `src/click/*.py`, infer which symbols are public, grep for importers): **~103,392+ tokens**, and the public-surface inference is the error-prone step Bounds removes.

That's **~17× cheaper** for the "orient + find the contract + find dependents" class of task, *and* more reliable because the surface and dependency edges are extracted deterministically, not inferred from a large, context-rot-prone source dump. **Honest caveat:** this is one model's observation on one task — the token figures are deterministic, the "correct?" judgment is the author's. And as throughout this page, Bounds is a *navigation* layer, not a *comprehension* one: for tasks that need behavior ("*how* does this function work"), you still read the source.

**Contribute your own numbers.** Run `make benchmark` (or `python benchmarks/oss_run.py`) and submit your model's/tokenizer's results per [`benchmarks/TEMPLATE.md`](../benchmarks/TEMPLATE.md) — exact tokenizer counts (cl100k, Claude, Gemini) all show the same order-of-magnitude reduction.

## How retrieval scales (and why it matters more as you grow)

The token win isn't a flat discount — it *widens* with codebase size, and that is the whole point.

- **Reading source is O(files).** To understand a subsystem by reading it, an agent's token cost grows with how much code that subsystem (and its neighbors) contains. Bigger codebase → bigger reads.
- **A Bounds contract is O(symbols exposed).** `bounds describe` returns only the declared, tree-sitter-verified surface — `exposes`, `consumes`, `consumed_by`. A subsystem with 50 internal functions and 5 exports is still ~5 expose entries. So the contract stays roughly **flat as a subsystem's *internals* grow** — but it **scales with how many symbols it *exposes***. A small, well-factored subsystem is cheap; a sprawling one with hundreds of exports is not.

The table below tracks the dimension that matters for the contract — *internal* size at a roughly fixed public API — so the describe column stays flat. (Widen the public API and that column grows; see the measured range below.)

| Subsystem internals (fixed public API) | Read the subsystem's source | `bounds describe <name>` |
|----------------------------------------|-----------------------------|--------------------------|
| Small (a few files) | hundreds–low-thousands of tokens | a few hundred tokens for a small exposed surface |
| Medium (dozens of files) | many thousands of tokens | roughly unchanged if the exposed API/table count is unchanged |
| Large (hundreds of files) | tens of thousands of tokens | still driven by exposed API/table count, not internal file count |

**Measured range (single-source data point, not a guarantee).** On a 185-manifest TypeScript repo, a `describe` contract measured **~170–570 tokens for a small, well-factored subsystem**, rising to **~1.5k–13k tokens** for large or poorly-factored ones (median ~3,240; max ~13,360 for a 255-export subsystem). The driver is the export count, not the line count — the takeaway is *factor your subsystems*, not *every contract is ~400 tokens*.

### The context-rot risk (framed as risk, not a guaranteed fix)

This compounds with a property of LLMs that *can* punish the naive approach: models often get worse as their context fills — the "lost-in-the-middle" / context-rot effect, where relevant facts buried in a large prompt are recalled less reliably. So in a large codebase the source-reading approach risks being doubly bad: it costs more tokens *and* may degrade reasoning quality.

To be clear about what Bounds claims and what it doesn't: targeted, minimal retrieval — one verified contract, the dependency map, a blast-radius query — is the behavior that *should* scale better, and Bounds is built to make that the cheap default. It does **not** guarantee an agent reasons well or finishes a task; it lowers token load and gives structure the agent can verify. The architecture lives outside the model's context until a single CLI call pulls in exactly the slice it needs.

## Methodology and per-model results

Token counts are tokenizer-dependent, and the figures above use the ~4 chars/token approximation rather than a real tokenizer. The measured token economics, the scaling methodology, and per-model community results (which note the specific model/tokenizer used) live in [`benchmarks/`](../benchmarks/README.md). Start there for the reproducible numbers and the exact methodology behind any claim on this page.

---

**See also:** [./how-it-works.md](./how-it-works.md) for the mechanism behind these numbers · [../benchmarks/README.md](../benchmarks/README.md) for methodology and per-model results · [../ARCHITECTURE.md](../ARCHITECTURE.md) for the full engineering contract.
