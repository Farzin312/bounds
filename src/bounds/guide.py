"""`bounds guide` — a state-aware setup walkthrough for humans and agents.

Newcomers (and agents dropped into an unfamiliar repo) shouldn't have to read the docs to learn
the setup path. ``bounds guide`` prints the ordered steps to get Bounds working — initialize,
generate contracts, wire AI agents, add the CI gate — each marked done/to-do from what's already
present, plus the daily commands and the single next action. JSON-first like every command; the
human view is a checklist. Pure detection, zero mutation.
"""

from __future__ import annotations

from pathlib import Path

from . import agentsync
from .manifest import loader as manifest_loader


def run_guide(project_root: Path) -> dict:
    """Return the setup checklist + daily commands for the project at ``project_root``.

    Never raises on a half-set-up or empty project: a missing ``.bounds/`` just means the early
    steps read as not-done. The payload is JSON-serializable and ordering is stable.
    """
    root = manifest_loader.find_root(project_root)
    base = root or project_root
    has_bounds = root is not None

    n_subsystems = 0
    if has_bounds:
        try:
            _root, subs, _issues = manifest_loader.load_all(base)
            n_subsystems = len(subs)
        except Exception:  # noqa: BLE001 - a broken manifest shouldn't break the guide
            n_subsystems = 0

    agent_check = agentsync.run_agent(base, mode="check")
    agents_done = bool(agent_check.get("ok")) and bool(agent_check.get("configured"))

    steps = [
        {
            "id": "init",
            "title": "Initialize Bounds",
            "command": "bounds init --root",
            "why": "Creates the hidden .bounds/ contracts directory at the project root.",
            "done": has_bounds,
        },
        {
            "id": "discover",
            "title": "Generate contracts from source",
            "command": "bounds discover --apply",
            "why": "Auto-maps subsystems + dependency edges so agents have a map to query.",
            "done": has_bounds and n_subsystems > 0,
        },
        {
            "id": "agents",
            "title": "Wire your AI coding agents",
            "command": "bounds agent --sync",
            "why": "Teaches Claude/Codex/Gemini/Cursor/… to query Bounds before reading source.",
            "done": agents_done,
        },
        {
            "id": "ci",
            "title": "Add the CI drift gate",
            "command": "bounds ci --install",
            "why": "Fails CI on architecture drift, boundary violations, and cycles.",
            "done": _ci_installed(base),
        },
    ]

    daily = [
        {"command": "bounds list", "use": "see the subsystem map before searching source"},
        {"command": "bounds describe <name>", "use": "one subsystem's verified API / tables"},
        {"command": "bounds where <symbol>", "use": "locate a symbol or table without grepping"},
        {"command": "bounds impact <name>", "use": "blast radius before a risky change"},
        {"command": "bounds validate --quick", "use": "catch drift after an edit"},
    ]

    pending = next((s for s in steps if not s["done"]), None)
    return {
        "mode": "guide",
        "steps": steps,
        "daily": daily,
        "next": pending["command"] if pending else None,
        "complete": pending is None,
    }


def _ci_installed(root: Path) -> bool:
    """True when a Bounds CI gate is already wired (GitHub Action / pre-commit / GitLab)."""
    if (root / ".github" / "workflows" / "bounds.yml").is_file():
        return True
    for rel in (".pre-commit-config.yaml", ".gitlab-ci.yml"):
        path = root / rel
        if path.is_file():
            try:
                if "bounds" in path.read_text(encoding="utf-8").lower():
                    return True
            except OSError:
                continue
    return False
