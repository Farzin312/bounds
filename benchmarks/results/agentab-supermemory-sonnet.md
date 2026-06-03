# Agent A/B benchmark — with vs without Bounds

- Repo: `supermemory`
- Agent model: `sonnet` · Judge: `sonnet`
- Bounds: `bounds 2026.6.24` (fixed local build)
- Design: 6 tasks × 2 conditions × 3 reps; only variable = access to Bounds.
- Tokens/cost/time are REAL Anthropic usage from the CLI result (not estimated). The fixed Claude Code system prompt is identical overhead in both arms, so the **delta** is the signal.

## Correctness scorecard

**WITH bounds: 18/18 runs correct  ·  WITHOUT bounds: 8/18 runs correct** (correct = blind judge score ≥ 70 vs verified ground truth).
The gap is concentrated in the structural questions a grep-only agent cannot answer reliably — dependencies, blast radius, and whole-repo decomposition. A trivial single-symbol lookup (T1) is a wash; Bounds' value scales with structural difficulty.

## Headline (means across all tasks & reps)

| Metric | WITHOUT bounds | WITH bounds | Δ (with vs without) |
|--------|---------------:|------------:|--------------------:|
| Total tokens | 190,540.06 | 76,553.44 | -60% |
| Cost (USD) | 0.24 | 0.07 | -71% |
| Wall-clock (ms) | 71,503.83 | 13,231.83 | -81% |
| Turns | 9.06 | 2.44 | -73% |
| Tool calls | 24 | 1.44 | -94% |
| Quality (0-100) | 55.06 | 98.61 | +43.5 pts |

## Per-task (mean)

| Task | Diff | Cond | Tokens | Cost | ms | Turns | Tools | bounds calls | Quality | Correct |
|------|------|------|-------:|-----:|---:|------:|------:|-------------:|--------:|--------:|
| T1_locate | easy | without | 67,971.67 | $0.03 | 7,746.67 | 2.33 | 1.33 | 0 | 100 | 3/3 |
| T1_locate | easy | with | 59,227.33 | $0.06 | 7,225 | 2 | 1 | 1 | 100 | 3/3 |
| T2_surface | medium | without | 90,606.67 | $0.23 | 46,464.67 | 4.67 | 13.67 | 0 | 95.67 | 3/3 |
| T2_surface | medium | with | 92,974 | $0.08 | 16,935.67 | 3 | 2 | 2 | 100 | 3/3 |
| T3_impact | hard | without | 364,190.33 | $0.48 | 153,715.67 | 14.33 | 60.33 | 3.33 | 40 | 1/3 |
| T3_impact | hard | with | 79,587.33 | $0.07 | 14,156 | 2.67 | 1.67 | 1.67 | 100 | 3/3 |
| T4_concept | medium | without | 194,075 | $0.29 | 97,191.33 | 9 | 31.33 | 2 | 78 | 1/3 |
| T4_concept | medium | with | 103,319 | $0.1 | 19,622.67 | 3 | 2 | 2 | 93.33 | 3/3 |
| T5_deps | medium | without | 195,771.33 | $0.18 | 50,759 | 7.67 | 17 | 2 | 16.67 | 0/3 |
| T5_deps | medium | with | 60,098.67 | $0.05 | 9,273.33 | 2 | 1 | 1 | 100 | 3/3 |
| T6_structure | easy | without | 230,625.33 | $0.22 | 73,145.67 | 16.33 | 20.33 | 1.67 | 0 | 0/3 |
| T6_structure | easy | with | 64,114.33 | $0.07 | 12,178.33 | 2 | 1 | 1 | 98.33 | 3/3 |

## Method & caveats

- WITHOUT tools: Read, Grep, Glob (native search; no Bash, so bounds is unreachable). WITH tools: Read, Grep, Glob, Bash(bounds:*) + a system note to prefer bounds.
- A non-zero `bounds calls` count in the WITHOUT arm = the agent *tried* to run bounds (the repo's AGENTS.md recommends it) and was denied, then fell back to grep — a measured signal that agents reach for bounds when a repo advertises it.
- `total_tokens` = input + cache_read + cache_creation + output (everything the model processed/produced); USD cost already reflects Anthropic's cache discounts.
- Quality is scored 0-100 by a blind judge (not told the condition) against a verified ground-truth fact per task; the facts are pinned in `TASKS` and were derived from the fixed bounds and cross-checked by hand.
- Same-account concurrency can add latency variance; cost reflects prompt-cache discounts (less cache reuse under concurrency slightly inflates cost vs a serial run).
- Results are specific to this repo + model + date; rerun `agent_ab.py` to refresh.