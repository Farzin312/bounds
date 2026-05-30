"""Validation orchestration: mode dispatch, file selection, cached extraction, and check execution.

Flow (see ARCHITECTURE.md §2):
  load manifests -> select files -> extract (cache-accelerated) -> classify dirty subsystems
  -> propagate to consumers -> build CheckContext -> run the mode's checks -> assemble report.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .. import config, errors, gitutil
from ..cache import store as cache_store
from ..extract import content_hash, get_adapter, supported_extensions, scan
from ..ignore import IgnoreMatcher, load_matcher
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
    include_ignored: bool = False,
    include_gitignored: bool = False,
    follow_symlinks: bool = False,
    fail_on_unowned: bool = False,
) -> ValidationReport:
    """Validate the project at ``project_root`` in the given ``mode`` and return a report.

    ``persist`` controls whether the extraction cache (``.bounds/cache.db``) is written back;
    read-only callers (e.g. ``describe``) pass ``persist=False`` to avoid mutating the cache.

    File-selection toggles (all default off, matching the "scan less, by default"
    posture): ``include_ignored`` disables ``.boundsignore``; ``include_gitignored`` scans
    files excluded by ``.gitignore``; ``follow_symlinks`` includes external symlinks instead
    of skipping them with a warning; ``fail_on_unowned`` promotes tracked source files outside
    every subsystem from silent to a blocking ``E_UNOWNED_FILE`` error — except files matching
    a ``root.entry_points`` glob, which stay non-blocking warnings.
    """
    started = time.perf_counter()
    if mode not in config.VALID_MODES:
        raise errors.BoundsError(
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
    repo = gitutil.repo_root(project_root) or project_root
    matcher = IgnoreMatcher([]) if include_ignored else load_matcher(project_root)
    # Root-declared bootstrap files (main.py, app.py, ...) — exempt from --fail-on-unowned.
    entry_matcher = IgnoreMatcher(root.entry_points)

    # ---- File selection: every supported file under each subsystem's paths ----
    # External symlinks and .boundsignore matches are filtered here; .gitignore is
    # applied in one batched git call afterwards.
    file_owner: dict[str, str] = {}
    files: list[tuple[str, Path, str]] = []  # (rel posix, abs path, owner)
    skipped_ignored = 0
    for name in sorted(subsystems):
        sub = subsystems[name]
        for abs_path in scan.iter_subsystem_files(project_root, sub, exts):
            rel = abs_path.relative_to(project_root).as_posix()
            if rel in file_owner:  # flat topology: first declared owner wins
                continue
            if not follow_symlinks and _is_external_symlink(abs_path, project_root):
                issues.append(
                    Issue(
                        errors.E_EXTERNAL_SYMLINK,
                        "warning",
                        f"skipped external symlink '{rel}' (resolves outside the project)",
                        subsystem=name,
                        file=rel,
                        fix="pass --follow-symlinks to include it, or replace it with an in-tree copy",
                    )
                )
                continue
            if matcher and matcher.matches(rel):
                skipped_ignored += 1
                continue
            file_owner[rel] = name
            files.append((rel, abs_path, name))

    skipped_gitignored = 0
    if not include_gitignored and files:
        ignored = gitutil.gitignored(repo, [rel for rel, _, _ in files])
        if ignored:
            files = [t for t in files if t[0] not in ignored]
            for rel in ignored:
                file_owner.pop(rel, None)
            skipped_gitignored = len(ignored)

    state = cache_store.load_state(project_root)
    was_cold = len(state.files) == 0

    # ---- In quick mode, git tells us which files actually changed ----
    changed_rel: set[str] = set()
    if mode == "quick":
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
            prev.subsystem = owner  # keep the cached owner current (partial reads)
            cache_hits += 1
            continue

        # Fail loud on an OWNED file we can't read or that's oversized — never silently drop it
        # (a dropped owned file makes a real symbol look like verified:false).
        try:
            if abs_path.stat().st_size > config.MAX_FILE_BYTES:
                issues.append(
                    Issue(
                        errors.E_EXTRACTION_FAILED,
                        "warning",
                        f"skipped '{rel}': exceeds MAX_FILE_BYTES ({config.MAX_FILE_BYTES} bytes)",
                        subsystem=owner,
                        file=rel,
                        fix="file too large to extract; split it or exclude it via .boundsignore",
                    )
                )
                continue
            source = abs_path.read_bytes()
        except OSError as exc:
            issues.append(
                Issue(
                    errors.E_EXTRACTION_FAILED,
                    "warning",
                    f"could not read '{rel}': {exc}",
                    subsystem=owner,
                    file=rel,
                    fix="check the file's permissions/encoding; Bounds skipped it",
                )
            )
            continue

        chash = content_hash(source)
        if prev is not None and prev.content_hash == chash:
            extracts[rel] = prev.to_result()
            prev.subsystem = owner  # keep the cached owner current (partial reads)
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
                    fix="check the file for syntax errors; Bounds skipped it",
                )
            )
        # A structural change (vs the prior cache) marks the owning subsystem dirty.
        if not was_cold and (prev is None or prev.structure_hash != result.structure_hash):
            dirty.add(owner)
        state.put(result, owner)
        extracts[rel] = result

    state.prune(set(file_owner))
    if persist:
        try:
            cache_store.save_state(project_root, state)
        except (OSError, sqlite3.Error):
            pass  # cache is an optimization; never fail validation over it (incl. a locked db)

    propagated = propagation.propagate(dirty, subsystems, root.criticality_registry())

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

    # ---- Ownership exhaustiveness (opt-in, blocks regardless of mode/enforce) ----
    # Computed after the quick downgrade so --fail-on-unowned stays a hard gate even in
    # quick mode (promote unowned from silent to error).
    # Entry-point files (root.entry_points) are reported but never block.
    unowned: list[Issue] = []
    entry_points: list[str] = []
    if fail_on_unowned:
        unowned, entry_points = _unowned_issues(
            project_root, repo, exts, file_owner, matcher, entry_matcher
        )
        issues.extend(unowned)

    status = _status(issues)
    unowned_blocks = any(i.severity == "error" for i in unowned)
    blocking = _is_blocking(issues, mode, final_enforce) or unowned_blocks
    duration_ms = int((time.perf_counter() - started) * 1000)

    # coverage: an honest signal that boundary checking is not as complete as a clean
    # report implies. unresolved_local_imports is measured only in boundary-checking modes
    # (full/preflight/audit); quick reports 0 (it runs no boundary check).
    coverage = {
        "files_owned": len(files),
        "unresolved_local_imports": ctx.unresolved_local_imports,
        "extraction_failures": sum(1 for i in issues if i.code == errors.E_EXTRACTION_FAILED),
    }

    stats = {
        "files_total": len(files),
        "files_parsed": parsed,
        "cache_hits": cache_hits,
        "subsystems": len(subsystems),
        "dirty": sorted(dirty),
        "propagated": sorted(propagated),
        "enforce": final_enforce,
        "skipped_ignored": skipped_ignored,
        "skipped_gitignored": skipped_gitignored,
        "unowned": sum(1 for i in unowned if i.severity == "error"),
        "entry_points": sorted(entry_points),
        "coverage": coverage,
        "duration_ms": duration_ms,
    }
    return ValidationReport(status=status, mode=mode, ok=not blocking, issues=issues, stats=stats)


# ===========================================================================
# Helpers
# ===========================================================================
def _is_external_symlink(abs_path: Path, project_root: Path) -> bool:
    """True if ``abs_path`` reaches its target through a symlink that escapes the project.

    A file that is itself a symlink, or that sits under a symlinked directory, and whose
    real location is outside ``project_root`` is "external". Internal symlinks (resolving
    back inside the tree) are followed normally. Fail soft: any OS error -> not external.
    """
    try:
        root_real = project_root.resolve()
        real = abs_path.resolve()
    except OSError:
        return False
    try:
        real.relative_to(root_real)
        return False  # resolves inside the project tree
    except ValueError:
        pass
    # Resolves outside the tree — confirm a symlink is actually on the path before flagging.
    probe = abs_path
    while True:
        try:
            if probe.is_symlink():
                return True
        except OSError:
            return False
        if probe == project_root or probe.parent == probe:
            return False
        probe = probe.parent


def _unowned_issues(
    project_root: Path,
    repo: Path,
    exts: set[str],
    file_owner: dict[str, str],
    matcher: IgnoreMatcher,
    entry_matcher: IgnoreMatcher,
) -> tuple[list[Issue], list[str]]:
    """One ``E_UNOWNED_FILE`` Issue per tracked source file outside every subsystem.

    The universe is git-tracked source files (or a filesystem walk when not a repo),
    minus ``.boundsignore`` matches and the hard-coded ignore directories. Anything in
    that universe not claimed by a subsystem's ``paths``/``files`` is unowned.

    Files matching a ``root.entry_points`` glob are known bootstrap files (main.py,
    app.py, ...): they degrade from a blocking ``error`` to a non-blocking ``warning`` and
    are never promoted by ``--fail-on-unowned``. Genuinely unowned
    files stay blocking errors. Returns ``(issues, entry_point_paths)``.
    """
    universe = _source_universe(project_root, repo, exts, matcher)
    owned = set(file_owner)
    out: list[Issue] = []
    entry_points: list[str] = []
    for rel in sorted(universe - owned):
        if entry_matcher and entry_matcher.matches(rel):
            entry_points.append(rel)
            out.append(
                Issue(
                    errors.E_UNOWNED_FILE,
                    "warning",
                    f"entry-point file '{rel}' is owned by no subsystem (declared in root.entry_points)",
                    file=rel,
                    fix="known entry point — exempt from --fail-on-unowned; "
                    "remove it from root.entry_points to enforce ownership",
                )
            )
            continue
        out.append(
            Issue(
                errors.E_UNOWNED_FILE,
                "error",
                f"source file '{rel}' is owned by no subsystem",
                file=rel,
                fix=(
                    f"add '{rel}' to a subsystem's paths/files, exclude it via .boundsignore, "
                    "or declare it an entry point in root.entry_points"
                ),
            )
        )
    return out, entry_points


def _source_universe(
    project_root: Path, repo: Path, exts: set[str], matcher: IgnoreMatcher
) -> set[str]:
    """Project-relative POSIX paths of all in-scope source files (for unowned detection)."""
    universe: set[str] = set()
    tracked = gitutil.tracked_files(repo)
    if tracked is not None:
        root_real = project_root.resolve()
        for rrel in tracked:
            abs_path = repo / rrel
            if abs_path.suffix not in exts:
                continue
            try:
                prel = abs_path.resolve().relative_to(root_real).as_posix()
            except (ValueError, OSError):
                continue  # tracked file outside the scoped project root
            if scan.in_default_ignores(abs_path, project_root):
                continue
            if matcher and matcher.matches(prel):
                continue
            universe.add(prel)
        return universe

    # Not a git repo: walk the filesystem instead (symlink-cycle-safe shared walker).
    for f in scan.walk_supported(project_root, exts):
        prel = f.relative_to(project_root).as_posix()
        if matcher and matcher.matches(prel):
            continue
        universe.add(prel)
    return universe


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
