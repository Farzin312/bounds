"""Agent footprint detection and generated-config verification."""

from __future__ import annotations

from pathlib import Path

from . import files, hook as agenthook, protocol
from .content import (
    _AGENTS,
    _AGENT_ARTIFACTS,
    _MARKDOWN,
    _MEMORY_FILES,
    AGENT_KEYS,
    AGENT_POINTER_BODY,
    CANONICAL_NAME,
    _Agent,
)


def detect(root: Path) -> dict:
    """Report agent footprints present in a project."""
    detected = [key for key in AGENT_KEYS if footprint_present(root, key)]
    return {"detected": sorted(detected), "invocation": protocol.invocation_level(root)}


def footprint_present(root: Path, key: str) -> bool:
    """Return whether one agent's native footprint exists."""
    checks = {
        "claude": (root / ".claude").is_dir,
        "codex": (root / CANONICAL_NAME).is_file,
        "opencode": (root / CANONICAL_NAME).is_file,
        "gemini": (root / "GEMINI.md").is_file,
        "copilot": (root / ".github" / "copilot-instructions.md").is_file,
        "cursor": (root / ".cursor").is_dir,
        "aider": (root / ".aider.conf.yml").is_file,
        "windsurf": (root / ".windsurf").is_dir,
    }
    return checks[key]()


def check(root: Path, selected: list[str]) -> dict:
    """Classify detected selected agents as configured, missing, or stale."""
    detected = set(detect(root)["detected"])
    targets = [key for key in selected if key in detected]
    sdd_cfg = protocol.sdd_config(root)
    level = protocol.invocation_level(root)
    configured: list[str] = []
    missing: list[str] = []
    stale: list[str] = []
    for key in targets:
        state = config_status(root, _AGENTS[key], sdd_cfg, level)
        {"missing": missing, "stale": stale, "configured": configured}[state].append(key)

    result: dict = {
        "ok": not missing and not stale,
        "detected": sorted(targets),
        "missing": sorted(missing),
        "stale": sorted(stale),
        "configured": sorted(configured),
        "invocation": level,
    }
    if missing or stale:
        result["fix"] = "bounds agent --sync"
    return result


def config_status(root: Path, agent: _Agent, sdd_cfg: dict, level: str) -> str:
    """Classify one agent across its pointer, artifacts, memory file, and hook."""
    pointer = files.target_status(
        root,
        agent.path,
        agent.fmt,
        protocol.expected_body(agent, sdd_cfg, level),
        agent.dedicated,
        protocol.front_matter(agent),
    )
    if pointer != "configured":
        return pointer

    for artifact in _AGENT_ARTIFACTS.get(agent.key, ()):
        if (root / Path(artifact.path)).exists():
            body = protocol.append_sdd(artifact.body.rstrip("\n"), agent.key, sdd_cfg)
            if files.target_status(
                root, artifact.path, artifact.fmt, body, True, artifact.front
            ) != "configured":
                return "stale"

    memory = _MEMORY_FILES.get(agent.key)
    if memory is not None and (root / Path(memory)).exists():
        memory_body = protocol.append_sdd(
            protocol.append_invocation(AGENT_POINTER_BODY.rstrip("\n"), level, _MARKDOWN),
            agent.key,
            sdd_cfg,
        )
        memory_status = files.target_status(
            root, memory, _MARKDOWN, memory_body, False, ""
        )
        authored_override = (
            memory_status == "missing"
            and files.looks_bounds_authored(files.read_text(root / Path(memory)))
        )
        if memory_status != "configured" and not authored_override:
            return "stale"

    if agent.key == "claude" and not agenthook.claude_hooks_current(root, level):
        return "stale"
    return "configured"
