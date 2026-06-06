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
| A subsystem's public surface or DB tables | `bounds describe <name>` |
| Where a symbol or table is defined | `bounds where <symbol>` |
| What breaks if you change a subsystem or table (blast radius) | `bounds impact <name>` |
| Confirm an edit didn't drift the contract | `bounds validate --quick` |
| Project health at a glance | `bounds overview` |

If the project opts into Spec-Driven Development with `sdd.enabled: true` in root.yaml, the same
generated files also include the SDD phase map: `overview`/`list` for `specify`,
`describe`/`where` for `clarify`, `impact` for `plan` and `tasks`, `validate`/`preflight` for
`analyze`, `validate --quick` for `implement`, and `preflight --ci` for `verify`. Bounds still does
not run the prose workflow or call a model; it provides verified facts and gates. See
[./sdd.md](./sdd.md).

## Compliance is advisory, not enforced

Bounds **writes these instructions** into the config files agents already read, but it **cannot
prevent** an agent from ignoring them or reading raw files directly. It works *with* cooperating
agents — lowering the cost of the right behavior rather than blocking the wrong one. The hard rule is
to avoid raw `.bounds` artifacts; source files are still appropriate once Bounds has scoped the
subsystem you need to edit. The CI gate is
the one **hard** enforcement point, and it runs in your pipeline, not in the agent. For the enforced
loop (pre-commit hooks + CI), see [./team-workflow.md](./team-workflow.md).

Between pure advice and the CI hard gate there is now a middle layer you can opt into: a harness hook
that actively *nudges* (or, at `strict`, *pauses*) the agent toward Bounds at search time — see
[Configuring agent invocation](#configuring-agent-invocation-off--nudge--strict). It is fail-open by
construction (a Bounds miss never blocks the agent), so it raises adoption without ever trapping a
legitimate search.

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

---

## Configuring agent invocation: `off` → `nudge` → `strict`

The instruction files above are *pull-based* — a cooperating agent reads them, but nothing makes it.
In practice agents often default to grep/search out of habit even when Bounds is wired, which is both
slower and far more token-expensive. To close that gap, `bounds agent --sync` can additionally write a
**harness hook** that actively reminds (or, at the strongest level, pauses) the agent at the moment it
reaches for a broad search. This is controlled by one knob in `root.yaml`:

```yaml
agentsync:
  invocation: nudge        # off | nudge | strict   (default: nudge)
```

Set it without hand-editing YAML — this writes `root.yaml` **and** re-syncs (installing, refreshing,
or removing the hook) in one step:

```bash
bounds agent --invocation off       # advisory files only (the pre-hook behavior)
bounds agent --invocation nudge      # gentle reminder hook (default)
bounds agent --invocation strict     # pause before a broad search Bounds can answer
bounds agent                          # bare detect also prints the current level
```

| Level | What it does |
|-------|--------------|
| **`off`** | Advisory files only — `AGENTS.md`, skills, rules. No hook is written; any previously-written Bounds hook is removed. This is exactly the pre-feature behavior. |
| **`nudge`** *(default)* | Everything in `off`, **plus** a one-line reminder injected into an architecture-shaped prompt — *"this repo uses Bounds; run `bounds describe`/`where`/`impact` instead of grepping."* Fires **once per session** and **never blocks** a tool. |
| **`strict`** | Everything in `nudge`, **plus** a pre-search gate: before a broad `Grep`/search-agent dispatch that Bounds can actually answer, it **pauses and asks** (you can still approve the search). It never hard-blocks (`deny`), and it only ever intercepts a search Bounds can answer. |

Every reminder ends with `disable: bounds agent --invocation off`, so the off-switch is always one
copy-paste away.

### Per-harness capability (graceful degradation)

Only **Claude Code** has a hook mechanism rich enough to enforce invocation today, so the levels
translate to the *strongest lever each harness actually supports* — you set one global level and
Bounds picks the mechanism per tool:

| Harness | `nudge` / `strict` mechanism |
|---------|------------------------------|
| **Claude Code** | Real harness hook in `.claude/settings.json` — `UserPromptSubmit` (nudge) and a `PreToolUse` gate on `Grep`/`Task` (strict). |
| **Cursor**, **Windsurf** | Always-on rule (`.cursor/rules`, `.windsurf/rules`) wording strengthened to an imperative "run Bounds first" directive. |
| **Codex**, **OpenCode**, **Gemini**, **Copilot**, **Aider** | The imperative directive folded into the always-read file (`AGENTS.md`, `GEMINI.md`, `.github/copilot-instructions.md`, `.aider.conf.yml`). No hook mechanism exists for these yet, so this is their strongest lever; **CI remains the universal hard gate**. |

### Non-regression guarantee (a miss is a green light, never a wall)

The hook is built so that **enforcement can only ever redirect the agent to an answer Bounds actually
has.** The moment Bounds can't answer, it *fails open* — it allows the tool and, where useful, hands
the agent Bounds' own recovery hints — so the stronger push can never turn a Bounds gap into a dead
end. Concretely, the `strict` gate stays silent (allows the search) when:

- there is **no `.bounds/`** in the project, or `bounds` isn't on `PATH`, or it errors/times out;
- the searched symbol/area is **not in Bounds' declared surface** (so it can't answer — searching is
  correct), an **unmapped area**, or an **unsupported language**;
- the agent **already ran a `bounds` command this session** (its follow-up search is legitimate);
- anything unexpected happens — any error degrades to "allow / no-op."

The gate's coverage check is **manifest-only** (it never runs a tree-sitter walk), so it stays well
inside the performance budget even though it fires on every search. The nudge reminder explicitly
tells the agent to grep directly when a lookup misses or an area is unmapped.

### What gets written, and is it committed?

`strict`/`nudge` add a small Bounds-owned block to **`.claude/settings.json`** (the project,
team-shared settings file), merged non-destructively: your own hooks and keys are preserved, and only
entries whose command is `bounds agent-hook` are managed by Bounds. A malformed `settings.json` is
reported and **left untouched**, never clobbered. Committing `.claude/settings.json` is what lets the
hook ship with the repo so teammates get it too. The once-per-session and "already consulted" state
the hook uses lives in a **transient temp file** (never committed; relocatable via
`BOUNDS_HOOK_STATE_DIR`).

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

When SDD is enabled in the root manifest, the generated block in each of these files also documents
how Bounds grounds and checks each SDD phase. Without that opt-in, the generated files stay focused
on the general Bounds command contract.

Shared files (`AGENTS.md`, `GEMINI.md`, and — for Claude — the always-loaded `CLAUDE.md`) get a
marked Bounds block that leaves your other content intact; hand-written configs are never clobbered.
If `CLAUDE.md` doesn't exist yet it is created; if it does, the block is appended without touching
your own instructions. The pointer is only the *instruction* layer — it tells the agent to use
Bounds. Most agents also get a native artifact that lets them *invoke* (or auto-trigger) Bounds; see
the next section.

**What ships vs what's generated:** only `AGENTS.md` is **committed** to the repo (the cross-ecosystem
standard file). Every per-tool pointer above — `CLAUDE.md`, `.claude/commands/bounds.md`, `GEMINI.md`,
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
bounds agent --invocation X    # set off|nudge|strict (how hard to push agents to Bounds), then re-sync
```

See [Configuring agent invocation](#configuring-agent-invocation-off--nudge--strict) for what the
`--invocation` levels do and how they translate per harness.

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

`bounds agent --check` follows the same ownership rule: an unmarked authored memory file that already
contains deliberate Bounds guidance is accepted, because re-running sync intentionally will not
replace it. An unrelated markerless file or an outdated managed block is still reported as stale.

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
subsystem's public surface or DB tables, **before** a risky change to a shared/core subsystem or a
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
- Run `bounds describe <name>` to read one subsystem's verified public surface/table catalog.
- Run `bounds impact <name>` before changing a subsystem interface or table.
- Run `bounds validate --quick` after edits and fix drift before expanding context.

Do not read `.bounds/cache.db`, `.bounds/*.json`, `.bounds/root.yaml`, or
`.bounds/manifests/*.yaml` directly. The CLI is the API: raw manifests bypass tree-sitter
verification, and the cache is a binary implementation detail.

If commands like `impact`, `coverage`, `sdd`, or `agent` are missing, the installed CLI is stale.
Refresh with `bounds upgrade` or, when the current directory is a Bounds clone,
`bounds upgrade --local .`.
```

## Roadmap: MCP server

A native MCP server (`bounds mcp`) is on the roadmap for **v0.3**, giving MCP-aware agents a
structured tool interface instead of shelling out to the CLI.
