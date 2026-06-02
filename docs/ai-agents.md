# For AI coding agents

*Wire Bounds into the agents you already use — advisory by design, with one command.*

[← Docs index](./README.md) · [Bounds README](../README.md)

---

## The universal instruction

Bounds is a plain CLI that emits JSON, so **any agent that can run a shell command can use it
today**. It is not a Claude-only or Codex-only workflow; the generated native files are conveniences
for tools that have a command/skill format, while the underlying contract is just CLI commands. The
instruction is the same regardless of agent:

> Start with `bounds list`, then prefer `bounds describe <name>` before reading source or
> migration history to understand architecture. Use `bounds impact <name>` before changing a
> subsystem interface or table. Output is JSON by default — parse it. Run `bounds validate --quick`
> after edits and treat a non-`fresh` `validation_status` as a signal to update the manifests.

### Which command for which task

The contract Bounds writes leads with this mapping so the agent knows exactly what to reach for:

| Task | Command |
|------|---------|
| Understand the layout / find the right subsystem | `bounds list` |
| A subsystem's public API or DB tables | `bounds describe <name>` |
| Where a symbol or table is defined | `bounds where <symbol>` |
| What breaks if you change a subsystem or table (blast radius) | `bounds impact <name>` |
| Confirm an edit didn't drift the contract | `bounds validate --quick` |
| Project health at a glance | `bounds overview` |

## Compliance is advisory, not enforced

Bounds **writes these instructions** into the config files agents already read, but it **cannot
prevent** an agent from ignoring them or reading raw files directly. It works *with* cooperating
agents — lowering the cost of the right behavior rather than blocking the wrong one. The hard rule is
to avoid raw `.bounds` artifacts; source files are still appropriate once Bounds has scoped the
subsystem you need to edit. The CI gate is
the one **hard** enforcement point, and it runs in your pipeline, not in the agent. For the enforced
loop (pre-commit hooks + CI), see [./team-workflow.md](./team-workflow.md).

Once manifests exist, an agent can wire that gate itself in one command: `bounds ci --install --github`
(or `--gitlab`) generates the CI config that runs `bounds preflight --ci` on every PR — the hard
enforcement of the contract, where agent compliance is only advisory. When the agent makes an
*intentional* surface change, that is not drift to fight: it updates the manifest, runs `bounds
calibrate --dump-baseline`, and commits the refreshed `.bounds/drift-baseline.json` so the gate fails
only on *new*, unintended drift. (This is exactly what the generated `AGENTS.md` contract tells the
agent — see [AGENTS.md is the canonical contract](#agentsmd-is-the-canonical-contract).)

Agents should also know the boundary of the tool. Bounds is a map, not execution or review: it can
show owners, exports, table surfaces, drift, and blast radius; it cannot prove runtime behavior,
business correctness, performance, or security. Use it before broad source search and after edits,
then still read the scoped implementation files and run the relevant tests.

For partial maps, use `bounds overview` first. Its `health.validation.trust_note` and `next_steps`
tell you whether the map is fully covered, which gaps remain, and whether to regenerate, resolve
duplicate ownership, or inspect source outside the mapped area. Do not present an unmapped area as
verified architecture.

> **No auto-loading — wiring is one explicit command.** There is **no** plugin that auto-detects a
> project's `.bounds/` directory; nothing auto-loads it (by design — see the binary-cache note below).
> The supported path is to run `bounds agent --sync` **once** per repo: it writes an instruction file
> that each coding agent already reads (see the table below), telling the agent to query `bounds
> describe` / `bounds list` before broad source reading — and, for most agents, a native command or
> auto-triggering skill so the agent can *invoke* Bounds, not just be told to (see
> [Native commands & skills](#native-commands--skills-not-just-a-pointer)). (Run bare `bounds agent`
> first if you just want to see which agents are present — it is read-only and writes nothing.) Native
> MCP detection —
> where an MCP-aware agent discovers Bounds as a structured tool with no instruction file — is on the
> roadmap (v0.3), not shipped today.

---

## Agent setup in three steps: detect → sync → check

No manual copy-paste. The flow is three commands, safe to run in order:

1. **`bounds agent`** — bare, read-only. Lists which coding agents this repo already has (it
   defaults to `--detect`, so typing it does nothing destructive). Start here.
2. **`bounds agent --sync`** — wire them. Writes the canonical contract into `AGENTS.md`, a
   pointer file for each agent, and — for the agents that support one — a native invokable
   command or auto-triggering skill (see [Native commands & skills](#native-commands--skills-not-just-a-pointer) below).
3. **`bounds agent --check`** — verify the wiring is current (JSON by default, so it drops straight
   into CI).

`bounds agent --sync` writes the canonical contract into `AGENTS.md` (the cross-ecosystem standard
agents already read) plus a short pointer file for **eight** coding agents — telling each to query
`bounds list`, `bounds describe`, and `bounds impact` before broad source searches, and to run
`bounds validate --quick` after edits:

| Agent | Pointer file written |
|-------|---------------------|
| **Claude Code** | `.claude/commands/bounds.md` |
| **Codex CLI** + **OpenCode** | shared `AGENTS.md` |
| **Gemini** | `GEMINI.md` |
| **GitHub Copilot** | `.github/copilot-instructions.md` |
| **Cursor** | `.cursor/rules/bounds.mdc` |
| **Aider** | `.aider.conf.yml` |
| **Windsurf** | `.windsurf/rules/bounds.md` |

Shared files (`AGENTS.md`, `GEMINI.md`) get a marked Bounds block that leaves your other content
intact; hand-written configs are never clobbered. The pointer is only the *instruction* layer — it
tells the agent to use Bounds. Most agents also get a native artifact that lets them *invoke* (or
auto-trigger) Bounds; see the next section.

**What ships vs what's generated:** only `AGENTS.md` is **committed** to the repo (the cross-ecosystem
standard file). Every per-tool pointer above — `.claude/commands/bounds.md`, `GEMINI.md`,
`.cursor/rules/bounds.mdc`, `.windsurf/rules/bounds.md`, `.aider.conf.yml`,
`.github/copilot-instructions.md` — plus the native command/skill artifacts (see
[Native commands & skills](#native-commands--skills-not-just-a-pointer)) is **gitignored and
regenerated locally** by `bounds agent --sync`, so a clone stays lean. Run `bounds agent --sync` after
cloning to (re)create the ones your editor uses.

### Commands & flags

```bash
bounds agent                   # bare = --detect: list which agents are present (read-only)
bounds agent --sync            # wire the detected agents (prompts in a terminal; see below)
bounds agent --sync --claude   # scope --sync/--check to one agent (--codex, --cursor, …)
bounds agent --sync --all      # wire every supported agent, no prompt
bounds agent --check           # verify each detected agent has an up-to-date Bounds config
```

Bare `bounds agent` is the read-only first step — it runs `--detect`, never errors, and writes
nothing. The three modes (`--detect`, `--sync`, `--check`) are mutually exclusive; passing two at
once is a usage error.

Run interactively (`bounds agent --sync` in a terminal, no tool flags) and Bounds asks which
tools to wire — pre-selecting the ones it detected — so you pick yours instead of getting all
eight. A piped/CI run, an explicit `--<tool>` flag, or `--all` skips the prompt; `AGENTS.md`
(the canonical contract) is **always** written either way.

---

## AGENTS.md is the canonical contract

`bounds agent --sync` is the single supported path. The canonical contract lives in
[`AGENTS.md`](../AGENTS.md) (committed; the standard filename agents already read) and every per-tool
pointer is **generated from it**, so there is no template to copy or keep in sync. `AGENTS.md` is
**always** (re)written by `--sync`, regardless of which agents you select — every pointer references it.

**It won't clobber a hand-written doc.** If your project already keeps a big hand-authored `AGENTS.md`
(or `GEMINI.md`) that happens to mention bounds but has no Bounds markers, `--sync` **leaves it alone**
— that's intentional, so your doc survives. To let Bounds manage a section inside it, add empty
`<!-- BOUNDS:START -->` / `<!-- BOUNDS:END -->` markers where you want the block, then re-sync; Bounds
fills and maintains only what's between the markers and never touches the rest.

**What `--sync` reports.** Each file lands in one of these buckets, so you can see exactly what
happened:

- `created` — the file (or its Bounds block) was written for the first time.
- `updated` — an existing Bounds block was refreshed to the current contract.
- `already current` — the block matched; nothing to do.
- `left alone (you maintain these)` — a human-authored file that mentions bounds but carries no
  markers; opt it in with the marker pair above.
- `left alone (you edited the bounds block)` — a managed block whose body you hand-edited since the
  last sync; Bounds won't overwrite your changes.

## Native commands & skills (not just a pointer)

The `AGENTS.md` contract and the per-agent pointer files are the **instruction** layer: they tell a
cooperating agent to reach for `bounds describe` / `bounds list` / `bounds impact` before broad source
reading, and to avoid raw `.bounds` files. But an instruction the agent has to remember to follow is weaker than a command it can
*invoke* — or a skill that *fires on its own* at the right moment. So `bounds agent --sync` also
generates, for each agent that supports one, a **native, invokable command or auto-triggering skill**
in that tool's own format and location:

| Agent | Native artifact | Kind | Argument syntax |
|-------|-----------------|------|-----------------|
| **Claude Code** | `.claude/skills/bounds/SKILL.md` (+ the `/bounds` dispatcher at `.claude/commands/bounds.md`) | Auto-triggering skill | — |
| **Codex CLI** | `.agents/skills/bounds/SKILL.md` | Auto-triggering skill | — |
| **Gemini** | `.gemini/commands/bounds.toml` | TOML custom command | `{{args}}` |
| **OpenCode** | `.opencode/commands/bounds.md` | Markdown command | `$ARGUMENTS` |
| **GitHub Copilot** | `.github/prompts/bounds.prompt.md` | Prompt file | `${input:…}` |
| **Cursor** | `.cursor/commands/bounds.md` | Command (the always-on `.cursor/rules/bounds.mdc` is the pointer) | — |
| **Windsurf** | `.windsurf/workflows/bounds.md` | Workflow (the always-on `.windsurf/rules/bounds.md` is the pointer) | — |
| **Aider** | **none** | — | — |

**The auto-trigger skills are the intelligence layer.** A Claude/Codex `SKILL.md` carries the decision
logic in its `description` front-matter — the matcher the model reads to decide *when* to invoke
Bounds. It encodes concrete trigger conditions, not a tagline: exploring an unfamiliar area, needing a
subsystem's public API or DB tables, **before** a risky change to a shared/core subsystem or a
migration (check the blast radius first), "what depends on X" / "what breaks if X changes," and
verifying drift after an edit. With the skill in place the agent reaches for Bounds on its own, rather
than only when a human reminds it.

**Aider gets none — on purpose.** Aider has no committable custom-command mechanism, so it receives
only its `.aider.conf.yml` pointer. Bounds never fakes a command file a tool won't actually load; an
honest "no native command here" beats a dead file.

**Every artifact is bounds-owned, marker-managed, and hand-edit safe.** Each is a dedicated
(bounds-only) file wrapped in `<!-- BOUNDS:START -->` / `<!-- BOUNDS:END -->` markers and stamped with
the bounds version + a content hash, so a re-sync of an unedited file is a no-op (`already current`).
If a human edits the body inside the markers, the next sync reports it `left alone (you edited the
bounds block)` and never clobbers the change. Like the per-tool pointers, these native artifacts are
**gitignored and regenerated locally** — run `bounds agent --sync` after cloning to (re)create the
ones your editor uses.

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
