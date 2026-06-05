# Agent A/B benchmark — with vs without Bounds

- Repo: `supermemory` @ `ad5734c` (github.com/supermemoryai/supermemory, shallow clone)
- Agent model: `sonnet` · Judge: `sonnet`
- Bounds: `bounds 2026.6.24` (fixed local build — `fix/supermemory-calibrate-workflow`, post BOUNDS-023/024)
- Date: 2026-06-05
- Design: 6 tasks × 2 conditions × 1 reps; only variable = access to Bounds.
- Fixed-build rerun complementing the reps=3 baseline in [`agentab-supermemory-sonnet.md`](agentab-supermemory-sonnet.md);
  all 3 structural ground-truth facts (T1 file, T2 22-export list, T3 blast radius) were re-verified
  live against this clone before the run, so the comparison is fair. Reproduce:
  `python benchmarks/agent_ab.py --repo /tmp/bench-supermemory --reps 1 --model sonnet --out benchmarks/results/agentab-supermemory-sonnet-fixedbuild.md`
- Tokens/cost/time are REAL Anthropic usage from the CLI result (not estimated). The fixed Claude Code system prompt is identical overhead in both arms, so the **delta** is the signal.

## Correctness scorecard

**WITH bounds: 6/6 runs correct  ·  WITHOUT bounds: 3/6 runs correct** (correct = blind judge score ≥ 70 vs verified ground truth).
The gap is concentrated in the structural questions a grep-only agent cannot answer reliably — dependencies, blast radius, and whole-repo decomposition. A trivial single-symbol lookup (T1) is a wash; Bounds' value scales with structural difficulty.

## Headline (means across all tasks & reps)

| Metric | WITHOUT bounds | WITH bounds | Δ (with vs without) |
|--------|---------------:|------------:|--------------------:|
| Total tokens | 81,057.83 | 76,349.17 | -6% |
| Cost (USD) | 0.16 | 0.07 | -56% |
| Wall-clock (ms) | 54,861.33 | 12,175.33 | -78% |
| Turns | 3.17 | 2.5 | -21% |
| Tool calls | 28.83 | 1.5 | -95% |
| Quality (0-100) | 55 | 99.17 | +44.2 pts |

## Per-task (mean)

| Task | Diff | Cond | Tokens | Cost | ms | Turns | Tools | bounds calls | Quality | Correct |
|------|------|------|-------:|-----:|---:|------:|------:|-------------:|--------:|--------:|
| T1_locate | easy | without | 86,909 | $0.04 | 8,466 | 3 | 2 | 0 | 100 | 1/1 |
| T1_locate | easy | with | 57,013 | $0.02 | 5,754 | 2 | 1 | 1 | 100 | 1/1 |
| T2_surface | medium | without | 59,623 | $0.15 | 45,309 | 2 | 15 | 0 | 90 | 1/1 |
| T2_surface | medium | with | 89,202 | $0.08 | 13,479 | 3 | 2 | 2 | 100 | 1/1 |
| T3_impact | hard | without | 131,466 | $0.26 | 125,718 | 6 | 60 | 0 | 0 | 0/1 |
| T3_impact | hard | with | 57,324 | $0.06 | 7,880 | 2 | 1 | 1 | 100 | 1/1 |
| T4_concept | medium | without | 59,103 | $0.21 | 74,179 | 2 | 41 | 0 | 100 | 1/1 |
| T4_concept | medium | with | 100,960 | $0.11 | 20,086 | 3 | 2 | 2 | 95 | 1/1 |
| T5_deps | medium | without | 60,418 | $0.2 | 43,491 | 3 | 39 | 0 | 40 | 0/1 |
| T5_deps | medium | with | 91,178 | $0.08 | 16,272 | 3 | 2 | 2 | 100 | 1/1 |
| T6_structure | easy | without | 88,828 | $0.12 | 32,005 | 3 | 16 | 0 | 0 | 0/1 |
| T6_structure | easy | with | 62,418 | $0.07 | 9,581 | 2 | 1 | 1 | 100 | 1/1 |

## Method & caveats

- WITHOUT tools: Read, Grep, Glob (native search; no Bash, so bounds is unreachable). WITH tools: Read, Grep, Glob, Bash(bounds:*) + a system note to prefer bounds.
- A non-zero `bounds calls` count in the WITHOUT arm = the agent *tried* to run bounds (the repo's AGENTS.md recommends it) and was denied, then fell back to grep — a measured signal that agents reach for bounds when a repo advertises it.
- `total_tokens` = input + cache_read + cache_creation + output (everything the model processed/produced); USD cost already reflects Anthropic's cache discounts.
- Quality is scored 0-100 by a blind judge (not told the condition) against a verified ground-truth fact per task; the facts are pinned in `TASKS` and were derived from the fixed bounds and cross-checked by hand.
- Same-account concurrency can add latency variance; cost reflects prompt-cache discounts (less cache reuse under concurrency slightly inflates cost vs a serial run).
- Results are specific to this repo + model + date; rerun `agent_ab.py` to refresh.