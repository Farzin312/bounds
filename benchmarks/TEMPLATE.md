# Bounds benchmark result — <agent> / <model>

> Copy this file to `benchmarks/results/<agent>-<model>.md`
> (e.g. `codex-gpt5.md`, `gemini-2.5-pro.md`), fill it in, and open a PR adding
> that single file to `benchmarks/results/`. Don't edit other submissions; add
> your own. Run `make benchmark` from the repo root to generate the token table.

## Environment

| Field | Value |
|-------|-------|
| Agent | <e.g. Claude Code, Codex CLI, Gemini CLI> |
| Model | <e.g. Claude Opus, GPT-5, Gemini 2.5 Pro> |
| Tokenizer | <e.g. tiktoken cl100k_base (exact), or char/4 estimate — REQUIRED> |
| Bounds version | <output of `bounds --version`> |
| Date | <YYYY-MM-DD> |

> Token counts are tokenizer-specific. State the tokenizer. No hardware specs —
> hardware is not a variable for token / retrieval / correctness metrics.

## 1. Token economics (headline)

Paste the table emitted by `make benchmark` (`python benchmarks/run.py`):

```
<paste the full markdown output here, including the tokenizer line>
```

Aggregate reduction: **<NN>%** of source-equivalent tokens.

## 2. Retrieval scaling observation

One or two sentences: `bounds describe` is ~O(public API) and stays roughly
flat as the implementation grows, while reading source is ~O(files). Note any
observation about context-rot / lost-in-the-middle in your own runs — did
keeping context small visibly help the model stay on task?

## 3. Latency (optional, de-emphasized)

Machine-relative; report only as "fast enough / not fast enough for a pre-commit
hook (sub-200ms quick-mode target)". NO hardware spec.

| Command | Median wall-clock |
|---------|-------------------|
| `bounds validate --quick` | <ms> |
| `bounds list` | <ms> |

## 4. Determinism & correctness

Confirm (or correct) for your run: structural drift detection caught every
export add/remove/rename; ~0 false positives on whitespace/comment-only edits.
These are zero-LLM and should be model-independent.

- Drift recall: <observed>
- False positives on reformatting: <observed>

## 5. Agent task outcomes (the part that varies by model/agent)

Structured, honest notes on a real task you gave the agent, with vs without
Bounds. Be specific. Examples:

- "Asked it to add a field to `models`; with Bounds it ran `bounds impact
  models` and updated all <N> consumers in 1 pass. Without, it missed <X>."
- "`bounds preflight` caught a boundary break (importing a private symbol)
  before commit."
- "Found the right subsystem in 1 `bounds describe` call vs <N> file reads."
