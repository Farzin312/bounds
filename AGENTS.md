<!-- BOUNDS:START -->
<!-- BOUNDS:GENERATED v=2026.6.24 h=7c78615b -->
> Managed by `bounds agent --sync` — edits inside this block are overwritten; edit the generator (`src/bounds/agents/content.py`) instead.

## Bounds — architecture contract for agents

Bounds models this codebase as subsystem boundary manifests. Query architecture through the CLI — never read raw `.bounds` files.

### Which command for which task
- Understand the layout / find the right subsystem → `bounds list`
- A subsystem's public surface or DB tables → `bounds describe <name>` (a few hundred tokens, tree-sitter-verified — use this before opening source or migrations)
- Every subsystem in a namespace → `bounds describe --namespace <ns>`
- Where a symbol or table is defined → `bounds where <symbol>`
- What breaks if you change a subsystem or table → `bounds impact <name>`
- Confirm an edit didn't drift the contract → `bounds validate --quick`
- Full mapping denominator / why one file is excluded → `bounds coverage` / `bounds coverage --why <path>`
- Project health, coverage, and trust guidance → `bounds overview`

### Workflow
1. `bounds list` before broad repo search.
2. `bounds describe <name>` to scope the contract, then read only the implementation files you need to edit.
3. `bounds impact <name>` before changing an interface or a migration.
4. If `bounds overview` reports partial coverage, overlaps, cycles, or validation errors, follow `health.validation.next_steps` before trusting that part of the map.
5. On an `E_COVERAGE_GAP`, use `bounds coverage`: assign `user_decision_needed` source to a subsystem; preview deterministic `algorithm_miss` exclusions with `bounds fix-coverage --auto`; hand-map dark unsupported source with durable `exposes:`.
6. `bounds validate --quick` after edits; it skips boundary/contract/cycle/coverage/orphan checks, so run full validation or preflight before claiming repo-wide health.

### When a lookup misses (don't fall back to grep)
A `bounds where <symbol>` that returns `count: 0` is NOT "bounds has nothing" — it usually means the name is a *sub-symbol* (a field, route, or key inside a larger exported object) or internal. The 0-result payload carries a `suggestions` block; follow it instead of grepping:
- `suggestions.did_you_mean` — symbols whose name contains your query; run the `try` command.
- `suggestions.subsystems` — subsystems whose name or description mentions your query; run `bounds describe <name>` to read the surface and find the concept in context (this is how you locate a route/field that lives inside one umbrella export).
- `suggestions.broaden` — retry with `--prefix` for a wider match.
A `bounds impact` miss likewise returns a `fix` with "did you mean" subsystems and a `bounds where <name>` hint when you passed a symbol where a subsystem was expected. The lookup chain is **symbol → owning-subsystem surface → file**, not "symbol or bust".

### Enforcement (CI is the hard gate, not agent goodwill)
- After manifests exist, wire the gate once: `bounds ci --install --github` (or `--gitlab`) generates the CI config that runs `bounds preflight --ci` on every PR. This — not agent compliance — is the hard enforcement of the contract.
- An **intentional** surface change is not drift to fight: update the manifest, then re-baseline with `bounds calibrate --dump-baseline` and commit `.bounds/drift-baseline.json`. `bounds calibrate --check` then fails only on NEW drift above that baseline.

### Output
- JSON by default — parse it. Add `-H`/`--human` for a readable view of the same data.

### Source of Truth
- GitHub is the single source of truth.
- This repo uses `setuptools-scm` for automatic versioning (no static version string).
- If the local `bounds` CLI is stale, run: `pipx install --force git+https://github.com/Farzin312/bounds.git`.

### Hard rules
- NEVER read `.bounds/cache.db`, `.bounds/*.json`, `.bounds/manifests/*.yaml`, or `.bounds/root.yaml` directly. The cache is binary; the manifests bypass tree-sitter verification.
- The CLI is the API for architecture. Use source files only for implementation details after Bounds has scoped the subsystem.

### Optional Spec-Driven Development
- If this repo enables `sdd:` in Bounds root config, treat Bounds as the verified architecture layer across specify → clarify → plan → tasks → analyze → implement → verify.
- Run `bounds sdd` for the configured phase map, `bounds sdd --phase <name>` for one command, or `bounds sdd --doctor` for architecture readiness.
- Bounds does not run or infer prose-workflow progress and never calls an LLM; it supplies deterministic facts (`overview`, `list`, `describe`, `where`, `impact`) and gates (`validate --quick`, `preflight --ci`, `calibrate --check`).
- Intentional contract changes belong in the spec: update the manifest, then re-baseline with `bounds calibrate --dump-baseline`.

**Invocation policy:** before grepping or opening files to answer an architecture question (what/where something is, what depends on it, what breaks if it changes), FIRST run the relevant Bounds command — `bounds list`, `bounds describe <area>`, `bounds where <symbol>`, `bounds impact <subsystem>`. Fall back to source search only when a lookup misses (a `count: 0` carries `suggestions` — try those first) or the area is unmapped / an unsupported language — then searching directly is expected.

## Bounds in Spec-Driven Development

SDD is enabled for `generic`; this `canonical` artifact wires Bounds into the project's customized SDD loop.
Bounds stays zero-LLM: it provides verified architecture facts and gates while the agent handles prose spec work.

### Phase contract
- **specify** → `bounds overview && bounds list` — ground the spec in the current map, coverage, and trust signals.
- **clarify** → `bounds describe <name> && bounds where <symbol>` — answer questions from verified subsystem and symbol context.
- **plan** → `bounds impact <name>` — account for declared consumers and the lower-bound blast radius.
- **tasks** → `bounds impact <name>` — order implementation work along subsystem dependency edges.
- **analyze** → `bounds validate` — run the full architecture checks before implementation.
- **implement** → `bounds validate --quick` — catch incremental drift after edits without claiming full health.
- **verify** → `bounds preflight --ci` — run the blocking architecture gate before review or merge.

### Freshness contract
- If the spec intentionally changes public surface, update the manifest in the same spec/plan change.
- Run `bounds calibrate --dump-baseline` only after the manifest reflects the intended contract.
- `bounds validate --quick` in the edit loop catches accidental drift; `bounds preflight --ci` is the final gate.
- For unsupported-language subsystems, keep hand-authored exposes in the manifest; calibrate routes unverifiable entries to review instead of deleting them.
<!-- BOUNDS:END -->
