"""Canonical contract and pointer bodies for coding agents.

Separated from sync.py so orchestration and generated content have distinct owners.
"""

from __future__ import annotations

import re

# markers
_MD_START = "<!-- BOUNDS:START -->"
_MD_END = "<!-- BOUNDS:END -->"
_YAML_START = "# BOUNDS:START"
_YAML_END = "# BOUNDS:END"

# stamps (captured in AGENTS.md / CLAUDE.md / etc.)
_MD_STAMP_RE = re.compile(r"<!-- BOUNDS:GENERATED v=(?P<v>\S+) h=(?P<h>[0-9a-f]{8}) -->")
_YAML_STAMP_RE = re.compile(r"# BOUNDS:GENERATED v=(?P<v>\S+) h=(?P<h>[0-9a-f]{8})")


CANONICAL_NAME = "AGENTS.md"

CANONICAL_BODY = """\
> Managed by `bounds agent --sync` — edits inside this block are overwritten; edit the generator (`src/bounds/agents/content.py`) instead.

## Bounds — architecture contract for agents

Bounds models this codebase as subsystem boundary manifests. Query architecture through the CLI — never read raw `.bounds` files.

### Which command for which task
- Understand the layout / find the right subsystem → `bounds list`
- A subsystem's public surface or DB tables → `bounds describe <name>` (a few hundred tokens, tree-sitter-verified — use this before opening source or migrations)
- Every subsystem in a namespace → `bounds describe --namespace <ns>`
- Where a symbol or table is defined → `bounds where <symbol>`
- What breaks if you change a subsystem or table → `bounds impact <name>`
- Confirm an edit didn't drift the contract → `bounds validate --quick`
- Full mapping denominator / why one file is excluded → `bounds coverage` / `bounds coverage --why <path>`
- Project health, coverage, and trust guidance → `bounds overview`

### Workflow
1. `bounds list` before broad repo search.
2. `bounds describe <name>` to scope the contract, then read only the implementation files you need to edit.
3. `bounds impact <name>` before changing an interface or a migration.
4. If `bounds overview` reports partial coverage, overlaps, cycles, or validation errors, follow `health.validation.next_steps` before trusting that part of the map.
5. On an `E_COVERAGE_GAP`, use `bounds coverage`: assign `user_decision_needed` source to a subsystem; preview deterministic `algorithm_miss` exclusions with `bounds fix-coverage --auto`; hand-map dark unsupported source with durable `exposes:`.
6. `bounds validate --quick` after edits; it skips boundary/contract/cycle/coverage/orphan checks, so run full validation or preflight before claiming repo-wide health.

### When a lookup misses (don't fall back to grep)
A `bounds where <symbol>` that returns `count: 0` is NOT "bounds has nothing" — it usually means the name is a *sub-symbol* (a field, route, or key inside a larger exported object) or internal. The 0-result payload carries a `suggestions` block; follow it instead of grepping:
- `suggestions.did_you_mean` — symbols whose name contains your query; run the `try` command.
- `suggestions.subsystems` — subsystems whose name or description mentions your query; run `bounds describe <name>` to read the surface and find the concept in context (this is how you locate a route/field that lives inside one umbrella export).
- `suggestions.broaden` — retry with `--prefix` for a wider match.
A `bounds impact` miss likewise returns a `fix` with "did you mean" subsystems and a `bounds where <name>` hint when you passed a symbol where a subsystem was expected. The lookup chain is **symbol → owning-subsystem surface → file**, not "symbol or bust".

### Enforcement (CI is the hard gate, not agent goodwill)
- After manifests exist, wire the gate once: `bounds ci --install --github` (or `--gitlab`) generates the CI config that runs `bounds preflight --ci` on every PR. This — not agent compliance — is the hard enforcement of the contract.
- An **intentional** surface change is not drift to fight: update the manifest, then re-baseline with `bounds calibrate --dump-baseline` and commit `.bounds/drift-baseline.json`. `bounds calibrate --check` then fails only on NEW drift above that baseline.

### Output
- JSON by default — parse it. Add `-H`/`--human` for a readable view of the same data.

### Source of Truth
- GitHub is the single source of truth.
- This repo uses `setuptools-scm` for automatic versioning (no static version string).
- If the local `bounds` CLI is stale, run: `pipx install --force git+https://github.com/Farzin312/bounds.git`.

### Hard rules
- NEVER read `.bounds/cache.db`, `.bounds/*.json`, `.bounds/manifests/*.yaml`, or `.bounds/root.yaml` directly. The cache is binary; the manifests bypass tree-sitter verification.
- The CLI is the API for architecture. Use source files only for implementation details after Bounds has scoped the subsystem.

### Optional Spec-Driven Development
- If this repo enables `sdd:` in Bounds root config, treat Bounds as the verified architecture layer across specify → clarify → plan → tasks → analyze → implement → verify.
- Run `bounds sdd` for the configured phase map, `bounds sdd --phase <name>` for one command, or `bounds sdd --doctor` for architecture readiness.
- Bounds does not run or infer prose-workflow progress and never calls an LLM; it supplies deterministic facts (`overview`, `list`, `describe`, `where`, `impact`) and gates (`validate --quick`, `preflight --ci`, `calibrate --check`).
- Intentional contract changes belong in the spec: update the manifest, then re-baseline with `bounds calibrate --dump-baseline`.
"""

# The token-lean body shared by every per-agent *pointer* file. Kept short on purpose:
# its only job is to redirect the agent to AGENTS.md and the essentials.
AGENT_POINTER_BODY = """\
This project uses **Bounds** to model its architecture as subsystem boundary manifests.
Read the architecture through the Bounds CLI, never by opening raw `.bounds` files.

- Find the right subsystem → `bounds list`
- A subsystem's surface/tables → `bounds describe <name>` (verified, a few hundred tokens — use before opening source)
- Where a symbol/table lives → `bounds where <symbol>`
- What breaks if you change it → `bounds impact <name>`
- Trust/coverage/next steps → `bounds overview`
- After an edit → `bounds validate --quick`

**Never** read `.bounds/cache.db`, `.bounds/*.json`, or `.bounds/manifests/*.yaml` directly —
the cache is binary and the manifests bypass tree-sitter verification. The CLI is the API for architecture.
See `AGENTS.md` for the full contract.
"""

# The Claude slash-command body. A thin pointer that *forwards* arguments to the CLI so the
# command is actually runnable, with a short usage list. `$ARGUMENTS` is Claude Code's verbatim
# substitution of whatever follows `/bounds`; empty args default to the overview.
CLAUDE_COMMAND_BODY = """\
# /bounds

Run the **Bounds** CLI to read this project's architecture (subsystem boundaries, public
surfaces, drift) — never read raw `.bounds/` files. Output is JSON by default;
add `-H` for human-readable.

Run:

```
bounds $ARGUMENTS
```

If no arguments were given, show the map instead:

```
bounds overview -H
```

Usage — `/bounds <subcommand> [args]` forwards verbatim to `bounds <subcommand> [args]`:
- `/bounds list` — all subsystems (roles + dependency counts)
- `/bounds describe <name>` — one subsystem's verified public surface/table catalog (a few hundred tokens for a small subsystem)
- `/bounds describe --namespace <ns>` — every subsystem in a namespace
- `/bounds where <symbol>` — locate a symbol/table without grepping
- `/bounds validate --quick` — catch drift after a change
- `/bounds impact <name>` — transitive blast radius before a risky code or schema change
- `/bounds overview -H` — project health at a glance

Never read `.bounds/cache.db`, `.bounds/*.json`, or `.bounds/manifests/*.yaml` directly — the
cache is binary and the manifests bypass tree-sitter verification. The CLI is the API for architecture. See
`AGENTS.md` for the full contract.
"""

# The auto-trigger description for skill files (Claude/Codex). It is the matcher the model reads
# to decide *when* to invoke — so it is written as concrete trigger conditions, not a tagline.
SKILL_TRIGGER = (
    "Read this codebase's architecture with the Bounds CLI before grepping or opening implementation files. "
    "Use when exploring an unfamiliar area, when you need a subsystem's public surface or database "
    "tables, before changing a shared or core subsystem or a migration (check blast radius first), "
    "when asked what depends on X or what breaks if X changes, or to verify drift after an edit. "
    "Never read .bounds/ files directly."
)

# Markdown body shared by the skill files — the task -> command decision table.
SKILL_BODY = """\
# Bounds — architecture navigation

Read this project's architecture through the Bounds CLI; never open `.bounds/` files or grep for
structure first. Output is JSON by default; add `-H` for human-readable.

## Which command for which task
- Find the right subsystem / get the map → `bounds list`
- A subsystem's verified public surface or DB tables → `bounds describe <name>`
- Where a symbol or table is defined → `bounds where <symbol>`
- Blast radius before changing a subsystem or migration → `bounds impact <name>`
- Project health, coverage, and trust signals → `bounds overview`
- Catch drift after an edit → `bounds validate --quick`

Run `bounds guide` for setup; see `AGENTS.md` for the full contract.
"""

# Gemini custom command (TOML). `#` comments are valid TOML, so the YAML marker style wraps it.
# `{{args}}` is Gemini's argument placeholder.
GEMINI_TOML_BODY = '''\
description = "Read this project's architecture via the Bounds CLI (subsystems, surfaces, drift)."
prompt = """
Use the Bounds CLI to answer the user's architecture question; never read .bounds/ files.
Run the right command and summarize its JSON:
- bounds list — the subsystem map
- bounds describe <name> — a subsystem's verified surface/tables
- bounds impact <name> — blast radius before a change
- bounds validate --quick — drift after an edit

User request: {{args}}
"""'''

# OpenCode custom command (Markdown). `$ARGUMENTS` forwards the user's input.
OPENCODE_CMD_BODY = """\
Read this project's architecture via the Bounds CLI — never read `.bounds/` files.

Run:

```
bounds $ARGUMENTS
```

If no arguments were given, run `bounds overview -H`. Common commands: `list`,
`describe <name>`, `impact <name>`, `validate --quick`, `where <symbol>`.
"""

# GitHub Copilot prompt file (Markdown). `${input:...}` is Copilot's input-variable syntax.
COPILOT_PROMPT_BODY = """\
Read this project's architecture via the Bounds CLI — never open `.bounds/` files.

Run the right command and summarize its JSON:
- `bounds list` — the subsystem map
- `bounds describe <name>` — a subsystem's verified surface/tables
- `bounds impact <name>` — blast radius before a change
- `bounds validate --quick` — drift after an edit

Task: ${input:task:What do you want to know about the architecture?}
"""

# Cursor command (Markdown prompt template; Cursor commands take no bound arguments).
CURSOR_CMD_BODY = """\
# Bounds

Use the Bounds CLI to scope this project's architecture before grepping or opening implementation files:

- `bounds list` — the subsystem map
- `bounds describe <name>` — a subsystem's verified surface/tables
- `bounds impact <name>` — blast radius before a change
- `bounds validate --quick` — drift after an edit

Never read `.bounds/` files directly. See `AGENTS.md` for the full contract.
"""

# Windsurf workflow (Markdown; workflows take no bound arguments, so steps prompt for the name).
WINDSURF_WORKFLOW_BODY = """\
# Bounds

Read this project's architecture via the Bounds CLI — never read `.bounds/` files.

1. Unsure of the layout? Run `bounds list`.
2. Need a subsystem's surface/tables? Run `bounds describe <name>`.
3. About to change a shared subsystem or a migration? Run `bounds impact <name>` first.
4. After an edit, run `bounds validate --quick` and fix drift.
"""

# SDD phases (appended only when sdd.enabled = true)
SDD_BODY_INTRO = """\
### Spec-Driven Development (SDD)
Bounds maps its architecture commands to your team's SDD phases. Follow this mapping to ensure your implementation stays within the declared boundaries.\
"""

# Invocation policy (appended only at nudge/strict level)
INVOCATION_DIRECTIVE = (
    "**Invocation policy:** before grepping or opening files to answer an architecture question "
    "(what/where something is, what depends on it, what breaks if it changes), FIRST run the "
    "relevant Bounds command — `bounds list`, `bounds describe <area>`, `bounds where <symbol>`, "
    "`bounds impact <subsystem>`. Fall back to source search only when a lookup misses (a "
    "`count: 0` carries `suggestions` — try those first) or the area is unmapped / an unsupported "
    "language — then searching directly is expected."
)

class _Agent:
    """Static description of one agent's bounds-config target."""
    __slots__ = ("key", "path", "dedicated", "fmt", "canonical")
    def __init__(self, key, path, dedicated, fmt, canonical=False):
        self.key = key
        self.path = path
        self.dedicated = dedicated
        self.fmt = fmt
        self.canonical = canonical

class _Artifact:
    """One bounds-owned command/skill file in an agent's native location."""
    __slots__ = ("path", "fmt", "body", "front")
    def __init__(self, path: str, fmt: str, body: str, front: str = ""):
        self.path = path
        self.fmt = fmt
        self.body = body
        self.front = front

_MARKDOWN = "markdown"
_YAML = "yaml"

# Stable, documented ordering of the eight supported agents.
AGENT_KEYS = (
    "claude",
    "codex",
    "opencode",
    "gemini",
    "copilot",
    "cursor",
    "aider",
    "windsurf",
)

_AGENTS = {
    "claude": _Agent("claude", ".claude/commands/bounds.md", True, _MARKDOWN),
    "codex": _Agent("codex", CANONICAL_NAME, False, _MARKDOWN, canonical=True),
    "opencode": _Agent("opencode", CANONICAL_NAME, False, _MARKDOWN, canonical=True),
    "gemini": _Agent("gemini", "GEMINI.md", False, _MARKDOWN),
    "copilot": _Agent("copilot", ".github/copilot-instructions.md", False, _MARKDOWN),
    "cursor": _Agent("cursor", ".cursor/rules/bounds.mdc", True, _MARKDOWN),
    "aider": _Agent("aider", ".aider.conf.yml", False, _YAML),
    "windsurf": _Agent("windsurf", ".windsurf/rules/bounds.md", True, _MARKDOWN),
}

_MEMORY_FILES = {"claude": "CLAUDE.md"}

_ARTIFACT_DESC = "Read this project's architecture via the Bounds CLI, not raw .bounds files"

def _skill_front() -> str:
    """YAML front-matter for a SKILL.md — ``name`` (display) + the auto-trigger ``description``."""
    return f"---\nname: bounds\ndescription: {SKILL_TRIGGER}\n---\n"

_AGENT_ARTIFACTS: dict[str, tuple[_Artifact, ...]] = {
    "claude": (_Artifact(".claude/skills/bounds/SKILL.md", _MARKDOWN, SKILL_BODY, _skill_front()),),
    "codex": (_Artifact(".agents/skills/bounds/SKILL.md", _MARKDOWN, SKILL_BODY, _skill_front()),),
    "gemini": (_Artifact(".gemini/commands/bounds.toml", _YAML, GEMINI_TOML_BODY),),
    "opencode": (
        _Artifact(".opencode/commands/bounds.md", _MARKDOWN, OPENCODE_CMD_BODY,
                  f"---\ndescription: {_ARTIFACT_DESC}\nagent: build\n---\n"),
    ),
    "copilot": (
        _Artifact(".github/prompts/bounds.prompt.md", _MARKDOWN, COPILOT_PROMPT_BODY,
                  f"---\nmode: agent\ndescription: {_ARTIFACT_DESC}\n---\n"),
    ),
    "cursor": (_Artifact(".cursor/commands/bounds.md", _MARKDOWN, CURSOR_CMD_BODY),),
    "windsurf": (
        _Artifact(".windsurf/workflows/bounds.md", _MARKDOWN, WINDSURF_WORKFLOW_BODY,
                  f"---\ndescription: {_ARTIFACT_DESC}\n---\n"),
    ),
}

_LEGACY_ARTIFACTS: tuple[tuple[str, str], ...] = (
    (".opencode/command/bounds.md", "opencode"),
    (".codex/skills/bounds/SKILL.md", "codex"),
)
