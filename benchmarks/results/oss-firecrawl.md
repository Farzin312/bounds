# Bounds benchmark — oss: firecrawl

## Environment

| Field | Value |
|-------|-------|
| Kind | `oss` |
| Repo(s) | `firecrawl` @ `222606e` (github.com/mendableai/firecrawl, shallow clone) |
| Agent | CLI harness (zero-LLM) |
| Model | N/A (zero-LLM structural extraction) |
| Tokenizer | tiktoken cl100k_base (exact) — **stated** |
| Bounds version | `bounds 2026.6.24` |
| Date | 2026-06-05 |
| Command | `make oss-bench REPO=/tmp/bench-firecrawl NAME=firecrawl LANG_LABEL=typescript` |

> Token counts are tokenizer-specific. No hardware specs — hardware is not a variable for
> token / retrieval / correctness metrics.

## Headline

On a large real-world polyglot repo (1,434 files; TS/JS core plus Java/PHP/Ruby/Rust/Go/C#/Elixir/Kotlin
SDKs), Bounds maps **90.0%** of supported non-test source with **zero leaked tracebacks** across the
full 30-command surface, fail-soft on 8 unsupported languages. Targeted retrieval saves **98.0%** of
tokens (`describe src-lib`: 9,176 tok vs 451,117 tok of source); the whole-map `bounds list`
orientation bound is 99.9% — cite the **range (~98–99.9%)**, never one flat number. This run is also
the live regression proof for two fixes shipped on this branch:

- **BOUNDS-024 (discover names):** the `.github/scripts` directory now yields a loader-valid
  `github-scripts.yaml`; `validate` reads every generated manifest with no `E_USAGE invalid subsystem
  name`. Pre-fix, discover wrote `.github-scripts.yaml`, which Bounds itself then refused to load.
- **BOUNDS-023 (calibrate orphan flood):** `calibrate --apply` now **reduces** validate issues
  (226 → 144) instead of inflating them. There are **0 `E_ORPHAN_EXPORT`** issues after the default
  apply — the flood is gone. Interface-level precision is now opt-in (see the flag matrix below).

## Results

```
# Bounds OSS report — `firecrawl` (typescript)

- Tokenizer: tiktoken cl100k_base (exact)
- Bounds version: `bounds 2026.6.24`
- Repo path: `/private/tmp/bench-firecrawl`

## Mapping coverage

Authoritative `bounds validate` metric. 3-way per source file — mapped / unowned-supported / unsupported-language (no partial tier; partial extraction is a separate signal). Tests are excluded from the denominator and tracked separately.

- Mapped source: **90.0%** (503 / 559 supported non-test source files)
- Unmapped: 56 unowned-but-supported  ·  unsupported-language: 183 dark, 20 hand-declared
- Unmapped by language: c#: 34, elixir: 5, go: 12, java: 47, kotlin: 2, php: 44, ruby: 26, rust: 33
- Tests linkage: 223 linked / 21 unlinked (of 244)
- Docs linkage: 9 linked / 72 unlinked (of 81)

## Token economics

| Metric | Value |
|--------|------:|
| Subsystems | 52 |
| `bounds list` tokens (whole-map orientation) | 4,380 |
| All-subsystem source tokens | 4,569,423 |
| Map reduction | 99.9% |
| Key subsystem | `src-lib` |
| `bounds describe src-lib` tokens | 9,176 |
| `bounds impact src-lib` tokens | 1,227 |
| `src-lib` source tokens | 451,117 |
| Surface reduction (`describe` vs source) | 98.0% |

Per-subsystem `describe` distribution (the honest spread — it scales with exposed API, not files):

| sampled | min | median | max |
|--------:|----:|-------:|----:|
| 52 | 128 | 984 | 9,176 |

> Whole-map `bounds list` reduction is the cheap-orientation bound; the repeatable, honest win is targeted retrieval (`describe`/`impact`). Cite the range, not a flat %.

## Correctness & health

| Signal | Value |
|--------|------:|
| Pre-setup (no `.bounds/`): `bounds list` rc | 2 (E_MANIFEST_NOT_FOUND) |
| Fresh `discover --apply` rc | 0 |
| `validate` issues on fresh discover | 226 (errors 214, warnings 2) |
| Clean validate on fresh discover? | False |
| Cycles | 130 |
| Schema issues | 0 |
| Command-surface invocations | 30 |
| Leaked tracebacks (crashes) | 0 |
| `calibrate --apply` -> clean validate? | False (before 226, after 144) |

Validate issue codes on fresh discover: `E_BOUNDARY_VIOLATION`, `E_COVERAGE_GAP`, `E_CYCLE_DETECTED`, `E_SCHEMA_UNPARSED`, `E_STRUCTURAL_DRIFT`
```

### Interface-precision flag matrix (BOUNDS-023 control surface, verified live on this clone)

| State | `E_ORPHAN_EXPORT` issues | manifests with interfaces |
|-------|------------------------:|--------------------------:|
| default `calibrate --apply` (bare edges) | **0** (no flood) | 0 |
| `calibrate --track-interfaces --apply` (opt-in precision) | 36 (intentional) | 42 |
| `calibrate --coarsen-interfaces --apply` (recovery) | **0** | 0 |

The default no longer auto-promotes discovered bare `consumes` edges into interface contracts, so
orphan-export checks stay off until a human opts in; `--coarsen-interfaces` fully reverses an
accidental opt-in.

## Caveats & honesty

- Tokenizer is tiktoken cl100k_base (stated above); reduction **ratios** are stable across
  tokenizers, absolute counts are not.
- The whole-map `bounds list` 99.9% reduction is a cheap-orientation upper bound — no agent reads a
  whole repo. The repeatable win is targeted `describe`/`impact` (~98% here). Cite the range.
- `validate` is **not** clean on fresh discover (226 issues: 130 cycles + boundary + coverage +
  drift). That is honest real signal on a large app repo, not a Bounds failure — it is exactly what
  the drift gate is supposed to surface for a human curation pass. Calibrate reduces it to 144 but
  does not (and is not meant to) reach zero; closing cycles and coverage gaps needs manifest/source
  edits, not calibration.
- 8 unsupported languages (Java/PHP/Ruby/Rust/Go/C#/Elixir/Kotlin SDKs) are correctly reported as
  dark coverage rather than crashing — the fail-soft contract held (0 tracebacks across 30 commands).
- The auto-`discover` partition is a draft (52 subsystems); a maintainer would merge/rename some
  before committing. That is the intended discover→curate workflow, not a defect.

## Reproduce

```
git clone --depth 1 https://github.com/mendableai/firecrawl.git /tmp/bench-firecrawl
make oss-bench REPO=/tmp/bench-firecrawl NAME=firecrawl LANG_LABEL=typescript

# Regression spot-checks (BOUNDS-023 / BOUNDS-024), from inside the clone:
bounds validate            # error:None, 0 E_ORPHAN_EXPORT, github-scripts.yaml loads
ls .bounds/manifests/ | grep -E '^\.'      # empty: no invalid dot-prefixed names
bounds calibrate --track-interfaces --apply && bounds validate   # 36 orphan-export (opt-in)
bounds calibrate --coarsen-interfaces --apply && bounds validate # back to 0 (recovery)
rm -rf /tmp/bench-firecrawl
```
