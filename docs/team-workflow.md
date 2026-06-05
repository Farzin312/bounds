# Team workflow

*How a team adopts Bounds and keeps its manifests fresh — the freshness loop is the whole discipline.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

A cheap, verified architecture contract is only useful if it still matches the code. The single most important thing a team does with Bounds is **keep the manifests fresh**. A stale manifest that still validates is worse than no manifest, because it lies confidently. This page is about the discipline that prevents that.

## Adoption path

Onboarding an existing repo is four steps:

```bash
cd your-project
bounds discover            # preview auto-generated manifests (dry-run)
bounds discover --apply    # write root.yaml + per-subsystem manifests
# → review the generated manifests by hand: fix boundaries, roles, names
git add .bounds/root.yaml .bounds/manifests && git commit -m "chore: adopt Bounds manifests"
bounds agent --sync        # wire Bounds into the coding agents your team uses
```

(Teammates can run bare `bounds agent` any time to see which agents are wired — it's read-only — and `bounds agent --check` is the CI-friendly way to verify the wiring is still current.)

1. **`bounds discover`** groups source by directory, tree-sitter-extracts each candidate's verified `exposes`, infers `consumes` from the cross-candidate import graph, and seeds `role`/`criticality` from graph degree. It is a dry-run by default and never overwrites existing manifests.
2. **Review the manifests.** Discovery proposes; humans decide. This is where you correct boundaries the heuristic got wrong and confirm the roles.
3. **Commit `.bounds/root.yaml` and `.bounds/manifests/`** so the contract is versioned with the code. (The cache `.bounds/cache.db` is gitignored and regenerated — never commit it.)
4. **`bounds agent --sync`** writes the canonical contract into `AGENTS.md` plus per-tool pointer files — including a marked block in each agent's always-loaded memory file (e.g. `CLAUDE.md`), created if absent or appended non-destructively if present — telling each agent to query `bounds describe`/`bounds list` instead of reading raw source.

## The freshness loop (the core discipline)

This is the rule that keeps the whole thing honest:

> **A PR that changes a subsystem's public surface MUST update that subsystem's manifest — in the same PR.**

You make that rule real with a CI gate, not a wiki page nobody reads. Every PR runs `bounds validate --quick` (git-diff incremental, safe for every commit). When the gate detects source/manifest drift, the author runs `bounds calibrate` to see the proposed manifest changes, `bounds calibrate --apply` to write them, and includes that manifest update **in the same PR** as the code change. Code and contract move together, always.

Do not overgeneralize calibration. `bounds calibrate --apply` reconciles manifests with extracted
source: exposes, missing provider edges, stale consumes edges, and similar drift. It keeps bare
`consumes` edges bare by default, because interface-level consumes activate orphan-export checks and
should mean "we curated this exact contract." Use `bounds calibrate --track-interfaces --apply` only
when that precision is intentional. If interface lists were added accidentally, use
`bounds calibrate --coarsen-interfaces --apply` to keep the provider edges but return to
subsystem-level contracts. Calibration does **not** map new files, close `E_COVERAGE_GAP`, or break
`E_CYCLE_DETECTED`; those are guided by `bounds validate -H` and usually require editing manifests'
`paths:` or changing source boundaries.

```
edit code ──► bounds validate --quick ──► status fresh? ──► yes ──► open PR
                                              │
                                              └─ no ──► bounds calibrate --apply
                                                          │
                                                          └─► commit manifest change WITH the code change ──► open PR
```

### The `validation_status` signal

Every `describe` and `validate` payload carries a machine-readable `validation_status`. It is the signal your CI, your hooks, and your agents branch on:

| `validation_status` | Meaning | What to do |
|---------------------|---------|------------|
| `fresh` | No errors; manifests match source. | Nothing — you're good. |
| `stale` | Structural drift or cross-subsystem impact detected; the manifest needs updating. | Run `bounds calibrate --apply` and commit the manifest change with the code. |
| `unresolved` | Forward references to subsystems/interfaces that don't exist yet (incremental adoption; warning-level). | Fine mid-adoption; resolve as you declare the missing pieces. |

When both apply — real drift *and* an unresolved forward reference — `validation_status` reports **`stale`**. The actionable state (errors you must fix) always outranks the benign one, so a status of `unresolved` never hides drift; the forward references remain in the full `issues` list regardless.

### Make it enforced, not optional

Everything above works by convention until you wire it into the pipeline. Agent compliance is **advisory** — Bounds can suggest the right behavior but cannot force it — so the one hard enforcement point is CI. Install it:

```bash
bounds ci --install --github     # GitHub Actions workflow (use --gitlab for GitLab CI)
bounds ci --install --precommit  # add a local pre-commit hook too (optional)
```

Pick your CI host with `--github` or `--gitlab` (add `--precommit` for a local hook, or `--all` for everything). With no provider flag, `bounds ci --install` auto-detects the one host your repo already uses (a `.github/` dir → GitHub; a `.gitlab-ci.yml` → GitLab) and installs only that — so a GitHub repo never gets a stray `.gitlab-ci.yml`. If it can't tell, it asks you to pick instead of guessing.

`--github` generates a `bounds preflight --ci` GitHub Action (idempotent, path-gated, with cache reuse keyed on your manifests); `--precommit` adds a local `bounds validate --quick --ci` hook. Each file lands at its canonical, host-mandated path (`.github/workflows/bounds.yml`, `.gitlab-ci.yml`, `.pre-commit-config.yaml`) and install is non-destructive — an existing pipeline keeps its jobs and the bounds entry is appended. Now the freshness rule is a failing check, not a habit you hope people keep. (`[skip bounds]` in a commit message is the documented escape hatch for the rare emergency.)

> **Today the generated config installs Bounds from git** (`pipx`/`pip install "git+https://github.com/Farzin312/bounds.git"`) because the `bounds-cli` PyPI package isn't published yet — so a committed pipeline works out of the box. When `bounds-cli` lands on PyPI the install line switches to it (the generated file records that intent in a comment).

### The four jobs CI does for you

The generated config wires up more than a single check. Each line in it maps to a distinct use case:

1. **Pre-PR gate.** `bounds preflight --ci` runs the full structural validation on every PR and **blocks the merge** on any blocking issue — drift, boundary violations, broken contracts, dependency cycles. This is the hard enforcement point.
2. **Incremental drift on every commit.** The optional `--precommit` hook runs `bounds validate --quick --ci` locally — git-diff incremental, sub-200ms — so an author catches drift at commit time, long before the PR gate.
3. **A drift baseline for *intentional* contract changes** (see below) — so a deliberate rewrite is a re-baseline commit, not a red build.
4. **Provider choice** — `--github`, `--gitlab`, `--precommit`, or `--all`; with no flag, Bounds auto-detects the one host your repo uses. The install is non-destructive (it appends to an existing pipeline) and every file lands at its host-mandated path.

### The drift baseline: intentional changes are not failures

A subsystem's public surface *should* change over time — that's healthy. The gate must fail on the drift you didn't mean, while letting a deliberate rewrite through. That's what `bounds calibrate --check` and the committed `.bounds/drift-baseline.json` are for:

- **`bounds calibrate --check`** (the freshness step in the generated CI config) compares the *current* manifest-vs-source drift against the baseline and **fails only on NEW drift above it** — never writes. The generated step is `bounds calibrate --check || true` so it's non-blocking until you commit a baseline and trust the signal; drop the `|| true` to make new drift fail the build.
- **`bounds calibrate --dump-baseline`** records the current drift as *accepted* in `.bounds/drift-baseline.json`. When you intentionally change a contract (and update its manifest in the same PR), re-run `--dump-baseline` and commit the updated baseline. The change becomes a deliberate, reviewable re-baseline commit rather than a surprise red build.

So the rule is: **unintended drift fails CI; an intentional surface change is a re-baseline you commit on purpose.** That keeps the gate honest without punishing legitimate evolution.

## The AI agent's definition of done

The same loop is what lets an agent **self-correct** after editing. The honest caveat first: this is advisory unless it's wired into a hook or CI — which is exactly why `bounds ci --install` matters. With the gate in place, the loop becomes enforced rather than merely recommended.

The loop an agent should follow:

1. **Before a risky change**, run `bounds impact <name>` to see the transitive blast radius and the exact interfaces each consumer relies on — reason about reach before editing.
2. Make the change.
3. **After editing**, run `bounds validate --quick`.
4. If `validation_status` is not `fresh`, read the `next_steps` in the payload or run
   `bounds validate -H`. Use `bounds calibrate` (`--apply` to write) for structural drift, but fix
   coverage gaps and cycles through the issue-specific guidance.

### Concrete example sequence

```bash
# An agent is about to change the `auth` subsystem's surface.

bounds impact auth              # who breaks? → billing, api, frontend (+ the exact interfaces)
bounds describe billing         # what does billing rely on from auth? (verified, a few hundred tokens for a small subsystem)

# ... the agent makes the edit, now knowing the reach ...

bounds validate --quick         # fast, zero-LLM structural self-check
# validation_status: "stale"  ← the surface changed; the manifest is now out of date

bounds calibrate --apply        # reconcile auth.yaml against tree-sitter reality
bounds validate --quick         # validation_status: "fresh" ✓

git add src/ .bounds/manifests/auth.yaml
git commit -m "feat(auth): rotate token API + update manifest"
# CI runs `bounds preflight --ci` on the PR and confirms nothing downstream broke
```

### Definition-of-done checklist

- [ ] `bounds impact <name>` was run before any change to a subsystem's public surface.
- [ ] The change is complete and `bounds validate --quick` reports `validation_status: fresh`.
- [ ] If it ever reported `stale`, `bounds calibrate --apply` was run and the manifest update is in **this** PR.
- [ ] `.bounds/manifests/*.yaml` changes are committed alongside the code, not in a follow-up.
- [ ] CI is installed (`bounds ci --install`) so the gate is enforced, not optional.

---

See also: [./why-bounds.md](./why-bounds.md) for the value model behind this discipline, and the [CLI reference](./cli-reference.md) and [AI agents guide](./ai-agents.md) for command-level detail.
