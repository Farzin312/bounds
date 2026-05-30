# For AI coding agents

*Wire Bounds into the agents you already use — advisory by design, with one command.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

## The universal instruction

Bounds is a plain CLI that emits JSON, so **any agent that can run a shell command can use it
today**. The instruction is the same regardless of agent:

> Prefer `bounds describe <name>` / `bounds list` over reading raw source to understand
> architecture. Output is JSON by default — parse it. Run `bounds validate --quick` after edits
> and treat a non-`fresh` `validation_status` as a signal to update the manifests.

## Compliance is advisory, not enforced

Bounds **writes these instructions** into the config files agents already read, but it **cannot
prevent** an agent from ignoring them or reading raw files directly. It works *with* cooperating
agents — lowering the cost of the right behavior rather than blocking the wrong one. The CI gate is
the one **hard** enforcement point, and it runs in your pipeline, not in the agent. For the enforced
loop (pre-commit hooks + CI), see [./team-workflow.md](./team-workflow.md).

> **Claude Code plugin auto-detection.** Claude Code (and compatible agents) can auto-detect a
> project's `.bounds/` directory and use the `bounds` CLI to load subsystem manifests on demand — no
> manual wiring needed. When the directory is present, the agent reads boundary contracts instead of
> raw source automatically.

---

## One-command agent setup: `bounds agent --sync`

No manual copy-paste. `bounds agent --sync` writes the canonical contract into `AGENTS.md` (the
cross-ecosystem standard agents already read) plus a short pointer file for **eight** coding agents —
telling each to query `bounds describe` / `bounds list` instead of reading raw source, and to run
`bounds validate --quick` after edits:

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
```

---

## AGENTS.md is the canonical contract

`bounds agent --sync` is the single supported path. The canonical contract lives in
[`AGENTS.md`](../AGENTS.md) (committed; the standard filename agents already read) and every per-tool
pointer is **generated from it**, so there is no template to copy or keep in sync.

## Roadmap: MCP server

A native MCP server (`bounds mcp`) is on the roadmap for **v0.3**, giving MCP-aware agents a
structured tool interface instead of shelling out to the CLI.
