<!-- BOUNDS:START -->
<!-- BOUNDS:GENERATED v=0.1.dev18+gad1b4cc7e.d20260531 h=aa2dffee -->
> Managed by `bounds agent --sync` — edits inside this block are overwritten; edit the generator (`src/bounds/agentsync.py`) instead.

## Bounds — architecture contract for agents

Bounds models this codebase as subsystem boundary manifests. Query them through the CLI — never read the raw files.

### Agent workflow
- Start with `bounds list` to find the subsystem map before searching the repo.
- Read `bounds describe <name>` for the verified public API/table catalog instead of opening source or migrations first.
- Run `bounds impact <name>` before changing a subsystem interface; run `bounds impact <table>` before editing migrations.
- After edits, run `bounds validate --quick` and fix drift before broadening context.

### Commands
- `bounds list` — all subsystems (the map; roles + dependency counts)
- `bounds describe <name>` — one subsystem's public surface/table catalog (a few hundred tokens for a small subsystem, tree-sitter-verified)
- `bounds describe --namespace <ns>` — every subsystem in a namespace
- `bounds validate --quick` — catch drift after a change
- `bounds impact <name>` — transitive blast radius before a risky code or schema change

### Source of Truth
- GitHub is the single source of truth.
- This repo uses `setuptools-scm` for automatic versioning (no static version string).
- If the local `bounds` CLI is stale, run: `pipx install --force git+https://github.com/Farzin312/bounds.git`.

### Hard rules
- NEVER read `.bounds/cache.db`, `.bounds/*.json`, `.bounds/manifests/*.yaml`, or `.bounds/root.yaml` directly. The cache is binary; the manifests bypass tree-sitter verification.
- The CLI is the API. Always use `bounds` commands to read architecture.
<!-- BOUNDS:END -->
