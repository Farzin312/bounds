"""Drift-related CLI command implementations.

Commands: validate, preflight, calibrate, coverage, fix-coverage.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import util
from ..core import (
    calibrate as calibrate_mod,
    coverage as coverage_mod,
)
from ..core.validate import engine as validate_engine
from ..shared import config, errors, output
from ..core.manifest import loader as manifest_loader

def validate_cmd(
    quick: bool,
    mode: str | None,
    enforce: str | None,
    base: str,
    include_ignored: bool,
    include_gitignored: bool,
    follow_symlinks: bool,
    fail_on_unowned: bool,
    is_ci: bool,
    human: bool,
) -> None:
    """Run architecture checks; catch drift, violations, and cycles."""

    def go() -> None:
        root = util.require_root()
        selected = "quick" if quick else (mode or "full")
        with util.progress("validating..."):
            report = validate_engine.run(
                root,
                mode=selected,
                base=base,
                enforce=enforce,
                include_ignored=include_ignored,
                include_gitignored=include_gitignored,
                follow_symlinks=follow_symlinks,
                fail_on_unowned=fail_on_unowned,
            )
        output.emit(report.to_dict(), human, ci=is_ci)
        sys.exit(config.EXIT_OK if report.ok else config.EXIT_BLOCKED)

    util.run_wrapped(human, go, ci=is_ci)


def preflight_cmd(
    is_ci: bool,
    include_ignored: bool,
    include_gitignored: bool,
    follow_symlinks: bool,
    fail_on_unowned: bool,
    human: bool,
) -> None:
    """Production-ready architecture gate (validates + coverage check)."""

    def go() -> None:
        root = util.require_root()
        with util.progress("running preflight checks..."):
            report = validate_engine.run(
                root,
                mode="preflight",
                include_ignored=include_ignored,
                include_gitignored=include_gitignored,
                follow_symlinks=follow_symlinks,
                fail_on_unowned=fail_on_unowned,
            )
        from collections import Counter

        counts = Counter()
        for issue in report.issues:
            counts[issue.code] += issue.count
        payload = report.to_dict()
        payload["checks"] = {
            "structural_drift": counts.get(errors.E_STRUCTURAL_DRIFT, 0),
            "boundary_compliance": counts.get(errors.E_BOUNDARY_VIOLATION, 0),
            "contract_compliance": counts.get(errors.E_CONTRACT_MISSING_EXPORT, 0),
            "cross_subsystem_impact": counts.get(errors.E_STALE_INTERFACE, 0),
            "cycle_detection": counts.get(errors.E_CYCLE_DETECTED, 0),
            "orphan_detection": counts.get(errors.E_ORPHAN_EXPORT, 0),
        }
        output.emit(payload, human, ci=is_ci)
        sys.exit(config.EXIT_OK if report.ok else config.EXIT_BLOCKED)

    util.run_wrapped(human, go, ci=is_ci)

def calibrate_cmd(
    subsystem: str | None,
    do_apply: bool,
    dry_run: bool,
    do_check: bool,
    do_dump: bool,
    do_prune: bool,
    prune_missing_exports: bool,
    track_interfaces: bool,
    coarsen_interfaces: bool,
    human: bool,
) -> None:
    """Keep contracts aligned with extracted source after code changes."""
    # Diff/apply/dump-baseline are interactive actions → announce in a terminal; --check is a
    # CI gate consumed by automation → keep it JSON-default (it still honors explicit --human).
    human = human if do_check else util.interactive_human(human)

    def go() -> None:
        # The four modes are mutually exclusive: diff (default) / apply / check / dump-baseline.
        chosen = [n for n, on in
                  (("--apply", do_apply), ("--check", do_check), ("--dump-baseline", do_dump)) if on]
        if len(chosen) > 1 or (do_apply and dry_run):
            raise errors.BoundsError(
                errors.E_USAGE, "pass at most one of --apply / --check / --dump-baseline",
                fix="omit all for a diff, --apply to write, --check to gate, --dump-baseline to record",
            )
        if track_interfaces and coarsen_interfaces:
            raise errors.BoundsError(
                errors.E_USAGE,
                "pass only one of --track-interfaces or --coarsen-interfaces",
                fix="use --track-interfaces to add exact consumes interfaces, or "
                    "--coarsen-interfaces to remove them",
            )
        root = util.require_root()
        if do_dump:
            with util.progress("calibrating..."):
                baseline = calibrate_mod.dump_baseline(root, subsystem=subsystem)
            output.emit(baseline, human)
            return
        if do_check:
            with util.progress("calibrating..."):
                payload = calibrate_mod.check_drift(root, subsystem=subsystem)
            output.emit(payload, human)
            sys.exit(config.EXIT_OK if payload["ok"] else config.EXIT_BLOCKED)
        with util.progress("calibrating..."):
            payload = calibrate_mod.run_calibrate(
                root, subsystem=subsystem, apply=do_apply, prune_unknown=do_prune,
                prune_missing_exports=prune_missing_exports,
                track_interfaces=track_interfaces,
                coarsen_interfaces=coarsen_interfaces,
            )
        output.emit(payload, human)

    util.run_wrapped(human, go)

def coverage_cmd(why_path: str | None, summary: bool, human: bool) -> None:
    """Report full mapping coverage or explain one path. Read-only."""

    def go() -> None:
        root = util.require_root()
        _rootm, subs, _schema_issues = manifest_loader.load_all(root)
        with util.progress("computing coverage..."):
            payload = coverage_mod.run_coverage(
                root,
                subs,
                why=why_path,
                summary_only=summary,
            )
        output.emit(payload, human)

    util.run_wrapped(human, go)

def fix_coverage_cmd(explain_path: str | None, do_auto: bool, apply: bool, human: bool) -> None:
    """Diagnose coverage gaps; preview before applying deterministic fixes."""
    human = util.interactive_human(human)

    def go() -> None:
        if apply and not do_auto:
            raise errors.BoundsError(
                errors.E_USAGE,
                "--apply requires --auto",
                fix="preview with `bounds fix-coverage --auto`, then add `--apply`",
            )
        root = util.require_root()
        _rootm, subs, _schema_issues = manifest_loader.load_all(root)
        with util.progress("computing coverage..."):
            payload = coverage_mod.run_fix_coverage(
                root,
                subs,
                explain_path=explain_path,
                auto=do_auto,
                apply=apply,
            )
        output.emit(payload, human)

    util.run_wrapped(human, go)
