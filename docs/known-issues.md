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
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via benchmark (lodash/zod `root=.` catch-alls; nested package dirs).
- **Symptom:** `describe`'s `file_count` double-counted a file a more-specific sibling subsystem now owns (disagreeing with `validate`); a genuine same-path conflict had no warning.
- **Root cause:** `describe.extract_owned` walked the subsystem's own paths blindly (`scan.iter_subsystem_files`) instead of resolving ownership.
- **Fix:** `gen9-correctness-mapping-100pct` — `describe._owned_files` reuses `scan.resolve_owners` (most-specific-path-wins), so `describe`'s `file_count`/`files` agree with `validate`; `describe.subsystem_overlaps` flags a genuine *equal-specificity* same-path conflict as a non-fatal `E_SUBSYSTEM_OVERLAP` warning (new append-only code) in the describe payload, with a fix hint. Nested paths (different specificity) are correctly never flagged.
- **Test:** `tests/validate/test_regression_describe_ownership.py` (no-double-count, agrees-with-resolve_owners, nested-not-flagged, same-path-emits-overlap, code-registered).

### BOUNDS-007 — `bounds where <file-path>` returns 0 results for a manifest-owned file
- **Severity / Status:** low / **Fixed**
- **Found:** 2026-06-01 via benchmark (click `tests/typing/…`, zod `packages/docs/…`).
- **Symptom:** `where` took a symbol by default; passing a file path returned 0 results — confusing UX even when correct.
- **Root cause:** `locate.run_where` only ever matched its argument against symbol names.
- **Fix:** `gen9-correctness-mapping-100pct` — `run_where` detects a path-shaped argument (`_path_query`: contains `/`, or matches an existing repo-relative source file by posix compare) and returns the owning subsystem plus every symbol that file defines (`_where_file`, `query_kind: "file"`), reusing the shared ownership map so it agrees with `validate`/`describe`. Symbol lookups unchanged; `cli.py` untouched (detection lives in `run_where`).
- **Test:** `tests/cli/test_where_path_arg.py` (symbol unchanged, path reports owner+symbols, bare existing filename treated as path, nonexistent symbol stays a symbol query, distinct human render).

### BOUNDS-008 — TS `export type`/re-export under-detection + symbol `kind`/`file` mislabels
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via benchmark; isolated as the dominant remaining `E_STRUCTURAL_DRIFT` source on TS repos (zod `core` ~165, axios ~22, chalk ~4) — `discover` declares a TS `export type`/`export { } from` symbol in `exposes` that `validate`'s extractor doesn't re-detect as exported. Plus kind mislabels (`enum`→`const`; an overload/class symbol pointing at the wrong file).
- **Symptom:** residual false structural drift on TS type-heavy modules; imprecise `kind`/`file` can misdirect navigation.
- **Root cause:** `extract/typescript.py` mapped `enum_declaration`→`const`, gave every `export { }` specifier `kind: unknown`, and emitted one symbol per overload signature; barrel re-exports attributed a name to the re-export file rather than its declaration.
- **Fix:** `gen9-correctness-mapping-100pct` — `enum_declaration`→`enum`; `function_signature` added + `_dedup_symbols` collapses an overload set to one `function`; `export * as ns`→`namespace`; a *local* re-export (`export { X }`, no `from`) resolves `X`'s kind from its in-file declaration (`_local_decl_kinds`) while a *cross-module* re-export (`export { A } from "./m"`) stays exported with `kind: unknown` (declaration is in another file); `export * from "./m"` emits no symbol (only the import edge), so `discover` never declares an unresolvable star name — keeping the discover/validate extractor **symmetric**. `discover._exposes_for` now prefers a real declaration over a bare `unknown` re-export on a name collision (correct `kind`/`file`). Symbol `kind` is advisory (drift is name-matched) but now derived correctly wherever the declaration is in-file.
- **Residual:** a cross-module re-export's `kind` is `unknown` by design (single-file extractor; the true kind lives in another file) — harmless for drift/boundary/contract checks.
- **Test:** `tests/extract/test_typescript_exports.py` (per-form kind/file/exported) + `tests/discover/test_ts_discover_validate_symmetry.py` (fresh discover→validate on a type-heavy TS package yields zero `E_STRUCTURAL_DRIFT`).

### BOUNDS-009 — `overview` reports `health.ok=true` while `validate` reports errors
- **Severity / Status:** low / **Fixed**
- **Found:** 2026-06-01 via benchmark (date-fns ok=true with 948 validate errors; lodash). Residual found 2026-06-02 while testing a large TypeScript backend: `overview` still read `ok` with error-severity validation issues when `enforce=off`.
- **Root cause:** `overview` health only counted schema errors + cycles, not drift/boundary.
- **Fix:** `gen9-correctness-mapping-100pct` — `overview_cmd` folds a real validation pass (`validate_engine.run(..., persist=False)`, reusing the content-hash cache, spinner-wrapped) into `health.ok`. The 2026-06-02 hardening made `health.ok` depend on zero error-severity validation issues, not `ValidationReport.ok`, because `report.ok` intentionally stays true under `enforce=off`. The JSON gains `health.validation` (errors/warnings/drift/boundary/ownership-overlap/contract/stale + `mapped_pct`); the human view renders the same line. Off the `--quick` budget path.
- **Test:** `tests/cli/test_cli.py::test_overview_health_reflects_drift`, `::test_overview_health_reflects_error_severity_drift_even_when_enforce_off`, `::test_overview_health_clean_when_no_drift`.

### BOUNDS-010 — `describe` JSON omits the file list its `--human`/`--full` view shows
- **Severity / Status:** low / **Fixed**
- **Found:** 2026-06-01 via benchmark (lodash, date-fns).
- **Symptom:** `describe` JSON had `file_count` but `files: []`, while `--human`/`--full` showed the list — violating the JSON-first invariant (`--human` must not expose what the JSON omits).
- **Fix:** `gen9-correctness-mapping-100pct` — `describe_one` populates `files` in the JSON under the *same* `--full` gate the human renderer keys off, so the two views are byte-consistent: default stays token-lean (count only), `--full` shows the roster in JSON and human alike. The roster is the `scan.resolve_owners`-based sorted list (see BOUNDS-006).
- **Test:** `tests/validate/test_regression_describe_ownership.py::test_describe_files_json_human_parity`.

### BOUNDS-011 — `validate` exits `0` on a fatal `E_MANIFEST_NOT_FOUND`
- **Severity / Status:** low / **Fixed** (regression-guarded; not reproducible on current `main`)
- **Found:** 2026-06-01 via benchmark (chalk, after a no-op dry-run `discover`).
- **Symptom:** reported as `validate` printing a fatal error object but exiting `0`, which could mislead CI.
- **Status detail:** on current code every fatal missing-manifest path already exits `2` (`_run` catches `BoundsError` → `sys.exit(config.EXIT_FATAL)`), including after a no-op dry-run `discover` — verified empirically and by test. The original report likely came from a wrapper/shell that swallowed the code. Locked with regression guards so it cannot silently regress.
- **Fix:** `gen9-correctness-mapping-100pct` — regression tests asserting exit `2` on the fatal path (including after a dry-run discover) and exit `0`/blocked on the normal path.
- **Test:** `tests/cli/test_cli.py` (validate fatal-exit guards).

### BOUNDS-012 — `E_ORPHAN_EXPORT` floods on libraries (every public export looks orphaned)
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via benchmark (click 183, fastapi 2,112, zod 1,751).
- **Symptom:** a library's public API is consumed by external users, not a sibling subsystem, so every export read as "consumed by nothing" — the dominant noise on the most common use case (point Bounds at a library).
- **Root cause:** `discover` records `consumes` edges at *subsystem* granularity with no `interfaces` (`discover.py`), but `check_orphans` judged orphans *per interface* — so with no interface data, every export looked unconsumed.
- **Fix:** `agent-plugins-aliases-benchmark` — `check_orphans` now only judges orphans for subsystems that have **interface-level** consumption recorded (curated contracts); subsystem-granularity edges (what `discover` emits) no longer flood. Verified: click orphan 183→0 (total validate issues 206→3, `ok: True`), flask 314→19, requests 146→6, express 53→1, zod 3,025→166.
- **Test:** `tests/validate/test_checks.py::test_orphans_not_flagged_without_interface_level_consumption`.

### BOUNDS-013 — fresh `discover → validate` was far from clean on real repos
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via benchmark (0/13 supported repos validated clean after a fresh discover; calibrate didn't converge).
- **Status detail:** BOUNDS-001/002/008/012/015/016 together collapse the issue counts on a *fresh* discover (no calibrate). Re-measured 2026-06-01 on `gen9-correctness-mapping-100pct` (tiktoken; `init → discover --apply → validate`, all `ok: True`):
  - **click** 206→**1** (coverage-gap only; 0 drift, 0 boundary) — note BOUNDS-015 was required to hold this: it had silently regressed to 479 (476 test-case drifts) after BOUNDS-014. BOUNDS-018 removed the final false boundary errors from tests importing private internals.
  - **requests** 146→**6** · **flask** 314→**19** · **express** 53→**1** · **axios** 191→**59** · **zod** 3,025→**155**.
  The library-source mapping is 100% on well-factored repos (click, axios) and is reported honestly with a fix hint elsewhere. **Residual (genuine, mostly info-severity):** auto-drawn boundary edges (axios 37, flask 5), TS cross-module re-export `kind: unknown` (advisory), and app-local Next.js component exports (BOUNDS-016 residual). All non-blocking.
- **Fix:** **Fixed** — the floods (orphan, test-case, framework) are eliminated; remaining issues are genuine or advisory. Convergence numbers above are the current honest baseline.

### BOUNDS-014 — `discover` listed every `test_*` case as a public export (manifest bloat)
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via a manifest-size review (a 397-test dir produced an 818-line `tests.yaml` `exposes`).
- **Symptom:** a test subsystem's `exposes` listed every `test_*` function / `Test*` class — symbols a test runner finds by convention, that nothing imports — bloating the manifest (and `describe` token cost) and misrepresenting them as a public surface.
- **Root cause:** `discover._exposes_for` emitted every exported, non-private symbol regardless of whether the file was a test file.
- **Fix:** `agent-plugins-aliases-benchmark` — `_exposes_for` excludes `test_*` functions and `Test*` classes in test files (`_is_test_file`/`_is_test_symbol`), keeping genuine helpers. Verified: click `tests.yaml` 818→70 lines (exposes 397→31).
- **Test:** `tests/discover/test_discover.py::test_discover_never_promotes_test_dirs_to_subsystems` (stronger guard: discover now links tests instead of creating generated test subsystems; see BOUNDS-020).

### BOUNDS-015 — test cases re-surfaced as `E_STRUCTURAL_DRIFT` "undeclared export" noise (BOUNDS-014 regression)
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-01 via post-fix OSS re-measurement (click showed **476** structural-drift issues on a fresh `discover → validate`, contradicting the documented `206→3`).
- **Symptom:** every `test_*` function / `Test*` class in a test subsystem produced an info-severity `E_STRUCTURAL_DRIFT` ("subsystem 'tests' exports 'test_…' which is not declared in exposes") — hundreds per repo — flooding `validate` output (click 476, every repo with a tests dir affected). `ok` stayed `true` (info severity) but the noise re-created exactly what BOUNDS-014 removed.
- **Root cause:** asymmetry introduced by BOUNDS-014. Discover *excludes* test cases from `exposes`, but `validate.checks.check_structural_drift`'s "undeclared public surface" branch still compared the full exported set (which includes `test_*`) against the (test-case-free) `exposes`, flagging each test case. The prior session measured BOUNDS-014 by manifest size, not by re-running `validate`, so the drift regression slipped in.
- **Fix:** `gen9-correctness-mapping-100pct` — `check_structural_drift` now excludes test cases symmetrically, using the shared `scan.is_test_file`/`scan.is_test_symbol` predicates (the same ones discover uses), gated on the file actually being a test file (a `test_*`-named symbol in non-test source still flags). Verified: click structural-drift **476→0**; later BOUNDS-018 removed the remaining test-private-import boundary false positives, leaving fresh `discover → validate` at 1 issue (`E_COVERAGE_GAP` only).
- **Test:** `tests/validate/test_checks.py::test_drift_excludes_test_cases_from_undeclared_export_noise`, `::test_drift_test_named_symbol_in_non_test_file_still_flags`.

### BOUNDS-016 — Next.js framework-entry exports flood `E_STRUCTURAL_DRIFT` (undeclared-export noise)
- **Severity / Status:** medium / **Fixed** (entry-file conventions; app-local component exports remain by design)
- **Found:** 2026-06-01 via post-fix OSS re-measurement (zod's bundled docs site produced ~165 info drifts: `GET`, `Page`, `generateMetadata`, `generateStaticParams`, `revalidate`, `dynamic`, `config`, `layout`…). Directly relevant to the Next.js + Postgres/Supabase target stack.
- **Symptom:** every Next.js App-/Pages-Router **entry file** (`page`/`layout`/`route`/…) exports symbols the framework invokes by convention (the default component, `GET`/`POST` route handlers, route-segment config). Nothing imports them, yet the drift check flagged each as an "undeclared export" — the same noise class as test cases (BOUNDS-015), hitting every real Next.js app.
- **Root cause:** the undeclared-surface drift branch (and discover's `exposes`) treated framework-invoked entry exports as a consumable surface.
- **Fix:** `gen9-correctness-mapping-100pct` — `scan.is_framework_entry_file(rel)` recognizes a Next.js special file (`page`/`layout`/`loading`/`error`/`route`/`middleware`/… ) **only** when it sits under an `app/` or `pages/` segment (so an ordinary `lib/route.ts` is never mistaken for a route entry). Such a file's exports are excluded symmetrically from discover's `_exposes_for` and from `check_structural_drift`'s undeclared-surface branch. Verified: zod 166→155 issues (the framework-callback drifts gone).
- **Residual (by design):** app-local **component** exports in ordinary `.tsx` files (e.g. `BlogCard`) are real module exports and still surface as info-drift; silencing them would risk hiding a genuine surface. A user curates `exposes` or `.boundsignore`s a bundled site.
- **Test:** `tests/validate/test_checks.py::test_drift_excludes_nextjs_framework_entry_exports`, `::test_drift_route_file_outside_app_dir_still_flags`.

### BOUNDS-017 — duplicate same-path subsystem ownership is too hidden
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-02 while testing Bounds on a large TypeScript backend (two generated subsystems claimed the same source directory; only `describe --full` surfaced overlap warnings).
- **Symptom:** `validate` and `overview` could show massive drift on a losing duplicate subsystem without explaining the underlying ownership conflict. Users had to guess which subsystem to inspect with `describe --full`.
- **Root cause:** `E_SUBSYSTEM_OVERLAP` diagnostics lived in `describe.subsystem_overlaps` only. The validation engine used the same most-specific-path-wins owner map, but did not report equal-specificity ties in the normal health path.
- **Fix:** equal-specificity overlap detection moved beside `extract.scan.resolve_owners` as private validation plumbing. `describe --full` still gets per-file diagnostics; `validate` gets aggregated warnings so broad duplicate paths do not flood output; `overview` reports `ownership_overlaps` in the validation summary.
- **Test:** `tests/validate/test_engine.py::test_engine_surfaces_equal_specificity_path_overlap`, `tests/validate/test_engine.py::test_engine_quick_surfaces_equal_specificity_path_overlap`, `tests/cli/test_cli.py::test_overview_counts_ownership_overlaps`.

### BOUNDS-018 — test files importing internals trigger false production boundary errors
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-02 via `benchmarks/oss_bench.py` on a fresh temp clone of `pallets/click`.
- **Affected:** `validate`, `overview`, benchmark health on repos where tests intentionally import private implementation symbols.
- **Symptom:** fresh `bounds init --root && bounds discover --apply && bounds validate` on Click reported 2 `E_BOUNDARY_VIOLATION` errors because `tests/test_parser.py` imported `_OptionParser` and `tests/test_stream_lifecycle.py` imported `_NamedTextIOWrapper`. The model looked broken immediately after discover even though tests are allowed to exercise internals.
- **Root cause:** `check_boundary` applied production public-API boundary rules to files recognized as tests. Bounds already treats tests separately for coverage and structural drift, but boundary compliance did not share that rule.
- **Fix:** `check_boundary` now skips files matched by the shared `is_test_file` predicate. Tests still contribute ownership, dependency, and coverage signals; they no longer turn private test imports into production boundary failures. Verified on Click: fresh validation errors **2→0**, boundary violations **2→0**, leaving only the honest `E_COVERAGE_GAP` warning.
- **Test:** `tests/validate/test_checks.py::test_boundary_allows_test_files_to_import_internals`.

### BOUNDS-019 — `discover --apply` can create duplicate manifests in already-bounded repos
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-02 while dogfooding Bounds on its own repo after adding benchmark tests.
- **Affected:** `bounds discover --apply` on repos that already have a curated `.bounds/` model.
- **Symptom:** re-running discover proposed alternate generated subsystems over files already owned by existing manifests, including test directories already linked through `tests:`. Keeping those files would make the architecture model larger and less trustworthy instead of improving coverage.
- **Root cause:** discovery grouped every supported source file from scratch, while `_write` merged candidates into existing `root.yaml`. That made `discover` act like a second bootstrap pass instead of a gap-filling pass.
- **Fix:** when a Bounds model already exists, `discover` loads it, resolves current source ownership, resolves linked tests, filters those files out of candidate discovery, and extracts existing-owned files only for import resolution. It now adds only unmapped source; if there is none, it writes nothing and points users to `bounds calibrate` for drift reconciliation.
- **Test:** `tests/discover/test_discover.py::test_discover_existing_model_does_not_create_duplicate_subsystems`, `::test_discover_existing_model_adds_only_unmapped_source`.

### BOUNDS-020 — `discover` can promote tests/docs into misleading architecture coverage
- **Severity / Status:** medium / **Fixed**
- **Found:** 2026-06-02 while dogfooding `bounds discover --apply` and auditing generated `.bounds/` output for open-source release quality.
- **Affected:** `bounds discover`, fresh generated manifests, docs/tests coverage UX.
- **Symptom:** high-volume `tests/` trees could become candidate subsystems, even though the rest of Bounds treats tests as coverage links for source subsystems. Separately, a single `tests/test_auth.py` or `docs/auth.md` could collapse to a broad `tests` or `docs` link, overclaiming future unrelated files.
- **Root cause:** discovery grouped all supported files before applying the tests/docs ownership model, and link collapsing allowed any clean directory to become a glob regardless of whether that directory was actually named for the owning subsystem.
- **Fix:** discovery now groups only non-test source into generated architecture manifests. Tests remain in the `tests` coverage bucket and are linked to source subsystems by explicit config or convention. Link collapsing is conservative: it collapses `tests/auth` or `docs/auth`, but keeps `tests/test_auth.py` and `docs/auth.md` file-scoped.
- **Test:** `tests/discover/test_discover.py::test_discover_never_promotes_test_dirs_to_subsystems`, `::test_discover_top_level_test_file_links_without_overclaiming_tests_dir`, `::test_discover_docs_convention_links_file_without_overclaiming_docs_dir`, `::test_discover_existing_model_reports_unlinked_tests_without_candidates`.

---

See also: [coverage.md](coverage.md) (the mapping-coverage metric + how to close a gap),
[testing.md](testing.md) (how to write the regression test for a fix),
[../benchmarks/results/oss-cross-language.md](../benchmarks/results/oss-cross-language.md) (how these
were found and the corpus they were found on).
