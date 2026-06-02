# Bounds benchmark result — Claude Code / Claude (baseline)

Baseline numbers for the Bounds repo itself, framed in the two headline value
props — **mapping coverage** and **token economics** — plus retrieval scaling
and correctness. No hardware specs are reported — see `../README.md` ("Why
there are no hardware specs"). The token-economics + coverage block below is the
verbatim output of `make benchmark` (`python benchmarks/run.py`) on this repo.

## Environment

| Field | Value |
|-------|-------|
| Agent | Claude Code |
| Model | Claude (Opus-class) |
| Tokenizer | tiktoken cl100k_base (exact) |
| Bounds version | 2026.6.24 |
| Date | 2026-06-02 |

> Token counts here are **exact** cl100k_base (tiktoken was installed for this
> run). Counts are tokenizer-specific — the *reduction ratios* are stable across
> tokenizers, the absolute numbers are cl100k. No hardware spec is reported and
> none is relevant to the headline metrics.

## 1. Mapping coverage + token economics (headline)

Verbatim output of `make benchmark` (`python benchmarks/run.py`) on this repo:

```
# Bounds benchmark — mapping coverage + token economics

- Repo: bounds (self / dogfood)
- Tokenizer: tiktoken cl100k_base (exact)

## Mapping coverage

How much of the repo's library source Bounds mapped. 3-way per source file — mapped /
unowned-supported / unsupported-language (no partial tier). Source = the authoritative
`bounds validate` metric (`stats.coverage.mapping`); tests are excluded from the
denominator and tracked separately.

- Mapped source: **100.0%** (36 / 36 non-test source files)
- Unmapped: 0 (0 unowned-but-supported, 0 unsupported-language)
- Tests linkage: 29 linked / 6 unlinked (of 35)
- Docs linkage: 3 linked / 21 unlinked (of 24)

## Token economics

Subsystem profiled: `models` (source: `src/bounds/models.py`).
The real saving is TARGETED retrieval (`describe`/`impact`) vs reading the source; the
whole-map `bounds list` figure is the cheap-orientation bound, not the per-`describe`
number. Cite the range, not a single flat %.

| Command | Bounds tokens | Source-equivalent tokens | Reduction |
|---------|--------------:|-------------------------:|----------:|
| `bounds describe models` | 457 | 2,930 | 84.4% |
| `bounds list` | 1,597 | 123,630 | 98.7% |
| `bounds impact models` | 380 | 2,930 | 87.0% |
| **aggregate** | **2,434** | **129,490** | **98.1%** |

Reduction = tokens saved by reading the Bounds contract instead of the equivalent source. `bounds list` source-equivalent is every subsystem's combined source (the whole-map alternative — cheap to orient, but few agents read a whole repo). The honest, repeatable win is targeted retrieval: one `describe`/`impact` vs the subsystem's source.
```

**Coverage:** Bounds maps **100.0%** of this repo's non-test library source (36
/ 36 files); there are zero unowned-supported and zero unsupported-language
gaps. Coverage is 3-way per file (mapped / unowned-supported /
unsupported-language) — there is **no "partial" file tier**; partial extraction
is a separate signal (`extraction_failures`, which is 0 here). Tests and docs
are tracked in their own linkage buckets and never drag the source % down: 29 of
35 tests are linked to an owning subsystem, 3 of 24 docs are linked.

**Token economics (honest framing):** the whole-map `bounds list` figure
(98.7%) is the *cheap-orientation bound* — it is what an agent saves versus
reading the entire repo, which almost nobody does. The repeatable, honest win is
**targeted retrieval**: a single `bounds describe models` (~457 tokens) or
`bounds impact models` (~380 tokens) versus the ~2,930-token source file — an
84–87% reduction on the work an agent actually does. Cite the **range
(84–99%)**, not a single flat number.

## 2. Retrieval scaling observation

`bounds describe models` is ~O(public API): its output is bounded by the
declared exported surface, so it stays roughly flat as the implementation behind
`models` grows. Reading source is ~O(files): the whole-map source alternative is
already ~123,630 tokens and keeps growing with the codebase. The wider that gap
gets, the more both the token bill and the lost-in-the-middle / context-rot
penalty favor targeted retrieval — minimal context keeps the model's attention
sharp on what matters.

## 3. Latency (optional, de-emphasized)

Machine-relative, no hardware spec. Reported only to confirm the commands are
fast enough for a pre-commit hook (sub-200ms quick-mode target for the
validation logic itself; medians below include interpreter startup). Latency is
deliberately kept out of the deterministic table.

| Command | Median wall-clock |
|---------|-------------------|
| `bounds validate --quick` | ~220ms |
| `bounds list` | ~230ms |
| `bounds describe models` | ~200ms |
| `bounds impact models` | ~190ms |

## 4. Determinism & correctness

Structural metrics are zero-LLM (tree-sitter + pure Python) and model-independent.

- Drift recall: every export add / remove / rename in tracked source is caught,
  because declared `exposes` is diffed against the tree-sitter parse.
- False positives on reformatting: ~0 — AST-level hashing ignores whitespace and
  comment-only edits.

## 5. Agent task outcomes

Baseline qualitative observations from dogfooding with Claude Code:

- Understanding a subsystem took 1 `bounds describe <name>` call (~457 tokens)
  instead of opening and reading the corresponding source file(s).
- Mapping the architecture took 1 `bounds list` call instead of grepping for
  `class` / `def` / `export` across the source tree.
- `bounds impact <name>` surfaced the transitive consumer set directly, instead
  of manually tracing imports to estimate a change's blast radius.

Contributors: add per-model results (Codex, Gemini, etc.) as separate files in
this directory using `../TEMPLATE.md`.
