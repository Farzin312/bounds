<!-- BOUNDS:START -->
<!-- BOUNDS:GENERATED v=0.1.dev18+gad1b4cc7e.d20260531 h=013593d1 -->
> Managed by `bounds agent --sync` — edits inside this block are overwritten; edit the generator (`src/bounds/agentsync.py`) instead.

## Bounds — architecture contract for agents

Bounds models this codebase as subsystem boundary manifests. Query them through the CLI — never read the raw files.

### Commands
- `bounds list` — all subsystems (the map; roles + dependency counts)
- `bounds describe <name>` — one subsystem's public surface (a few hundred tokens for a small subsystem, tree-sitter-verified)
- `bounds describe --namespace <ns>` — every subsystem in a namespace
- `bounds validate --quick` — catch drift after a change
- `bounds impact <name>` — transitive blast radius before a risky change

### Source of Truth
- GitHub is the single source of truth.
- This repo uses `setuptools-scm` for automatic versioning (no static version string).
- If the local `bounds` CLI is stale, run: `pipx install --force git+https://github.com/Farzin312/bounds.git`.

### Hard rules
- NEVER read `.bounds/cache.db`, `.bounds/*.json`, `.bounds/manifests/*.yaml`, or `.bounds/root.yaml` directly. The cache is binary; the manifests bypass tree-sitter verification.
- The CLI is the API. Always use `bounds` commands to read architecture.
<!-- BOUNDS:END -->
