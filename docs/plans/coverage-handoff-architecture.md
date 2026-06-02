# Plan — Coverage truth + CLI↔agent↔human handoff (Option C)

**Branch:** `plan/coverage-100-supported-split` · **Date:** 2026-06-02 · **Status:** Stages 1–4 implemented (see below); Stage 5 (adapters) is roadmap

> One sentence: make Bounds map **100% of what it can parse deterministically**, hand the rest to an AI agent with a *contextual, token-lean* template, keep that hand-authored map **honest as the repo grows** (deterministic staleness detection, no LLM), and ground every marketing claim in that mechanically-true number — so the coverage signal, the agent handoff, and the growth story all tell the same truth.

## Implementation status (2026-06-02)

Stages 1, 2, and 4 are implemented, tested, and reviewed (PASS) on this branch; Stage 3 was folded
into Stage 1. **544 tests pass.** Deltas from the original proposal below:

- **Stage 1 — `558a9b1`.** Supported-only `mapped_pct` + `declared`/`dark` split + shared
  `scan.coverage_has_gap`. *Delta:* kept `mapped_pct` top-level (single headline) rather than only
  nested; the gap `fix` carries a numbered procedure + concrete `template_ref` (this absorbed **Stage
  3** — no always-on config bloat, one short AGENTS.md/skill nudge points at the contextual fix).
- **Stage 2 — `f62ce8a`.** Undeclared-export info-drift rolls up to one issue per subsystem with a new
  `Issue.count` field (serialized only when >1). *Delta vs "drop info-fix":* kept a single shared
  fix; overview tallies sum `i.count` so `structural_drift` magnitude is preserved. Only the
  info branch rolls; error/warning drift stays per-item.
- **Stage 4 — `4031bae`.** `E_UNSUPPORTED_SURFACE_STALE` via a per-file content-hash digest. **Design
  change (important):** the digest lives in a **committed `.bounds/surface-baseline.json`** (written by
  `calibrate --dump-baseline`), NOT the cache — because `cache.db` is gitignored, so a cache-based
  baseline would be empty in CI / on a fresh clone. Consequence: **no `STATE_VERSION` bump, no cache
  schema change** (the §3.2/§5.2/§8 cache-digest plan is superseded). New module `surface.py`; the
  signal fires in `validate` (full/preflight), off the `--quick` path; opt-in (no baseline ⇒ no
  signal).
- **Not yet done:** the JSON↔human parity nits (§4c: `schema_coverage.note` passthrough, clean
  `validate -H` `mapped_pct` line) and the "two coverages" relabel (§4d) — small follow-ups. Stage 5
  (language adapters, §6.1) is the roadmap.

---

This plan is the synthesis of a code-grounded audit (citations inline). It is deliberately broader than the original "fix the %" ask, because the audit found the metric is one of **five** connected gaps in the CLI → agent → human handoff. They share a root cause and must be fixed together or they re-diverge.

---

## 1. Problem statement — the five gaps (all evidenced in code)

### G1 — The headline coverage number is mechanically false
`mapping_coverage` puts unsupported-language files **in the denominator**: `total = mapped + unowned_supported + unsupported` (`scan.py:692`), with a hard cap forcing `99.9` while any gap remains (`scan.py:694-695`). The numerator `owned` is built **supported-extensions-only** (`engine.py:68` `exts = supported_extensions()` → `engine.py:81` `resolve_owners(..., exts)` → `engine.py:294` `set(file_owner)`). So a repo with 15 shell files **cannot reach 100%**, and `guide` step 3 (`"done": files_unmapped == 0`, `guide.py:70`; `files_unmapped = unowned_supported + unsupported`, `scan.py:703`) is **permanently uncheckable**. Yet README claims *"100% means 100% of supported-language source"* (`README.md:196-197`) and `coverage.md` claims *"hand-author a manifest → reach 100%"* (`docs/coverage.md:122-137`). **The code satisfies neither promise.**

### G2 — Hand-authoring a manifest doesn't move the number
Because `owned` is supported-only, a shell file declared in `paths:` is filtered out by `iter_subsystem_files(..., supported_extensions())` before it can enter `owned` (`scan.py:286`). Its **exposes** become durable (verified true — `checks.py:310-313`, `calibrate.py:145-150`), but `mapped` never rises. The two signals (`subsystems_with_unsupported_source` vs the coverage numerator) never meet. So the documented remediation is mechanically inert against the metric.

### G3 — The agent never receives the *procedure*, only a doc pointer
The full 4-step procedure + minimal-manifest template lives **only** in `docs/coverage.md:122-165`. The files an agent loads after `bounds agent --sync` (`agentsync.py` `CANONICAL_BODY`, `_SKILL_BODY`, `_CLAUDE_COMMAND_BODY`, gemini/opencode bodies) contain **zero** coverage guidance — `AGENTS.md` only says "follow `health.validation.next_steps`", and no such structured field exists in the JSON (only a prose `fix` string ending "see docs/coverage.md", `engine.py:380-384`). A JSON-first agent gets a nudge, never the steps.

### G4 — Token bloat in the agent-facing surface (the anti-pattern Bounds exists to kill)
`ValidationReport.to_dict()` (`models.py:345-354`) emits **every** issue uncapped, each with full `message` + `fix` (`models.py:322-330`). Structural drift is **one issue per undeclared symbol** (`checks.py:366-379`), unresolved refs one per reference (`checks.py:439-446`). The user's run = ~94 infos + ~24 warnings ≈ **7,000–10,000 tokens dumped into agent context on a single `validate`** — and `--quick` is the command AGENTS.md tells agents to run after every edit. Only overlaps aggregate today (`scan.py:332-360`, capped `[:3]`). This is the single largest agent-facing bloat in the product, and it directly contradicts the reason Bounds exists.

### G5 — Silent staleness as the consuming repo grows (lifecycle hole)
For an unsupported-language subsystem, **nothing ever flags a stale hand-authored expose** (renamed/removed symbol in a `.sh`): `check_structural_drift` short-circuits (`checks.py:310-313`), calibrate routes declared-but-missing to `needs_review` excluded from drift keys (`calibrate.py:145-150,216`), so `calibrate --check` CI never fires. Worse, a *consumer* depending on that now-dead interface stays green (`check_contract` only checks the interface is still *declared*, `checks.py:451`). Staleness accumulates invisibly until an adapter ships, then lands as one noisy drift burst (scenario 4 below) that's easy to rubber-stamp `calibrate --apply` and accidentally strip intentional entries.

**Plus two JSON-first parity violations** (the invariant: `--human` re-renders the *same* data, never more/less): `schema_coverage.note` is dropped in human view and replaced with a hardcoded string (`output.py:667-671` vs `describe.py:317-320`); `validate -H` dumps the whole `coverage` dict as a raw Python repr in the stats line (`output.py:589-594` `_format_stats_line`) so `mapped_pct` is buried in brace-soup, while `overview` renders it cleanly as `mapped: X%` (`output.py:286`). And there are **two unrelated signals both called "coverage"** — source mapping-coverage vs schema parse-coverage (`E_SCHEMA_UNPARSED`) — never cross-referenced, easy to conflate.

---

## 2. Guiding principles (every change is checked against these)

1. **Token-lean or token-negative for the agent.** No change may grow always-loaded agent context. Procedure/templates surface **contextually** (only when a gap exists), never in always-on config. G4's rollup is a net **reduction** — that's the bar.
2. **Honest 100% — deterministic first, AI second.** `100%` = 100% of what Bounds can parse with zero LLM. Everything else is *named*, never silently dropped, and the AI-fill path is a first-class, durable, *self-policing* workflow.
3. **JSON-first parity, one source of truth.** Every number an agent or human sees comes from one computed field. `--human` re-renders, never re-derives.
4. **Determinism.** Sorted at serialization boundaries; no wall-clock/random; capped samples are deterministic slices of sorted lists.
5. **Append-only contracts.** Error codes are string-named and append-only (`errors.py`); new codes only. The `stats.coverage.mapping` sub-shape is reshaped once, deliberately, documented as a breaking change, all internal consumers updated in lockstep.
6. **Marketing == the computed number.** No claim that the CLI can't print live. The growth story *is* the honesty story (§7).

---

## 3. The coverage model (Option C) — design

### 3.1 New `stats.coverage.mapping` shape (token-lean, parity-clean)

```jsonc
{
  "supported": {                 // the deterministic, reach-100% metric
    "total": 514,                // supported-ext, non-test, non-ignored source files
    "mapped": 514,
    "unowned": 0,                // supported files in no subsystem (fix: add to paths — deterministic)
    "mapped_pct": 100.0,         // mapped / total  ← THE headline number; 100% now reachable
    "unowned_sample": []         // sorted, capped [:10]
  },
  "unsupported": {               // no adapter yet — named, never parsed, never silently dropped
    "total": 15,
    "declared": 12,              // owned by a manifest's paths → covered + durable
    "dark": 3,                   // owned by NO manifest → the real gap; drive to 0
    "dark_sample": [],           // sorted, capped [:10]
    "by_language": {"shell": 15} // bounded count dict (one entry per language)
  },
  "tests": { ... },              // unchanged (_linkage_bucket)
  "docs":  { ... }               // unchanged
}
```

- **Headline `mapped_pct` = `supported.mapped_pct`** = `mapped / (mapped + unowned)` over supported files only. 100% is reachable by adding supported files to `paths:`. The README sentence becomes literally true.
- **`unsupported.dark` is the new honest gap to chase** — a file owned by no manifest. **`unsupported.declared`** = a manifest claims it → hand-authoring now *visibly* moves a file `dark → declared` (closes G2: the documented remediation finally has a mechanical effect on the report).
- Implementation: `mapping_coverage` **already walks every file once** (`scan.py:663` `walk_supported(project_root, None)`) — no second tree traversal. We pass in **two** owner sets: `owned_supported` (today's `set(file_owner)`) and `owned_all` (a `resolve_owners(..., exts=None)` pass, bounded by *declared paths* not repo size). `declared` = unsupported files in `owned_all`; `dark` = unsupported files in neither. Sample lists reuse the `_linkage_bucket` `[:10]` idiom (`scan.py:714-727`).

### 3.2 Gate + guide semantics (reachable, honest)
- `E_COVERAGE_GAP` (warning) fires when **`supported.unowned > 0` OR `unsupported.dark > 0`**. It does **not** fire on `unsupported.declared` (that's covered).
- `guide` step 3 `"done"` ⇔ `supported.unowned == 0 AND unsupported.dark == 0`. **Now reachable on any repo**, polyglot included.
- `--fail-on-unowned` keeps current behavior for `supported.unowned` (blocking error); `unsupported.dark` stays **advisory by default** (adoption-friendly, §6.4), with `--fail-on-dark` opt-in for teams that want it hard.

---

## 4. CLI ↔ agent ↔ human handoff fixes

### 4a. Issue rollup — the biggest token win (G4)
Group high-cardinality issue families before serialization. In a pre-serialize step (engine) or `ValidationReport.to_dict()`:
- **`E_STRUCTURAL_DRIFT` infos** → one rollup per subsystem: `"src-feed: 17 undeclared exports — FeedItem, RankService, calculateScore, +14 more"` with **one shared `fix`**, instead of 17 issues × (message+fix). Mirror the existing overlap aggregation (`scan.py:332-360`).
- **`E_UNRESOLVED_REFERENCE`** → roll up per consuming subsystem when count warrants.
- **Drop `fix` on `info`-severity issues** (infos never block; per-symbol fix is low-value-per-token).
- Preserve `stats.coverage` counts and the per-subsystem totals so `overview` drift counts (`output.py:284`) and the human renderer (`output.py:550-573`) stay accurate — update both renderers in the same change (parity).
- **Expected effect: ~85% reduction** of the ~7–10k-token dump. This is the change that most directly serves "don't bloat the AI."

### 4b. Contextual agent procedure — *not* always-on bloat (G3)
- Add a structured **`next_steps: [...]`** array (3–4 short imperative strings) and a **`template_ref`** (path to an existing `.bounds/manifests/*.yaml` to copy) to the `E_COVERAGE_GAP` issue — emitted **only when a gap exists**. The agent pays the tokens only when relevant.
- Add **one line** (~15 tokens) to `_SKILL_BODY`/`CANONICAL_BODY`: *"On `E_COVERAGE_GAP`, follow the issue's `next_steps`; for unsupported languages copy `template_ref` and fill `paths`+`exposes`."* Always-on cost is negligible; the procedure itself is contextual.
- Net: the agent gains an actionable procedure **without** growing baseline context — the opposite of dumping `coverage.md` into every config.

### 4c. JSON-first parity fixes (the two violations)
- Surface `schema_coverage.note` **verbatim** in the human renderer (`output.py:667-671`) instead of the hardcoded substitute.
- Render `mapped_pct` (and the supported/unsupported one-liners) as a clean stats line in `validate -H`, not a raw dict repr (`output.py:589-594`).

### 4d. Kill the "two coverages" ambiguity
Rename in human/labels (not the JSON keys, which stay): **"source mapping coverage"** vs **"schema parse coverage."** Add a one-line cross-reference so `schema coverage: complete` can't be misread as source-mapped.

---

## 5. Lifecycle & edge cases as the repo grows (the durability story)

### 5.1 Scenario table (what's automatic vs manual)

| Repo change | Bounds does automatically | Human/AI must | When AI re-runs |
|---|---|---|---|
| **New file under an existing unsupported `paths:` glob** | Ownership auto-picks-it-up (`iter_subsystem_files` re-globs every run, `scan.py:216-227`); coverage/`declared` hold | Hand-add any **new** public symbol to `exposes` (Bounds can't derive it — `add_exposes` ⊆ `extracts`, `calibrate.py:131-136`) | Only to add the new symbol |
| **New unsupported file in an unmapped dir** | Becomes `unsupported.dark` → one advisory `E_COVERAGE_GAP` | Declare a manifest (or `.boundsignore`) | When you want it covered |
| **Hand-authored `exposes` goes stale** (symbol renamed/removed) | **TODAY: nothing detects it (G5).** → **NEW: hash-staleness flags it (§5.2)** | Re-confirm `exposes` against the changed file | **Triggered by the new `E_UNSUPPORTED_SURFACE_STALE` signal** |
| **Bounds ships an adapter for that language** | Subsystem leaves `unsupported_owners`; files now extract; prior hand-authored exposes are reconciled against real symbols → one-time drift burst | Review burst; run `calibrate`; re-baseline if needed | **Re-run `validate`+`calibrate` right after upgrade** (the reconciliation moment) |
| **`consumes`/dependency edges of an unsupported subsystem** | Fully validated like any other (`E_UNRESOLVED_REFERENCE`, cycles, missing-export — `checks.py:432-467`); **not exempt** | Keep `consumes` accurate by hand (can't be derived) | On manual edits |

### 5.2 Deterministic staleness detection — the answer to "when must the AI re-run" (closes G5, no LLM)
The cache already stores a **`content_hash` per file** (`cache/store.py:59,81`). Extend it to also record **unsupported-owned files** (today they're excluded because ownership is supported-only). Then:
- A subsystem's unsupported surface gets a **`surface_digest`** = ordered hash of its owned unsupported files' `content_hash`es (deterministic; reuse `hash_*` conventions; no wall-clock).
- On `validate`/`calibrate`, if the digest **differs from the last confirmed** digest, emit **`E_UNSUPPORTED_SURFACE_STALE`** (new, append-only, severity `warning`): *"shell file(s) in `<subsystem>` changed since `exposes` were last confirmed — re-verify the public surface."* The fix carries `next_steps` (re-read file → update `exposes` → re-run). Confirmation re-baselines the digest (tie to `calibrate --apply` / a lightweight `bounds confirm`).
- **This is the precise mechanism the user asked for:** Bounds deterministically notices the unsupported file changed → tells the agent *exactly when* to re-author exposes → agent patches → Bounds records the new baseline. The hand-authored map stays honest as the repo grows, with **zero LLM** in the structural path (binding constraint preserved).
- **Cache impact:** adding unsupported-file records changes extraction output for unchanged source → **bump `config.STATE_VERSION` "4" → "5"** (`config.py:71`; `cache/store.py` `_schema_version` ties `PRAGMA user_version` to it, auto-invalidating old caches — `store.py:218-247`). `tests/meta/test_versioning.py` untouched (CalVer is commit-count derived).
- **Mixed-subsystem caveat (today's over-protection, `checks.py:310-313`):** the `owns_unsupported` flag is subsystem-wide, so a stale *supported*-side expose in a mixed subsystem is also shielded. Once we have per-file `surface_digest`, we can **narrow drift suppression to only the unsupported files**, restoring real drift detection on the supported portion of a mixed subsystem. (Stage 4 — optional hardening.)

---

## 6. Scaling architecture, connected to the roadmap

The model is built so **the gap shrinks as Bounds grows**, with no metric redefinition — this is the architectural spine that ties coverage to the product roadmap (tracked as GitHub Milestones per `CLAUDE.md`).

### 6.1 Adapter expansion = the metric self-improves
`supported_extensions()` (`registry.py:55`) is the **single source of truth** for "supported." Shipping an adapter (subclass `LanguageAdapter` + register, `registry.py:33`) automatically:
- moves that language's files from `unsupported` → `supported` in `mapping_coverage` (no change to the metric definition),
- turns previously `declared` (hand-asserted) exposes into **extracted-and-verified** ones,
- triggers the one-time reconciliation (§5.1 row 4), now **safely absorbed** by the `E_UNSUPPORTED_SURFACE_STALE` + `needs_review` machinery instead of a blind strip.

**Adapter roadmap (priority by real-world prevalence), each a milestone + release moment:**
1. **Shell / bash** (`tree-sitter-bash` upstream) — highest hit-rate gap (it's what the user just hit); closes the most common polyglot hole.
2. **Go**, 3. **Rust**, 4. **Java/Kotlin**, 5. **Ruby/PHP**, then community-driven by `unmapped_by_language` telemetry (`docs/coverage.md:179` already frames this breakdown as the prioritization signal).

### 6.2 Coverage model evolution over time
- **Today:** headline = % of supported source; unsupported split into declared/dark.
- **As adapters ship:** `unsupported.declared` shrinks (files become verified), `dark` is driven to 0 by declaration, and the *number of languages verified deterministically* rises. The headline number and the language count both climb **automatically and truthfully**.
- **Forward-compatible by construction:** no consumer hard-codes the language set (binding constraint: resolve via the registry, never a hard-coded enum at a check site). Adding a language is purely additive.

### 6.3 Performance scaling
- **Quick path untouched:** `mapping_coverage` is gated `mode != "quick"` (`engine.py:292`); the new `owned_all` pass and `surface_digest` never run on the sub-200ms hot path.
- **No new tree walk:** the declared/dark split reuses `mapping_coverage`'s existing single full walk; `owned_all` is bounded by *declared paths*, not repo size.
- **Bounded output:** every new list is a sorted `[:10]` sample; `by_language` is a count dict; the issue rollup makes `validate` output **shrink with repo size instead of grow**. This is the property that lets Bounds scale to large repos without bloating the agent.
- **Determinism preserved** at every serialization boundary.

### 6.4 CI adoption scaling (don't wall a big polyglot repo)
Phased severity so adoption isn't a wall of red: `dark` is advisory by default; teams opt into `--fail-on-unowned` (supported) and `--fail-on-dark` (unsupported) as they harden. A 10k-file polyglot repo can adopt Bounds, get an honest number day one, and ratchet enforcement up over time.

### 6.5 Milestone mapping
- **M1 (this plan, stages 1–3):** honest metric + rollup + contextual agent procedure + parity fixes + docs/marketing grounding.
- **M2:** `E_UNSUPPORTED_SURFACE_STALE` + cache digest (STATE_VERSION 5) + mixed-subsystem narrowing.
- **M3+:** adapter roadmap (shell → Go → Rust …), each shrinking the gap and each a release/launch surface (§7).

---

## 7. Marketing — grounded truth as the growth/stars engine

Honesty here is not a constraint on marketing — it **is** the marketing. (Aligns with the standing "honest number hook": keep a concrete number, always qualified, never a falsifiable flat/universal claim.)

- **Hero number stays token-savings, not coverage.** The value prop is *"stop your agent burning 50k tokens grepping the repo."* Keep the benchmark band (`README.md:137-142`, "98.7–100% smaller than reading every file; 54–100% per `describe`") as the headline — qualified, range-form, reproducible. Coverage is the **trust** metric, not the hero metric; don't conflate them.
- **The grounded coverage claim that earns trust (and survives a skeptic's 30-second test):** *"Bounds maps 100% of supported-language source deterministically, zero LLM — and for everything else it names exactly what it can't parse and hands your agent a fill-in template that stays honest via content-hash staleness checks. N languages supported, growing."* This is **falsifiable in our favor**: a dev runs it on their polyglot repo, sees an honest number + a clear path, and trusts the tool. The old *"100% mapping"* framing is falsifiable **against** us — disproved instantly on a shell repo → bounce, no star, maybe a public miss.
- **Public adapter roadmap = a follow-magnet.** A visible "shell ✓ → Go → Rust → request yours" roadmap (GitHub Milestones, §6.5) gives devs a reason to **star-to-follow** and to open "support \<my language\>" issues. Each adapter PR is a release/launch moment with its own audience.
- **Dogfooding as proof.** *"Bounds validates its own repo at 100% supported coverage"* (`README.md:200`) — keep it accurate under the new metric; it's credibility that converts.
- **Low contribution bar = more contributors = more stars.** "Add your language = subclass `LanguageAdapter` + register" is already true (`CLAUDE.md`); surface it prominently as a contributor on-ramp.
- **Edits:** rewrite `README.md:169-200` and `docs/coverage.md:122-137` to the supported-only + declared/dark model and the grounded claim above; fix `ARCHITECTURE.md:830` `E_COVERAGE_GAP` description to the new gate semantics; add the adapter roadmap section linked to Milestones.

---

## 8. Migration & backward compatibility
- **Breaking JSON change** to `stats.coverage.mapping` (flat keys → `supported`/`unsupported` nesting). We control every consumer (enumerated in §Appendix); update all in lockstep. Document in CHANGELOG + `docs/coverage.md`. No flat mirror keys (would bloat; principle 1).
- **Error codes:** add `E_UNSUPPORTED_SURFACE_STALE` (warning) — append-only, never renumber/rename existing (`errors.py` convention). Register severity + exit-code per `ARCHITECTURE.md §8`.
- **STATE_VERSION "4" → "5"** when unsupported-file cache records land (§5.2); old caches auto-invalidate via `PRAGMA user_version` (`store.py:218-247`). No CalVer/version-string impact.

## 9. Testing strategy (across everything — "works well across everything")
- **`tests/validate/test_coverage.py`** — rewrite for the new shape: supported-only `mapped_pct`; `declared` vs `dark` split; 100% reachable on a repo whose only gap is unsupported-but-declared; `dark>0` still warns; `E_COVERAGE_GAP` gate fires on `unowned||dark`, not on `declared`.
- **`tests/validate/test_engine.py`** — gate semantics, rollup correctness (N drift infos → 1 rollup, counts preserved), `next_steps`/`template_ref` present only when a gap exists.
- **`tests/cli/test_cli.py`** + **a new `tests/output/` parity test** — JSON↔human parity for the coverage block, `schema_coverage.note` surfaced, clean `mapped_pct` line, no raw-dict repr.
- **New `tests/validate/test_staleness.py`** — change an unsupported file's bytes → `E_UNSUPPORTED_SURFACE_STALE`; confirm → cleared; deterministic digest; mixed-subsystem narrowing.
- **`tests/benchmarks/test_oss_bench.py`** — assert the issue rollup keeps `validate` output token-bounded as repo size grows (regression guard for G4).
- **Lifecycle integration test** — simulate the §5.1 scenarios end-to-end (new file auto-owned; adapter-ships reconciliation burst is absorbed, not blindly stripped).
- **Determinism test** — byte-stable coverage JSON across runs; sorted samples.
- Full suite via `.venv/bin/pytest`; run `/review` after each implement stage and `ship-review` before PR (project review gates).

## 10. Phased delivery (each stage independently shippable)
1. **Stage 1 — Honest metric (G1/G2).** Reshape `mapping_coverage` (supported-only % + declared/dark), update gate + `guide.done` + all consumers (Appendix), docs/marketing grounding (§7). *Ships the core truth fix.*
2. **Stage 2 — Token discipline (G4).** Issue rollup + drop info-fix + parity fixes (4c/4d) + benchmark guard. *Ships the anti-bloat win.*
3. **Stage 3 — Contextual agent procedure (G3).** `next_steps`/`template_ref` on the gap issue + the one-line config nudge. *Ships the actionable handoff.*
4. **Stage 4 — Lifecycle honesty (G5).** `E_UNSUPPORTED_SURFACE_STALE` + cache digest (STATE_VERSION 5) + mixed-subsystem narrowing. *Ships durability-as-repo-grows.*
5. **Stage 5+ — Adapter roadmap (§6).** Shell first, then by telemetry. *Shrinks the gap; each a launch moment.*

---

## Appendix — file-by-file change map (with citations)
- `src/bounds/extract/scan.py` — `mapping_coverage` reshape (`627-711`); add `owned_all` param/pass via `resolve_owners(exts=None)` (`262-301`); reuse `_linkage_bucket` cap (`714-727`); `surface_digest` helper (Stage 4).
- `src/bounds/validate/engine.py` — gate on `supported.unowned||unsupported.dark` (`292-300`); `_coverage_gap_issue` → `next_steps`/`template_ref` (`339-392`); pass `owned_all`; rollup pre-serialize step.
- `src/bounds/validate/checks.py` — narrow `owns_unsupported` drift suppression to per-file (`310-313`) [Stage 4]; rollup-aware drift emission (`366-379`, `439-446`).
- `src/bounds/models.py` — `ValidationReport.to_dict` rollup (`345-354`); drop `fix` on info issues (`322-330`).
- `src/bounds/guide.py` — `done` + `_coverage_why` to new shape (`62-71`, `191-214`).
- `src/bounds/discover.py` — `next_step` + coverage reads (`99`, `249-266`).
- `src/bounds/cli.py` — overview coverage block (`482-531`).
- `src/bounds/output.py` — human parity: `mapped_pct` line (`589-594`), `schema_coverage.note` (`667-671`), coverage labels (`286`).
- `src/bounds/agentsync.py` — one-line `next_steps` nudge in `_SKILL_BODY`/`CANONICAL_BODY` (`61-104`, `483-497`).
- `src/bounds/errors.py` — add `E_UNSUPPORTED_SURFACE_STALE` (Stage 4).
- `src/bounds/cache/store.py` + `src/bounds/config.py` — unsupported-file records + `STATE_VERSION` 5 (Stage 4) (`store.py:56-59,218-247`; `config.py:71`).
- `README.md` (`137-200`), `docs/coverage.md` (`1-197`), `ARCHITECTURE.md` (`830`) — grounded model + roadmap.
- Tests per §9.
