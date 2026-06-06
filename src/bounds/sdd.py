"""Deterministic Spec-Driven Development command mapping.

Bounds does not run a prose workflow or infer what a team has completed. It
only maps configured phases to architecture queries/gates and checks whether
those commands can run against the current project.
"""

from __future__ import annotations

from pathlib import Path

from . import config, errors
from .models import RootManifest, SubsystemCompact
from .validate import engine as validate_engine

__all__ = ["phase_steps", "resolve_config", "run_sdd"]


_PHASE_COMMANDS = {
    "specify": {
        "command": "bounds overview && bounds list",
        "use": "ground the spec in the current map, coverage, and trust signals",
    },
    "clarify": {
        "command": "bounds describe <name> && bounds where <symbol>",
        "use": "answer questions from verified subsystem and symbol context",
    },
    "plan": {
        "command": "bounds impact <name>",
        "use": "account for declared consumers and the lower-bound blast radius",
    },
    "tasks": {
        "command": "bounds impact <name>",
        "use": "order implementation work along subsystem dependency edges",
    },
    "analyze": {
        "command": "bounds validate",
        "use": "run the full architecture checks before implementation",
    },
    "implement": {
        "command": "bounds validate --quick",
        "use": "catch incremental drift after edits without claiming full health",
    },
    "verify": {
        "command": "bounds preflight --ci",
        "use": "run the blocking architecture gate before review or merge",
    },
}


def resolve_config(root: RootManifest | None) -> dict:
    """Resolve optional SDD settings while preserving ``null`` versus ``[]``."""
    raw = root.sdd if root is not None and isinstance(root.sdd, dict) else {}
    agent = str(raw.get("agent") or "generic")
    if agent not in config.SDD_AGENTS:
        agent = "generic"
    requested = raw.get("phases")
    if requested is None:
        requested = config.SDD_PHASES
    requested_set = set(requested) if isinstance(requested, (list, tuple, set)) else set()
    phases = [name for name in config.SDD_PHASES if name in requested_set]
    return {
        "enabled": bool(raw.get("enabled", False)),
        "agent": agent,
        "phases": phases,
    }


def phase_steps(sdd_config: dict) -> list[dict]:
    """Return the ordered deterministic Bounds command for each configured phase."""
    enabled = set(sdd_config.get("phases", config.SDD_PHASES))
    return [
        {"phase": name, **_PHASE_COMMANDS[name]}
        for name in config.SDD_PHASES
        if name in enabled
    ]


def run_sdd(
    project_root: Path,
    root: RootManifest,
    subsystems: dict[str, SubsystemCompact],
    *,
    phase_name: str | None = None,
    doctor: bool = False,
) -> dict | None:
    """Build the public payload for the ``bounds sdd`` command."""
    if phase_name:
        return _phase(root, phase_name)
    if doctor:
        return _doctor(project_root, root, subsystems)
    return _status(root)


def _status(root: RootManifest | None) -> dict:
    """Describe configured phases without pretending to know prose progress."""
    resolved = resolve_config(root)
    payload = {
        "mode": "sdd-status",
        **resolved,
        "steps": phase_steps(resolved),
        "note": (
            "Bounds maps architecture commands to SDD phases; it does not infer "
            "which prose phase your team has completed."
        ),
    }
    payload["next_step"] = (
        "run `bounds sdd --phase <phase>` for one phase"
        if resolved["enabled"]
        else "enable `sdd: { enabled: true }` in root.yaml, or use `bounds guide --sdd` to preview"
    )
    return payload


def _phase(root: RootManifest | None, name: str) -> dict | None:
    """Return one phase mapping, including phases omitted by current customization."""
    if name not in _PHASE_COMMANDS:
        return None
    resolved = resolve_config(root)
    return {
        "mode": "sdd-phase",
        "enabled": resolved["enabled"],
        "configured": bool(resolved["enabled"] and name in resolved["phases"]),
        "available_in_preview": name in resolved["phases"],
        "phase": name,
        **_PHASE_COMMANDS[name],
    }


def _doctor(
    project_root: Path,
    root: RootManifest,
    subsystems: dict[str, SubsystemCompact],
) -> dict:
    """Run deterministic readiness checks; never claim prose-phase completion."""
    resolved = resolve_config(root)
    checks: list[dict] = [
        {
            "name": "configuration",
            "ok": resolved["enabled"],
            "detail": "SDD enabled" if resolved["enabled"] else "SDD is not enabled",
        },
        {
            "name": "subsystem_map",
            "ok": bool(subsystems),
            "detail": f"{len(subsystems)} subsystem(s)",
        },
    ]
    for name, mode in (("full_validation", "full"), ("quick_validation", "quick"), ("preflight", "preflight")):
        try:
            report = validate_engine.run(project_root, mode=mode, persist=False)
            error_count = sum(
                1
                for issue in report.issues
                if errors.SEVERITY.get(issue.code, issue.severity) == "error"
            )
            checks.append(
                {
                    "name": name,
                    "ok": error_count == 0,
                    "detail": (
                        f"{len(report.issues)} issue(s), {error_count} canonical error(s); "
                        f"status={report.status}"
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 - doctor reports each failed probe
            checks.append(
                {
                    "name": name,
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "mode": "sdd-doctor",
        "ok": all(check["ok"] for check in checks),
        "configuration": resolved,
        "checks": checks,
        "steps": phase_steps(resolved),
        "note": "Readiness checks are architecture facts, not proof that prose phase work is complete.",
        "next_step": next(
            (check["name"] for check in checks if not check["ok"]),
            "run the Bounds command for the phase selected in your SDD workflow",
        ),
    }
