"""Orchestrate cross-agent detection, sync, and verification."""

from __future__ import annotations

from pathlib import Path

from ..shared import errors
from . import files, hook as agenthook, protocol, status
from .content import (
    _AGENTS,
    _AGENT_ARTIFACTS,
    _LEGACY_ARTIFACTS,
    _MARKDOWN,
    _MEMORY_FILES,
    AGENT_KEYS,
    AGENT_POINTER_BODY,
    CANONICAL_NAME,
)

__all__ = ["run_agent"]

VALID_MODES = {"sync", "detect", "check"}


def run_agent(project_root: Path, *, mode: str, only: set[str] | None = None) -> dict:
    """Run the cross-agent protocol and return a JSON-serializable payload."""
    root = Path(project_root)
    if mode not in VALID_MODES:
        raise errors.BoundsError(
            errors.E_USAGE,
            f"unknown agent mode {mode!r}",
            fix=f"use one of: {', '.join(sorted(VALID_MODES))}",
        )
    selected = _resolve_selection(only)
    if mode == "detect":
        return status.detect(root)
    if mode == "check":
        return status.check(root, selected)
    return _sync(root, selected)


def _resolve_selection(only: set[str] | None) -> list[str]:
    """Validate and return selected agent keys in stable order."""
    if only is None:
        return list(AGENT_KEYS)
    unknown = sorted(only - set(AGENT_KEYS))
    if unknown:
        raise errors.BoundsError(
            errors.E_USAGE,
            f"unknown agent key(s): {', '.join(unknown)}",
            fix=f"valid keys: {', '.join(AGENT_KEYS)}",
        )
    return [key for key in AGENT_KEYS if key in only]


def _sync(root: Path, selected: list[str]) -> dict:
    """Write canonical and tool-specific agent files non-destructively."""
    buckets = files.Buckets()
    sdd_cfg = protocol.sdd_config(root)
    invocation = protocol.invocation_level(root)

    outcome = files.upsert_block(
        root / CANONICAL_NAME,
        _MARKDOWN,
        protocol.expected_body(_AGENTS["codex"], sdd_cfg, invocation),
    )
    buckets.record(outcome, CANONICAL_NAME)

    done: set[str] = {CANONICAL_NAME}
    for key in selected:
        agent = _AGENTS[key]
        if agent.canonical or agent.path in done:
            continue
        done.add(agent.path)
        outcome = files.upsert_block(
            root / Path(agent.path),
            agent.fmt,
            protocol.expected_body(agent, sdd_cfg, invocation),
            prefix=protocol.front_matter(agent),
            dedicated=agent.dedicated,
        )
        buckets.record(outcome, Path(agent.path).as_posix())

    for key in selected:
        memory = _MEMORY_FILES.get(key)
        if memory is None or memory in done:
            continue
        done.add(memory)
        body = protocol.append_sdd(
            protocol.append_invocation(
                AGENT_POINTER_BODY.rstrip("\n"), invocation, _MARKDOWN
            ),
            key,
            sdd_cfg,
        )
        outcome = files.upsert_block(root / Path(memory), _MARKDOWN, body)
        buckets.record(outcome, Path(memory).as_posix())

    if "claude" in selected:
        rel_settings = agenthook.settings_path(root).relative_to(root).as_posix()
        try:
            outcome = agenthook.sync_claude_hooks(root, invocation)
        except Exception:  # noqa: BLE001 - config files still sync if hook settings are malformed
            outcome = "skipped_malformed"
        buckets.record(outcome, rel_settings)

    for key in selected:
        for artifact in _AGENT_ARTIFACTS.get(key, ()):
            if artifact.path in done:
                continue
            done.add(artifact.path)
            outcome = files.upsert_block(
                root / Path(artifact.path),
                artifact.fmt,
                protocol.append_sdd(artifact.body.rstrip("\n"), key, sdd_cfg),
                prefix=artifact.front,
                dedicated=True,
            )
            buckets.record(outcome, Path(artifact.path).as_posix())

    _remove_selected_legacy_artifacts(root, set(selected))
    return {
        "created": sorted(buckets.created),
        "updated": sorted(buckets.updated),
        "unchanged": sorted(buckets.unchanged),
        "skipped_custom": sorted(buckets.skipped),
        "skip_reasons": {key: buckets.reasons[key] for key in sorted(buckets.reasons)},
        "canonical": CANONICAL_NAME,
        "invocation": invocation,
    }


def _remove_selected_legacy_artifacts(root: Path, selected: set[str]) -> None:
    """Remove only renamed Bounds-owned files for agents being synced."""
    for rel, owner in _LEGACY_ARTIFACTS:
        if owner not in selected:
            continue
        legacy = root / Path(rel)
        if legacy.is_file():
            try:
                legacy.unlink()
            except OSError:
                continue
            try:
                if not any(legacy.parent.iterdir()):
                    legacy.parent.rmdir()
            except OSError:
                pass
