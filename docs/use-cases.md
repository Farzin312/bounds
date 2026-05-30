# Use cases

*Three concrete workflows where a Bounds contract beats reading raw source — with realistic walkthroughs.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

These are the three jobs Bounds is built for. Each one trades a pile of source reads for one or two
cheap, tree-sitter-verified CLI calls. See [./cli-reference.md](./cli-reference.md) for the full flag
set on every command, and [./team-workflow.md](./team-workflow.md) for how these fit into a team's
day-to-day.

---

## 1. "Will this change break anything?"

The hardest question in a large codebase is *what does this change touch*. Bounds answers it
structurally, in a few hundred tokens, so an agent (or you) can reason about blast radius **before**
writing the change and **prove** nothing broke after.

```bash
bounds impact auth          # who depends on auth? → billing, api, frontend (+ the exact interfaces)
bounds describe billing     # what does billing rely on from auth? (verified contract, ~400 tokens)
# … the agent makes the change, now knowing the reach …
bounds preflight            # contracts + boundaries + cycles + drift — fails if a consumer was broken
```

`bounds impact` returns the transitive consumer set and the interfaces each consumer relies on.
`bounds describe` then hands back the verified public surface of any consumer you want to look at
more closely — every interface flagged `verified: true/false` so you know tree-sitter confirmed it
exists. `bounds preflight` finally catches the failure modes that actually matter — a removed export
a consumer still imports, a new cross-boundary dependency, an introduced cycle — each with a
deterministic fix suggestion.

### Walkthrough

You're asked to rename `auth.verify` to `auth.verify_token`. Before touching anything:

1. `bounds impact auth` reports that `billing`, `api`, and `frontend` consume `auth`, and lists the
   exact interfaces each relies on. `frontend` relies on `verify`.
2. `bounds describe frontend` confirms how `frontend` declares its dependency on that interface — a
   ~400-token contract instead of opening the `frontend/` source tree.
3. You make the rename and update the three consumer manifests.
4. `bounds preflight` runs the 6 checks; if you missed a consumer, it fails with the orphaned import
   named and a fix suggested.

The agent never had to read `billing/`, `api/`, and `frontend/` to learn it was about to break them.
The reach came from the dependency graph; the proof came from `preflight`. That's the whole point —
the downstream subsystems stayed out of context until a single CLI call pulled in exactly the slice
that mattered.

---

## 2. Drop an agent into a strange repo and be productive fast

A fresh agent (or a new contributor) normally burns its first thousands of tokens spelunking
directories and grepping for `class` / `def` / `export` to reconstruct the architecture. Two calls
replace all of it.

```bash
bounds list                 # the whole architecture map (roles + dependency counts) — one cheap call
bounds describe payments    # one subsystem's verified public surface, instead of opening its files
```

`bounds list` is the whole-system map: every subsystem with its role, criticality, and dependency
edges in one JSON object. `bounds describe <name>` then zooms into a single subsystem's verified
public surface without opening a single source file.

### Walkthrough

A new agent is asked to add a discount field to the checkout flow in an unfamiliar repo:

1. `bounds list` returns the full map — the agent sees `payments`, `checkout`, `cart`, and `auth`,
   their roles, and that `checkout` consumes `payments` and `cart`. One call, no grepping.
2. `bounds describe checkout` returns `checkout`'s verified exposes and its declared consumes — the
   agent now knows the public surface it must preserve and the dependencies it can rely on.
3. The agent makes a targeted edit, having read zero source files to build its mental model.

No directory spelunking, no guessing where a symbol lives. `bounds agent --sync` wires this in as the
default workflow for whatever agent the contributor uses, so a cooperating agent reaches for `list` /
`describe` before it reaches for the file tree.

---

## 3. Enforce architecture in CI, not in a wiki

Architecture rules written in a wiki page rot the moment someone ignores them. Bounds turns boundary
violations and drift into a failing check with a fix suggestion — a toggle, not a convention nobody
follows.

```bash
bounds ci --install         # generate a GitHub Action / pre-commit hook / GitLab job
```

`bounds ci --install` generates ready-to-commit, idempotent, path-gated gate config:

- **`.github/workflows/bounds.yml`** runs `bounds preflight --ci` and caches the warm extraction
  cache keyed on `root.yaml` + the manifests.
- **`.pre-commit-config.yaml`** adds a local `bounds validate --quick --ci` hook.
- **`.gitlab-ci.yml`** is the GitLab equivalent.

Use `--action` / `--precommit` / `--gitlab` / `--all` to pick targets.

### Walkthrough

Your team keeps accidentally importing across a boundary that's supposed to stay decoupled:

1. `bounds ci --install --action` drops `.github/workflows/bounds.yml` into the repo. Commit it.
2. A teammate opens a PR that adds a cross-boundary dependency `auth → billing` that no manifest
   declares.
3. The CI job runs `bounds preflight --ci` and fails the check, naming the undeclared edge and
   suggesting the fix (declare the `consumes` edge, or don't add it).
4. The reviewer sees a red check with a concrete reason instead of relying on someone remembering the
   wiki rule.

This is the one **hard** enforcement point in Bounds. Everything an agent does is advisory — the CI
gate is the only place a violation actually blocks a merge, and it runs in your pipeline, not inside
the agent. Putting `[skip bounds]` in a commit message is the documented escape hatch.

---

See also: [./cli-reference.md](./cli-reference.md) for every command and flag, and
[./team-workflow.md](./team-workflow.md) for adopting these across a team.
