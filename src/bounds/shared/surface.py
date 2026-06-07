"""Unsupported-language surface staleness — the committed "re-verify your exposes" signal.

For an unsupported language Bounds has no adapter, so it cannot tell when a hand-authored ``exposes``
has gone stale against its source. This records a per-file content hash of each subsystem's owned
unsupported source in a COMMITTED baseline (``.bounds/surface-baseline.json``), written by
``calibrate --dump-baseline``. ``validate`` (full/preflight, off the ``--quick`` path) compares the
live files against it and emits :data:`errors.E_UNSUPPORTED_SURFACE_STALE` when one changed or was
removed — the deterministic signal that the agent (or a human) must re-verify the exposes, then
re-confirm with ``calibrate --dump-baseline``. Committed (not the gitignored ``cache.db``) so the
signal survives a fresh clone and works in CI. Zero LLM, like every structural path.

Digests are produced by :func:`bounds.extract.scan.unsupported_surface_files`; this module owns only
the committed-file I/O and the baseline-vs-current comparison, so it depends on nothing but
:mod:`config` (no import cycle with the engine or calibrate, which both also touch ``scan``).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config

_BASELINE_VERSION = 1


def _baseline_path(project_root: Path) -> Path:
    return config.config_dir(project_root) / config.SURFACE_BASELINE_FILE


def load_baseline(project_root: Path) -> dict[str, dict[str, str]]:
    """Return the committed ``{subsystem: {rel: content_hash}}`` baseline, or ``{}`` if absent.

    Fail-soft: a missing, unreadable, or malformed file yields ``{}`` (no baseline ⇒ no staleness
    signal, exactly like the drift baseline) rather than raising.
    """
    path = _baseline_path(project_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    surfaces = data.get("surfaces") if isinstance(data, dict) else None
    if not isinstance(surfaces, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for name, files in surfaces.items():
        if isinstance(files, dict):
            out[str(name)] = {str(k): str(v) for k, v in files.items()}
    return out


def write_baseline(project_root: Path, surfaces: dict[str, dict[str, str]]) -> Path:
    """Write the unsupported-surface baseline (committed) and return its path. Deterministic JSON."""
    path = _baseline_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {name: dict(sorted(files.items())) for name, files in sorted(surfaces.items())}
    path.write_text(
        json.dumps({"version": _BASELINE_VERSION, "surfaces": normalized}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def stale_subsystems(
    baseline: dict[str, dict[str, str]], current: dict[str, dict[str, str]]
) -> dict[str, list[str]]:
    """``{subsystem: [changed-or-removed rel, …]}`` for subsystems whose confirmed unsupported surface
    drifted from ``baseline`` to ``current``.

    Flags a file whose content hash CHANGED, or one present in the baseline but now GONE — both mean a
    confirmed hand-authored expose may no longer match its source. A brand-new unsupported file is NOT
    staleness (it's a coverage / declared-vs-dark concern), so additions are ignored here. Only
    subsystems present in the baseline are considered — a never-confirmed subsystem can't be "stale".
    Deterministic: sorted rel paths.
    """
    out: dict[str, list[str]] = {}
    for name, base_files in baseline.items():
        cur_files = current.get(name, {})
        affected = [
            rel for rel, digest in base_files.items()
            if rel not in cur_files or cur_files[rel] != digest
        ]
        if affected:
            out[name] = sorted(affected)
    return out
