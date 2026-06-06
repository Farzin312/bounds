"""`bounds guide` — a state-aware setup walkthrough for humans and agents.

Newcomers (and agents dropped into an unfamiliar repo) shouldn't have to read the docs to learn
the setup path. ``bounds guide`` prints the ordered steps to get Bounds working — initialize,
generate contracts, wire AI agents, add the CI gate — each marked done/to-do from what's already
present, plus the daily commands and the single next action. JSON-first like every command; the
human view is a checklist. Pure detection, zero mutation.
"""

from __future__ import annotations

from pathlib import Path

from ..agents import sync as agentsync
from ..shared import gitutil
from ..shared.ignore import load_matcher
from ..shared.models import RootManifest
from . import sdd as sdd_mod
from .extract import scan, supported_extensions
from .manifest import loader as manifest_loader


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
    manifest_error: str | None = None
    if has_bounds:
        try:
            rootm, subs, _issues = manifest_loader.load_all(base)
            n_subsystems = len(subs)
        except Exception as exc:  # noqa: BLE001 - surface the error to the caller, do not silently zero
            # The previous behavior of "swallow to 0/5 steps done" hid manifest YAML/syntax
            # errors behind a happy-looking checklist — a new user thought Bounds was broken
            # when really their root.yaml had a parse error. The fix is to capture the
            # error and surface it in the guide payload so the user is told what to do.
            manifest_error = str(exc)
            n_subsystems = 0
            subs = {}

    coverage = _coverage(base, subs) if n_subsystems > 0 else None

    agent_check = agentsync.run_agent(base, mode="check")
    detected = agent_check.get("detected", [])
    configured = agent_check.get("configured", [])
    # Done if at least one detected agent is configured.
    agents_done = bool(configured)

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
            "command": "bounds coverage -H",
            "why": _coverage_why(coverage),
            "done": coverage is not None and not scan.coverage_has_gap(coverage),
        },
        {
            "id": "agents",
            "title": "Wire your AI coding agents",
            "command": "bounds agent --sync",
            "why": _agents_why(agent_check),
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
        {"command": "bounds describe <name>", "use": "one subsystem's verified surface / tables"},
        {"command": "bounds where <symbol>", "use": "locate a symbol or table without grepping"},
        {"command": "bounds impact <name>", "use": "blast radius before a risky change"},
        {"command": "bounds validate --quick", "use": "catch drift after an edit"},
        {"command": "bounds guide", "use": "setup status + optional features (SDD, invocation level)"},
    ]

    pending = next((s for s in steps if not s["done"]), None)
    payload = {
        "mode": "guide",
        "steps": steps,
        "daily": daily,
        "next": pending["command"] if pending else None,
        "complete": pending is None,
    }
    if manifest_error:
        payload["manifest_error"] = {
            "message": manifest_error,
            "fix": "run `bounds validate -H` for the structured manifest error",
        }
        payload["next"] = "bounds validate -H"

    sdd_cfg = sdd_mod.resolve_config(rootm)
    if sdd or sdd_cfg["enabled"]:
        payload["sdd"] = {
            **sdd_cfg,
            "forced": bool(sdd and not sdd_cfg["enabled"]),
            "steps": sdd_mod.phase_steps(sdd_cfg),
            "freshness": {
                "contract": "manifests evolve with the spec; intentional surface changes update manifests and re-baseline drift",
                "during_implementation": "bounds validate --quick",
                "ci_gate": "bounds preflight --ci",
                "intentional_change": "bounds calibrate --dump-baseline",
            },
        }

    # Always surface optional features so users know what's available, even during setup.
    invocation = rootm.invocation_level() if rootm else "nudge"
    payload["optional"] = [
        {
            "id": "sdd",
            "title": "Spec-Driven Development",
            "enabled": sdd_cfg["enabled"],
            "current": "enabled" if sdd_cfg["enabled"] else "disabled",
            "command": "bounds sdd --enable" if not sdd_cfg["enabled"] else "bounds sdd --doctor",
            "use": "map SDD phases (specify → verify) to deterministic architecture checks",
            "configure": "bounds sdd --enable / --disable / --phases x,y / --add-phase / --remove-phase",
        },
        {
            "id": "invocation",
            "title": "Agent invocation level",
            "enabled": invocation != "off",
            "current": invocation,
            "command": f"bounds agent --invocation {invocation}",
            "use": "how assertively agents push toward Bounds first",
            "configure": "bounds agent --invocation off / nudge / strict",
        },
    ]
    return payload


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
    if not coverage or not scan.coverage_has_gap(coverage):
        return (
            "Bounds maps 100% of supported-language source deterministically; declared unsupported "
            "files are covered too. Keep new source mapped so none falls outside the map."
        )
    bits: list[str] = []
    breakdown = coverage["supported"].get("unowned_breakdown", {})
    decisions = breakdown.get("user_decision_needed", {}).get("count", 0)
    misses = breakdown.get("algorithm_miss", {}).get("count", 0)
    unowned = coverage["supported"]["unowned"]
    dark = coverage["unsupported"]["dark"]
    if decisions:
        bits.append(f"{decisions} source file(s) need an ownership decision")
    if misses:
        bits.append(f"{misses} tool-config miss(es) — run `bounds fix-coverage --auto`")
    if unowned and not breakdown:
        bits.append(f"{unowned} supported file(s) in no subsystem")
    if dark:
        langs = ", ".join(sorted(coverage["unsupported"]["by_language"]))
        bits.append(
            f"{dark} unsupported file(s) no manifest claims"
            + (f" ({langs})" if langs else "")
            + " — hand-author a manifest's `exposes` (durable: calibrate/validate keep it)"
        )
    detail = "; ".join(bits) or "some library source is unmapped"
    return (
        f"mapped {coverage.get('mapped_pct', 0.0)}% of supported-language source; {detail}. "
        "`bounds coverage -H` prints the distinct fix for each bucket; calibration does not map files. "
        "See docs/coverage.md."
    )


def _agents_why(check: dict) -> str:
    """One-line rationale for the agents step — names missing/stale tools when present."""
    detected = check.get("detected", [])
    configured = check.get("configured", [])
    missing = check.get("missing", [])
    stale = check.get("stale", [])
    
    if not detected:
        return "Teaches Claude/Codex/Gemini/Cursor/… to query Bounds before reading source."
    
    if check.get("ok") and len(configured) == len(detected):
        return (
            f"All {len(detected)} detected coding agents ({', '.join(configured)}) are wired. "
            "Re-syncing is only needed after contract or SDD changes."
        )

    bits: list[str] = []
    if configured:
        bits.append(f"configured: {', '.join(configured)}")
    if missing:
        bits.append(f"missing: {', '.join(missing)}")
    if stale:
        bits.append(f"stale: {', '.join(stale)}")
    
    detail = "; ".join(bits)
    return f"Wired agent status ({detail}). Run `bounds agent --sync` to refresh."


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
