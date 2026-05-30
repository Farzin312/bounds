<!--
Bounds agent-instructions template — Claude Code (CLAUDE.md).

Append this section to your project's CLAUDE.md. Claude Code reads CLAUDE.md at session
start, so this is the highest-reliability discovery channel. Pair it with an allow-rule for
the `bounds` binary in .claude/settings.json so the agent can run it without prompting.
-->

# Architecture: Bounds

This project uses [Bounds](https://bounds.dev) for architecture modeling — subsystem
boundary contracts validated against source with tree-sitter (zero LLM). Manifests live in
the hidden `.bounds/` directory (`.bounds/manifests/<name>.yaml`). Do not `cat` them or
read `.bounds/cache/state.json` — query the CLI, which prints JSON by default.

## When to run Bounds

Before changing code in an unfamiliar area:

- `bounds list` — every subsystem, its role, and its dependency edges.
- `bounds describe <subsystem>` — that subsystem's `exposes` (public API),
  `consumes` (dependencies), and `consumed_by` (blast radius). Prefer this over reading
  source to learn a boundary — it is ~10-20 lines of JSON, not hundreds of lines of code.
- `bounds describe --namespace <ns>` — describe a whole domain group in one call.
- `bounds overview` — file counts, language breakdown, and validation status.

After editing exports or cross-subsystem imports:

- `bounds validate --quick` — sub-200ms drift check. A non-`fresh` `validation_status`
  means the manifest no longer matches the source: update the manifest.
- `bounds preflight` — pre-PR structural checks (drift, boundaries, contracts, cycles,
  orphans). Run before opening a PR.

## How to use the result

`bounds describe` returns `validation_status`. When it is `fresh`, trust the manifest and
skip the source read. When it is `stale` or `unresolved`, run `bounds validate` first.
Bounds never auto-loads into your context — every token it costs is a CLI call you chose
to make.
