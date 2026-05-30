"""Validation orchestration: mode dispatch, file selection, cached extraction, and check execution.

Flow (see ARCHITECTURE.md §2):
  load manifests -> select files -> extract (cache-accelerated) -> classify dirty subsystems
  -> propagate to consumers -> build CheckContext -> run the mode's checks -> assemble report.
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import config, errors, gitutil
from ..cache import store as cache_store
from ..extract import content_hash, get_adapter, supported_extensions
from ..manifest import loader as manifest_loader
from ..models import Issue, ValidationReport
from . import propagation
from .checks import CHECKS_BY_MODE, CheckContext


def run(
    project_root: Path,
    mode: str = "full",
    base: str = "HEAD",
    enforce: str | None = None,
    persist: bool = True,
) -> ValidationReport:
    """Validate the project at ``project_root`` in the given ``mode`` and return a report.

    ``persist`` controls whether the extraction cache (state.json) is written back; read-only
    callers (e.g. ``describe``) pass ``persist=False`` to avoid mutating the cache.
    """
    started = time.perf_counter()
    if mode not in config.VALID_MODES:
        raise errors.CompactError(
            errors.E_USAGE,
            f"unknown validation mode '{mode}'",
            fix=f"use one of: {', '.join(sorted(config.VALID_MODES))}",
        )

    root, subsystems, issues = manifest_loader.load_all(project_root)
    final_enforce = (enforce or root.enforce or "off").lower()

    # hotfix: no checks, always clean.
    if mode == "hotfix":
        return ValidationReport(
            status="fresh",
            mode="hotfix",
            ok=True,
            issues=issues,
            stats={"subsystems": len(subsystems), "note": "hotfix mode: no checks run"},
        )

    exts = supported_extensions()

    # ---- File selection: every supported file under each subsystem's paths ----
    file_owner: dict[str, str] = {}
    files: list[tuple[str, Path, str]] = []  # (rel posix, abs path, owner)
    for name in sorted(subsystems):
        sub = subsystems[name]
        for abs_path in _iter_files(project_root, sub, exts):
            rel = abs_path.relative_to(project_root).as_posix()
            if rel in file_owner:  # flat topology: first declared owner wins
                continue
            file_owner[rel] = name
            files.append((rel, abs_path, name))

    state = cache_store.load_state(project_root)
    was_cold = len(state.files) == 0

    # ---- In quick mode, git tells us which files actually changed ----
    changed_rel: set[str] = set()
    if mode == "quick":
        repo = gitutil.repo_root(project_root) or project_root
        root_resolved = project_root.resolve()
        for changed in gitutil.changed_files(repo, base):
            try:
                changed_rel.add(changed.resolve().relative_to(root_resolved).as_posix())
            except (ValueError, OSError):
                continue

    # ---- Extraction (cache-accelerated) ----
    extracts: dict[str, "object"] = {}
    dirty: set[str] = set()
    parsed = 0
    cache_hits = 0

    for rel, abs_path, owner in files:
        adapter = get_adapter(rel)
        if adapter is None:
            continue
        prev = state.get(rel)

        # Quick mode: a file git says is unchanged is trusted from cache without hashing/parsing.
        if mode == "quick" and rel not in changed_rel and prev is not None:
            extracts[rel] = prev.to_result()
            cache_hits += 1
            continue

        try:
            source = abs_path.read_bytes()
        except OSError:
            continue

        chash = content_hash(source)
        if prev is not None and prev.content_hash == chash:
            extracts[rel] = prev.to_result()
            cache_hits += 1
            continue

        result = adapter.extract(rel, source)
        parsed += 1
        if result.error:
            issues.append(
                Issue(
                    errors.E_EXTRACTION_FAILED,
                    "warning",
                    f"could not parse '{rel}': {result.error}",
                    subsystem=owner,
                    file=rel,
                    fix="check the file for syntax errors; Compact skipped it",
                )
            )
        # A structural change (vs the prior cache) marks the owning subsystem dirty.
        if not was_cold and (prev is None or prev.structure_hash != result.structure_hash):
            dirty.add(owner)
        state.put(result)
        extracts[rel] = result

    state.prune(set(file_owner))
    if persist:
        try:
            cache_store.save_state(project_root, state)
        except OSError:
            pass  # cache is an optimization; never fail validation over it

    propagated = propagation.propagate(dirty, subsystems)

    ctx = CheckContext(
        project_root=project_root,
        root=root,
        subsystems=subsystems,
        extracts=extracts,  # type: ignore[arg-type]
        file_owner=file_owner,
        dirty=dirty,
        propagated=propagated,
    )
    for check in CHECKS_BY_MODE.get(mode, []):
        issues.extend(check(ctx))

    # Quick mode is advisory: downgrade errors to warnings so it never blocks.
    if mode == "quick":
        for issue in issues:
            if issue.severity == "error":
                issue.severity = "warning"

    status = _status(issues)
    blocking = _is_blocking(issues, mode, final_enforce)
    duration_ms = int((time.perf_counter() - started) * 1000)

    stats = {
        "files_total": len(files),
        "files_parsed": parsed,
        "cache_hits": cache_hits,
        "subsystems": len(subsystems),
        "dirty": sorted(dirty),
        "propagated": sorted(propagated),
        "enforce": final_enforce,
        "duration_ms": duration_ms,
    }
    return ValidationReport(status=status, mode=mode, ok=not blocking, issues=issues, stats=stats)


# ===========================================================================
# Helpers
# ===========================================================================
def _iter_files(project_root: Path, sub, exts: set[str]) -> list[Path]:
    """All supported source files belonging to a subsystem (deterministically sorted)."""
    out: list[Path] = []
    seen: set[Path] = set()

    def add(f: Path) -> None:
        try:
            key = f.resolve()
        except OSError:
            key = f
        if key in seen:
            return
        seen.add(key)
        out.append(f)

    for raw in sub.paths or []:
        base = project_root / raw
        if base.is_dir():
            for f in base.rglob("*"):
                if f.is_file() and f.suffix in exts and not _ignored(f, project_root):
                    add(f)
        elif base.is_file():
            if base.suffix in exts:
                add(base)
        else:  # treat as a glob relative to the project root
            for f in sorted(project_root.glob(raw)):
                if f.is_file() and f.suffix in exts and not _ignored(f, project_root):
                    add(f)

    for raw in sub.files or []:
        f = project_root / raw
        if f.is_file() and f.suffix in exts:
            add(f)

    return sorted(out, key=lambda p: p.as_posix())


def _ignored(f: Path, project_root: Path) -> bool:
    try:
        rel = f.relative_to(project_root)
    except ValueError:
        return False
    return any(part in config.DEFAULT_IGNORES for part in rel.parts)


def _status(issues: list[Issue]) -> str:
    if any(i.code == errors.E_UNRESOLVED_REFERENCE for i in issues):
        return "unresolved"
    if any(i.severity == "error" for i in issues):
        return "stale"
    return "fresh"


def _is_blocking(issues: list[Issue], mode: str, enforce: str) -> bool:
    has_error = any(i.severity == "error" for i in issues)
    if mode == "preflight":
        return has_error
    if mode == "full":
        return has_error and enforce == "on"
    # quick, hotfix, audit never block.
    return False
