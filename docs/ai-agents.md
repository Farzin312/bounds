# For AI coding agents

*Wire Bounds into the agents you already use — advisory by design, with one command.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

## The universal instruction

Bounds is a plain CLI that emits JSON, so **any agent that can run a shell command can use it
today**. The instruction is the same regardless of agent:

> Start with `bounds list`, then prefer `bounds describe <name>` over reading raw source or
> migration history to understand architecture. Use `bounds impact <name>` before changing a
> subsystem interface or table. Output is JSON by default — parse it. Run `bounds validate --quick`
> after edits and treat a non-`fresh` `validation_status` as a signal to update the manifests.

## Compliance is advisory, not enforced

Bounds **writes these instructions** into the config files agents already read, but it **cannot
prevent** an agent from ignoring them or reading raw files directly. It works *with* cooperating
agents — lowering the cost of the right behavior rather than blocking the wrong one. The CI gate is
the one **hard** enforcement point, and it runs in your pipeline, not in the agent. For the enforced
loop (pre-commit hooks + CI), see [./team-workflow.md](./team-workflow.md).

> **No auto-detection — wiring is one explicit command.** There is **no** plugin that auto-detects a
> project's `.bounds/` directory; nothing auto-loads it (by design — see the binary-cache note below).
> The supported path is to run `bounds agent --sync` **once** per repo: it writes an instruction file
> that each coding agent already reads (see the table below), telling the agent to query `bounds
> describe` / `bounds list` instead of reading raw source. Native MCP detection — where an MCP-aware
> agent discovers Bounds as a structured tool with no instruction file — is on the roadmap (v0.3),
> not shipped today.

---

## One-command agent setup: `bounds agent --sync`

No manual copy-paste. `bounds agent --sync` writes the canonical contract into `AGENTS.md` (the
cross-ecosystem standard agents already read) plus a short pointer file for **eight** coding agents —
telling each to query `bounds list`, `bounds describe`, and `bounds impact` before broad source
searches, and to run `bounds validate --quick` after edits:

| Agent | Config file written |
|-------|---------------------|
| **Claude Code** | `.claude/commands/bounds.md` |
| **Codex CLI** + **OpenCode** | shared `AGENTS.md` |
| **Gemini** | `GEMINI.md` |
| **GitHub Copilot** | `.github/copilot-instructions.md` |
| **Cursor** | `.cursor/rules/bounds.mdc` |
| **Aider** | `.aider.conf.yml` |
| **Windsurf** | `.windsurf/rules/bounds.md` |

Shared files (`AGENTS.md`, `GEMINI.md`) get a marked Bounds block that leaves your other content
intact; hand-written configs are never clobbered.

**What ships vs what's generated:** only `AGENTS.md` is **committed** to the repo (the cross-ecosystem
standard file). Every per-tool pointer above — `.claude/commands/bounds.md`, `GEMINI.md`,
`.cursor/rules/bounds.mdc`, `.windsurf/rules/bounds.md`, `.aider.conf.yml`,
`.github/copilot-instructions.md` — is **gitignored and regenerated locally** by `bounds agent --sync`,
so a clone stays lean. Run `bounds agent --sync` after cloning to (re)create the ones your editor uses.

### Companion flags

```bash
bounds agent --detect          # list which agents are present in this project
bounds agent --check           # verify each detected agent has a Bounds config
bounds agent --sync --claude   # scope --sync/--check to one agent (--codex, --cursor, …)
bounds agent --sync --all      # wire every supported agent, no prompt
```

Run interactively (`bounds agent --sync` in a terminal, no tool flags) and Bounds asks which
tools to wire — pre-selecting the ones it detected — so you pick yours instead of getting all
eight. A piped/CI run, an explicit `--<tool>` flag, or `--all` skips the prompt; `AGENTS.md`
(the canonical contract) is written either way.

---

## AGENTS.md is the canonical contract

`bounds agent --sync` is the single supported path. The canonical contract lives in
[`AGENTS.md`](../AGENTS.md) (committed; the standard filename agents already read) and every per-tool
pointer is **generated from it**, so there is no template to copy or keep in sync.

## Manual copy block

Use this only when you cannot run `bounds agent --sync` yet, or when you want to paste the guidance
into an existing `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, Cursor rule, or Copilot instruction file
without letting Bounds write files. Add it under your existing project instructions; do not replace
your current rules.

```md
## Bounds workflow for AI agents

This repo uses Bounds for architecture context. Query architecture through the CLI before broad
source search:

- Run `bounds list` first to see the subsystem map.
- Run `bounds describe <name>` to read one subsystem's verified public API/table catalog.
- Run `bounds impact <name>` before changing a subsystem interface or table.
- Run `bounds validate --quick` after edits and fix drift before expanding context.

Do not read `.bounds/cache.db`, `.bounds/*.json`, `.bounds/root.yaml`, or
`.bounds/manifests/*.yaml` directly. The CLI is the API: raw manifests bypass tree-sitter
verification, and the cache is a binary implementation detail.

If commands like `impact`, `discover`, or `agent` are missing, the installed CLI is stale. Refresh
with `bounds upgrade` or, from a local clone, `bounds upgrade --local .`.
```

## Roadmap: MCP server

A native MCP server (`bounds mcp`) is on the roadmap for **v0.3**, giving MCP-aware agents a
structured tool interface instead of shelling out to the CLI.
