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

### Misleadingly high counts
If you see hundreds of cycles, don't panic. A single "god-node" or catch-all subsystem (like `root`, `common`, or `lib`) that many things import, and which in turn imports something high-level, can create hundreds of path permutations.

**The Fix:**
1. Run `bounds validate -H` locally to see the breakdown.
2. Look for the **shortest cycles** first (usually 2 nodes: `A <-> B`).
3. Pick **one direction to cut** for each cycle:
   - Move the shared code *down* into a lower-level library subsystem.
   - Invert the dependency (have the lower module take a callback or prop instead of importing the higher one).
   - If it's a "god-node" (like `root`), narrow its manifest in `.bounds/manifests/` so it doesn't act as a universal sink.

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

## Re-arming the gate

Once you've resolved the issues locally:
1. Run `bounds preflight --ci` locally and confirm it exits 0.
2. Ensure `.github/workflows/bounds.yml` does **not** have the `|| true` stopgap.
3. Commit your fixes (code, manifests, and baseline).

For a deeper dive into the validation engine, see [how-it-works.md](./how-it-works.md).
