# OSS cross-repo re-run — fixed build (2026-06)

A focused 8-repo regression sweep run with the **fixed** local build (miss-recovery +
description-coverage + the `_lib` coverage-rendering fix). Purpose: confirm Bounds runs across
diverse real codebases without regression, that the coverage `?`-rendering bug stays fixed, and that
unsupported languages fail soft. Complements the larger [16-repo sweep](oss-cross-language.md) (which
stays as-is).

- Build: `bounds 2026.6.24`, branch `feat/miss-recovery-hints`
- Tokenizer: tiktoken cl100k_base (exact) · Date: 2026-06-03
- Harness: `make oss-bench REPO=<clone>` (`oss_report.py` = `oss_bench.py` + `oss_features.py`)
- Zero-LLM, no API cost. Each repo shallow-cloned, benchmarked, then deleted (one at a time).

## Results

| repo | lang | subsystems | mapped % | mapped/total | describe median tok | key reduction % | discover rc | validate issues (fresh discover) | crash |
|------|------|-----------:|---------:|-------------:|--------------------:|----------------:|------------:|----------------------------------|:-----:|
| click | python | 2 | 60.6% | 20/33 | 3,540 | 92.5% | 0 | 1 warn (`E_COVERAGE_GAP`) | N |
| flask | python | 5 | 86.5% | 32/37 | 917 | 94.2% | 0 | 6 err / 1 warn (boundary, cycle×2, drift, coverage) | N |
| requests | python | 1 | 86.4% | 19/22 | 5,281 | 89.4% | 0 | 1 warn (`E_COVERAGE_GAP`) | N |
| zod | typescript | 17 | 100.0% | 238/238 | 248 | 65.6% (`core`) | 0 | 4 (0 err) `E_STRUCTURAL_DRIFT` | N |
| axios | javascript | 6 | 100.0% | 87/87 | 276 | 99.0% (`root`) | 0 | 2 err `E_BOUNDARY_VIOLATION`, `E_STRUCTURAL_DRIFT` | N |
| express | javascript | 4 | 32.0% | 16/50 | 301 | 95.2% (`lib`) | 0 | 1 warn (`E_COVERAGE_GAP`) | N |
| cobra | go | 0 | n/a (unsupported) | — | — | — | 2 (fail-soft) | — | N |
| fd | rust | 0 | n/a (unsupported) | — | — | — | — (no subsystems) | — | N |

`describe` distribution highlights (the spread is real, tracks exposed API): zod min 89 / median 248
/ **max 40,627** (`core` is a huge public surface; `describe` still cuts it 65.6% vs 117,947 source
tokens). axios `root` `describe` 2,399 tok vs 235,745 source (99.0%). `impact <key>` was a flat
~130 tokens everywhere.

## What this confirms

- **No regression, no crashes.** 30-command surface exercised on each supported repo; **0 leaked
  Python tracebacks** — the fail-soft contract holds.
- **Coverage rendering is fixed.** Every repo reports real `mapped %` + `mapped/total` (and, for
  express, the by-tier unmapped breakdown: 34 unowned-but-supported, 0 dark). No `?` placeholders —
  the recently-fixed flat-key bug did not recur.
- **Unsupported languages fail soft.** Go (cobra) and Rust (fd): 0 subsystems, exit 0, a structured
  note (Go's `init`/`discover` emit a well-formed `E_USAGE` JSON, not an exception), no crash. This is
  the most important pass — an unsupported language degrades gracefully.
- **Token economics replicate** across languages: whole-map `list` ~99.7–99.9% smaller than all
  source; targeted `describe` 65–99% smaller than its subsystem's source (lower on fat-API subsystems
  like zod `core` — exactly why we cite the range, never one flat number).

## Honest findings (NOT regressions — pre-existing `calibrate --apply` quality)

The broader sweep surfaced two `calibrate --apply` convergence problems on repos with awkward
auto-discovered partitions. Neither is caused by the changes on this branch (which don't touch
`calibrate`), and neither is a fail-soft violation — but they mean **`calibrate --apply` is not a
safe one-shot fixer on cycle-heavy or flat-layout repos**:

- **flask** (cycle-heavy): `calibrate --apply` roundtrip took validate from 8 → **145** issues.
- **axios** (flat single-`lib/` layout, the only fresh-discover `E_BOUNDARY_VIOLATION`):
  `calibrate --apply` took validate from 4 → **30** issues.

By contrast click/requests/zod/express stayed flat or converged downward. This matches the README's
existing "auto-`discover` contracts are a starting draft to curate" framing, but the magnitude on
flask/axios is worth a dedicated calibrate-convergence investigation (tracked separately, not on this
branch). express's 32% mapped is legitimately low (flat layout leaves 34 supported files unowned on
fresh discover → the `E_COVERAGE_GAP` warning) — honest expected behavior, not a bug.
