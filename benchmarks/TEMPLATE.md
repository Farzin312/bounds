# Bounds benchmark — <kind>: <discriminator>

<!--
HOW TO CONTRIBUTE A RESULT (read first)

1. Pick your benchmark KIND and run its command (table below).
2. Name your file  benchmarks/results/<kind>-<discriminator>.md  per the naming rule.
3. Fill the five standard sections (keep their order + headings). STATE YOUR TOKENIZER.
4. Open a PR that adds ONLY your one file. Never edit someone else's result file.

⭐ MOST VALUABLE CONTRIBUTION: an `oss` cross-repo result on a project we haven't
covered yet. It proves the numbers aren't self-selected. See "Repos we'd love
covered" in benchmarks/README.md — one `make oss-bench REPO=<clone>` produces a
finished block you can paste straight into the `## Results` section below.
-->

## Which benchmark am I submitting?

| Kind | Command | File name | Measures |
|------|---------|-----------|----------|
| `dogfood` | `make benchmark` | `dogfood-<model>.md` | coverage + token economics on **this** repo |
| `oss` | `make oss-bench REPO=<clone> [NAME=… LANG_LABEL=…]` | `oss-<repo-or-topic>.md` | coverage + tokens + command health on a **third-party** repo |
| `agentab` | `make agent-ab REPO=<repo> [REPS=3 MODEL=…]` | `agentab-<repo>-<model>.md` | **measured agent A/B** — with vs without Bounds (tokens, cost, time, quality) |

**Naming rule:** every result file is `<kind>-<discriminator>.md`, where `kind` is one of
`dogfood` / `oss` / `agentab` (machine-run kinds) or `submission` (a per-collaborator writeup).
Pair any raw data with its report by stem (e.g. `agentab-foo-sonnet.raw.jsonl`). Delete nothing;
add your own file.

## Environment

| Field | Value |
|-------|-------|
| Kind | `<dogfood \| oss \| agentab \| submission>` |
| Repo(s) | `<this repo, or the third-party repo + commit>` |
| Agent | `<Claude Code, Codex CLI, Gemini CLI, …>` |
| Model | `<Claude Sonnet, GPT-5, Gemini 2.5 Pro, …>` |
| Tokenizer | `<tiktoken cl100k_base (exact) \| char/4 estimate \| real provider usage>` — **REQUIRED** |
| Bounds version | `<output of bounds --version>` |
| Date | `<YYYY-MM-DD>` |
| Command | `<the exact command you ran>` |

> Token counts are tokenizer-specific — state the tokenizer. No hardware specs: hardware is not a
> variable for token / retrieval / correctness metrics (latency, if reported, is machine-relative
> and de-emphasized).

## Headline

One honest paragraph: the single takeaway a reader should leave with. For `agentab`, lead with the
measured delta (e.g. "−60% tokens, 18/18 vs 8/18 correct") **and** the scaling caveat (trivial
lookups are a wash; value scales with structural difficulty). Cite **ranges**, never one flat number.

## Results

Paste the generated block (the harness prints a finished markdown block for every kind), or the
metric table for a `submission`. Keep the tokenizer line that the harness emits.

```
<paste the harness output here>
```

## Caveats & honesty

- Tokenizer stated above; ratios are stable across tokenizers, absolute counts are not.
- The whole-map `bounds list` reduction is a cheap-orientation upper bound (no agent reads a whole
  repo) — the repeatable win is targeted `describe`/`impact`. Cite the range.
- Note anything that surprised you, any command that failed, any auto-`discover` partition you'd curate.

## Reproduce

The exact command(s) so anyone can regenerate this result:

```
<e.g. make oss-bench REPO=/tmp/clone NAME=flask LANG_LABEL=python>
```
