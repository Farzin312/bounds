# Bounds benchmarks

Bounds is a retrieval / token-economics tool: it lets an AI agent obtain a
codebase's structural contract (exported symbols, dependency edges, blast
radius) without reading the source. So the benchmarks that matter are about
**how much of the repo Bounds maps, how many tokens that saves, retrieval
scaling, and how agents behave** — not about the machine the numbers were
collected on.

These benchmarks report **two** headline value props, side by side, in one
reproducible format:

1. **Mapping coverage** — what fraction of the repo's library source Bounds
   actually maps, and an honest breakdown of what it cannot (see Metric 0).
2. **Token economics** — how many tokens an agent saves by reading the Bounds
   contract instead of the source (Metric 1).

This directory is contributor-extensible. The headline metrics are produced by
deterministic, model-agnostic harnesses; the per-model, per-agent behavioral
results live in `results/`, one file per submission, so coverage grows over time
(Claude today; Codex, Gemini, and others as people contribute).

```
benchmarks/
  README.md          # this file: methodology, metrics, how to contribute, how to re-run
  TEMPLATE.md        # copy-paste submission template (coverage + token economics)
  _lib.py            # SHARED helpers — the one home for: the tokenizer seam (_make_counter +
                     #   its label), the `bounds` subprocess runner, manifest_source_files,
                     #   pick_subsystem, and read_coverage() (authoritative coverage from the CLI)
  run.py             # dogfood harness: coverage + token economics on THIS repo (`make benchmark`)
  oss_bench.py       # per-repo engine: coverage + token economics + discover↔validate health (JSON)
  oss_features.py    # full-command smoke matrix + discover→calibrate→validate convergence (JSON)
  oss_report.py      # runs oss_bench + oss_features on one cloned repo, prints a COMBINED
                     #   markdown report (coverage + tokens + command health) — `make oss-bench`
  agent_ab.py        # REAL-agent A/B: drives `claude -p` with vs without Bounds and measures
                     #   actual tokens/cost/time/turns + blind-judged answer quality (Metric 5)
  results/
    claude-baseline.md          # living proof: `make benchmark` output on this repo (coverage + tokens)
    agent-bounds-ab.md          # real-agent A/B (supermemory): with vs without Bounds, measured
    oss-cross-language.md       # 16-repo sweep: economics, correctness, full-command coverage
    oss-fresh-discover-trust.md # first-run OSS trust/recovery benchmark
    oss-token-economics.md      # ARCHIVED — original 2-repo estimate run (superseded; do not cite)
    <agent>-<model>.md          # per-agent submissions
```

### How to re-run (any AI collaborator or human)

Use the project venv (`.venv/bin/python`, `.venv/bin/bounds`); the Makefile
prefers it automatically.

- **Dogfood (this repo):** `make benchmark` — prints the mapping-coverage block
  and the token-economics table for the Bounds repo. Byte-stable; paste straight
  into a results file. (`make benchmark` == `python benchmarks/run.py`.)
- **One third-party repo:** clone it to a throwaway dir, then
  `make oss-bench REPO=/path/to/clone [NAME=flask LANG_LABEL=python]`. This bootstraps
  a fresh `.bounds/` in that clone (`init` + `discover`), runs the full command
  surface, and prints a finished combined markdown report — coverage, token
  economics, and command-surface health in one block. **It writes into the
  target repo, so point it at a throwaway clone, never your working tree.**
- **Raw JSON (for tooling):** `python benchmarks/oss_bench.py --repo <clone>` and
  `python benchmarks/oss_features.py --repo <clone>` each emit one JSON object.
  `oss_report.py` just runs both and renders the markdown.

All harnesses import their shared logic from `_lib.py`, so the tokenizer, the
`bounds` runner, the source-file resolution, the subsystem pick, and the
coverage read are defined exactly once and can never disagree across harnesses.

## Why there are no hardware specs

Token cost, retrieval shape, and drift-detection correctness are properties of
the *codebase and the tokenizer*, not of the CPU or RAM — a token reduction of
this magnitude is the same on a laptop or a server. The only machine-relative
number we report is wall-clock latency, which is de-emphasized and stated
without a hardware spec (see Metric 3).

## State the tokenizer

Token counts are **tokenizer-specific**. The same string tokenizes to different
counts under cl100k_base (GPT-class), Claude's tokenizer, Gemini's, etc. The
harness (`_lib._make_counter`) uses tiktoken `cl100k_base` (exact) when it is
installed and otherwise a clearly-labeled `~1 token / 4 chars` estimate; either
way it prints which method it used. The **reduction ratios are stable across
tokenizers** (same order of magnitude); only the absolute counts are
cl100k-specific. Every submission must state its tokenizer so numbers are
comparable. tiktoken is an *optional* import, never a Bounds dependency — if it
isn't installed, the labeled chars/4 fallback is expected and fine.

## The metrics

### 0. Mapping coverage (headline, alongside tokens)

**What fraction of the repo's library source did Bounds actually map?** This is
the honesty metric: it stops a polyglot repo from looking "fully mapped" while
half of it is an unsupported language. The number comes straight from the CLI's
own authoritative metric — `src/bounds/extract/scan.py::mapping_coverage`,
surfaced at `bounds validate` → `stats.coverage.mapping` (also on
`bounds overview` → `health.validation.mapped_pct` and on `bounds discover`).
The benchmarks read it via `_lib.read_coverage(repo)` rather than re-walking the
tree, so a benchmark can never disagree with what the CLI reports.

Coverage is **3-way per source file**:

- **mapped** — the file is owned by a subsystem (in some manifest's `paths:`).
- **unowned-supported** — Bounds *has* an adapter for that language, the file is
  just not in any manifest yet. Fixable: add it to a manifest (deterministically
  mappable).
- **unsupported-language** — no adapter for that language yet. Fixable: an
  adapter, or a hand/AI-authored manifest.

There is **no "partial" file tier.** A file is mapped or it isn't.
"Partial extraction" is a *different* concept and is reported **separately** as
`extraction_failures` (a file Bounds tried to parse but couldn't fully) — it is
not a coverage bucket. Do not invent a partial-coverage tier.

`mapped_pct` is `files_mapped / files_source_total` over **non-test** source
files. **Test files are excluded from the denominator** (they can never drag the
% down) and tracked in a separate `tests` linkage bucket; docs get their own
`docs` bucket. Both buckets report `{total, linked, unlinked, unlinked_sample}`
— "linked" meaning the test/doc is associated with an owning subsystem. The
coverage walk is gitignore-aware: a gitignored file is in neither the numerator
nor the denominator.

`make benchmark` prints this as a `## Mapping coverage` block; `make oss-bench`
includes it for a third-party repo. On the Bounds repo itself coverage is
**100.0%** (every non-test source file is owned) — see
`results/claude-baseline.md`.

### 1. Token economics (headline)

Tokens to obtain a subsystem's contract via Bounds versus reading its source:

- `bounds describe <name>` vs reading that subsystem's source file(s)
- `bounds list` vs reading every subsystem's source (the whole-map alternative)
- `bounds impact <name>` vs manually tracing imports across the source

`run.py` reports per-command token counts (Bounds output vs source-equivalent)
and an aggregate reduction %.

**Be honest about the framing.** The whole-map `bounds list` reduction (~98.7%
on this repo) is the *cheap-orientation* bound — it is what you save versus
reading the *entire* repo, which almost no agent actually does, so it overstates
real-world savings. The **repeatable, honest win is targeted retrieval**: a
single `bounds describe <name>` or `bounds impact <name>` versus reading that one
subsystem's source — typically an **84–87%** reduction on this repo. Always cite
the **range** (~84–99%), never one flat number, and never read the whole-map %
as if it were the per-`describe` number. See `results/claude-baseline.md` for the
current per-command breakdown (regenerated by `make benchmark`).

To measure on real third-party repos (so the numbers aren't self-selected),
clone one and run `make oss-bench REPO=<clone>` — it bootstraps `.bounds/`,
records the Bounds version, and prints both coverage and the token table. The
underlying `oss_bench.py` also reports the full per-subsystem `describe`
distribution (min/median/max) so the spread is honest: `describe` scales with
*exposed API*, not file count, so a fat-API subsystem can be tens of thousands of
tokens while most are a few hundred. See `results/oss-cross-language.md` for a
16-repo sweep and `results/oss-fresh-discover-trust.md` for the first-run trust
benchmark.

### 2. Retrieval scaling + context-rot (the core large-codebase argument)

This is the headline value and the reason Bounds matters *more* as a codebase
grows, not less.

- `bounds describe` is roughly **O(public API)** — the contract for one
  subsystem is bounded by its exported surface, which stays roughly constant
  regardless of how large the implementation behind it grows.
- Reading source to understand a subsystem is roughly **O(files)** — it grows
  with the codebase.

LLMs also degrade as their context fills: the "lost-in-the-middle" /
context-rot effect means a model attends less reliably to information buried in
a large context window. So dumping more source into context is doubly bad — it
costs more tokens *and* lowers the model's attention over them. Minimal,
targeted retrieval therefore matters **more** at scale.

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
`bounds list` is ~1,600 tokens versus ~123,000 tokens of combined source (exact
cl100k counts from the latest `make benchmark`) — and the 123k would also be the
worst case for lost-in-the-middle degradation.

### 3. Latency (de-emphasized, machine-relative)

Wall-clock per command, framed only as: **is it fast enough for a pre-commit
hook?** The quick path targets sub-200ms for the validation logic itself
(Python interpreter startup adds overhead on top). We report a median and
nothing more. These numbers are machine-relative and carry **no hardware
spec** — don't read latency as a comparison axis between submissions. `run.py`
keeps it out of the deterministic table; it is an optional, separately-labeled
note in a results file.

### 4. Determinism & correctness (zero-LLM, model-independent)

All structural metrics come from tree-sitter + pure Python, so they are
deterministic and identical across models:

- **Drift-detection recall:** every export add / remove / rename is caught,
  because the manifest's declared `exposes` is diffed against the tree-sitter
  parse of the source.
- **~0 false positives:** AST-level hashing ignores whitespace and comments, so
  reformatting or re-commenting does not register as drift.

These do not vary by model or tokenizer; they are a property of the engine.

### 5. Agent task outcomes — measured A/B (`agent_ab.py`)

Metrics 1–2 measure a **static proxy**: Bounds output tokens vs reading a subsystem's
*whole* source. That is an **upper bound** on the saving — no real agent reads all the
source; it greps selectively — so it answers "how much smaller is the contract" not "how
much does an agent actually save." `agent_ab.py` closes that gap by measuring the **real
thing**: it drives the actual Claude Code CLI (`claude -p`) on a set of architecture tasks,
twice, holding model/tasks/repo constant and changing **only** whether the agent can use
Bounds.

- WITHOUT: tools = Read, Grep, Glob (native search; no Bounds).
- WITH: tools = Read, Grep, Glob, Bash(bounds:*) + a note to prefer Bounds.

It records **real Anthropic usage** (tokens, USD, wall-clock, turns, tool calls — from the
CLI's own JSON result, not an estimate) and scores **answer quality 0–100** with a blind LLM
judge against a verified ground-truth fact per task. N reps per cell; mean + median + spread.

```
make agent-ab REPO=/path/to/repo                       # or:
python benchmarks/agent_ab.py --repo <repo> --reps 3   # full matrix
python benchmarks/agent_ab.py --repo <repo> --smoke    # 1 task, both arms (quick check)
python benchmarks/agent_ab.py --repo <repo> --render-only results/agent_ab_raw.jsonl  # re-render
```

First run (`supermemory`, Sonnet, 6 tasks × 3 reps — see
[`results/agent-bounds-ab.md`](results/agent-bounds-ab.md)): **−60% tokens, −71% cost, −81%
wall-clock, +43.5 quality points**, and **18/18 vs 8/18 correct** — the correctness gap
concentrated in dependency / blast-radius / whole-repo-structure questions a grep-only agent
cannot answer reliably. A trivial single-symbol lookup is a wash, so cite the **range across
task difficulty**, not one flat number. (Each agent run costs real money — budget ~$0.05–0.50
per run; the headline matrix is ~36 runs + 36 cheap judge calls.)

Contributors can still add qualitative notes (e.g. "`bounds preflight` caught a boundary break
before commit"); be specific about the task and model.

## How to contribute a result

Run `make benchmark` (or `python benchmarks/run.py`) from the repo root. Copy
`TEMPLATE.md` to `benchmarks/results/<agent>-<model>.md` (e.g.
`codex-gpt5.md`, `gemini-2.5-pro.md`), paste in the generated block (it contains
**both** the `## Mapping coverage` section and the `## Token economics` table),
fill in your environment (agent, model, tokenizer, Bounds version, date), add the
scaling observation and any agent-task-outcome notes, then open a PR adding that
single file. Don't edit other people's result files; add your own. State your
tokenizer. To submit numbers on a third-party repo instead, run
`make oss-bench REPO=<clone>` and paste its combined report.

## Results index

| Submission | Agent | Model | Tokenizer |
|------------|-------|-------|-----------|
| [`claude-baseline.md`](results/claude-baseline.md) | Claude Code | Claude (Opus) | tiktoken cl100k_base |
| [`agent-bounds-ab.md`](results/agent-bounds-ab.md) | Claude Code (`claude -p` A/B) | Claude (Sonnet) | real Anthropic usage |
| [`oss-cross-language.md`](results/oss-cross-language.md) | CLI harness | Claude (Opus 4.8) | tiktoken cl100k_base |
| [`oss-cross-language-rerun-2026-06.md`](results/oss-cross-language-rerun-2026-06.md) | CLI harness (fixed build) | N/A (zero-LLM) | tiktoken cl100k_base |
| [`oss-fresh-discover-trust.md`](results/oss-fresh-discover-trust.md) | CLI harness | N/A | tiktoken cl100k_base |
| [`oss-token-economics.md`](results/oss-token-economics.md) | (ARCHIVED — superseded) | Claude (Opus 4.8) | char/4 estimate |
