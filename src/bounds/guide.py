"""`bounds guide` — a state-aware setup walkthrough for humans and agents.

Newcomers (and agents dropped into an unfamiliar repo) shouldn't have to read the docs to learn
the setup path. ``bounds guide`` prints the ordered steps to get Bounds working — initialize,
generate contracts, wire AI agents, add the CI gate — each marked done/to-do from what's already
present, plus the daily commands and the single next action. JSON-first like every command; the
human view is a checklist. Pure detection, zero mutation.
"""

from __future__ import annotations

from pathlib import Path

from . import agentsync, config, gitutil
from .extract import scan, supported_extensions
from .ignore import load_matcher
from .manifest import loader as manifest_loader
from .models import RootManifest


def run_guide(project_root: Path, *, sdd: bool = False) -> dict:
    """Return the setup checklist + daily commands for the project at ``project_root``.

    Never raises on a half-set-up or empty project: a missing ``.bounds/`` just means the early
    steps read as not-done. The payload is JSON-serializable and ordering is stable.
    """
    root = manifest_loader.find_root(project_root)
    base = root or project_root
    has_bounds = root is not None

    n_subsystems = 0
    subs: dict = {}
    rootm: RootManifest | None = None
    if has_bounds:
        try:
            rootm, subs, _issues = manifest_loader.load_all(base)
            n_subsystems = len(subs)
        except Exception:  # noqa: BLE001 - a broken manifest shouldn't break the guide
            n_subsystems = 0
            subs = {}

    coverage = _coverage(base, subs) if n_subsystems > 0 else None

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
            "id": "coverage",
            "title": "Close coverage gaps (map 100% of library source)",
            "command": "bounds validate -H",
            "why": _coverage_why(coverage),
            # Done only once subsystems exist AND everything is mapped; until then this surfaces the
            # gap loudly so an agent/human knows what's still dark and the durable hand-authored fix
            # for unsupported languages. Not-done before discover (discover, ordered first, is `next`).
            "done": coverage is not None and coverage.get("files_unmapped", 0) == 0,
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
    payload = {
        "mode": "guide",
        "steps": steps,
        "daily": daily,
        "next": pending["command"] if pending else None,
        "complete": pending is None,
    }
    sdd_cfg = _sdd_config(rootm)
    if sdd or sdd_cfg["enabled"]:
        payload["sdd"] = {
            **sdd_cfg,
            "forced": bool(sdd and not sdd_cfg["enabled"]),
            "steps": _sdd_steps(sdd_cfg),
            "freshness": {
                "contract": "manifests evolve with the spec; intentional surface changes update manifests and re-baseline drift",
                "during_implementation": "bounds validate --quick",
                "ci_gate": "bounds preflight --ci",
                "intentional_change": "bounds calibrate --dump-baseline",
            },
        }
    return payload


def _sdd_config(root: RootManifest | None) -> dict:
    """Resolved optional SDD settings from root.yaml, with deterministic defaults."""
    raw = root.sdd if root is not None and isinstance(root.sdd, dict) else {}
    enabled = bool(raw.get("enabled", False))
    agent = str(raw.get("agent") or "generic")
    if agent not in config.SDD_AGENTS:
        agent = "generic"
    requested = raw.get("phases")
    if requested is None:
        requested = config.SDD_PHASES
    phases = [p for p in config.SDD_PHASES if p in set(requested)]
    return {"enabled": enabled, "agent": agent, "phases": phases}


def _sdd_steps(sdd_cfg: dict) -> list[dict]:
    """Bounds' deterministic command contract for each enabled SDD phase."""
    phase_map = {
        "specify": {
            "command": "bounds overview && bounds list",
            "use": "ground the spec in the current subsystem map, coverage, and boundaries",
        },
        "clarify": {
            "command": "bounds describe <name> && bounds where <symbol>",
            "use": "answer what the current verified contract of a subsystem or symbol is",
        },
        "plan": {
            "command": "bounds impact <name>",
            "use": "account for the blast radius and declared manifests the plan must respect",
        },
        "tasks": {
            "command": "bounds impact <name>",
            "use": "scope and order implementation tasks by subsystem dependency edges",
        },
        "analyze": {
            "command": "bounds validate && bounds preflight",
            "use": "cross-check the plan/tasks against declared boundaries and drift",
        },
        "implement": {
            "command": "bounds validate --quick",
            "use": "catch drift after each edit; update manifests when the spec intentionally changes surface",
        },
        "verify": {
            "command": "bounds preflight --ci",
            "use": "run the final architecture gate before review or merge",
        },
    }
    return [
        {"phase": phase, **phase_map[phase]}
        for phase in config.SDD_PHASES
        if phase in set(sdd_cfg.get("phases", config.SDD_PHASES))
    ]


def _coverage(base: Path, subs: dict) -> dict | None:
    """Mapping coverage over the repo's library source, or None if it can't be computed.

    Fail soft: any error (e.g. a permissions issue while walking) just drops the coverage step's
    detail — the guide must never raise. Uses the same `scan.mapping_coverage` signal as
    `validate`/`overview`, so the three never disagree about what's mapped.
    """
    try:
        matcher = load_matcher(base)
        exts = supported_extensions()
        owners = scan.resolve_owners(base, subs, exts)
        owned = set(owners)
        repo = gitutil.repo_root(base) or base
        return scan.mapping_coverage(base, owned, matcher, repo=repo, subsystems=subs)
    except Exception:  # noqa: BLE001 - coverage is advisory; never break the guide over it
        return None


def _coverage_why(coverage: dict | None) -> str:
    """One-line rationale for the coverage step — names the live gap when there is one."""
    if not coverage or coverage.get("files_unmapped", 0) == 0:
        return (
            "Bounds is authoritative only for source it mapped; aim for 100% so no library code is "
            "outside the map. See docs/coverage.md."
        )
    bits: list[str] = []
    unowned = coverage.get("unmapped_unowned_supported", 0)
    unsupported = coverage.get("unmapped_unsupported_language", 0)
    if unowned:
        bits.append(f"{unowned} supported file(s) in no subsystem — add to a manifest's `paths:`")
    if unsupported:
        langs = ", ".join(sorted(coverage.get("unsupported_languages", []) or []))
        bits.append(
            f"{unsupported} file(s) in unsupported languages"
            + (f" ({langs})" if langs else "")
            + " — hand-author a manifest's `exposes` (durable: calibrate/validate keep it)"
        )
    detail = "; ".join(bits) or "some library source is unmapped"
    return (
        f"mapped {coverage.get('mapped_pct', 0.0)}% of library source; {detail}. "
        "See docs/coverage.md."
    )


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
