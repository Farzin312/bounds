<!--
Bounds agent-instructions template — cross-tool standard (AGENTS.md).

Copy this block into your project's AGENTS.md (used by Codex CLI, OpenCode, and any
agent that reads the AGENTS.md convention). It is the single source other tool-specific
templates (CLAUDE.md, .cursorrules) mirror. Keep it short: agents read it every session.
-->

## Architecture: Bounds

This project uses [Bounds](https://bounds.dev) for architecture modeling — subsystem
boundary contracts validated against source with tree-sitter (zero LLM). Manifests live in
the hidden `.bounds/` directory (`.bounds/manifests/<name>.yaml`); never read them
directly — use the CLI, which emits JSON by default.

Before making significant changes:

- `bounds list` — discover the subsystems and how they connect.
- `bounds describe <subsystem>` — the subsystem's public surface (`exposes`), its
  dependencies (`consumes`), and who depends on it (`consumed_by`). Read this instead of
  opening raw source to understand a boundary.
- `bounds describe --namespace <ns>` — every subsystem in a domain at once.
- `bounds overview` — project health: file counts, language breakdown, validation status.

After any change that touches exports or cross-subsystem imports:

- `bounds validate --quick` — fast (sub-200ms) drift check. Treat a non-`fresh`
  `validation_status` as a signal to update the affected manifest, not to ignore.
- `bounds preflight` — boundary, contract, cycle, and orphan checks before opening a PR.

Trust rule: when `validation_status` is `fresh`, rely on the manifest and skip the source
read. When it is `stale` or `unresolved`, run `bounds validate` first, then trust.
