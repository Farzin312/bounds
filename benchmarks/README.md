# Bounds benchmarks

Bounds is a retrieval / token-economics tool: it lets an AI agent obtain a
codebase's structural contract (exported symbols, dependency edges, blast
radius) without reading the source. So the benchmarks that matter are about
**tokens, retrieval scaling, and how agents behave** — not about the machine
the numbers were collected on.

This directory is contributor-extensible. The headline token and scaling
metrics are produced by a deterministic, model-agnostic harness
(`run.py` / `make benchmark`). The per-model, per-agent behavioral results live
in `results/`, one file per submission, so coverage grows over time (Claude
today; Codex, Gemini, and others as people contribute).

```
benchmarks/
  README.md          # this file: methodology, metrics, how to contribute
  TEMPLATE.md        # copy-paste submission template
  run.py             # deterministic token-economics harness
  results/
    claude-baseline.md   # first entry: Claude / tiktoken-estimate baseline
    <agent>-<model>.md   # your submission
```

## Why there are no hardware specs

Token cost, retrieval shape, and drift-detection correctness are properties of
the *codebase and the tokenizer*, not of the CPU or RAM. A token reduction of
~98% is the same on a laptop or a server. Reporting "MacBook, 18 GB RAM" would
imply hardware is a variable that matters here — it is not. The only
machine-relative number we report at all is wall-clock latency, and it is
explicitly de-emphasized and stated without any hardware spec (see Metric 3).

## State the tokenizer

Token counts are **tokenizer-specific**. The same string tokenizes to different
counts under cl100k_base (GPT-class), Claude's tokenizer, Gemini's, etc. The
harness uses tiktoken `cl100k_base` when it is installed and otherwise a
clearly-labeled `~1 token / 4 chars` estimate; either way it prints which method
it used. Every submission must state its tokenizer so numbers are comparable.
tiktoken is an *optional* import, never a Bounds dependency.

## The metrics

### 1. Token economics (headline)

Tokens to obtain a subsystem's contract via Bounds versus reading its source:

- `bounds describe <name>` vs reading that subsystem's source file(s)
- `bounds list` vs reading every subsystem's source (the whole-map alternative)
- `bounds impact <name>` vs manually tracing imports across the source

`run.py` reports per-command token counts (Bounds output vs source-equivalent)
and an aggregate reduction %. On the Bounds repo itself the aggregate reduction
is ~98% (see `results/claude-baseline.md`).

### 2. Retrieval scaling + context-rot (the core large-codebase argument)

This is the headline value and the reason Bounds matters *more* as a codebase
grows, not less.

- `bounds describe` is roughly **O(public API)** — the contract for one
  subsystem is bounded by its exported surface, which stays roughly constant
  regardless of how large the implementation behind it grows.
- Reading source to understand a subsystem is roughly **O(files)** — it grows
  with the codebase.

Crucially, LLMs degrade as their context fills: the "lost-in-the-middle" /
context-rot effect means a model attends less reliably to information buried in
a large context window. So dumping more source into context is doubly bad — it
costs more tokens *and* it lowers the quality of the model's attention over
those tokens. Minimal, targeted retrieval therefore matters **more** at scale.

Illustration — tokens to understand one subsystem as a repo grows (Bounds stays
flat because the public API is unchanged; source reading grows with the files
behind that subsystem):

| Repo size (subsystem source) | Read source (~tokens) | `bounds describe` (~tokens) |
|------------------------------|----------------------:|----------------------------:|
| 1 small file                 | ~2,900                | ~400                        |
| grows to 5 files             | ~14,000               | ~400 (API unchanged)        |
| grows to 20 files            | ~55,000               | ~400 (API unchanged)        |

The Bounds column is flat; the source column tracks file count. The wider the
gap, the more both the token bill and the context-rot penalty favor targeted
retrieval. The whole-map case is the same story sharpened: on this repo
`bounds list` is ~660 tokens versus ~52,000 tokens of combined source — and the
52k would also be the worst case for lost-in-the-middle degradation.

### 3. Latency (de-emphasized, machine-relative)

Wall-clock per command, framed only as: **is it fast enough for a pre-commit
hook?** The quick path targets sub-200ms for the validation logic itself
(Python interpreter startup adds overhead on top). We report a median and
nothing more. These numbers are machine-relative and intentionally carry **no
hardware spec** — do not read latency as a comparison axis between submissions.
`run.py` deliberately keeps latency out of its deterministic table; it is an
optional, separately-labeled note in a results file.

### 4. Determinism & correctness (zero-LLM, model-independent)

All structural metrics come from tree-sitter + pure Python, so they are
deterministic and identical across models:

- **Drift-detection recall:** every export add / remove / rename is caught,
  because the manifest's declared `exposes` is diffed against the tree-sitter
  parse of the source.
- **~0 false positives:** AST-level hashing ignores whitespace and comments, so
  reformatting or re-commenting does not register as drift.

These do not vary by model or tokenizer; they are a property of the engine.

### 5. Agent task outcomes (contributor-submitted, per model/agent)

The part that genuinely varies by model/agent and where contributors add value
over time: did the agent make an architecture-safe change *using* Bounds versus
without it? Structured, honest notes — e.g. "found the right subsystem in 1
`bounds describe` call instead of 6 file reads", or "`bounds preflight` caught a
boundary break before commit". Qualitative is fine; be specific about the task
and the model.

## How to contribute a result

Run `make benchmark` (or `python benchmarks/run.py`) from the repo root. Copy
`TEMPLATE.md` to `benchmarks/results/<agent>-<model>.md` (e.g.
`codex-gpt5.md`, `gemini-2.5-pro.md`), paste in the generated token-economics
table, fill in your environment (agent, model, tokenizer, Bounds version,
date), add the scaling observation and any agent-task-outcome notes, then open a
PR adding that single file. Don't edit other people's result files; add your
own. State your tokenizer.

## Results index

| Submission | Agent | Model | Tokenizer |
|------------|-------|-------|-----------|
| [`claude-baseline.md`](results/claude-baseline.md) | Claude Code | Claude (Opus) | char/4 estimate |
