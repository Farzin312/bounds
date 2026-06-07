"""Coverage command services.

This module owns the reusable behavior behind ``bounds coverage`` and
``bounds fix-coverage``. Click parsing and stdout rendering stay in ``cli``;
filesystem classification, explanations, and safe ignore-file updates live
here so they can be tested without a CLI runner.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath

from ..shared import config, gitutil
from ..shared.ignore import load_matcher
from ..shared.models import SubsystemCompact
from .extract import scan, supported_extensions
from .extract.scan import is_config_file

__all__ = [
    "run_coverage",
    "run_fix_coverage",
]


def run_coverage(
    root: Path,
    subsystems: dict[str, SubsystemCompact],
    *,
    why: str | None = None,
    summary_only: bool = False,
) -> dict:
    """Build the public payload for ``bounds coverage``."""
    mapping = _analyze(root, subsystems)
    if why:
        return _explain(root, subsystems, why)
    if summary_only:
        return _summary(mapping)
    return {"mode": "coverage", **mapping, "next_step": _next_step(mapping)}


def run_fix_coverage(
    root: Path,
    subsystems: dict[str, SubsystemCompact],
    *,
    explain_path: str | None = None,
    auto: bool = False,
    apply: bool = False,
) -> dict:
    """Build or apply the public payload for ``bounds fix-coverage``."""
    if explain_path:
        return _explain(root, subsystems, explain_path)
    if auto:
        return _apply_algorithm_fixes(root, subsystems, apply=apply)
    return _diagnose(_analyze(root, subsystems))


def _analyze(
    root: Path,
    subsystems: dict[str, SubsystemCompact],
    *,
    sample_cap: int | None = 10,
) -> dict:
    """Compute the full coverage payload using the same ignore rules as validation."""
    matcher = load_matcher(root)
    repo = gitutil.repo_root(root) or root
    owners = scan.resolve_owners(root, subsystems, supported_extensions(), matcher, repo)
    return scan.mapping_coverage(
        root,
        set(owners),
        matcher,
        repo=repo,
        subsystems=subsystems,
        sample_cap=sample_cap,
    )


def _summary(mapping: dict) -> dict:
    """Return the token-lean headline view for ``bounds coverage --summary``."""
    supported = mapping.get("supported", {})
    breakdown = supported.get("unowned_breakdown", {})
    return {
        "mode": "coverage-summary",
        "mapped_pct": mapping.get("mapped_pct", 0.0),
        "supported_total": supported.get("total", 0),
        "supported_mapped": supported.get("mapped", 0),
        "supported_unowned": supported.get("unowned", 0),
        "user_decision_needed": breakdown.get("user_decision_needed", {}).get("count", 0),
        "algorithm_miss": breakdown.get("algorithm_miss", {}).get("count", 0),
        "unsupported_dark": mapping.get("unsupported", {}).get("dark", 0),
        "principled_exclusions": mapping.get("principled_exclusions", {}).get("counts", {}),
        "next_step": _next_step(mapping),
    }


def _diagnose(mapping: dict) -> dict:
    """Return distinct remediation buckets without mutating the project."""
    supported = mapping.get("supported", {})
    breakdown = supported.get("unowned_breakdown", {})
    return {
        "mode": "coverage-diagnose",
        "mapped_pct": mapping.get("mapped_pct", 0.0),
        "principled_exclusions": mapping.get("principled_exclusions", {}),
        "user_decision_needed": {
            **breakdown.get("user_decision_needed", {"count": 0, "sample": []}),
            "fix": "assign each file with `bounds init --subsystem <name> --path <file-or-dir>`",
        },
        "algorithm_miss": {
            **breakdown.get("algorithm_miss", {"count": 0, "sample": [], "reasons": {}}),
            "fix": "preview `bounds fix-coverage --auto`; add `--apply` to update .boundsignore",
        },
        "unsupported_dark": {
            "count": mapping.get("unsupported", {}).get("dark", 0),
            "sample": mapping.get("unsupported", {}).get("dark_sample", []),
            "by_language": mapping.get("unsupported", {}).get("by_language", {}),
            "fix": "declare ownership and hand-written exposes for unsupported source",
        },
        "next_step": _next_step(mapping),
    }


def _next_step(mapping: dict) -> str:
    """Return one prioritized, copy-pasteable next action."""
    breakdown = mapping.get("supported", {}).get("unowned_breakdown", {})
    decisions = breakdown.get("user_decision_needed", {}).get("count", 0)
    misses = breakdown.get("algorithm_miss", {}).get("count", 0)
    dark = mapping.get("unsupported", {}).get("dark", 0)
    if misses:
        suffix = f", then assign {decisions} source file(s)" if decisions else ""
        return f"run `bounds fix-coverage --auto`{suffix}"
    if decisions:
        return (
            f"assign {decisions} source file(s) with "
            "`bounds init --subsystem <name> --path <file-or-dir>`"
        )
    if dark:
        return f"declare ownership and exposes for {dark} unsupported-language file(s)"
    return "coverage is complete; keep new source mapped"


def _explain(
    root: Path,
    subsystems: dict[str, SubsystemCompact],
    path: str,
) -> dict:
    """Explain exactly how one path is classified and what action, if any, follows."""
    requested = Path(path)
    absolute = requested.resolve() if requested.is_absolute() else (root / requested).resolve()
    try:
        rel = absolute.relative_to(root.resolve()).as_posix()
    except ValueError:
        return {
            "path": path,
            "status": "outside_project",
            "reason": "the path resolves outside the Bounds project root",
            "command": "pass a path inside the project",
        }
    if not absolute.is_file():
        return {
            "path": rel,
            "status": "not_found",
            "reason": "the file does not exist",
            "command": "verify the repo-relative path",
        }

    parts = PurePosixPath(rel).parts
    ignored_dir = next((part for part in parts[:-1] if part in config.DEFAULT_IGNORES), None)
    if ignored_dir:
        return {
            "path": rel,
            "status": "excluded_by_design",
            "reason": f"directory `{ignored_dir}` is a built-in recursive ignore",
            "command": "no action needed",
        }

    matcher = load_matcher(root)
    if matcher.matches(rel):
        return {
            "path": rel,
            "status": "excluded_by_design",
            "reason": "matched .boundsignore",
            "command": "no action needed, or remove the matching ignore rule",
        }
    repo = gitutil.repo_root(root) or root
    if rel in set(gitutil.gitignored(repo, [rel])):
        return {
            "path": rel,
            "status": "excluded_by_design",
            "reason": "matched .gitignore",
            "command": "no action needed, or run validation with --include-gitignored",
        }
    if scan.is_test_file(rel):
        return {
            "path": rel,
            "status": "excluded_test",
            "reason": "recognized by the shared test-file convention",
            "command": "link it with `tests:` when it covers a subsystem",
        }

    ext = absolute.suffix
    language = config.KNOWN_SOURCE_EXTS.get(ext)
    if language is None:
        return {
            "path": rel,
            "status": "non_source",
            "reason": f"extension `{ext or '(none)'}` is outside the source denominator",
            "command": "no action needed",
        }

    supported = supported_extensions()
    owners = scan.resolve_owners(root, subsystems, None, matcher, repo)
    if rel in owners:
        return {
            "path": rel,
            "status": "owned" if ext in supported else "unsupported_declared",
            "owner": owners[rel][0],
            "language": language,
            "reason": "claimed by a subsystem manifest",
            "command": "no action needed",
        }
    if ext not in supported:
        return {
            "path": rel,
            "status": "unsupported_declarable",
            "language": language,
            "reason": "Bounds has no adapter and no subsystem claims this file",
            "command": "declare `paths:` and hand-written `exposes:` for the owning subsystem",
        }
    if is_config_file(rel):
        return {
            "path": rel,
            "status": "algorithm_miss",
            "language": language,
            "reason": "known build/tool configuration was classified as source",
            "command": "preview `bounds fix-coverage --auto`; add `--apply` to update .boundsignore",
        }
    return {
        "path": rel,
        "status": "needs_your_decision",
        "language": language,
        "reason": "supported source is not claimed by any subsystem",
        "command": "run `bounds init --subsystem <name> --path <file-or-dir>`",
    }


def _apply_algorithm_fixes(
    root: Path,
    subsystems: dict[str, SubsystemCompact],
    *,
    apply: bool,
) -> dict:
    """Preview or apply exact repo-relative ignore rules for deterministic misses."""
    mapping = _analyze(root, subsystems, sample_cap=None)
    misses = (
        mapping.get("supported", {})
        .get("unowned_breakdown", {})
        .get("algorithm_miss", {})
        .get("sample", [])
    )
    proposed = sorted(set(misses))
    existing = set(_existing_root_ignore_rules(root))
    additions = [path for path in proposed if path not in existing]
    if apply and additions:
        _append_ignore_rules(root, additions)
    return {
        "mode": "coverage-auto-fix",
        "ok": True,
        "dry_run": not apply,
        "applied": bool(apply and additions),
        "proposed": proposed,
        "added": additions if apply else [],
        "ignore_file": ".boundsignore",
        "remaining_user_decisions": (
            mapping.get("supported", {})
            .get("unowned_breakdown", {})
            .get("user_decision_needed", {})
            .get("count", 0)
        ),
        "next_step": (
            "no deterministic algorithm misses remain; run `bounds coverage` for other gap buckets"
            if not proposed
            else
            "re-run `bounds coverage`"
            if apply
            else "review the exact paths, then run `bounds fix-coverage --auto --apply`"
        ),
    }


def _append_ignore_rules(root: Path, additions: list[str]) -> None:
    """Atomically append deterministic rules without discarding existing comments."""
    target = root / ".boundsignore"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    block = [
        "# Added by `bounds fix-coverage --auto --apply`: tool config, not library source",
        *additions,
    ]
    text = existing.rstrip("\n")
    updated = f"{text}\n\n" if text else ""
    updated += "\n".join(block) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=root,
        prefix=".boundsignore.",
        delete=False,
    ) as handle:
        handle.write(updated)
        temporary = Path(handle.name)
    try:
        mode = (target.stat().st_mode & 0o777) if target.exists() else 0o644
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _existing_root_ignore_rules(root: Path) -> list[str]:
    """Read only the exact ignore file this command owns and mutates."""
    target = root / ".boundsignore"
    if not target.exists():
        return []
    return [
        line.strip()
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
