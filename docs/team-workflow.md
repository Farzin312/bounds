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

1. **`bounds discover`** groups source by directory, tree-sitter-extracts each candidate's verified `exposes`, infers `consumes` from the cross-candidate import graph, and seeds `role`/`criticality` from graph degree. It is a dry-run by default and never overwrites existing manifests.
2. **Review the manifests.** Discovery proposes; humans decide. This is where you correct boundaries the heuristic got wrong and confirm the roles.
3. **Commit `.bounds/root.yaml` and `.bounds/manifests/`** so the contract is versioned with the code. (The cache `.bounds/cache.db` is gitignored and regenerated — never commit it.)
4. **`bounds agent --sync`** writes the canonical contract into `AGENTS.md` plus per-tool pointer files, telling each agent to query `bounds describe`/`bounds list` instead of reading raw source.

## The freshness loop (the core discipline)

This is the rule that keeps the whole thing honest:

> **A PR that changes a subsystem's public surface MUST update that subsystem's manifest — in the same PR.**

You make that rule real with a CI gate, not a wiki page nobody reads. Every PR runs `bounds validate --quick` (git-diff incremental, safe for every commit). When the gate detects drift, the author runs `bounds calibrate` to see the proposed manifest changes, `bounds calibrate --apply` to write them, and includes that manifest update **in the same PR** as the code change. Code and contract move together, always.

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

### Make it enforced, not optional

Everything above works by convention until you wire it into the pipeline. Agent compliance is **advisory** — Bounds can suggest the right behavior but cannot force it — so the one hard enforcement point is CI. Install it:

```bash
bounds ci --install        # generates a GitHub Action + pre-commit hook (and GitLab job)
```

This generates a `bounds preflight --ci` GitHub Action (idempotent, path-gated, with cache reuse keyed on your manifests) and a local `bounds validate --quick --ci` pre-commit hook. Now the freshness rule is a failing check, not a habit you hope people keep. (`[skip bounds]` in a commit message is the documented escape hatch for the rare emergency.)

## The AI agent's definition of done

The same loop is what lets an agent **self-correct** after editing. The honest caveat first: this is advisory unless it's wired into a hook or CI — which is exactly why `bounds ci --install` matters. With the gate in place, the loop becomes enforced rather than merely recommended.

The loop an agent should follow:

1. **Before a risky change**, run `bounds impact <name>` to see the transitive blast radius and the exact interfaces each consumer relies on — reason about reach before editing.
2. Make the change.
3. **After editing**, run `bounds validate --quick`.
4. If `validation_status` is not `fresh`, run `bounds calibrate` (`--apply` to write) and **commit the manifest change together with the code change** — never separately.

### Concrete example sequence

```bash
# An agent is about to change the `auth` subsystem's surface.

bounds impact auth              # who breaks? → billing, api, frontend (+ the exact interfaces)
bounds describe billing         # what does billing rely on from auth? (verified, ~400 tokens)

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
