<!-- BOUNDS:START -->
<!-- BOUNDS:GENERATED v=2026.6.24 h=bda66b52 -->
> Managed by `bounds agent --sync` — edits inside this block are overwritten; edit the generator (`src/bounds/agentsync.py`) instead.

## Bounds — architecture contract for agents

Bounds models this codebase as subsystem boundary manifests. Query architecture through the CLI — never read raw `.bounds` files.

### Which command for which task
- Understand the layout / find the right subsystem → `bounds list`
- A subsystem's public API or DB tables → `bounds describe <name>` (a few hundred tokens, tree-sitter-verified — use this before opening source or migrations)
- Every subsystem in a namespace → `bounds describe --namespace <ns>`
- Where a symbol or table is defined → `bounds where <symbol>`
- What breaks if you change a subsystem or table → `bounds impact <name>`
- Confirm an edit didn't drift the contract → `bounds validate --quick`
- Project health, coverage, and trust guidance → `bounds overview`

### Workflow
1. `bounds list` before broad repo search.
2. `bounds describe <name>` to scope the contract, then read only the implementation files you need to edit.
3. `bounds impact <name>` before changing an interface or a migration.
4. If `bounds overview` reports partial coverage, overlaps, cycles, or validation errors, follow `health.validation.next_steps` before trusting that part of the map.
5. `bounds validate --quick` after edits; fix drift before broadening context.

### Output
- JSON by default — parse it. Add `-H`/`--human` for a readable view of the same data.

### Source of Truth
- GitHub is the single source of truth.
- This repo uses `setuptools-scm` for automatic versioning (no static version string).
- If the local `bounds` CLI is stale, run: `pipx install --force git+https://github.com/Farzin312/bounds.git`.

### Hard rules
- NEVER read `.bounds/cache.db`, `.bounds/*.json`, `.bounds/manifests/*.yaml`, or `.bounds/root.yaml` directly. The cache is binary; the manifests bypass tree-sitter verification.
- The CLI is the API for architecture. Use source files only for implementation details after Bounds has scoped the subsystem.
<!-- BOUNDS:END -->
