"""Resolve generated agent bodies from project configuration."""

from __future__ import annotations

from pathlib import Path

from ..core import sdd as sdd_mod
from ..core.manifest import loader as manifest_loader
from ..shared import config
from .content import (
    _MARKDOWN,
    _YAML,
    AGENT_POINTER_BODY,
    CANONICAL_BODY,
    CANONICAL_NAME,
    CLAUDE_COMMAND_BODY,
    INVOCATION_DIRECTIVE,
    _Agent,
)


def sdd_config(root: Path | None) -> dict:
    """Return resolved SDD config, failing soft for agent setup."""
    rootm = None
    if root is not None:
        try:
            found = manifest_loader.find_root(root)
            if found is not None:
                rootm = manifest_loader.load_root(found)
        except Exception:  # noqa: BLE001 - agent sync must survive broken setup
            rootm = None
    return sdd_mod.resolve_config(rootm)


def invocation_level(root: Path | None) -> str:
    """Return the configured invocation level, failing soft to the default."""
    if root is None:
        return config.DEFAULT_INVOCATION
    try:
        found = manifest_loader.find_root(root)
        if found is not None:
            return manifest_loader.load_root(found).invocation_level()
    except Exception:  # noqa: BLE001 - agent sync must survive broken setup
        pass
    return config.DEFAULT_INVOCATION


def expected_body(
    agent: _Agent,
    sdd_cfg: dict | None = None,
    level: str | None = None,
) -> str:
    """Return the exact managed body expected for one agent."""
    sdd_cfg = sdd_cfg or sdd_config(None)
    level = level or config.DEFAULT_INVOCATION
    if agent.canonical or agent.path == CANONICAL_NAME:
        base = append_invocation(CANONICAL_BODY.rstrip("\n"), level, _MARKDOWN)
        return append_sdd(base, "canonical", sdd_cfg)
    if agent.dedicated:
        base = append_invocation(dedicated_body(agent).rstrip("\n"), level, agent.fmt)
        return append_sdd(base, agent.key, sdd_cfg)
    base = append_invocation(pointer_body(agent.fmt).rstrip("\n"), level, agent.fmt)
    return append_sdd(base, agent.key, sdd_cfg)


def append_sdd(body: str, agent_key: str, sdd_cfg: dict) -> str:
    """Append the configured SDD phase contract when enabled."""
    if not sdd_cfg.get("enabled"):
        return body
    return body.rstrip("\n") + "\n\n" + sdd_body(agent_key, sdd_cfg).rstrip("\n")


def append_invocation(body: str, level: str, fmt: str = _MARKDOWN) -> str:
    """Append the invocation directive for nudge and strict modes."""
    if level not in ("nudge", "strict"):
        return body
    text = INVOCATION_DIRECTIVE
    if fmt == _YAML:
        text = "\n".join("# " + line for line in text.split("\n"))
    return body.rstrip("\n") + "\n\n" + text


def sdd_body(agent_key: str, sdd_cfg: dict) -> str:
    """Render the deterministic SDD phase contract."""
    lines = [
        "## Bounds in Spec-Driven Development",
        "",
        f"SDD is enabled for `{sdd_cfg.get('agent', 'generic')}`; this `{agent_key}` artifact wires Bounds into the project's customized SDD loop.",
        "Bounds stays zero-LLM: it provides verified architecture facts and gates while the agent handles prose spec work.",
        "",
        "### Phase contract",
    ]
    for step in sdd_mod.phase_steps(sdd_cfg):
        lines.append(f"- **{step['phase']}** → `{step['command']}` — {step['use']}.")
    lines.extend(
        [
            "",
            "### Freshness contract",
            "- If the spec intentionally changes public surface, update the manifest in the same spec/plan change.",
            "- Run `bounds calibrate --dump-baseline` only after the manifest reflects the intended contract.",
            "- `bounds validate --quick` in the edit loop catches accidental drift; `bounds preflight --ci` is the final gate.",
            "- For unsupported-language subsystems, keep hand-authored exposes in the manifest; calibrate routes unverifiable entries to review instead of deleting them.",
        ]
    )
    return "\n".join(lines)


def front_matter(agent: _Agent) -> str:
    """Return tool-activating front matter for dedicated files."""
    desc = "Read this project's architecture via the Bounds CLI, not raw .bounds files"
    front = {
        "claude": f"---\ndescription: {desc}\n---\n",
        "cursor": f"---\ndescription: {desc}\nalwaysApply: true\n---\n",
        "windsurf": f"---\ntrigger: always_on\ndescription: {desc}\n---\n",
    }
    return front.get(agent.key, "")


def dedicated_body(agent: _Agent) -> str:
    """Return the managed body for a dedicated tool file."""
    if agent.key == "claude":
        return CLAUDE_COMMAND_BODY
    return "# Bounds\n\n" + AGENT_POINTER_BODY


def pointer_body(fmt: str) -> str:
    """Return the managed body for a shared pointer file."""
    if fmt == _YAML:
        return "\n".join(
            [
                "# Bounds models this codebase as subsystem boundary manifests.",
                "# Read architecture via the CLI, never raw .bounds files:",
                "#   bounds list / bounds describe <name> / bounds validate --quick",
                "# Never read .bounds/cache.db, .bounds/*.json, or .bounds/manifests/*.yaml.",
                "# The CLI is the API for architecture. See AGENTS.md for the full contract.",
                "read: [AGENTS.md]",
            ]
        )
    return "## Bounds\n\n" + AGENT_POINTER_BODY.rstrip("\n")
