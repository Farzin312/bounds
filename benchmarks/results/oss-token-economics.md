# OSS token-economics + capability head-to-head

> **Superseded (2026-06-01).** This is the original two-repo run using the `~4 chars/token`
> *estimate*. It is kept for history. The current, larger run — **16 repos across Python/TS/JS with
> exact `tiktoken cl100k_base` counts, full-command coverage, and the bugs it surfaced** — is
> [`oss-cross-language.md`](oss-cross-language.md). Cite that one.

Cross-repo run of the deterministic harness (`benchmarks/oss_run.py`) on real, third-party
open-source projects — so the headline numbers aren't measured only on Bounds itself — plus a
same-model capability head-to-head (does the model answer an architecture question *correctly*,
and at what token cost, with Bounds vs without).

- **Tokenizer:** `~1 token / 4 chars` estimate (tiktoken not installed in this run; the harness
  prints which method it used). Counts are tokenizer-specific — re-run with tiktoken for exact
  cl100k numbers.
- **Bounds version:** the `agent-plugins-aliases-benchmark` branch (post dotted-filename +
  tsconfig-alias resolver fixes).
- **Reproduce:** `python benchmarks/oss_run.py` (clones each cited repo, runs `bounds discover`,
  measures). Numbers below are from the cited commits.

## 1. Token economics (deterministic)

| Repo | Commit | Subsystems | `bounds list` | All source | Map reduction | `bounds describe` | Subsystem source | API reduction |
|------|--------|-----------:|-------------:|-----------:|--------------:|------------------:|-----------------:|--------------:|
| click | `c480210` | 4 | 205 | 208,242 | **99.9%** | 5,971 (`click`) | 103,392 | **94.2%** |
| axios | `4306df2` | 16 | 814 | 573,740 | **99.9%** | 901 (`lib`) | 50,483 | **98.2%** |

- *Map reduction* = `bounds list` (whole-repo orientation) vs reading every subsystem's source.
- *API reduction* = `bounds describe <name>` (one subsystem's verified contract) vs reading that
  subsystem's source.
- **axios is TypeScript** — these edges only resolve because of the dotted-filename + tsconfig
  path-alias resolver fixes on this branch; on `main` axios's graph is badly undercounted.

### Test subjects (cited)
- **click** — https://github.com/pallets/click @ `c480210` (BSD-3-Clause)
- **axios** — https://github.com/axios/axios @ `4306df2` (MIT)

## 2. Capability head-to-head (same model, with vs without Bounds)

> Model: **Claude (Opus 4.8)**. One concrete task, run both ways on `click` @ `c480210`. This is a
> single-model qualitative observation (the token figures are deterministic; the "correct?"
> judgment is the author's) — contribute your own per `TEMPLATE.md`.

**Task:** "I want to safely extend click's core. What is its public API, and what depends on it?"

| Approach | Steps | Tokens into context | Answer quality |
|----------|-------|--------------------:|----------------|
| **With Bounds** | `bounds describe click` → `bounds impact click` | **~6,149** | Correct + verified: 183 exported symbols (tree-sitter-checked against source), consumers = `tests`, `typing`. Direct answer in 2 commands. |
| **Without Bounds** | read `src/click/*.py` (17 files), infer the public surface, grep for importers | **~103,392+** | Reachable but lossy: the model must *infer* which symbols are public and trace importers by hand — ~17× the tokens, and the public-surface inference is exactly the error-prone step Bounds removes. |

**Verdict:** for the "orient + find the contract + find dependents" class of task, Bounds is both
~17× cheaper *and* more reliable, because the public surface and dependency edges are extracted
deterministically rather than inferred from a large, context-rot-prone source dump. For tasks that
need behavior ("*how* does this function work"), you still read the source — Bounds is a navigation
layer, not a comprehension layer.

## Honest caveats

- The token tokenizer here is the char/4 estimate; exact counts differ under cl100k/Claude/Gemini
  tokenizers (all show the same order-of-magnitude reduction).
- `describe` reduction depends on subsystem size, so it varies (axios `lib` 98.2%, click `click`
  94.2%). The whole-map `bounds list` reduction is the stable headline.
- The capability section is one model's observation on one task; it is illustrative, not a
  statistical claim.
