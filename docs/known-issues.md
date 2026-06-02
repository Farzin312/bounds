# Known issues & bug registry

The single, durable home for bugs in Bounds — for humans **and** AI agents. The cross-language
benchmark ([benchmarks/results/oss-cross-language.md](../benchmarks/results/oss-cross-language.md))
is how several of these were *found*; this file is where they are *tracked and fixed*. Goal:
deterministic, honest, 100%-mapping behavior. A bug here is a commitment to fix, not an accepted
limitation.

## How this registry works

Every bug gets a stable `BOUNDS-NNN` id (never reused). An entry records **what, severity, when/how
found, status, where fixed (branch + commit), root cause, and the test that locks the fix in.** When
a bug is fixed, its status flips to **Fixed** with the branch/commit — it is never deleted, so the
history stays auditable.

**Status:** `Open` · `Fixed` · `Mitigated` (partly addressed; residual tracked) · `Won't-fix`
(with a reason). **Severity:** `high` (wrong/contradictory output or silent data loss) · `medium`
(misleading or noisy on real repos) · `low` (cosmetic/edge).

### Filing a bug (humans and AI)

1. Reproduce it minimally (ideally a `tmp_path` fixture; see [testing.md](testing.md)).
2. Add the next `BOUNDS-NNN` entry below using the template, with a concrete repro.
3. Add a **failing** regression test that asserts the *correct* behavior. If you are not fixing it
   now, mark the test `@pytest.mark.xfail(strict=True, reason="BOUNDS-NNN: …")` so it documents the
   defect and auto-detects the fix (a strict xfail that starts passing becomes a build failure — the
   signal to remove the marker). Prefer fixing over parking.
4. When you fix it: make the test pass, remove any xfail, flip the entry to **Fixed** with the branch
   + commit and the test path.

AI agents working in this repo: treat a benchmark finding or a user-reported defect as a registry
entry, not a one-off — add it here, with a test, in the same change.

### Entry template

```
### BOUNDS-NNN — <one-line title>
- **Severity / Status:** <sev> / <status>
- **Found:** <YYYY-MM-DD> via <how> (e.g. cross-language benchmark on <repos>)
- **Affected:** <commands / repos / shapes>
- **Symptom:** <what the user sees>
- **Root cause:** <file:line / mechanism>
- **Fix:** <branch @ commit, or "proposed: …">
- **Test:** <tests/…::test_…>
```

---

## Registry

### BOUNDS-001 — `discover` and `validate` disagree on identical source (nested-path ownership)
- **Severity / Status:** high / **Fixed**
- **Found:** 2026-06-01 via cross-language benchmark (click, flask, chalk, fastapi, zod, nest, …).
- **Affected:** `validate`/`preflight`/`calibrate`/`where`/`impact` on any repo where one subsystem's path nests inside another's.
- **Symptom:** a fresh `discover → validate` reported `E_STRUCTURAL_DRIFT` ("declares X but no source file exports it") for symbols `describe` simultaneously marked `[verified]` — the tool contradicting itself on unchanged source (click: 20 false drifts; flask: ~133).
- **Root cause:** ownership was "first declared owner wins" over `sorted(subsystems)` (`engine.py`, `scan.py`), so an alphabetically-earlier *parent* swallowed a nested child's files, starving the child.
- **Fix:** `agent-plugins-aliases-benchmark` — centralized ownership in `scan.resolve_owners` with **most-specific-path-wins** (deepest declared path owns the file); `engine.py` + `extract_project` both use it. Verified: click structural-drift 20→0, flask ~133→11.
- **Test:** `tests/validate/test_regression_nested_paths.py`.

### BOUNDS-002 — `discover` wrote `consumes` edges to subsystems it never materialized
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via benchmark (click `→complex`, requests `→testserver`, express, flask, fastapi, nest).
- **Symptom:** a fresh `discover → validate` raised self-inflicted `E_UNRESOLVED_REFERENCE` ("consumes unknown subsystem 'complex'").
- **Root cause:** a consume-edge could point at a low-score candidate that was dropped (not kept), so the referenced subsystem was never written.
- **Fix:** `agent-plugins-aliases-benchmark` — `discover` filters each candidate's `consumes` to the final kept set before writing (`discover.py`). Verified: click `E_UNRESOLVED_REFERENCE` 1→0.
- **Test:** covered by `tests/discover/test_discover.py` (consume-edge tests); fresh-discover-validate-clean asserted via the click re-measure.

### BOUNDS-003 — `init`/`discover` hardcoded `languages: [python]`
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via benchmark (documenso — pure TS/Prisma — and every unsupported-language repo declared `languages: [python]`).
- **Root cause:** `cli.py` `_ROOT_TEMPLATE` wrote a static `[python]` and `discover` never overwrote it.
- **Fix:** `agent-plugins-aliases-benchmark` — `discover` derives `languages` from extracted source and writes it authoritatively (`discover.py`). Verified: chalk→`[typescript]`, requests→`[python]`.
- **Test:** `tests/discover/test_discover.py::test_discover_overwrites_hardcoded_python_default_for_ts`.

### BOUNDS-004 — tsconfig overlapping `paths` aliases ignored TypeScript's longest-prefix rule
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via external code review (chatgpt-codex-connector) on `src/bounds/tsconfig.py`.
- **Symptom:** with overlapping `@/*` and `@/foo/*`, an import like `@/foo/bar` resolved to the broad `@/*` target (patterns were sorted alphabetically; `*` sorts before `f`), misattributing imports across subsystems in `boundary`/`discover`/`where`.
- **Root cause:** `_compile` stored patterns with `sorted(patterns.items())` and `candidate_stems` emitted them in that order.
- **Fix:** `agent-plugins-aliases-benchmark` — `_pattern_specificity` orders patterns most-specific first (exact, then longest literal prefix before `*`, then longest suffix, then the pattern string), matching TypeScript.
- **Test:** `tests/extract/test_tsconfig.py::test_overlapping_aliases_prefer_longest_prefix`, `::test_exact_alias_wins_over_wildcard`, `::test_resolve_prefers_specific_alias_target`.

### BOUNDS-005 — polyglot repos silently mapped only the supported languages
- **Severity / Status:** high / **Fixed**
- **Found:** 2026-06-01 via benchmark + design review (a TS-repo-with-Go looked "fully mapped" while the Go was invisible).
- **Symptom:** no command reported how much source went unmapped; a partial map looked complete.
- **Root cause:** every walk/owner was scoped to supported extensions; there was no repo-wide source denominator.
- **Fix:** `agent-plugins-aliases-benchmark` — `scan.mapping_coverage` (mapped %, by-language breakdown of the unmapped) feeds `validate` (`stats.coverage.mapping` + a loud non-blocking `E_COVERAGE_GAP` with a next step) and `discover` (`coverage` + `next_step`). See [coverage.md](coverage.md).
- **Test:** `tests/validate/test_coverage.py`.

### BOUNDS-006 — `discover` can emit overlapping subsystem paths with no diagnostic
- **Severity / Status:** medium / **Mitigated**
- **Found:** 2026-06-01 via benchmark (lodash/zod `root=.` catch-alls; nested package dirs).
- **Status detail:** BOUNDS-001 makes nesting *correct* (deepest path owns the file), so the false drift is gone. **Residual (Open):** a genuine same-path conflict still has no warning, and `describe`'s `file_count` can double-count a file that a more-specific sibling now owns.
- **Fix:** proposed — a manifest-load overlap/ambiguity diagnostic; make `describe` file counting use `scan.resolve_owners` so it agrees with `validate`.
- **Test:** `tests/validate/test_regression_nested_paths.py` covers the ownership half; describe-consistency test still to add.

### BOUNDS-007 — `bounds where <file-path>` returns 0 results for a manifest-owned file
- **Severity / Status:** low / **Mitigated**
- **Found:** 2026-06-01 via benchmark (click `tests/typing/…`, zod `packages/docs/…`).
- **Status detail:** the underlying ownership starvation is fixed by BOUNDS-001 (the file's *symbols* are now findable via `where <symbol>`). **Residual (Open):** `where` takes a symbol by default; passing a file path returns 0, which is a confusing UX even when correct.
- **Fix:** proposed — detect a path-shaped argument and report the owning subsystem, or document the symbol-only contract more loudly.

### BOUNDS-008 — TS `export type`/re-export under-detection + symbol `kind`/`file` mislabels
- **Severity / Status:** medium / **Open** (now the leading residual drift after BOUNDS-001/012)
- **Found:** 2026-06-01 via benchmark; isolated as the dominant remaining `E_STRUCTURAL_DRIFT` source on TS repos (zod `core` ~165, axios ~22, chalk ~4) — `discover` declares a TS `export type`/`export { } from` symbol in `exposes` that `validate`'s extractor doesn't re-detect as exported. Plus kind mislabels (fastapi `Body` function→class file; nest `HttpStatus` enum→const; chalk `export class Chalk`→const).
- **Symptom:** residual false structural drift on TS type-heavy modules; imprecise `kind`/`file` can misdirect navigation.
- **Root cause:** `extract/typescript.py` export/kind detection for `export type`, `export interface`, `enum`, re-exports, and overloaded names.
- **Fix:** proposed — tighten TS type/re-export export detection so `discover` and `validate` agree; add per-adapter tests. This is the next correctness lever for clean fresh-discover on TS repos.

### BOUNDS-009 — `overview` reports `health.ok=true` while `validate` reports errors
- **Severity / Status:** low / **Open**
- **Found:** 2026-06-01 via benchmark (date-fns ok=true with 948 validate errors; lodash).
- **Root cause:** `overview` health only counts schema errors + cycles, not drift/boundary.
- **Fix:** proposed — fold a lightweight validate summary (or the coverage %) into `overview` health, or rename the field so it doesn't read as "the project is healthy."

### BOUNDS-010 — `describe` JSON omits the file list its `--human`/`--full` view shows
- **Severity / Status:** low / **Open**
- **Found:** 2026-06-01 via benchmark (lodash, date-fns).
- **Symptom:** `describe` JSON has `file_count` but `files: []`, while `--human`/`--full` show the list — violating the JSON-first invariant (`--human` must not expose what the JSON omits).
- **Fix:** proposed — populate `files` in JSON (or only at `--full`, consistently across both renderings).

### BOUNDS-011 — `validate` exits `0` on a fatal `E_MANIFEST_NOT_FOUND`
- **Severity / Status:** low / **Open**
- **Found:** 2026-06-01 via benchmark (chalk, after a no-op dry-run `discover`).
- **Symptom:** `validate` printed a fatal error object but exited `0`, which can mislead CI into reading a missing-manifest fatal as success.
- **Fix:** proposed — return the fatal exit code (2) on the fatal-error path.

### BOUNDS-012 — `E_ORPHAN_EXPORT` floods on libraries (every public export looks orphaned)
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via benchmark (click 183, fastapi 2,112, zod 1,751).
- **Symptom:** a library's public API is consumed by external users, not a sibling subsystem, so every export read as "consumed by nothing" — the dominant noise on the most common use case (point Bounds at a library).
- **Root cause:** `discover` records `consumes` edges at *subsystem* granularity with no `interfaces` (`discover.py`), but `check_orphans` judged orphans *per interface* — so with no interface data, every export looked unconsumed.
- **Fix:** `agent-plugins-aliases-benchmark` — `check_orphans` now only judges orphans for subsystems that have **interface-level** consumption recorded (curated contracts); subsystem-granularity edges (what `discover` emits) no longer flood. Verified: click orphan 183→0 (total validate issues 206→3, `ok: True`), flask 314→19, requests 146→6, express 53→1, zod 3,025→166.
- **Test:** `tests/validate/test_validate.py::test_orphans_not_flagged_without_interface_level_consumption`.

### BOUNDS-013 — fresh `discover → validate` was far from clean on real repos
- **Severity / Status:** medium / **Mitigated**
- **Found:** 2026-06-01 via benchmark (0/13 supported repos validated clean after a fresh discover; calibrate didn't converge).
- **Status detail:** BOUNDS-001/002/012 together collapsed the issue counts on a *fresh* discover (no calibrate): click 206→3 (`ok: True`), flask 314→19, requests 146→6, express 53→1, axios 191→59, zod 3,025→166. The floods are gone. **Residual (Open):** TS type-export extraction drift (BOUNDS-008, e.g. zod's `core`) and boundary edges on auto-drawn partitions. These are smaller and either genuine or tracked under BOUNDS-008.
- **Fix:** continue with BOUNDS-008; re-run the corpus convergence numbers as each lands.

### BOUNDS-014 — `discover` listed every `test_*` case as a public export (manifest bloat)
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via a manifest-size review (a 397-test dir produced an 818-line `tests.yaml` `exposes`).
- **Symptom:** a test subsystem's `exposes` listed every `test_*` function / `Test*` class — symbols a test runner finds by convention, that nothing imports — bloating the manifest (and `describe` token cost) and misrepresenting them as a public surface.
- **Root cause:** `discover._exposes_for` emitted every exported, non-private symbol regardless of whether the file was a test file.
- **Fix:** `agent-plugins-aliases-benchmark` — `_exposes_for` excludes `test_*` functions and `Test*` classes in test files (`_is_test_file`/`_is_test_symbol`), keeping genuine helpers. Verified: click `tests.yaml` 818→70 lines (exposes 397→31).
- **Test:** `tests/discover/test_discover.py::test_discover_excludes_test_cases_from_exposes`.

---

See also: [coverage.md](coverage.md) (the mapping-coverage metric + how to close a gap),
[testing.md](testing.md) (how to write the regression test for a fix),
[../benchmarks/results/oss-cross-language.md](../benchmarks/results/oss-cross-language.md) (how these
were found and the corpus they were found on).
