"""Validation orchestration: mode dispatch, file selection, cached extraction, and check execution.

Flow (see ARCHITECTURE.md §2):
  load manifests -> select files -> extract (cache-accelerated) -> classify dirty subsystems
  -> propagate to consumers -> build CheckContext -> run the mode's checks -> assemble report.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .. import config, errors, gitutil, surface
from ..cache import store as cache_store
from ..extract import content_hash, get_adapter, supported_extensions, scan
from ..ignore import IgnoreMatcher, has_generated_marker, load_matcher
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
    # Ownership (most-specific declared path wins) is resolved once in scan.resolve_owners so this
    # agrees with calibrate/where/impact; external symlinks and .boundsignore matches are filtered
    # here; .gitignore is applied in one batched git call afterwards.
    file_owner: dict[str, str] = {}
    files: list[tuple[str, Path, str]] = []  # (rel posix, abs path, owner)
    skipped_ignored = 0
    owners = scan.resolve_owners(project_root, subsystems, exts)
    for rel in sorted(owners):
        name, abs_path = owners[rel]
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

    issues.extend(
        scan._ownership_overlap_issues(
            project_root,
            subsystems,
            exts,
            matcher,
            repo,
            include_gitignored,
            aggregate=True,
        )
    )

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
    generated_files: set[str] = set()
    parsed = 0
    cache_hits = 0
    state_changed = False

    for rel, abs_path, owner in files:
        adapter = get_adapter(rel)
        if adapter is None:
            continue
        prev = state.get(rel)

        # Quick mode: a file git says is unchanged is trusted from cache without hashing/parsing.
        if mode == "quick" and rel not in changed_rel and prev is not None and not scan.is_oversized(abs_path):
            cached = prev.to_result()
            extracts[rel] = cached
            if cached.generated:
                generated_files.add(rel)
            if prev.subsystem != owner:
                prev.subsystem = owner  # keep the cached owner current (partial reads)
                state_changed = True
            cache_hits += 1
            continue

        # Fail loud on an OWNED file we can't read or that's oversized — never silently drop it
        # (a dropped owned file makes a real symbol look like verified:false). The size/read
        # mechanics live in scan.read_source_bytes; only the loud-Issue policy is ours.
        source, reason = scan.read_source_bytes(abs_path)
        if source is None:
            if reason == "oversized":
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
            else:
                issues.append(
                    Issue(
                        errors.E_EXTRACTION_FAILED,
                        "warning",
                        f"could not read '{rel}': {reason}",
                        subsystem=owner,
                        file=rel,
                        fix="check the file's permissions/encoding; Bounds skipped it",
                    )
                )
            continue

        chash = content_hash(source)
        if prev is not None and prev.content_hash == chash:
            cached = prev.to_result()
            extracts[rel] = cached
            if cached.generated:
                generated_files.add(rel)
            if prev.subsystem != owner:
                prev.subsystem = owner  # keep the cached owner current (partial reads)
                state_changed = True
            cache_hits += 1
            continue

        result = adapter.extract(rel, source)
        result.generated = has_generated_marker(source)
        if result.generated:
            generated_files.add(rel)
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
        state_changed = True
        extracts[rel] = result

    before_prune = set(state.files)
    live = set(file_owner)
    # A provider file that was deleted on disk is gone from the on-disk owner map, so the
    # extraction loop above never marks its subsystem dirty (it only iterates present files).
    # In quick mode that silently starves propagation: cross_impact then never warns the
    # deleted provider's consumers (E_STALE_INTERFACE). Recover the owner from the CACHED
    # FileRecord (the only surviving source) and mark it dirty BEFORE pruning the row. Skipped
    # on a cold first run (no prior cache) so a fresh repo marks nothing dirty.
    if not was_cold:
        for path in before_prune - live:
            cached = state.files[path]
            if cached.subsystem:
                dirty.add(cached.subsystem)
    state.prune(live)
    if set(state.files) != before_prune:
        state_changed = True
    if persist and state_changed:
        try:
            cache_store.save_state(project_root, state)
        except (OSError, sqlite3.Error):
            pass  # cache is an optimization; never fail validation over it (incl. a locked db)

    propagated = propagation.propagate(dirty, subsystems, root.criticality_registry())

    # Subsystems owning an UNSUPPORTED-language source file (Go/Rust/Java/…): the SAME signal
    # calibrate computes (scan.subsystems_with_unsupported_source), so structural-drift never flags a
    # hand-authored expose Bounds couldn't verify while calibrate routes it to needs_review. A
    # directory/extension walk only (no source reads), so it stays within the --quick budget.
    unsupported_owners = scan.subsystems_with_unsupported_source(project_root, subsystems, matcher)

    ctx = CheckContext(
        project_root=project_root,
        root=root,
        subsystems=subsystems,
        extracts=extracts,  # type: ignore[arg-type]
        file_owner=file_owner,
        dirty=dirty,
        propagated=propagated,
        unsupported_owners=unsupported_owners,
        generated_files=generated_files,
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

    # mapping coverage: how much of the repo's SOURCE Bounds actually mapped — the honest answer to
    # "did you map everything?". Walks all files, so it's gated off the --quick hot path. A non-empty
    # gap surfaces as one loud, advisory E_COVERAGE_GAP warning with a next step, never silently.
    mapping = None
    if mode != "quick":
        mapping = scan.mapping_coverage(
            project_root, set(file_owner), matcher,
            repo=repo, include_gitignored=include_gitignored, subsystems=subsystems,
        )
        # The gap fires only on the CLOSEABLE set — supported files in no subsystem and unsupported
        # files no manifest claims (`dark`). Declared unsupported files are covered (durable), and
        # tests/docs are tracked separately — none of those block.
        if scan.coverage_has_gap(mapping):
            issues.append(_coverage_gap_issue(mapping, project_root, subsystems))

        # Unsupported-language surface staleness (off --quick): a hand-authored expose for a file
        # Bounds can't parse has no extraction to verify against, so the deterministic signal that it
        # may be outdated is "the file changed since it was confirmed". Compare the live unsupported
        # surface to the COMMITTED baseline (.bounds/surface-baseline.json, written by
        # `calibrate --dump-baseline`); absent baseline ⇒ no signal (opt-in, like the drift baseline).
        baseline = surface.load_baseline(project_root)
        if baseline and subsystems:
            # The baseline is always dumped gitignore-filtered (calibrate's default); validate passes
            # its own flag. A `--include-gitignored` run makes `current_surface` a SUPERSET of the
            # baseline, never a subset — and `stale_subsystems` ignores additions (only changed/removed
            # baseline files are stale), so the asymmetry can't produce a spurious signal. Don't
            # "fix" it by forcing the flag here.
            current_surface = scan.unsupported_surface_files(
                project_root, subsystems, matcher, repo=repo, include_gitignored=include_gitignored
            )
            for name, changed in surface.stale_subsystems(baseline, current_surface).items():
                issues.append(_surface_stale_issue(name, changed))

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
    if mapping is not None:
        coverage["mapping"] = mapping

    # Quick mode deliberately does not walk the full tree, so it cannot claim a
    # coverage percentage or completeness. It can still report the owned-file
    # evidence already in hand and point at the skipped full check.
    coverage_summary: dict | None = None
    if mode == "quick":
        coverage_summary = {
            "complete": None,
            "owned_files_current": len(file_owner),
            "cached_file_records": len(state.files),
            "note": (
                "coverage completeness is unknown because --quick skips the full-tree coverage check; "
                "run `bounds coverage` for the breakdown or `bounds validate` for the full report"
            ),
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
    if coverage_summary is not None:
        # Top-level so the `mapping not in coverage` contract the existing test
        # relies on still holds (--quick omits `mapping`, but always reports a
        # summary; downstream consumers can read either field).
        stats["coverage_summary"] = coverage_summary
    if mode == "quick":
        stats["skipped_checks"] = ["boundary", "contract", "cycles", "coverage", "orphans"]
    return ValidationReport(
        status=status,
        mode=mode,
        ok=not blocking,
        issues=issues,
        next_steps=_next_steps(issues, mode=mode),
        stats=stats,
    )


# ===========================================================================
# Helpers
# ===========================================================================
_SURFACE_SAMPLE_CAP = 5  # token-lean: name at most this many changed files, then "+N more"


def _surface_stale_issue(subsystem: str, changed: list[str]) -> Issue:
    """Advisory warning: a subsystem's UNSUPPORTED-language source changed since its hand-authored
    ``exposes`` were confirmed, so the surface may be outdated (Bounds has no adapter to verify it).
    Names a capped sample of the changed/removed files and tells the agent to re-verify, then
    re-confirm — the deterministic "re-run" trigger, no LLM."""
    n = len(changed)
    sample = ", ".join(changed[:_SURFACE_SAMPLE_CAP])
    if n > _SURFACE_SAMPLE_CAP:
        sample += f", +{n - _SURFACE_SAMPLE_CAP} more"
    return Issue(
        errors.E_UNSUPPORTED_SURFACE_STALE,
        "warning",
        f"subsystem '{subsystem}': {n} unsupported-language file(s) changed since its exposes "
        f"were confirmed ({sample})",
        subsystem=subsystem,
        count=n,
        fix=f"re-verify {subsystem}.exposes against the changed file(s), then "
        "`bounds calibrate --dump-baseline` to re-confirm",
    )


def _next_steps(issues: list[Issue], *, mode: str) -> list[str]:
    """Group raw issue codes into the remediation ladder a human or agent should follow.

    Individual issues still carry exact fixes; this higher-level list prevents a common mistake:
    treating `bounds calibrate --apply` as a universal repair command when coverage gaps and cycles
    require different work.
    """
    codes = {i.code for i in issues}
    steps: list[str] = []
    if mode == "quick":
        steps.append(
            "`--quick` skips boundary, contract, cycle, coverage, and orphan checks; run "
            "`bounds validate -H` before claiming the repo is fully clean."
        )
    if errors.E_COVERAGE_GAP in codes:
        steps.append(
            "Close E_COVERAGE_GAP first with `bounds coverage`: assign `user_decision_needed` "
            "source with `bounds init --subsystem <name> --path <file-or-dir>`, preview "
            "`algorithm_miss` repairs with `bounds fix-coverage --auto`, or hand-author a "
            "manifest for dark unsupported source. `bounds calibrate` does not map files."
        )
    if errors.E_CYCLE_DETECTED in codes:
        steps.append(
            "Break E_CYCLE_DETECTED in source: invert the dependency, move shared code into a lower "
            "library subsystem, or merge boundaries that do not represent separate architecture."
        )
    if any(c in codes for c in (
        errors.E_STRUCTURAL_DRIFT,
        errors.E_CONTRACT_MISSING_EXPORT,
        errors.E_STALE_INTERFACE,
    )):
        steps.append(
            "For source/manifest drift, run `bounds calibrate`, review the diff, then use "
            "`bounds calibrate --apply` only when the contract change is intentional."
        )
    if errors.E_UNRESOLVED_REFERENCE in codes:
        steps.append(
            "For unresolved manifest references, declare the missing subsystem/interface, fix the name, "
            "or use `bounds calibrate --prune-unknown --apply` for stale dangling consumes edges."
        )
    if errors.E_BOUNDARY_VIOLATION in codes:
        steps.append(
            "For boundary violations, either import a declared public interface or change the subsystem "
            "contract deliberately and re-run validation."
        )
    if errors.E_SUBSYSTEM_OVERLAP in codes:
        steps.append(
            "For ownership overlaps, narrow one manifest path or move a shared file to `files:` so one "
            "subsystem owns it deterministically."
        )
    if errors.E_UNSUPPORTED_SURFACE_STALE in codes:
        steps.append(
            "For unsupported surface staleness, manually re-check the hand-authored exposes and then "
            "run `bounds calibrate --dump-baseline` to confirm the new file hashes."
        )
    return steps


def _sample_manifest_path(project_root: Path, subsystems: dict) -> str | None:
    """A concrete existing manifest an agent can copy as a template (alphabetically first), or None."""
    for name in sorted(subsystems or {}):
        rel = f"{config.BOUNDS_DIR}/{config.MANIFESTS_DIR}/{name}.yaml"
        if (project_root / rel).is_file():
            return rel
    return None


def _coverage_gap_issue(mapping: dict, project_root: Path, subsystems: dict) -> Issue:
    """One loud, advisory issue for the CLOSEABLE coverage gap + a concrete, agent-followable procedure.

    Fired only when a closeable gap remains. Three gap kinds, three moves (named separately because
    the right fix differs):

      * **user_decision_needed** — a real source file in no subsystem (we have an adapter — Python/
        TS/JS/SQL/Prisma/shell). The user must decide where it goes. Deterministic fix: add it to a
        manifest's `paths:` or `bounds init --subsystem <name> --path <file-or-dir>`.
      * **algorithm_miss** — a file Bounds should have caught but didn't (e.g. a `vite.config.ts` in
        a monorepo not in any subsystem). The fix is `bounds fix-coverage --auto` for known
        patterns, or widen a subsystem's `paths:` glob.
      * **unsupported-language file no manifest claims** (`dark`, no adapter yet): author a manifest;
        the `exposes` you hand-write are DURABLE (calibrate routes them to needs_review, validate
        never flags them as drift). A *declared* unsupported file is already covered and never here.

    The `fix` carries a numbered procedure and a concrete `template_ref` so a JSON-first agent can
    act without opening the docs. The headline % is supported-language source only — reachable.
    Test/doc linkage is reported separately in stats, never here.
    """
    sup = mapping["supported"]
    unsup = mapping["unsupported"]
    breakdown = sup.get("unowned_breakdown", {})
    user_decision_count = breakdown.get("user_decision_needed", {}).get("count", 0) if breakdown else sup.get("unowned", 0)
    algo_miss_count = breakdown.get("algorithm_miss", {}).get("count", 0) if breakdown else 0
    # Back-compat: when the breakdown is absent (older mapping payloads), `unowned` carries both.
    if not breakdown and user_decision_count:
        algo_miss_count = sup.get("unowned", 0) - user_decision_count

    parts = []
    if user_decision_count:
        parts.append(f"{user_decision_count} supported file(s) need your decision (add to a subsystem's `paths:`)")
    if algo_miss_count:
        parts.append(f"{algo_miss_count} supported file(s) are algorithm misses (e.g. build config) — run `bounds fix-coverage --auto`")
    if unsup["dark"]:
        langs = ", ".join(f"{lang}×{n}" for lang, n in sorted(unsup["by_language"].items()))
        parts.append(f"{unsup['dark']} unsupported-language file(s) no manifest claims ({langs})")
    detail = "; ".join(parts) or "some source files"
    samples = []
    if breakdown:
        ud_sample = breakdown.get("user_decision_needed", {}).get("sample", [])
        am_sample = breakdown.get("algorithm_miss", {}).get("sample", [])
        if ud_sample:
            samples.append("needs your decision: " + ", ".join(ud_sample))
        if am_sample:
            samples.append("algorithm miss: " + ", ".join(am_sample))
    elif sup.get("unowned_sample"):
        # Legacy payload fallback.
        samples.append("unmapped supported: " + ", ".join(sup["unowned_sample"]))
    if unsup.get("dark_sample"):
        samples.append("dark unsupported: " + ", ".join(unsup["dark_sample"]))
    sample_text = "; ".join(samples)

    template_ref = _sample_manifest_path(project_root, subsystems)
    steps: list[str] = []
    if user_decision_count:
        steps.append(
            "user-decision files (real source in no subsystem) → add each to a subsystem's `paths:` "
            "(`bounds init --subsystem <name> --path <file-or-dir>` scaffolds/registers one) — "
            "deterministic, no AI"
        )
    if algo_miss_count:
        steps.append(
            "algorithm-miss files (e.g. build configs the walker classified as source) → run "
            "`bounds fix-coverage --auto` to apply known fixes, or `bounds coverage --why <path>` "
            "to see the per-file reason"
        )
    if unsup["dark"]:
        tmpl = f" (copy `{template_ref}` as a template)" if template_ref else ""
        steps.append(
            "unsupported-language files (no adapter yet) → author a manifest with `paths:` + a "
            f"hand-written `exposes:`{tmpl}, then re-run `bounds validate`; those exposes are DURABLE "
            "(calibrate keeps them as needs_review, validate never flags them as drift)"
        )
    fix = (
        "reach 100% of supported source + 0 dark files — " + "; ".join(steps)
        + f". {unsup['declared']} unsupported file(s) are already covered by a manifest. "
        "Run `bounds validate -H` to see this issue with samples, `bounds coverage -H` for the "
        "gap breakdown, or see docs/coverage.md."
    )
    if sample_text:
        fix += f" First samples: {sample_text}."
    return Issue(
        errors.E_COVERAGE_GAP,
        "warning",
        f"mapped {mapping['mapped_pct']}% of supported-language source "
        f"({sup['mapped']}/{sup['total']} files); gap: {detail} "
        f"({unsup['declared']} unsupported file(s) already covered; tests/docs tracked separately)"
        + (f"; samples: {sample_text}" if sample_text else ""),
        fix=fix,
    )
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
    # Errors (drift / boundary violations — "run calibrate") outrank warning-level unresolved
    # references ("forward refs, fine mid-adoption"). When BOTH are present the headline must
    # report the actionable state, not the benign one: a status of "unresolved" tells an agent or
    # CI "nothing to do" (see docs/team-workflow.md), so letting it mask real drift would suppress
    # the very signal that should trigger a calibrate. Severity wins; the forward refs stay in the
    # issues list either way.
    if any(i.severity == "error" for i in issues):
        return "stale"
    if any(i.code == errors.E_UNRESOLVED_REFERENCE for i in issues):
        return "unresolved"
    return "fresh"


def _is_blocking(issues: list[Issue], mode: str, enforce: str) -> bool:
    has_error = any(i.severity == "error" for i in issues)
    if enforce == "warn":
        # Report-but-never-block: warn mode surfaces every issue (CI still prints the
        # report and the JSON still carries the errors) yet always exits clean, so a team
        # can watch structural drift on every PR before committing to a hard gate.
        return False
    if mode == "preflight":
        return has_error
    if mode == "full":
        return has_error and enforce == "on"
    # quick, hotfix, audit never block.
    return False
