"""Tier-1 + Tier-2 ``describe`` assembly (extracted from cli.py — s-34).

``bounds describe`` merges the declared manifest (Tier 2, human YAML) with tree-sitter
facts (Tier 1, deterministic) so an agent can trust a subsystem's contract without reading
source. This module owns that merge; ``cli.py`` only wires arguments to it.

It deliberately reuses the shared owned-file walk (:func:`extract.scan.iter_subsystem_files`)
and single-file extractor (:func:`extract.scan.extract_file`) instead of carrying its own
copies, so describe and validate agree on which files a subsystem owns and how each is parsed.
Everything here is zero-LLM and deterministic: posix paths, sorted output.
"""

from __future__ import annotations

from pathlib import Path

from . import errors
from .extract import scan, supported_extensions
from .ignore import IgnoreMatcher
from .models import SubsystemCompact, ValidationReport
from .validate import engine as validate_engine


def extract_owned(root: Path, sub: SubsystemCompact) -> tuple[dict[str, str], list[str]]:
    """Tier-1 extraction for one subsystem.

    Returns ``(exported_symbol_name -> owning_file, owned_files)``. Every file the subsystem
    owns is recorded in ``owned_files`` regardless of whether it parses (so ``files`` reflects
    the declared surface), but only cleanly-extracted exported symbols populate the symbol map
    used to mark ``exposes`` entries ``verified``.

    File selection is the engine's owned-file walk, so describe and validate own the same set.
    """
    exts = supported_extensions()
    extracted_symbols: dict[str, str] = {}  # symbol_name -> owning file (rel posix)
    owned_files: list[str] = []

    for abs_path in scan.iter_subsystem_files(root, sub, exts):
        rel = abs_path.relative_to(root).as_posix()
        if rel not in owned_files:
            owned_files.append(rel)
        result, _ = scan.extract_file(root, rel)
        if result is not None:  # None ⇒ unsupported/unreadable/parse-fail (fail soft)
            for sym in result.symbols:
                if sym.exported:
                    extracted_symbols[sym.name] = rel

    return extracted_symbols, owned_files


def describe_one(
    root: Path,
    sub: SubsystemCompact,
    deep: bool,
    report: ValidationReport | None,
    entry_matcher: IgnoreMatcher,
) -> dict:
    """Build the merged Tier-1 + Tier-2 describe payload for a single subsystem.

    ``report`` is the one shared read-only quick run (:func:`status_report`); status is derived
    from it twice (s-31): ``validation_status`` is **scoped to this subsystem** (so describing a
    clean subsystem reads fresh even when drift lives elsewhere), and ``project_status`` is the
    project-wide rollup kept additively alongside it.

    Owned files matching a ``root.entry_points`` glob are surfaced: each is listed under
    ``entry_points`` and any ``exposes`` entry backed by one is flagged ``entry_point: true``,
    so an agent sees a symbol lives in a bootstrap file.
    """
    payload = sub.to_dict()
    extracted_symbols, owned_files = extract_owned(root, sub)
    for expose in payload.get("exposes", []):
        ename = expose.get("name", "")
        if ename in extracted_symbols:
            expose["file"] = extracted_symbols[ename]
            expose["verified"] = True
            if entry_matcher and entry_matcher.matches(expose["file"]):
                expose["entry_point"] = True
        else:
            expose["verified"] = False
    payload["files"] = sorted(owned_files)
    # Always present (like ``files``) for a stable shape; the human renderer hides it when empty.
    payload["entry_points"] = sorted(
        f for f in owned_files if entry_matcher and entry_matcher.matches(f)
    )
    payload["validation_status"] = subsystem_status(report, sub.name)
    payload["project_status"] = project_status(report)
    if deep:
        payload["semantic"] = {"note": "LLM enrichment (Tier 3) not enabled in this build"}
    return payload


def status_report(root: Path) -> ValidationReport | None:
    """The shared read-only quick run backing describe's status (or ``None`` when fatal).

    Issues carry quick-downgraded severities (errors→warnings); callers re-derive the original
    status via :func:`_derive_status`, which reads each code's canonical severity instead.
    """
    try:
        return validate_engine.run(root, mode="quick", persist=False)
    except errors.BoundsError:
        return None


def _derive_status(issues: list) -> str:
    """fresh | stale | unresolved from an issue list, using CANONICAL severities (s-31).

    Quick mode downgrades errors→warnings (so it never blocks), which would otherwise make a
    per-subsystem status never read 'stale'. We therefore key off the code's canonical severity
    (``errors.SEVERITY``) rather than the live one — except an issue whose *live* severity is
    ``info`` (e.g. an undeclared-export drift, s-32) is never error-class.
    """
    if any(i.code == errors.E_UNRESOLVED_REFERENCE for i in issues):
        return "unresolved"
    if any(i.severity != "info" and errors.SEVERITY.get(i.code) == "error" for i in issues):
        return "stale"
    return "fresh"


def project_status(report: ValidationReport | None) -> str:
    """Project-wide validation status re-derived from the shared quick run."""
    return _derive_status(report.issues) if report is not None else "unresolved"


def subsystem_status(report: ValidationReport | None, name: str) -> str:
    """Validation status scoped to one subsystem's own issues (s-31)."""
    if report is None:
        return "unresolved"
    return _derive_status([i for i in report.issues if i.subsystem == name])
