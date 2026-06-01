<!-- BOUNDS:START -->
<!-- BOUNDS:GENERATED v=2026.6.24 h=9dea5636 -->
> Managed by `bounds agent --sync` — edits inside this block are overwritten; edit the generator (`src/bounds/agentsync.py`) instead.

## Bounds — architecture contract for agents

Bounds models this codebase as subsystem boundary manifests. Query them through the CLI — never read the raw files.

### Which command for which task
- Understand the layout / find the right subsystem → `bounds list`
- A subsystem's public API or DB tables → `bounds describe <name>` (a few hundred tokens, tree-sitter-verified — read this instead of opening source or migrations)
- Every subsystem in a namespace → `bounds describe --namespace <ns>`
- Where a symbol or table is defined → `bounds where <symbol>`
- What breaks if you change a subsystem or table → `bounds impact <name>`
- Confirm an edit didn't drift the contract → `bounds validate --quick`
- Project health at a glance → `bounds overview`

### Workflow
1. `bounds list` before searching the repo.
2. `bounds describe <name>` instead of opening source or migrations.
3. `bounds impact <name>` before changing an interface or a migration.
4. `bounds validate --quick` after edits; fix drift before broadening context.

### Output
- JSON by default — parse it. Add `-H`/`--human` for a readable view of the same data.

### Source of Truth
- GitHub is the single source of truth.
- This repo uses `setuptools-scm` for automatic versioning (no static version string).
- If the local `bounds` CLI is stale, run: `pipx install --force git+https://github.com/Farzin312/bounds.git`.

### Hard rules
- NEVER read `.bounds/cache.db`, `.bounds/*.json`, `.bounds/manifests/*.yaml`, or `.bounds/root.yaml` directly. The cache is binary; the manifests bypass tree-sitter verification.
- The CLI is the API. Always use `bounds` commands to read architecture.
<!-- BOUNDS:END -->
