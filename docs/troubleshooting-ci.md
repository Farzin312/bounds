# Troubleshooting CI failures

*How to resolve red Bounds gates, from architectural cycles to structural drift.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

When `bounds preflight --ci` fails in your pipeline, it is surfacing architectural debt or manifest drift. This guide helps you diagnose and fix the most common failure modes.

## TL;DR: The "Stopgap" (not recommended for long-term)

If you need to unblock a PR immediately and you trust the changes, you can make the check advisory in `.github/workflows/bounds.yml`:

```yaml
- run: bounds preflight --ci || true
```

**Remove this once you fix the underlying issues.** A red gate that stays red is a gate nobody looks at.

---

## 1. Architectural Cycles (`E_CYCLE_DETECTED`)

This is the most common reason a build fails with a large number of errors (e.g., "259 cycles detected").

### Why it happens
A cycle exists when subsystem A depends on B, and B (transitively) depends back on A. Bounds flags these because they make the architecture hard to reason about and break the "layered" mental model.

### Read the root-cause summary, not the path dump
When there are more than 10 cycles, Bounds collapses the long tail into one **root-cause** issue: a *ranked minimal feedback-arc-set* — the smallest set of edges whose removal breaks **every** cycle, each annotated with how many cycles it breaks, plus the strongly-connected-component (tangle) count:

```
… and 109 more cycles across 2 strongly-connected components; these 13 root edges break all of
them: 'rankers->sellers' (breaks 76), 'interfaces->src-personalization' (breaks 74), …
```

119 truncated cycle paths are unactionable; a 13-edge ranked cut list is a to-do list. **Cut the top edge first** (it resolves the most cycles), re-run, repeat.

### Parent/child nesting is NOT a cycle
If your taxonomy nests a subsystem inside another's path (e.g. `src/auth/guards` inside `src/auth`), a module file importing its own subdirectory and back is normal intra-module layering — Bounds recognizes the **containment** relationship (from path nesting, or an explicit `parent:` key in the child's manifest) and does not report it as a cycle. A genuine cross-domain cycle that merely *passes through* such an edge is still reported.

### The catch-all composition root (`E_COMPOSITION_ROOT`)
If one subsystem (often `root`/`src`/`common`) both imports nearly every sibling **and** is imported by nearly every sibling, Bounds emits an advisory `E_COMPOSITION_ROOT`: it's a DI/HTTP composition root fused with shared leaf utilities, and it forms a cycle with essentially everything. The fix: declare it an `entry_point` in `root.yaml` (a source-only composition root) **and** split its shared leaf directories (`geo`, `mail`, `errors`, …) into their own subsystems so siblings depend on the leaves, not the root.

**The Fix (genuine cycles):**
1. Run `bounds validate -H` locally to see the breakdown and the ranked cut list.
2. For each root edge, pick **one direction to cut**:
   - Move the shared code *down* into a lower-level library subsystem.
   - Invert the dependency (have the lower module take a callback/interface instead of importing the higher one).

### Fail only on *new* cycles (cycle baseline)
You don't have to clear all pre-existing cycle debt to arm the gate. Run `bounds calibrate --dump-baseline` once on a clean main branch and commit `.bounds/cycle-baseline.json`. `preflight` then **reports** the baselined cycles (suppressed, non-blocking) and **fails only on cycles a branch newly introduces** — parity with the drift baseline. Re-dump after intentionally changing the accepted set.

---

## 2. Subsystem Overlap (`E_SUBSYSTEM_OVERLAP`)

### Symptom
A warning that multiple subsystems claim ownership of the same file.

### The Fix
Ownership is resolved by "most specific path wins." If two subsystems have the *exact same* path glob and claim the same file, Bounds flags it as a tie.
- Narrow one subsystem's path glob in its `.bounds/manifests/*.yaml`.
- Or move the shared files into an explicit `files:` list under a single owner.

---

## 3. Structural Drift (`E_STRUCTURAL_DRIFT`)

### Symptom
`E_STRUCTURAL_DRIFT` errors mean your code's exports no longer match what is declared in the manifest.

### The Fix
1. Run `bounds calibrate` to see the proposed changes.
2. If the changes are intentional (you added/removed an export on purpose), run `bounds calibrate --apply`.
3. Commit the updated manifest **in the same PR** as your code change.

---

## 4. Coverage Gaps (`E_COVERAGE_GAP`)

### Symptom
Bounds found supported source files that aren't owned by any subsystem.

### The Fix
1. Run `bounds coverage --why <path>` to see why a file is unmapped.
2. For intentional exclusions (tooling, config), run `bounds fix-coverage --auto` then `--apply`.
3. For real source, add the path to the relevant subsystem's `paths:` in its manifest.

---

## 5. Governing the gate without `allow_failure` (`.bounds/policy.yaml`)

The gate is otherwise all-or-nothing: any `error` fails `preflight`, so one library-gap class can force a blanket `allow_failure: true` that *also* masks genuine regressions. Instead, commit a `.bounds/policy.yaml` to govern it precisely:

```yaml
version: 1
# Re-grade a noisy class without silencing it (still printed, just non-blocking).
severity:
  E_ORPHAN_EXPORT: warning
# Hard-gate the findings you CAN fix, regardless of mode/enforce (overrides enforce: warn).
fail_on:
  - E_BOUNDARY_VIOLATION
# Accept a specific known finding — the eslint-disable equivalent. `reason` is required.
suppress:
  - code: E_CYCLE_DETECTED
    subsystem: rankers
    message_contains: sellers
    reason: "accepted legacy coupling, tracked in JIRA-123"
    owner: farzin
    expires: 2026-12-31   # advisory metadata only — never gated against wall-clock (determinism)
```

- A **suppressed** finding stays in the report (`suppressed: true` + an audit `note`) but never blocks — and the *next* unannotated finding of that code still fails.
- `--fail-on=E_CYCLE_DETECTED` (repeatable / comma-separated) is the per-run flag equivalent of the policy `fail_on` list.
- A malformed policy degrades to advisory warnings — it never crashes the run or silently drops the gate.

Use this to hard-gate the classes you can fix while demoting known library gaps, instead of disabling the whole job.

---

## Re-arming the gate

Once you've resolved the issues locally:
1. Run `bounds preflight --ci` locally and confirm it exits 0.
2. Ensure `.github/workflows/bounds.yml` does **not** have the `|| true` stopgap.
3. Commit your fixes (code, manifests, and baseline).

For a deeper dive into the validation engine, see [how-it-works.md](./how-it-works.md).
